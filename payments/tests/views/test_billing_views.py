"""Integration tests for the self-serve billing endpoints
(``payments/billing_views.py``): plan catalog, usage, subscription detail,
upgrade/downgrade, and add-on purchase, driven through DRF routing exactly
like a real client. Permissions, idempotency, and the key acceptance scenario
(a blocked invitation succeeds after an upgrade, with no manual step) are all
exercised through real HTTP requests.
"""

import datetime
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

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
from payments.services.payment_adapters.base import BasePaymentAdapter
from payments.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)
from payments.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from payments.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)
from payments.services.subscription_service import (
    SubscriptionService,
    retry_payment_idempotency_key,
)
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
        # Pinned to MercadoPago -- matching `billing_client`/`webhook_client`'s
        # `mercadopago_payment_adapter`/`mercadopago_subscription_adapter` DI
        # overrides below. Add-on purchase (`purchase_add_on` ->
        # `PaymentService.create_payment`) resolves the provider from this pin
        # (Rule B, Payment Provider Selection Phase 4); leaving it unpinned would
        # resolve to `settings.DEFAULT_PAYMENT_PROVIDER` (`stripe`) and drive the
        # real, unmocked Stripe adapter over the network.
        payment_provider=PaymentProviders.MERCADOPAGO,
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


@pytest.fixture(autouse=True)
def _no_live_stripe_calls(di_container):
    """Structural guard for the whole module: every ``Subscription``/``Payment``
    here resolves its provider from the organization (Rule B) or its own row
    (Rule A), so *any* organization this module builds without an explicit
    MercadoPago pin resolves to ``settings.DEFAULT_PAYMENT_PROVIDER``
    (``stripe``) and would drive the real, unmocked Stripe adapters over the
    network.

    ``billing_client``/``webhook_client`` below only override the two MercadoPago
    slots; before this fixture existed the plan-change tests were green purely
    because ``create_subscription_for_organization`` hardcoded ``mercadopago``.
    Autouse (rather than a fixture each test must remember to request) and
    module-wide, matching the treatment
    ``payments/tests/services/test_payment_services.py``'s ``payment_service``
    fixture already gives both provider slots.

    Teardown asserts neither double received any call -- a call reaching one is
    a wrong-provider routing regression, and a silent mock absorbing it would
    make the guard structural in name only. ``TestUnconfiguredProviderMapsTo409``
    deliberately overrides both DI slots again with a *different* double inside
    its own fixture, which shadows these for the duration of that test (the
    inner ``override`` context manager takes precedence), so these two remain
    uncalled there too -- no test-specific exemption needed.
    """
    stripe_payment = MagicMock(spec=BasePaymentAdapter)
    stripe_payment.provider = PaymentProviders.STRIPE
    stripe_subscription = MagicMock(spec=BaseSubscriptionAdapter)
    stripe_subscription.provider = PaymentProviders.STRIPE
    with (
        di_container.stripe_payment_gateway.override(stripe_payment),
        di_container.stripe_subscription_gateway.override(stripe_subscription),
    ):
        yield
    assert stripe_payment.mock_calls == [], (
        "A test in this module routed a call to the Stripe payment adapter -- "
        "every organization here is pinned to MercadoPago (or deliberately "
        "overrides this guard), so this is a wrong-provider routing regression, "
        f"not a legitimate call: {stripe_payment.mock_calls!r}"
    )
    assert stripe_subscription.mock_calls == [], (
        "A test in this module routed a call to the Stripe subscription adapter "
        f"-- see the payment-adapter assertion above: {stripe_subscription.mock_calls!r}"
    )


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


def retry_payment_url() -> str:
    return reverse("api:BillingSubscription-retry-payment")


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

    def test_plain_member_is_forbidden_from_retrying_payment(
        self, auth_client, plain_member_membership, subscription
    ):
        response = auth_client.post(
            retry_payment_url(),
            {"payment_token": "tok-1", "idempotency_key": "idem-1"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_billing_owner_may_cancel(self, billing_client, billing_owner_membership, subscription):
        response = billing_client.post(cancel_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.CANCELLED

    def test_admin_of_a_child_org_cannot_change_the_pooled_roots_plan(
        self, auth_client, user, pro_plan
    ):
        """Exercise the *object-level* DENY branch of
        ``IsBillingOwnerOrAdmin``, not the coarse ``has_permission`` check. The
        caller is a genuine admin (so ``has_permission`` passes), but of a child
        org that pools against a reseller root it does not administer. The
        subscription lives at the root, so ``has_object_permission`` must reject
        managing it. A split permission whose object check never fires would
        wrongly allow this."""
        reseller_root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller_root, can_invite_organizations=False)
        # The root (the billing root the child pools against) has the subscription.
        root_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 1})
        SubscriptionService().create_subscription_for_organization(reseller_root, plan=root_plan)
        # The caller is an ADMIN of the child only -- the coarse check passes.
        baker.make(
            OrganizationMembership,
            user=user,
            organization=child,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

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

    def test_admin_of_a_child_org_cannot_purchase_an_add_on_against_the_root(
        self, auth_client, user
    ):
        """Same object-level DENY, exercised on the ``AddOnViewSet.create`` write
        path (which calls ``check_object_permissions`` independently)."""
        reseller_root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller_root, can_invite_organizations=False)
        root_plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: 3})
        SubscriptionService().create_subscription_for_organization(reseller_root, plan=root_plan)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=child,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

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


@pytest.mark.django_db
class TestUnconfiguredProviderMapsTo409:
    """Payment Provider Selection: the plan's **Guiding Decisions** commit to
    HTTP 409 for ``PaymentProviderNotConfiguredError``, and Phase 4 is what makes
    it reachable from these actions. Mapped centrally in
    ``common.exception_handlers.vinta_exception_handler`` (with ``set_rollback()``)
    rather than per-action, so a new billing write cannot forget it and 500 on a
    money path.

    Each test repoints the organization onto Stripe *and* swaps the Stripe DI
    slot for a real adapter built with an empty secret -- exactly what the
    container produces when ``STRIPE_SECRET_KEY`` is unset. No network call is
    reachable: resolution raises before any adapter method runs.

    The **transactional** half of the mapping (``set_rollback()``, without which
    the swallowed exception would commit everything written before the raise
    under production's ``ATOMIC_REQUESTS``) is asserted in
    ``payments/tests/test_over_limit_rollback.py`` -- deliberately not here.
    ``ATOMIC_REQUESTS`` is a production-only setting, so a "nothing was written"
    assertion in this module would pass identically with or without
    ``set_rollback()`` and prove nothing.
    """

    @pytest.fixture
    def unconfigured_stripe_client(self, di_container, billing_profile, auth_client):
        billing_profile.payment_provider = PaymentProviders.STRIPE
        billing_profile.save(update_fields=["payment_provider"])
        with (
            di_container.stripe_payment_gateway.override(
                StripePaymentAdapter(api_key="", webhook_secret="")
            ),
            di_container.stripe_subscription_gateway.override(
                StripeSubscriptionAdapter(api_key="", webhook_secret="")
            ),
        ):
            yield auth_client

    def _stripe_subscription(self, subscription, *, external_id: str = ""):
        subscription.payment_provider = PaymentProviders.STRIPE
        subscription.external_id = external_id
        subscription.save(update_fields=["payment_provider", "external_id"])
        return subscription

    def test_change_plan_returns_409(
        self, unconfigured_stripe_client, admin_membership, subscription, pro_plan
    ):
        self._stripe_subscription(subscription)

        response = unconfigured_stripe_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-409-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "stripe" in response.data["detail"]

    def test_cancel_returns_409(self, unconfigured_stripe_client, admin_membership, subscription):
        # `SubscriptionService.cancel_subscription` skips the provider round trip
        # entirely for a subscription that never attached an instrument, so an
        # `external_id` is required for this path to reach adapter resolution at
        # all -- without it the request legitimately 200s.
        self._stripe_subscription(subscription, external_id="stripe-sub-ext-1")

        response = unconfigured_stripe_client.post(cancel_url())

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "stripe" in response.data["detail"]

    def test_add_on_create_returns_409(
        self, unconfigured_stripe_client, admin_membership, subscription, billing_profile
    ):
        response = unconfigured_stripe_client.post(
            add_ons_url(),
            {
                "resource_key": LimitedResource.RESOURCE_CALENDARS,
                "quantity": 1,
                "is_recurring": False,
                "idempotency_key": "idem-409-3",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "stripe" in response.data["detail"]


@pytest.mark.django_db
class TestBillingErrorCodes:
    """Billing API Contract Hardening, Phase 1: every billing error rendered by
    ``common.exception_handlers.vinta_exception_handler`` now carries a stable,
    machine-readable ``code`` a client can branch on instead of matching on
    ``detail``'s message string.
    """

    def test_change_plan_without_a_required_payment_token_returns_400_with_its_code(
        self, billing_client, admin_membership, subscription, pro_plan
    ):
        """``subscription`` (the ``free_plan`` fixture) has no provider-side
        instrument yet (``external_id`` is blank) -- upgrading to a paid plan
        without a ``payment_token`` cannot attach one."""
        response = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-no-token-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "payment_token_required"
        assert "detail" in response.data

    def test_add_on_purchase_of_an_unpriced_resource_returns_400_with_its_code(
        self, billing_client, admin_membership, subscription, billing_profile
    ):
        """``free_plan`` (the ``subscription`` fixture's plan) carries no
        ``overage_unit_price`` for ``CALENDAR_GROUPS`` -- there is no
        catalog-derived price to charge for it as an add-on."""
        response = billing_client.post(
            add_ons_url(),
            {
                "resource_key": LimitedResource.CALENDAR_GROUPS,
                "quantity": 1,
                "is_recurring": False,
                "idempotency_key": "idem-unpriced-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "add_on_not_purchasable"
        assert "detail" in response.data

    def test_second_in_flight_plan_change_returns_409_with_its_code(
        self, billing_client, admin_membership, subscription, pro_plan
    ):
        premium_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 200}, monthly_price=Decimal("200")
        )
        first_change = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": pro_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-in-flight-1",
                "payment_token": "tok-1",
            },
            format="json",
        )
        assert first_change.status_code == status.HTTP_200_OK

        second_change = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": premium_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-in-flight-2",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert second_change.status_code == status.HTTP_409_CONFLICT
        assert second_change.data["code"] == "unconfirmed_plan_change"
        assert "detail" in second_change.data


@pytest.mark.django_db
class TestRetryPaymentEndpoint:
    """``POST /billing/subscription/retry-payment/`` -- Billing API Contract
    Hardening, Phase 3. Mostly exercises the error/shape contract through the
    endpoint here; permissions have their own dedicated case
    (``TestPermissions.test_plain_member_is_forbidden_from_retrying_payment``,
    since ``retry_payment`` is on the same ``write_actions`` gate as the other
    billing writes there); the ordering/idempotency-namespacing/webhook-recovery
    behavior against a hand-written double is ``test_grace_recovery.py``'s job.
    """

    def _make_grace_subscription(
        self,
        subscription: Subscription,
        *,
        billing_state: str = BillingState.GRACE,
        external_id: str = "already-on-file",
    ) -> Subscription:
        subscription.billing_state = billing_state
        subscription.external_id = external_id
        subscription.grace_period_ends_at = datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(days=5)
        subscription.save(update_fields=["billing_state", "external_id", "grace_period_ends_at"])
        return subscription

    def test_returns_409_retry_payment_not_applicable_from_active(
        self, billing_client, admin_membership, subscription
    ):
        subscription.billing_state = BillingState.ACTIVE
        subscription.external_id = "already-on-file"
        subscription.save(update_fields=["billing_state", "external_id"])

        response = billing_client.post(
            retry_payment_url(),
            {"payment_token": "tok-new-card", "idempotency_key": "idem-retry-1"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "retry_payment_not_applicable"
        assert "detail" in response.data

    def test_returns_409_retry_payment_not_applicable_from_free(
        self, billing_client, admin_membership, subscription
    ):
        # `subscription` (the `free_plan` fixture) is already FREE -- the
        # subscription-just-created state, never having entered a
        # failed-payment episode.
        response = billing_client.post(
            retry_payment_url(),
            {"payment_token": "tok-new-card", "idempotency_key": "idem-retry-2"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "retry_payment_not_applicable"

    def test_returns_409_subscription_not_attached_when_external_id_is_blank(
        self, billing_client, admin_membership, subscription
    ):
        self._make_grace_subscription(subscription, external_id="")

        response = billing_client.post(
            retry_payment_url(),
            {"payment_token": "tok-new-card", "idempotency_key": "idem-retry-3"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "subscription_not_attached"
        assert "detail" in response.data

    def test_returns_400_when_payment_token_is_missing(
        self, billing_client, admin_membership, subscription
    ):
        self._make_grace_subscription(subscription)

        response = billing_client.post(
            retry_payment_url(),
            {"idempotency_key": "idem-retry-4"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "payment_token" in response.data

    def test_returns_400_when_payment_token_is_blank(
        self, billing_client, admin_membership, subscription
    ):
        self._make_grace_subscription(subscription)

        response = billing_client.post(
            retry_payment_url(),
            {"payment_token": "", "idempotency_key": "idem-retry-5"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "payment_token" in response.data

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_mercadopago_attaches_the_token_then_refuses_to_collect(
        self,
        billing_client,
        mercadopago_subscription_adapter,
        admin_membership,
        subscription,
        billing_state,
    ):
        """Billing API Contract Hardening, Phase 4: MercadoPago's
        ``pay_outstanding_invoice`` is an explicit, typed refusal (see
        ``MercadoPagoSubscriptionAdapter.pay_outstanding_invoice`` -- unverified
        against a real account, and no organization is routed to MercadoPago
        today). Driven through the real HTTP path -> real (SDK-mocked) adapter
        boundary, this proves the endpoint no longer *silently* reports success
        for a provider it cannot actually collect through -- it fails loudly
        instead, which is the whole point of this phase.

        Before Phase 4, this exact setup returned 200 having collected $0.00
        (Phase 3's ``change_subscription_plan``-based retry) -- see
        ``TestRetryPaymentEndpoint`` module's Stripe-based sibling test below for
        the provider retry-payment actually recovers money through today.

        Parametrized over GRACE and RESTRICTED: RESTRICTED is where
        write-blocks/sync-pauses are in force, so whether a RESTRICTED org can
        even reach this endpoint (up to the point of the refusal) is worth
        pinning here too.
        """
        self._make_grace_subscription(subscription, billing_state=billing_state)

        response = billing_client.post(
            retry_payment_url(),
            {"payment_token": "tok-new-card", "idempotency_key": "idem-retry-6"},
            format="json",
        )

        # Billing API Contract Hardening, Phase 4 reviewer finding SHOULD-FIX
        # 7: the refusal must reach the client as a typed 409, not an
        # unhandled 500 -- `CollectionNotSupportedError` is a `BillingError`
        # subclass with a `common.exception_handlers.vinta_exception_handler`
        # branch, unlike the plain `PaymentAdapterError` this replaced.
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "collection_not_supported"
        assert "detail" in response.data

        # The token was attached (real `sdk.preapproval().update(...)` call)
        # before the refusal fired -- ordering still holds even though the
        # second step is a refusal rather than a charge.
        update_calls = mercadopago_subscription_adapter.sdk.preapproval().update.call_args_list
        assert len(update_calls) == 1
        attach_args, _attach_kwargs = update_calls[0]
        assert attach_args[0] == subscription.external_id
        assert attach_args[1]["card_token_id"] == "tok-new-card"

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_stripe_attaches_the_token_then_pays_the_open_invoice(
        self,
        di_container,
        auth_client,
        admin_membership,
        subscription,
        billing_state,
    ):
        """Billing API Contract Hardening, Phase 4's headline fix, proven
        through the real HTTP path: unlike MercadoPago's refusal above,
        Stripe's ``pay_outstanding_invoice`` actually locates the
        subscription's open invoice and pays it -- ``stripe.Invoice.pay``, not
        a proration on a freshly-minted price (``change_subscription_plan``,
        Phase 3's mistaken primitive). Stripe SDK calls are mocked at the
        adapter module's own boundary, same pattern as
        ``test_stripe_subscription_adapter.py``.

        Repoints ``subscription`` (built on MercadoPago by this module's
        fixtures) onto Stripe directly and swaps in a real
        ``StripeSubscriptionAdapter`` for the duration of the request --
        mirrors ``TestUnconfiguredProviderMapsTo409.unconfigured_stripe_client``'s
        established pattern for shadowing the module's ``_no_live_stripe_calls``
        guard for one test.
        """
        self._make_grace_subscription(
            subscription, billing_state=billing_state, external_id="sub_stripe_1"
        )
        subscription.payment_provider = PaymentProviders.STRIPE
        subscription.save(update_fields=["payment_provider"])

        stripe_adapter = StripeSubscriptionAdapter(api_key="sk_test_123", webhook_secret="whsec")
        with (
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".Subscription"
            ) as mock_subscription_resource,
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".PaymentMethod"
            ) as mock_payment_method,
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".Customer"
            ) as mock_customer,
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice"
            ) as mock_invoice,
            di_container.stripe_subscription_gateway.override(stripe_adapter),
        ):
            mock_subscription_resource.retrieve.return_value = MagicMock(customer="cus_stripe_1")
            mock_invoice.list.side_effect = [
                MagicMock(data=[MagicMock(id="in_open_1", created=1000)]),
                MagicMock(data=[]),
            ]

            response = auth_client.post(
                retry_payment_url(),
                {"payment_token": "tok-new-card", "idempotency_key": "idem-retry-7"},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == billing_state

        # `update_subscription_payment_token`: retrieve the customer, attach
        # the (unattached-by-default) PaymentMethod, then pin it as both the
        # customer's and the subscription's default (SHOULD-FIX 6).
        mock_subscription_resource.retrieve.assert_called_once_with(
            "sub_stripe_1", api_key="sk_test_123"
        )
        mock_payment_method.attach.assert_called_once_with(
            "tok-new-card", customer="cus_stripe_1", api_key="sk_test_123"
        )
        mock_customer.modify.assert_called_once_with(
            "cus_stripe_1",
            invoice_settings={"default_payment_method": "tok-new-card"},
            api_key="sk_test_123",
        )
        mock_subscription_resource.modify.assert_called_once_with(
            "sub_stripe_1", default_payment_method="tok-new-card", api_key="sk_test_123"
        )

        # `pay_outstanding_invoice`: both `open` and `uncollectible` are
        # queried (SHOULD-FIX 5), no `limit` (SHOULD-FIX 4), and the invoice
        # is paid with the new token passed explicitly (SHOULD-FIX 6) under a
        # per-invoice idempotency key.
        assert mock_invoice.list.call_args_list == [
            ((), {"subscription": "sub_stripe_1", "status": "open", "api_key": "sk_test_123"}),
            (
                (),
                {
                    "subscription": "sub_stripe_1",
                    "status": "uncollectible",
                    "api_key": "sk_test_123",
                },
            ),
        ]
        expected_key = retry_payment_idempotency_key(subscription.pk, "idem-retry-7")
        mock_invoice.pay.assert_called_once_with(
            "in_open_1",
            payment_method="tok-new-card",
            idempotency_key=f"{expected_key}-in_open_1",
            api_key="sk_test_123",
        )

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_stripe_no_outstanding_invoice_returns_409(
        self,
        di_container,
        auth_client,
        admin_membership,
        subscription,
        billing_state,
    ):
        """Reviewer finding SHOULD-FIX 10: no endpoint-level test previously
        asserted the `no_outstanding_balance` 409 -- the only thing that would
        have pinned `set_rollback()` on that branch in
        `common.exception_handlers.vinta_exception_handler`."""
        self._make_grace_subscription(
            subscription, billing_state=billing_state, external_id="sub_stripe_2"
        )
        subscription.payment_provider = PaymentProviders.STRIPE
        subscription.save(update_fields=["payment_provider"])

        stripe_adapter = StripeSubscriptionAdapter(api_key="sk_test_123", webhook_secret="whsec")
        with (
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".Subscription"
            ) as mock_subscription_resource,
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".PaymentMethod"
            ),
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".Customer"
            ),
            patch(
                "payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice"
            ) as mock_invoice,
            di_container.stripe_subscription_gateway.override(stripe_adapter),
        ):
            mock_subscription_resource.retrieve.return_value = MagicMock(customer="cus_stripe_2")
            mock_invoice.list.return_value = MagicMock(data=[])

            response = auth_client.post(
                retry_payment_url(),
                {"payment_token": "tok-new-card", "idempotency_key": "idem-retry-8"},
                format="json",
            )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "no_outstanding_balance"
        assert "detail" in response.data
        mock_invoice.pay.assert_not_called()


@pytest.mark.django_db
class TestChangePlanReaffirmingSettledPlanStaysANoOp:
    """Regression pin for the billing API contract hardening plan's explicit
    non-goal: ``change-plan`` re-requesting the plan/interval the subscription
    is *already* settled on stays a silent 200 no-op -- its ``already_settled``
    guard is load-bearing for the concurrent-first-upgrade story (see
    ``SubscriptionService.request_plan_change``'s docstring) and this plan does
    not touch it. ``retry-payment`` (``TestRetryPaymentEndpoint`` above), not a
    ``change-plan`` fix, is the supported way to recover a GRACE/RESTRICTED
    subscription -- see the plan's **Risk & Rollout Notes**, "What this plan
    does not fix"."""

    def test_reaffirming_the_settled_plan_is_still_a_200_no_op(
        self, billing_client, admin_membership, subscription, free_plan
    ):
        response = billing_client.post(
            change_plan_url(),
            {
                "plan_slug": free_plan.slug,
                "billing_interval": BillingInterval.MONTHLY,
                "idempotency_key": "idem-reaffirm-1",
                "payment_token": "tok-1",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["plan"]["slug"] == free_plan.slug
        assert response.data["billing_state"] == BillingState.FREE
