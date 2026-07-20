"""Integration tests for the Phase 9 self-serve billing surface
(``payments/billing_views.py``): plan catalog, usage, subscription detail,
upgrade/downgrade, and add-on purchase, driven through DRF routing exactly
like a real client -- permissions, idempotency, and the spec's acceptance
scenario (a blocked invitation succeeds after an upgrade, with no manual
step) are all exercised through real HTTP requests.
"""

import datetime
import json
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.services import OrganizationService
from payments.billing_constants import BillingInterval, BillingState, LimitedResource, LimitKind
from payments.constants import PaymentProviders
from payments.exceptions import OverLimitError
from payments.models import (
    BillingPlan,
    PaymentMethod,
    PlanLimit,
    Subscription,
    SubscriptionAddOn,
)
from payments.services.entitlement_service import EntitlementService
from payments.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from payments.services.subscription_service import SubscriptionService
from payments.tests.views.test_payment_webhooks import sign as sign_webhook


WEBHOOK_SECRET = "test-webhook-secret"

# This module places every organization on a specific, hand-built plan (to
# control prices/limits precisely) via `SubscriptionService` directly, so it
# opts out of conftest's autouse `provision_default_subscription` -- otherwise
# the org would already have a `Subscription` (on the seeded `unlimited` plan)
# by the time these fixtures run, and `create_subscription_for_organization`'s
# idempotent get_or_create would silently keep it on `unlimited` instead.
pytestmark = pytest.mark.no_auto_subscription


def make_complete_plan(
    limit_values: dict[str, int | None] | None = None,
    *,
    monthly_price: Decimal = Decimal("0"),
) -> BillingPlan:
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=monthly_price,
        annual_price=None,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
            overage_unit_price=Decimal("2.5000") if resource_key in limit_values else None,
        )
    return plan


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def billing_profile(organization):
    billing_address = baker.make(
        "payments.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )
    return baker.make(
        "payments.BillingProfile",
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


@pytest.fixture
def admin_membership(user, organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def billing_owner_membership(user, organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.MEMBER,
        is_active=True,
        is_billing_owner=True,
    )


@pytest.fixture
def plain_member_membership(user, organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.MEMBER,
        is_active=True,
        is_billing_owner=False,
    )


@pytest.fixture
def free_plan():
    return make_complete_plan(
        {LimitedResource.ORGANIZATION_MEMBERS: 1, LimitedResource.RESOURCE_CALENDARS: 3},
        monthly_price=Decimal("0"),
    )


@pytest.fixture
def pro_plan():
    return make_complete_plan(
        {LimitedResource.ORGANIZATION_MEMBERS: 10, LimitedResource.RESOURCE_CALENDARS: 20},
        monthly_price=Decimal("50"),
    )


@pytest.fixture
def subscription(organization, free_plan, billing_profile):
    """Requires ``billing_profile`` so every test using this fixture already has
    the payer identity real provider round trips (``process_subscription`` /
    ``create_payment``) need -- without it those calls raise
    ``MissingBillingProfileError``/``BillingProfileContactEmailMissingError``."""
    return SubscriptionService().create_subscription_for_organization(organization, plan=free_plan)


@pytest.fixture
def mercadopago_payment_adapter():
    with patch(
        "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("test-access-token", webhook_secret=WEBHOOK_SECRET)
        adapter.sdk = mock_sdk.return_value
        adapter.sdk.payment().create.return_value = {"response": {"id": "mp-payment-1"}}
        yield adapter


@pytest.fixture
def mercadopago_subscription_adapter():
    with patch(
        "payments.services.subscription_adapters.mercadopago_subscription_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoSubscriptionAdapter("test-access-token", webhook_secret=WEBHOOK_SECRET)
        adapter.sdk = mock_sdk.return_value
        adapter.sdk.plan().create.return_value = {"response": {"id": "mp-plan-1"}}
        adapter.sdk.preapproval().create.return_value = {"response": {"id": "mp-sub-1"}}
        adapter.sdk.preapproval().get.return_value = {"response": {}}
        yield adapter


@pytest.fixture
def billing_client(
    di_container, mercadopago_payment_adapter, mercadopago_subscription_adapter, auth_client
):
    """``auth_client`` with the provider adapters swapped for SDK-mocked ones --
    every billing-mutation endpoint drives a real (if faked) provider round
    trip, so tests exercising them need this rather than the bare
    ``auth_client``."""
    with (
        di_container.payment_gateway.override(mercadopago_payment_adapter),
        di_container.subscription_gateway.override(mercadopago_subscription_adapter),
    ):
        yield auth_client


@pytest.fixture
def webhook_client(di_container, mercadopago_payment_adapter, mercadopago_subscription_adapter):
    with (
        di_container.payment_gateway.override(mercadopago_payment_adapter),
        di_container.subscription_gateway.override(mercadopago_subscription_adapter),
    ):
        yield APIClient()


def change_plan_url() -> str:
    return reverse("api:BillingSubscription-change-plan")


def cancel_url() -> str:
    return reverse("api:BillingSubscription-cancel")


def subscription_url() -> str:
    return reverse("api:BillingSubscription-retrieve")


def usage_url() -> str:
    return reverse("api:BillingUsage-retrieve")


def plans_url() -> str:
    return reverse("api:BillingPlan-list")


def add_ons_url() -> str:
    return reverse("api:BillingAddOn-list")


def add_on_detail_url(pk) -> str:
    return reverse("api:BillingAddOn-detail", kwargs={"pk": pk})


def subscription_payment_update_url(provider: str = PaymentProviders.MERCADOPAGO) -> str:
    return reverse(
        "api:Payments-subscription-payment-update", kwargs={"provider": provider, "pk": 1}
    )


@pytest.mark.django_db
class TestReadEndpoints:
    def test_list_plans_is_open_to_any_authenticated_member(
        self, auth_client, plain_member_membership, free_plan, pro_plan
    ):
        response = auth_client.get(plans_url())

        assert response.status_code == status.HTTP_200_OK
        slugs = {row["slug"] for row in response.data["results"]}
        assert free_plan.slug in slugs
        assert pro_plan.slug in slugs

    def test_retrieve_subscription(self, auth_client, plain_member_membership, subscription):
        response = auth_client.get(subscription_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["plan"]["slug"] == subscription.plan.slug
        assert response.data["billing_state"] == BillingState.FREE

    def test_retrieve_usage(self, auth_client, plain_member_membership, subscription):
        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.FREE
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        assert rows[LimitedResource.ORGANIZATION_MEMBERS]["limit_value"] == 1
        # A plain member (this fixture) already occupies the one seat.
        assert rows[LimitedResource.ORGANIZATION_MEMBERS]["current_usage"] == 1

    def test_reads_require_authentication(self, anonymous_client):
        for url in (plans_url(), usage_url(), subscription_url()):
            response = anonymous_client.get(url)
            assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestPermissions:
    def test_plain_member_is_forbidden_from_changing_plan(
        self, auth_client, plain_member_membership, subscription, pro_plan
    ):
        response = auth_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_billing_owner_may_change_plan(
        self, billing_client, billing_owner_membership, subscription, pro_plan
    ):
        response = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["plan"]["slug"] == pro_plan.slug

    def test_admin_may_change_plan(self, billing_client, admin_membership, subscription, pro_plan):
        response = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

    def test_plain_member_is_forbidden_from_purchasing_an_add_on(
        self, auth_client, plain_member_membership, subscription
    ):
        response = auth_client.post(
            add_ons_url(),
            {
                "resource_key": LimitedResource.RESOURCE_CALENDARS,
                "quantity": 1,
                "is_recurring": False,
                "idempotency_key": "idem-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_plain_member_is_forbidden_from_cancelling(
        self, auth_client, plain_member_membership, subscription
    ):
        response = auth_client.post(cancel_url())

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_billing_owner_may_cancel(self, billing_client, billing_owner_membership, subscription):
        response = billing_client.post(cancel_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.CANCELLED


@pytest.mark.django_db
class TestUpgradeGrantsNoCapacitySynchronously:
    def test_initiated_but_unconfirmed_upgrade_grants_no_capacity(
        self, billing_client, admin_membership, organization, subscription, pro_plan
    ):
        response = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        effective_limit = EntitlementService().get_effective_limit(
            organization, LimitedResource.ORGANIZATION_MEMBERS
        )
        # Still the free plan's ceiling -- the webhook never fired.
        assert effective_limit.limit_value == 1


@pytest.mark.django_db
class TestAddOnIdempotency:
    def test_same_idempotency_key_twice_yields_one_add_on_and_one_charge(
        self,
        billing_client,
        mercadopago_payment_adapter,
        admin_membership,
        subscription,
        billing_profile,
    ):
        body = {
            "resource_key": LimitedResource.RESOURCE_CALENDARS,
            "quantity": 2,
            "is_recurring": False,
            "idempotency_key": "idem-add-on-1",
            "payment_token": "tok-1",
        }

        first = billing_client.post(add_ons_url(), body, format="json")
        second = billing_client.post(add_ons_url(), body, format="json")

        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_201_CREATED
        assert first.data["id"] == second.data["id"]
        assert (
            SubscriptionAddOn.objects.filter(purchase_idempotency_key="idem-add-on-1").count() == 1
        )
        # The provider is charged exactly once -- the second request's
        # `get_or_create` short-circuits before any adapter call.
        assert mercadopago_payment_adapter.sdk.payment().create.call_count == 1

    def test_purchase_grants_no_capacity_until_confirmed(
        self, billing_client, admin_membership, organization, subscription, billing_profile
    ):
        response = billing_client.post(
            add_ons_url(),
            {
                "resource_key": LimitedResource.RESOURCE_CALENDARS,
                "quantity": 2,
                "is_recurring": False,
                "idempotency_key": "idem-add-on-2",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["is_active"] is False
        effective_limit = EntitlementService().get_effective_limit(
            organization, LimitedResource.RESOURCE_CALENDARS
        )
        assert effective_limit.limit_value == 3

    def test_cancel_add_on_stops_recurrence(
        self, billing_client, admin_membership, subscription, billing_profile
    ):
        created = billing_client.post(
            add_ons_url(),
            {
                "resource_key": LimitedResource.RESOURCE_CALENDARS,
                "quantity": 1,
                "is_recurring": True,
                "idempotency_key": "idem-add-on-3",
                "payment_token": "tok-1",
            },
            format="json",
        )

        response = billing_client.delete(add_on_detail_url(created.data["id"]))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["is_recurring"] is False

    def test_another_organizations_add_on_is_not_reachable(
        self, billing_client, admin_membership, subscription, billing_profile
    ):
        other_organization = baker.make(Organization)
        other_plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: 3})
        other_subscription = baker.make(
            Subscription,
            organization=other_organization,
            plan=other_plan,
            billing_interval=BillingInterval.MONTHLY,
            current_period_start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            current_period_end=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
            payment_provider=PaymentProviders.MERCADOPAGO,
        )
        foreign_add_on = baker.make(
            SubscriptionAddOn,
            subscription=other_subscription,
            resource_key=LimitedResource.RESOURCE_CALENDARS,
            quantity=1,
            is_recurring=True,
            purchase_idempotency_key="foreign-idem",
        )

        response = billing_client.delete(add_on_detail_url(foreign_add_on.pk))

        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestAcceptanceScenario:
    """Spec acceptance scenario 1: an org at its seat limit upgrades, pays, and
    the previously-rejected invitation succeeds with no manual step."""

    def test_blocked_invitation_succeeds_after_upgrade_and_webhook_confirmation(
        self, billing_client, admin_membership, user, organization, subscription, pro_plan
    ):
        organization_service = OrganizationService()

        # 1. At the seat limit (1), a new invite is blocked.
        with pytest.raises(OverLimitError):
            organization_service.invite_user_to_organization(
                email="new-hire@example.com",
                first_name="New",
                last_name="Hire",
                organization=organization,
                invited_by=user,
                send_email=False,
            )

        # 2. The org's admin upgrades to a plan with a higher seat limit.
        change_plan_response = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-upgrade-1",
                "payment_token": "tok-1",
            },
            format="json",
        )
        assert change_plan_response.status_code == status.HTTP_200_OK

        # Still blocked -- payment has not been confirmed yet.
        with pytest.raises(OverLimitError):
            organization_service.invite_user_to_organization(
                email="new-hire@example.com",
                first_name="New",
                last_name="Hire",
                organization=organization,
                invited_by=user,
                send_email=False,
            )

        # 3. The provider confirms payment via the subscription-payment webhook.
        subscription.refresh_from_db()
        payload = json.dumps(
            {
                "type": "subscription_authorized_payment",
                "id": "notif-1",
                "data": {"id": subscription.external_id},
            }
        ).encode()
        with patch(
            "payments.services.subscription_adapters.mercadopago_subscription_adapter"
            ".mercadopago.SDK"
        ) as mock_sdk:
            adapter = MercadoPagoSubscriptionAdapter(
                "test-access-token", webhook_secret=WEBHOOK_SECRET
            )
            adapter.sdk = mock_sdk.return_value
            adapter.sdk.preapproval().get.return_value = {
                "response": {"last_payment_id": "mp-payment-99"}
            }
            adapter.sdk.payment().get.return_value = {
                "response": {
                    "id": "mp-payment-99",
                    "transaction_amount": "50.00",
                    "currency_id": "USD",
                    "payment_method_id": "credit_card",
                    "description": "Subscription payment",
                    "status": "approved",
                    "status_detail": "accredited",
                    "payer": {
                        "email": "billing@example.com",
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                        "identification": {"type": "CPF", "number": "12345678900"},
                        "address": {
                            "street_name": "Test Street",
                            "street_number": "123",
                            "neighborhood": "",
                            "city": "Test City",
                            "federal_unit": "Test State",
                            "country": "Test Country",
                            "zip_code": "12345",
                        },
                    },
                }
            }
            from di_core.containers import container

            with container.subscription_gateway.override(adapter):
                webhook_response = APIClient().post(
                    subscription_payment_update_url(),
                    data=payload,
                    content_type="application/json",
                    **sign_webhook(subscription.external_id),
                )
        assert webhook_response.status_code == status.HTTP_200_OK

        # 4. The invitation now succeeds, with no manual/support step.
        invitation = organization_service.invite_user_to_organization(
            email="new-hire@example.com",
            first_name="New",
            last_name="Hire",
            organization=organization,
            invited_by=user,
            send_email=False,
        )
        assert invitation is not None

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        assert PaymentMethod.objects.filter(organization=organization, is_active=True).exists()
