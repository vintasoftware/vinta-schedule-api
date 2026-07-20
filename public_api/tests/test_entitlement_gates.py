"""Phase 6c: boolean entitlement gates.

Spec use-case 2 (blocked with a useful message) and use-case 7 (the partner API
is not a bypass), applied to the three named chokepoints:

- ``partner_api`` in ``PublicApiSystemUserMiddleware`` -- an organization without
  it cannot use the GraphQL API at all, not just individual mutations.
- ``external_calendar_google`` / ``external_calendar_microsoft`` in
  ``CalendarService.authenticate`` -- the common chokepoint both connection paths
  flow through.

Unlike a pre-paid limit, an entitlement is boolean and uses
``EntitlementService.has_entitlement``, which fails **closed** on an existing
subscription whose entitlement row is absent or disabled, but fails **open**
when the organization has no subscription at all (the same "we don't know"
treatment ``get_effective_limit`` already gives that condition) -- see
``EntitlementService.has_entitlement``'s docstring. Every test in this module
that asserts a block was confirmed to fail when its corresponding guard was
removed.
"""

import datetime

from django.urls import reverse
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


def _organization_with_entitlement(entitlement_key: str, is_enabled: bool) -> Organization:
    """A standalone (non-reseller) organization whose subscription carries an
    explicit ``SubscriptionEntitlement`` row for ``entitlement_key``."""
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionEntitlement,
        subscription=subscription,
        entitlement_key=entitlement_key,
        is_enabled=is_enabled,
    )
    return organization


def _unlimited_organization() -> Organization:
    """An organization on the seeded ``unlimited`` plan -- every entitlement
    enabled. The rollout's own kill switch: this is what "no feature flag" means
    in practice, and every enforcement phase carries a test against it."""
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    plan = BillingPlan.objects.get(slug="unlimited")
    from payments.services.subscription_service import SubscriptionService

    SubscriptionService().create_subscription_for_organization(organization, plan=plan)
    return organization


# ---------------------------------------------------------------------------
# partner_api -- PublicApiSystemUserMiddleware
# ---------------------------------------------------------------------------

TYPENAME_QUERY = "{ __typename }"
USERS_QUERY = "query { users { id } }"

_EXPECTED_ERROR_KEYS = {"detail", "code", "resource", "current_usage", "limit", "remedy"}


def _post_graphql(client, system_user, token, query):
    from di_core.containers import container

    auth_service = PublicAPIAuthService()
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": {}},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


def _make_system_user(organization: Organization, *, with_user_access: bool = False) -> tuple:
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"test-integration-{organization.id}",
        organization=organization,
    )
    if with_user_access:
        # OrganizationResourceAccess (unrelated to this phase's gate) requires an
        # explicit grant per resource. Only needed on the positive-control paths
        # below that expect the `users` query to actually resolve.
        baker.make(ResourceAccess, system_user=system_user, resource_name="user")
    return system_user, token


@pytest.mark.django_db
class TestPartnerApiGate:
    def test_blocks_a_trivial_query_with_a_structured_402(self):
        """Even a document with no application-level field (``__typename``) never
        reaches graphql-core -- the gate is transport-level, not per-resolver."""
        organization = _organization_with_entitlement(Entitlement.PARTNER_API, is_enabled=False)
        system_user, token = _make_system_user(organization)
        client = APIClient()

        response = _post_graphql(client, system_user, token, TYPENAME_QUERY)

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        body = response.json()
        assert set(body.keys()) == _EXPECTED_ERROR_KEYS
        assert body["code"] == "limit_exceeded"
        assert body["resource"] == Entitlement.PARTNER_API
        assert body["remedy"] == "upgrade_plan"

    def test_blocks_a_real_query_with_the_same_structured_402(self):
        organization = _organization_with_entitlement(Entitlement.PARTNER_API, is_enabled=False)
        system_user, token = _make_system_user(organization)
        client = APIClient()

        response = _post_graphql(client, system_user, token, USERS_QUERY)

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.json()["resource"] == Entitlement.PARTNER_API

    def test_missing_entitlement_row_on_a_real_subscription_also_blocks(self):
        """No row at all (rather than an explicit ``is_enabled=False`` row) is how
        ``SubscriptionService._sync_entitlements`` represents a revoked grant --
        must be treated identically to an explicit denial."""
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        now = timezone.now()
        baker.make(
            Subscription,
            organization=organization,
            plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
            billing_state=BillingState.FREE,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
        )
        system_user, token = _make_system_user(organization)
        client = APIClient()

        response = _post_graphql(client, system_user, token, TYPENAME_QUERY)

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED

    def test_org_with_partner_api_enabled_is_not_blocked(self):
        organization = _organization_with_entitlement(Entitlement.PARTNER_API, is_enabled=True)
        system_user, token = _make_system_user(organization, with_user_access=True)
        client = APIClient()

        response = _post_graphql(client, system_user, token, USERS_QUERY)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "errors" not in data or not data.get("errors")

    def test_unlimited_plan_org_is_never_blocked(self):
        """The rollout's kill switch: every organization is on ``unlimited`` until
        deliberately migrated, so this must see byte-for-byte unchanged behavior."""
        organization = _unlimited_organization()
        system_user, token = _make_system_user(organization, with_user_access=True)
        client = APIClient()

        response = _post_graphql(client, system_user, token, USERS_QUERY)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "errors" not in data or not data.get("errors")

    def test_anonymous_requests_are_unaffected(self):
        """No credentials presented -> no organization resolved -> the gate never
        runs. A genuinely anonymous caller is rejected (if at all) by the normal
        ``IsAuthenticated`` permission class, not this gate -- and never with the
        entitlement error body."""
        client = APIClient()

        response = client.post(
            "/graphql/",
            data={"query": USERS_QUERY, "variables": {}},
            format="json",
        )

        assert response.status_code != status.HTTP_402_PAYMENT_REQUIRED


# ---------------------------------------------------------------------------
# external_calendar_google / external_calendar_microsoft -- CalendarService.authenticate
# ---------------------------------------------------------------------------


def _admin_membership(organization: Organization) -> OrganizationMembership:
    from users.factories import UserFactory

    user = UserFactory().create_user()
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


def _google_account_with_token(user) -> SocialAccount:
    account = baker.make(SocialAccount, user=user, provider=CalendarProvider.GOOGLE)
    baker.make(
        SocialToken,
        account=account,
        token="fake-google-access-token",
        token_secret="fake-google-refresh-token",
        expires_at=timezone.now() + datetime.timedelta(hours=1),
    )
    return account


@pytest.mark.django_db
class TestExternalCalendarGoogleGate:
    def test_cannot_import_from_a_google_account_without_the_entitlement(self):
        organization = _organization_with_entitlement(
            Entitlement.EXTERNAL_CALENDAR_GOOGLE, is_enabled=False
        )
        membership = _admin_membership(organization)
        _google_account_with_token(membership.user)

        client = APIClient()
        client.force_authenticate(user=membership.user)

        response = client.post(reverse("api:Calendars-request-import"))

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.data["resource"] == Entitlement.EXTERNAL_CALENDAR_GOOGLE

    def test_can_import_from_a_google_account_with_the_entitlement(self):
        organization = _organization_with_entitlement(
            Entitlement.EXTERNAL_CALENDAR_GOOGLE, is_enabled=True
        )
        membership = _admin_membership(organization)
        _google_account_with_token(membership.user)

        client = APIClient()
        client.force_authenticate(user=membership.user)

        response = client.post(reverse("api:Calendars-request-import"))

        assert response.status_code == status.HTTP_202_ACCEPTED

    def test_unlimited_plan_org_can_import_unchanged(self):
        organization = _unlimited_organization()
        membership = _admin_membership(organization)
        _google_account_with_token(membership.user)

        client = APIClient()
        client.force_authenticate(user=membership.user)

        response = client.post(reverse("api:Calendars-request-import"))

        assert response.status_code == status.HTTP_202_ACCEPTED
