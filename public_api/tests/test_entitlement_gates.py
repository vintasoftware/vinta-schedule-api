"""Phase 6c: boolean entitlement gates.

Spec use-case 2 (blocked with a useful message) and use-case 7 (the partner API
is not a bypass), applied to the three named chokepoints:

- ``partner_api`` in ``PublicApiSystemUserMiddleware`` -- an organization without
  it cannot use the GraphQL API at all, not just individual mutations.
- ``external_calendar_google`` / ``external_calendar_microsoft`` in
  ``CalendarService.authenticate`` (the account's provider) **and** in
  ``CalendarService._get_write_adapter_for_calendar`` (the calendar's provider,
  which can differ -- see ``TestWriteAdapterProviderGate``).

Unlike a pre-paid limit, an entitlement is boolean and uses
``EntitlementService.has_entitlement``, which fails **closed** in every unknown
case: an absent or disabled row on a real subscription, and a missing subscription
entirely. That is deliberately *not* symmetric with ``get_effective_limit``'s
fail-open — for a numeric ceiling "we don't know" means unlimited, which is what
the rollout seeds anyway; for a boolean gate it would mean *granted*, handing paid
features to organizations whose billing state is corrupt. See
``EntitlementService.has_entitlement``'s docstring.

Every test in this module that asserts a block was confirmed to fail when its
corresponding guard was removed.
"""

import datetime
from typing import TYPE_CHECKING

from django.test import override_settings
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
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement
from public_api.models import ResourceAccess


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


if TYPE_CHECKING:
    from di_core.containers import AppContainer


def _container() -> "AppContainer":
    """The wired DI container, narrowed for mypy.

    Imported inside the function body on purpose: ``di_core.containers.container`` is
    only *assigned* in ``DICoreConfig.ready()``, so a module-level ``from ... import
    container`` binds ``None`` forever. The root ``conftest.py``'s ``di_container``
    fixture defers the import for the same reason.
    """
    from di_core.containers import container

    assert container is not None, "DI container is only assigned in DICoreConfig.ready()"
    return container


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
    from payments.services.subscription_service import SubscriptionService

    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    plan = BillingPlan.objects.get(slug="unlimited")
    SubscriptionService().create_subscription_for_organization(organization, plan=plan)
    return organization


# ---------------------------------------------------------------------------
# partner_api -- PublicApiSystemUserMiddleware
# ---------------------------------------------------------------------------

TYPENAME_QUERY = "{ __typename }"
USERS_QUERY = "query { users { id } }"

_EXPECTED_ERROR_KEYS = {"detail", "code", "resource", "current_usage", "limit", "remedy"}


def _post_graphql(client, system_user, token, query):
    auth_service = _container().public_api_auth_service()
    with _container().public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": {}},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


def _make_system_user(organization: Organization, *, with_user_access: bool = False) -> tuple:
    auth_service = _container().public_api_auth_service()
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
        entitlement error body.

        Asserts the exact status rather than ``!= 402``: ``!= 402`` also passes on a
        500, which would hide the gate breaking the anonymous path outright.
        """
        client = APIClient()

        response = client.post(
            "/graphql/",
            data={"query": USERS_QUERY, "variables": {}},
            format="json",
        )

        # graphql-core reports authorization failures in the `errors` array of a 200.
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["errors"]


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


# The Microsoft adapter refuses to construct without these. Only the *positive*
# controls below need them: the blocked cases never get as far as building an adapter,
# which is itself the point of gating before `get_calendar_adapter_for_account`.
_with_ms_credentials = override_settings(
    MS_CLIENT_ID="test-ms-client-id",
    MS_CLIENT_SECRET="test-ms-client-secret",  # noqa: S106 - dummy value, not a credential
)


def _microsoft_account_with_token(user) -> SocialAccount:
    account = baker.make(SocialAccount, user=user, provider=CalendarProvider.MICROSOFT)
    baker.make(
        SocialToken,
        account=account,
        token="fake-ms-access-token",
        token_secret="fake-ms-refresh-token",
        expires_at=timezone.now() + datetime.timedelta(hours=1),
    )
    return account


def _organization_with_entitlements(**grants: bool) -> Organization:
    """A standalone organization whose subscription carries an explicit
    ``SubscriptionEntitlement`` row per keyword (``entitlement_key=is_enabled``)."""
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
    for entitlement_key, is_enabled in grants.items():
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=entitlement_key,
            is_enabled=is_enabled,
        )
    return organization


@pytest.mark.django_db
class TestExternalCalendarMicrosoftGate:
    """The Microsoft half of ``_PROVIDER_ENTITLEMENTS``.

    The phase body names both providers; only Google was exercised, so a Microsoft-only
    regression (a typo in the mapping, a provider constant drift) would have gone
    unnoticed.
    """

    def test_microsoft_account_is_blocked_without_the_entitlement(self):
        from calendar_integration.services.calendar_service import CalendarService

        organization = _organization_with_entitlements(
            **{Entitlement.EXTERNAL_CALENDAR_MICROSOFT: False}
        )
        membership = _admin_membership(organization)
        account = _microsoft_account_with_token(membership.user)

        service = _container().calendar_service()
        with pytest.raises(OverLimitError) as exc_info:
            service.authenticate(account=account, organization=organization)

        assert exc_info.value.resource_key == Entitlement.EXTERNAL_CALENDAR_MICROSOFT
        assert isinstance(service, CalendarService)

    @_with_ms_credentials
    def test_microsoft_account_is_allowed_with_the_entitlement(self):
        organization = _organization_with_entitlements(
            **{Entitlement.EXTERNAL_CALENDAR_MICROSOFT: True}
        )
        membership = _admin_membership(organization)
        account = _microsoft_account_with_token(membership.user)

        service = _container().calendar_service()
        service.authenticate(account=account, organization=organization)

        assert service.organization == organization

    def test_google_entitlement_does_not_unlock_microsoft(self):
        """The two entitlements are independent — holding one must not imply the other."""
        organization = _organization_with_entitlements(
            **{
                Entitlement.EXTERNAL_CALENDAR_GOOGLE: True,
                Entitlement.EXTERNAL_CALENDAR_MICROSOFT: False,
            }
        )
        membership = _admin_membership(organization)
        account = _microsoft_account_with_token(membership.user)

        service = _container().calendar_service()
        with pytest.raises(OverLimitError) as exc_info:
            service.authenticate(account=account, organization=organization)

        assert exc_info.value.resource_key == Entitlement.EXTERNAL_CALENDAR_MICROSOFT


@pytest.mark.django_db
class TestWriteAdapterProviderGate:
    """``authenticate`` is not the sole chokepoint, and this pins the gap it leaves.

    ``_get_write_adapter_for_calendar`` resolves an adapter from the **calendar's**
    provider, not the authenticated account's, via the *static*
    ``get_calendar_adapter_for_account`` — a different code path from the instance the
    authenticate-time gate ran against.

    The bypass this closes: an organization holds ``external_calendar_google`` but not
    ``external_calendar_microsoft``. Calendar ``C`` has ``provider=microsoft`` and is
    owned by a user who holds a Microsoft ``SocialAccount``. The actor authenticates with
    their **Google** account, so the authenticate gate passes — correctly, it is a Google
    account. Every write to ``C`` then builds a Microsoft adapter for that owner:
    unmetered, ungated Microsoft traffic.
    """

    def _setup(self, *, microsoft_enabled: bool):
        from calendar_integration.models import Calendar, CalendarOwnership

        organization = _organization_with_entitlements(
            **{
                Entitlement.EXTERNAL_CALENDAR_GOOGLE: True,
                Entitlement.EXTERNAL_CALENDAR_MICROSOFT: microsoft_enabled,
            }
        )
        actor = _admin_membership(organization)
        _google_account_with_token(actor.user)

        owner = _admin_membership(organization)
        _microsoft_account_with_token(owner.user)

        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.MICROSOFT,
            external_id="ms-calendar-1",
        )
        baker.make(
            CalendarOwnership,
            organization=organization,
            calendar=calendar,
            membership_user_id=owner.user_id,
            is_default=True,
        )

        service = _container().calendar_service()
        # Passes: the *account* is Google and the org holds external_calendar_google.
        service.authenticate(account=actor.user, organization=organization)
        return service, calendar

    def test_write_adapter_is_blocked_for_an_unentitled_calendar_provider(self):
        service, calendar = self._setup(microsoft_enabled=False)

        with pytest.raises(OverLimitError) as exc_info:
            service._get_write_adapter_for_calendar(calendar)

        assert exc_info.value.resource_key == Entitlement.EXTERNAL_CALENDAR_MICROSOFT

    @_with_ms_credentials
    def test_write_adapter_is_returned_when_the_calendar_provider_is_entitled(self):
        service, calendar = self._setup(microsoft_enabled=True)

        assert service._get_write_adapter_for_calendar(calendar) is not None
