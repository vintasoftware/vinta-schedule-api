"""Phase 8: the postpaid ``event_occurrences`` guard, driven through every real
entry point that reaches ``CalendarEventService.create_event``.

The Guiding Decision behind this phase names six distinct entry points: REST
(``calendar_integration/views.py`` -- ``CalendarEventViewSet``), the token surface
(``calendar_integration/token_views.py``), and three GraphQL mutations
(``public_api/mutations.py``'s ``scheduleEvent``, and
``calendar_integration/mutations.py``'s ``createCalendarEventWithCode`` /
``createCalendarGroupEventWithCode``), plus the bulk sync writer
(``calendar_integration/services/calendar_sync_service.py``). Guarding a viewset
would leave the sync path unmetered, so the enforcement layer is the service --
these tests exist to prove every *surface* still reaches it and renders the
resulting ``OverLimitError`` correctly rather than a raw 500 or a silent success.

Also covers the fan-out case Phase 7's tracking doc names as the plan's own
recurring failure shape (guard and meter disagreeing on what one booking costs):
a bundle event over five ``INTERNAL`` children costs 5 units of headroom, one over
five Google children costs 1 -- because a ``BlockedTime`` is never billable.
"""

import base64
import datetime
from unittest.mock import Mock, patch

from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
    CalendarPermissionService,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import (
    BillingState,
    Entitlement,
    LimitedResource,
    LimitKind,
    LimitRemedy,
)
from payments.exceptions import OverLimitError
from payments.models import (
    BillingPlan,
    MeteredOccurrence,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import Profile, User


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def _organization_with_postpaid_limit(
    limit_value: int | None, billing_state: str = BillingState.FREE
) -> tuple[Organization, Subscription]:
    """A standalone (non-reseller) organization with a ceiling on ``event_occurrences``.

    ``limit_value=None`` builds an ``unlimited``-shaped subscription -- every
    organization's actual state for the whole rollout, and the property every
    surface below must prove is inert.

    Every ``Entitlement`` is granted so the ``partner_api`` gate (public GraphQL)
    and the ``external_calendar_google`` / ``external_calendar_microsoft`` gates
    (any Google/Microsoft-provider calendar) never mask the postpaid guard under
    test -- this file is about ``event_occurrences``, not those other gates.
    """
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = datetime.datetime.now(datetime.UTC)
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=billing_state,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.EVENT_OCCURRENCES,
        limit_value=limit_value,
        kind=LimitKind.POSTPAID,
    )
    for entitlement_key in Entitlement.values:
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=entitlement_key,
            is_enabled=True,
        )
    return organization, subscription


def _seed_metered_occurrences(organization: Organization, subscription: Subscription, count: int):
    MeteredOccurrence.objects.bulk_create(
        [
            MeteredOccurrence(
                organization=organization,
                subscription=subscription,
                event_id=800000 + i,
                occurrence_start=subscription.current_period_start + datetime.timedelta(hours=i),
                billing_period_start=subscription.current_period_start,
                is_within_allowance=True,
                unit_price=0,
            )
            for i in range(count)
        ]
    )


def _at_the_allowance_no_payment_method(limit_value: int = 1) -> tuple[Organization, Subscription]:
    """An organization sitting exactly at its ``event_occurrences`` allowance with no
    payment method on file -- the one state every surface below must block on."""
    organization, subscription = _organization_with_postpaid_limit(limit_value, BillingState.FREE)
    _seed_metered_occurrences(organization, subscription, limit_value)
    return organization, subscription


# ----------------------------------------------------------------------------------
# 1. REST -- calendar_integration/views.py CalendarEventViewSet.perform_create
# ----------------------------------------------------------------------------------


def _google_backed_owner(
    organization: Organization, calendar: Calendar
) -> tuple[User, SocialAccount]:
    """A User with a real Google SocialAccount/SocialToken, membership, ownership,
    and a calendar-level ``CalendarManagementToken`` for ``calendar`` -- the
    minimum a *real* (non-DI-mocked) User-driven ``create_event`` needs.
    ``CalendarService.authenticate`` resolves an adapter from the account
    regardless of the target calendar's own provider, and
    ``CalendarPermissionService.initialize_with_user`` requires an existing
    calendar-level token to resolve permissions from (mirrors
    ``test_calendar_event_service.py``'s ``calendar_owner_token`` fixture).
    """
    from calendar_integration.models import CalendarManagementToken, CalendarOwnership

    user = User.objects.create_user(email=f"rest-{organization.pk}@example.com", password="x")
    Profile.objects.create(user=user)
    OrganizationMembership.objects.create(user=user, organization=organization, is_active=True)
    social_account = SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid=f"uid-{organization.pk}"
    )
    SocialToken.objects.create(
        account=social_account,
        token="access-token",
        token_secret="refresh-token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=user.id, organization=organization
    )
    token = CalendarManagementToken.objects.create(
        calendar_fk=calendar, membership_user_id=user.id, organization=organization
    )
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token, permission=permission, organization=organization
        )
    return user, social_account


@pytest.fixture
def mock_google_adapter():
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


def _rest_payload(calendar: Calendar, organization: Organization) -> dict:
    start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    return {
        "organization": organization.id,
        "calendar": calendar.id,
        "title": "REST Surface Event",
        "description": "",
        "start_time": start.isoformat(),
        "end_time": (start + datetime.timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
        "resource_allocations": [],
        "attendances": [],
        "external_attendances": [],
    }


@pytest.mark.django_db
class TestRestSurface:
    def test_blocked_at_the_allowance_returns_402(self, mock_google_adapter):
        organization, _subscription = _at_the_allowance_no_payment_method()
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id=f"rest-cal-{organization.pk}",
        )
        user, _social_account = _google_backed_owner(organization, calendar)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            reverse("api:CalendarEvents-list"),
            _rest_payload(calendar, organization),
            format="json",
        )

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        body = response.json()
        assert body["code"] == "limit_exceeded"
        assert body["resource"] == LimitedResource.EVENT_OCCURRENCES
        assert body["remedy"] == LimitRemedy.ADD_PAYMENT_METHOD
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()

    def test_unlimited_plan_is_unchanged(self, mock_google_adapter):
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 10_000)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            external_id=f"rest-cal-unlimited-{organization.pk}",
        )
        user, _social_account = _google_backed_owner(organization, calendar)
        mock_google_adapter.create_event.return_value = CalendarEventAdapterOutputData(
            calendar_external_id=calendar.external_id,
            external_id="rest-created-event",
            title="REST Surface Event",
            description="",
            start_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1),
            end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1, hours=1),
            timezone="UTC",
            attendees=[],
            resources=[],
            original_payload={},
        )

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            reverse("api:CalendarEvents-list"),
            _rest_payload(calendar, organization),
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content


# ----------------------------------------------------------------------------------
# 2. Token REST -- calendar_integration/token_views.py TokenCalendarEventViewSet
# ----------------------------------------------------------------------------------


def _auth_header(token_id: int, token_str: str) -> str:
    return f"Bearer {base64.b64encode(f'{token_id}:{token_str}'.encode()).decode()}"


def _token_for_owned_calendar(
    organization: Organization, calendar: Calendar
) -> tuple[CalendarManagementToken, str]:
    user = User.objects.create_user(email=f"token-{organization.pk}@example.com", password="x")
    Profile.objects.create(user=user)
    OrganizationMembership.objects.create(user=user, organization=organization, is_active=True)
    token_str = generate_long_lived_token()
    token = CalendarManagementToken.objects.create(
        calendar_fk=calendar,
        membership_user_id=user.id,
        token_hash=hash_long_lived_token(token_str),
        organization=organization,
    )
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token, permission=permission, organization=organization
        )
    return token, token_str


def _token_payload(calendar: Calendar) -> dict:
    start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    return {
        "title": "Token Surface Event",
        "description": "",
        "start_time": start.isoformat(),
        "end_time": (start + datetime.timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
        "calendar": calendar.id,
        "attendances": [],
        "external_attendances": [],
        "resource_allocations": [],
    }


@pytest.mark.django_db
class TestTokenSurface:
    def test_blocked_at_the_allowance_returns_402(self):
        organization, _subscription = _at_the_allowance_no_payment_method()
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.INTERNAL,
            external_id=f"token-cal-{organization.pk}",
        )
        token, token_str = _token_for_owned_calendar(organization, calendar)

        client = APIClient()
        response = client.post(
            reverse(
                "calendar_token_api:token-events-list", kwargs={"organization_id": organization.id}
            ),
            data=_token_payload(calendar),
            format="json",
            HTTP_AUTHORIZATION=_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        body = response.json()
        assert body["resource"] == LimitedResource.EVENT_OCCURRENCES
        assert body["remedy"] == LimitRemedy.ADD_PAYMENT_METHOD
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()

    def test_unlimited_plan_is_unchanged(self):
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 10_000)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.INTERNAL,
            external_id=f"token-cal-unlimited-{organization.pk}",
        )
        token, token_str = _token_for_owned_calendar(organization, calendar)

        client = APIClient()
        response = client.post(
            reverse(
                "calendar_token_api:token-events-list", kwargs={"organization_id": organization.id}
            ),
            data=_token_payload(calendar),
            format="json",
            HTTP_AUTHORIZATION=_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content


# ----------------------------------------------------------------------------------
# 3. Public GraphQL -- public_api/mutations.py scheduleEvent
# ----------------------------------------------------------------------------------

_SCHEDULE_EVENT = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) {
        id
        title
    }
}
"""


def _scoped_system_user(
    organization: Organization, membership: OrganizationMembership
) -> tuple[object, str]:
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"sched-surface-{organization.pk}",
        organization=organization,
        scoped_to_membership=membership,
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    return system_user, token


def _owner_with_calendar(
    organization: Organization,
) -> tuple[User, OrganizationMembership, Calendar]:
    from calendar_integration.models import CalendarOwnership

    owner = User.objects.create_user(
        email=f"sched-owner-{organization.pk}@example.com", password="x"
    )
    Profile.objects.create(user=owner)
    membership = OrganizationMembership.objects.create(
        user=owner, organization=organization, is_active=True
    )
    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        external_id=f"sched-cal-{organization.pk}",
    )
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=owner.id, organization=organization
    )
    return owner, membership, calendar


@pytest.mark.django_db
class TestPublicApiScheduleEventSurface:
    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_blocked_at_the_allowance_carries_the_shared_body(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        organization, _subscription = _at_the_allowance_no_payment_method()
        _owner, membership, calendar = _owner_with_calendar(organization)
        system_user, token = _scoped_system_user(organization, membership)

        start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": _SCHEDULE_EVENT,
                "variables": {
                    "input": {
                        "organizationId": organization.id,
                        "calendarId": calendar.id,
                        "startTime": start.isoformat(),
                        "endTime": (start + datetime.timedelta(hours=1)).isoformat(),
                        "timezone": "UTC",
                        "title": "Public API Surface Event",
                    }
                },
            },
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )

        assert response.status_code == 200
        errors = response.json()["errors"]
        body = errors[0]["extensions"]
        assert body["code"] == "limit_exceeded"
        assert body["resource"] == LimitedResource.EVENT_OCCURRENCES
        assert body["remedy"] == LimitRemedy.ADD_PAYMENT_METHOD
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_unlimited_plan_is_unchanged(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 10_000)
        _owner, membership, calendar = _owner_with_calendar(organization)
        system_user, token = _scoped_system_user(organization, membership)

        start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": _SCHEDULE_EVENT,
                "variables": {
                    "input": {
                        "organizationId": organization.id,
                        "calendarId": calendar.id,
                        "startTime": start.isoformat(),
                        "endTime": (start + datetime.timedelta(hours=1)).isoformat(),
                        "timezone": "UTC",
                        "title": "Public API Surface Event",
                    }
                },
            },
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data
        assert data["data"]["scheduleEvent"]["title"] == "Public API Surface Event"


# ----------------------------------------------------------------------------------
# 4. Booking-code GraphQL -- calendar_integration/mutations.py createCalendarEventWithCode
# ----------------------------------------------------------------------------------

_CREATE_EVENT_WITH_CODE = """
mutation CreateCalendarEventWithCode($input: CreateEventWithCodeInput!) {
    createCalendarEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event { id }
    }
}
"""


def _booking_code_for_calendar(
    organization: Organization, calendar: Calendar
) -> tuple[CalendarManagementToken, str]:
    permission_service = CalendarPermissionService()
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.mark.django_db
class TestBookingCodeEventSurface:
    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_blocked_at_the_allowance_is_a_graphql_error_not_a_result(self, mock_rate_limiter):
        """Unlike the domain errors this mutation maps to ``CodeEventResult``, an
        over-limit block is not something a patient can retry around -- it must
        surface via the shared GraphQL error contract instead."""
        mock_rate_limiter.return_value = iter([None])
        organization, _subscription = _at_the_allowance_no_payment_method()
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.INTERNAL,
            accepts_public_scheduling=False,
            external_id=f"code-cal-{organization.pk}",
        )
        token, code = _booking_code_for_calendar(organization, calendar)

        start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": _CREATE_EVENT_WITH_CODE,
                "variables": {
                    "input": {
                        "code": code,
                        "title": "Booking Code Surface Event",
                        "description": "",
                        "startTime": start.isoformat(),
                        "endTime": (start + datetime.timedelta(hours=1)).isoformat(),
                        "timezone": "UTC",
                        "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors"), data
        body = data["errors"][0]["extensions"]
        assert body["code"] == "limit_exceeded"
        assert body["resource"] == LimitedResource.EVENT_OCCURRENCES
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()
        # The code must not have been consumed by a rejected booking.
        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_unlimited_plan_is_unchanged(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 10_000)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.INTERNAL,
            accepts_public_scheduling=False,
            external_id=f"code-cal-unlimited-{organization.pk}",
        )
        _token, code = _booking_code_for_calendar(organization, calendar)

        start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": _CREATE_EVENT_WITH_CODE,
                "variables": {
                    "input": {
                        "code": code,
                        "title": "Booking Code Surface Event",
                        "description": "",
                        "startTime": start.isoformat(),
                        "endTime": (start + datetime.timedelta(hours=1)).isoformat(),
                        "timezone": "UTC",
                        "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True


# ----------------------------------------------------------------------------------
# 5. Booking-code GraphQL -- createCalendarGroupEventWithCode
# ----------------------------------------------------------------------------------

_CREATE_GROUP_EVENT_WITH_CODE = """
mutation CreateCalendarGroupEventWithCode($input: CreateGroupEventWithCodeInput!) {
    createCalendarGroupEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event { id }
    }
}
"""


def _group_with_one_slot(organization: Organization) -> tuple[object, object, Calendar]:
    from calendar_integration.models import (
        CalendarGroup,
        CalendarGroupSlot,
        CalendarGroupSlotMembership,
    )

    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        accepts_public_scheduling=False,
        external_id=f"group-code-cal-{organization.pk}",
    )
    group = baker.make(CalendarGroup, organization=organization)
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Providers", order=0, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return group, slot, calendar


def _group_booking_code(organization: Organization, group) -> tuple[CalendarManagementToken, str]:
    permission_service = CalendarPermissionService()
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.mark.django_db
class TestBookingCodeGroupEventSurface:
    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_blocked_at_the_allowance_is_a_graphql_error_not_a_result(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        organization, _subscription = _at_the_allowance_no_payment_method()
        group, slot, calendar = _group_with_one_slot(organization)
        token, code = _group_booking_code(organization, group)

        start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": _CREATE_GROUP_EVENT_WITH_CODE,
                "variables": {
                    "input": {
                        "code": code,
                        "title": "Group Booking Code Surface Event",
                        "description": "",
                        "startTime": start.isoformat(),
                        "endTime": (start + datetime.timedelta(hours=1)).isoformat(),
                        "timezone": "UTC",
                        "slotSelections": [{"slotId": slot.id, "calendarIds": [calendar.id]}],
                        "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors"), data
        body = data["errors"][0]["extensions"]
        assert body["code"] == "limit_exceeded"
        assert body["resource"] == LimitedResource.EVENT_OCCURRENCES
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()
        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_unlimited_plan_is_unchanged(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None])
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 10_000)
        group, slot, calendar = _group_with_one_slot(organization)
        _token, code = _group_booking_code(organization, group)

        start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={
                "query": _CREATE_GROUP_EVENT_WITH_CODE,
                "variables": {
                    "input": {
                        "code": code,
                        "title": "Group Booking Code Surface Event",
                        "description": "",
                        "startTime": start.isoformat(),
                        "endTime": (start + datetime.timedelta(hours=1)).isoformat(),
                        "timezone": "UTC",
                        "slotSelections": [{"slotId": slot.id, "calendarIds": [calendar.id]}],
                        "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data
        assert data["data"]["createCalendarGroupEventWithCode"]["success"] is True


# ----------------------------------------------------------------------------------
# 6. Bulk sync writer -- calendar_integration/services/calendar_sync_service.py
# ----------------------------------------------------------------------------------


def _sync_setup(organization: Organization) -> tuple[CalendarSyncService, Calendar]:
    """A ``CalendarSyncService`` wired with a fake write adapter and no real
    provider auth -- ``authenticate()`` itself is out of scope here (it is a
    separate, already-tested entitlement gate); this exercises the sync-writer
    guard specifically.
    """
    calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_id=f"sync-cal-{organization.pk}",
    )
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)
    # `is_authenticated_calendar_service` requires `account` and `calendar_adapter`
    # to both be non-None; account's contents are never read by the sync writer.
    facade.account = Mock(name="fake-social-account")
    facade.calendar_adapter = Mock(name="fake-calendar-adapter")
    facade.calendar_adapter.get_events.return_value = {"events": [], "next_sync_token": None}
    sync_service = CalendarSyncService(
        context=facade._build_context_snapshot(), calendar_cache={}, host=facade
    )
    return sync_service, calendar


def _recurring_master_adapter_event(external_id: str) -> CalendarEventAdapterOutputData:
    start = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    return CalendarEventAdapterOutputData(
        calendar_external_id="irrelevant",
        external_id=external_id,
        title="Synced recurring master",
        description="",
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=5",
    )


@pytest.mark.django_db
class TestBulkSyncWriterSurface:
    """Drives ``CalendarSyncService`` directly (not through the Celery task): the
    guard runs inside ``_execute_calendar_sync``, and ``sync_events`` already wraps
    it in ``try/except Exception`` -- the same behavior a real scheduled sync gets."""

    def test_blocked_at_the_allowance_fails_the_sync_and_writes_nothing(self):
        organization, _subscription = _at_the_allowance_no_payment_method()
        sync_service, calendar = _sync_setup(organization)

        from calendar_integration.models import CalendarSync

        calendar_sync = CalendarSync.objects.create(
            organization=organization,
            calendar=calendar,
            start_datetime=datetime.datetime.now(datetime.UTC),
            end_datetime=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
            should_update_events=False,
        )
        sync_service._context.calendar_adapter.get_events.return_value = {
            "events": [_recurring_master_adapter_event("sync-master-1")],
            "next_sync_token": None,
        }

        sync_service.sync_events(calendar_sync)

        calendar_sync.refresh_from_db()
        assert calendar_sync.status == "failed"
        assert calendar_sync.error_message  # the OverLimitError's message, not empty
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()

    def test_unlimited_plan_is_unchanged(self):
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 10_000)
        sync_service, calendar = _sync_setup(organization)

        from calendar_integration.models import CalendarSync

        calendar_sync = CalendarSync.objects.create(
            organization=organization,
            calendar=calendar,
            start_datetime=datetime.datetime.now(datetime.UTC),
            end_datetime=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
            should_update_events=False,
        )
        sync_service._context.calendar_adapter.get_events.return_value = {
            "events": [_recurring_master_adapter_event("sync-master-unlimited")],
            "next_sync_token": None,
        }

        sync_service.sync_events(calendar_sync)

        calendar_sync.refresh_from_db()
        assert calendar_sync.status == "success"
        assert CalendarEvent.objects.filter(
            calendar=calendar, external_id="sync-master-unlimited"
        ).exists()


# ----------------------------------------------------------------------------------
# Bundle fan-out: 1 + n_internal_children, never per member calendar
# ----------------------------------------------------------------------------------


def _bundle_with_children(
    organization: Organization, provider: str, count: int
) -> tuple[Calendar, Calendar, list[Calendar]]:
    """A bundle calendar with ``count`` children of ``provider``, all
    ``accepts_public_scheduling=True`` (so the permission gate never masks the
    postpaid guard under test). Returns ``(bundle_calendar, primary, children)``."""
    children = [
        baker.make(
            Calendar,
            organization=organization,
            provider=provider,
            calendar_type=CalendarType.PERSONAL,
            accepts_public_scheduling=True,
            manage_available_windows=False,
            external_id=f"bundle-child-{provider}-{organization.pk}-{i}",
        )
        for i in range(count)
    ]
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)
    bundle_calendar = facade.create_bundle_calendar(
        name=f"Bundle {provider} {organization.pk}",
        child_calendars=children,
        primary_calendar=children[0],
        accepts_public_scheduling=True,
    )
    return bundle_calendar, children[0], children


def _create_bundle_event_with_open_availability(
    facade: CalendarService, bundle_calendar: Calendar, event_data: CalendarEventInputData
) -> CalendarEvent:
    """Drive ``facade.create_event`` against a bundle with availability mocked open.

    Mirrors ``test_calendar_bundle_service.py``'s established pattern: once the
    primary event is created (tagged ``bundle_calendar=<this bundle>``),
    ``AvailabilityService.get_unavailable_time_windows_in_range`` treats it as busy
    on *every* member calendar by design (a bundle slot is booked across the whole
    bundle) -- so each subsequent child's own, real ``create_event`` availability
    check would otherwise see its own sibling's just-created event as an exact,
    gap-free conflict. That correctly reflects how a booked bundle slot behaves;
    it is not what this test is about, so availability is mocked open the same way
    the bundle service's own test suite does, leaving the postpaid guard, the
    permission checks, and every DB write completely real.
    """
    from calendar_integration.services.dataclasses import AvailableTimeWindow

    availability_window = [
        AvailableTimeWindow(start_time=event_data.start_time, end_time=event_data.end_time)
    ]
    with patch.object(
        facade, "get_availability_windows_in_range", return_value=availability_window
    ):
        return facade.create_event(bundle_calendar.id, event_data)


@pytest.mark.django_db
class TestBundleEventFanOutHeadroom:
    def test_five_internal_children_checks_five_units(self):
        """1 (primary) + 4 (remaining internal children) = 5 -- headroom for 4 is
        not enough, even though a single-event booking would fit."""
        organization, subscription = _organization_with_postpaid_limit(4, BillingState.FREE)
        bundle_calendar, _primary, _children = _bundle_with_children(
            organization, CalendarProvider.INTERNAL, 5
        )
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = subscription.current_period_start + datetime.timedelta(days=1)
        with pytest.raises(OverLimitError) as exc_info:
            _create_bundle_event_with_open_availability(
                facade,
                bundle_calendar,
                CalendarEventInputData(
                    title="Bundle Fan-Out",
                    description="",
                    start_time=start,
                    end_time=start + datetime.timedelta(hours=1),
                    timezone="UTC",
                    attendances=[],
                    external_attendances=[],
                    resource_allocations=[],
                ),
            )

        assert exc_info.value.resource_key == LimitedResource.EVENT_OCCURRENCES
        assert not CalendarEvent.objects.filter(bundle_calendar=bundle_calendar).exists()

    def test_five_internal_children_with_headroom_for_five_succeeds(self):
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.FREE)
        bundle_calendar, _primary, _children = _bundle_with_children(
            organization, CalendarProvider.INTERNAL, 5
        )
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)
        # `CalendarEvent.external_id` is globally unique and every un-adapted create
        # stamps `""`. Five real CalendarEvent writes in one fan-out would collide on
        # the second one -- give the facade a fake write adapter that stamps a
        # distinct external_id per call, exactly what an authenticated production
        # facade would have (its own real provider adapter). No owner/ownership is
        # set up, so `user_or_token` stays None and permission is still granted
        # purely through `accepts_public_scheduling=True`.
        counter = iter(range(1000))
        fake_adapter = Mock()
        fake_adapter.create_event.side_effect = lambda *a, **k: CalendarEventAdapterOutputData(
            calendar_external_id="irrelevant",
            external_id=f"bundle-fanout-{next(counter)}",
            title="irrelevant",
            description="",
            start_time=datetime.datetime.now(datetime.UTC),
            end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
            timezone="UTC",
            attendees=[],
            resources=[],
            original_payload={},
        )
        facade.calendar_adapter = fake_adapter

        start = subscription.current_period_start + datetime.timedelta(days=1)
        event = _create_bundle_event_with_open_availability(
            facade,
            bundle_calendar,
            CalendarEventInputData(
                title="Bundle Fan-Out Fits",
                description="",
                start_time=start,
                end_time=start + datetime.timedelta(hours=1),
                timezone="UTC",
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            ),
        )

        assert event.pk is not None
        # Primary + 4 internal representations = 5 CalendarEvent rows for this bundle --
        # the same 5 units the guard above checked headroom for.
        assert CalendarEvent.objects.filter(bundle_calendar=bundle_calendar).count() == 5

    def test_five_google_children_checks_only_one_unit(self):
        """A bundle over five Google calendars costs 1, not 5: only the primary gets
        a real ``CalendarEvent``, the rest get a non-billable ``BlockedTime``."""
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        bundle_calendar, _primary, _children = _bundle_with_children(
            organization, CalendarProvider.GOOGLE, 5
        )
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        # At the allowance (1 used, ceiling 1) with no payment method: a fan-out
        # costing only 1 unit must still be BLOCKED (it is a new unit on top of an
        # already-full allowance) -- proving the guard actually counted 1, not 0.
        start = subscription.current_period_start + datetime.timedelta(days=1)
        with pytest.raises(OverLimitError):
            _create_bundle_event_with_open_availability(
                facade,
                bundle_calendar,
                CalendarEventInputData(
                    title="Google Bundle Fan-Out",
                    description="",
                    start_time=start,
                    end_time=start + datetime.timedelta(hours=1),
                    timezone="UTC",
                    attendances=[],
                    external_attendances=[],
                    resource_allocations=[],
                ),
            )

    def test_five_google_children_with_headroom_for_one_succeeds(self):
        """The same bundle succeeds once headroom for just 1 unit exists -- proving
        the guard did *not* charge 5 (which would still block at ceiling=1)."""
        organization, subscription = _organization_with_postpaid_limit(2, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        bundle_calendar, _primary, _children = _bundle_with_children(
            organization, CalendarProvider.GOOGLE, 5
        )
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = subscription.current_period_start + datetime.timedelta(days=1)
        event = _create_bundle_event_with_open_availability(
            facade,
            bundle_calendar,
            CalendarEventInputData(
                title="Google Bundle Fan-Out Fits",
                description="",
                start_time=start,
                end_time=start + datetime.timedelta(hours=1),
                timezone="UTC",
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            ),
        )

        assert event.pk is not None
        # Only the primary got a real CalendarEvent -- the other four Google children
        # got a BlockedTime instead, which is exactly why this fan-out costs 1 unit.
        assert list(
            CalendarEvent.objects.filter(bundle_calendar=bundle_calendar).values_list(
                "pk", flat=True
            )
        ) == [event.pk]


# ----------------------------------------------------------------------------------
# Coverage registry: every named surface has a probe here
# ----------------------------------------------------------------------------------

GUARDED_SURFACES = {
    "rest": TestRestSurface,
    "token": TestTokenSurface,
    "public_api_schedule_event": TestPublicApiScheduleEventSurface,
    "booking_code_single_event": TestBookingCodeEventSurface,
    "booking_code_group_event": TestBookingCodeGroupEventSurface,
    "bulk_sync_writer": TestBulkSyncWriterSurface,
}


def test_every_named_surface_has_a_blocked_and_unlimited_probe():
    """The plan names exactly six ``CalendarEvent`` creation entry points. A
    surface class registered here without both a blocked-at-the-allowance and an
    unlimited-plan test method would defeat the point of this file."""
    assert set(GUARDED_SURFACES) == {
        "rest",
        "token",
        "public_api_schedule_event",
        "booking_code_single_event",
        "booking_code_group_event",
        "bulk_sync_writer",
    }
    for name, test_class in GUARDED_SURFACES.items():
        method_names = {m for m in dir(test_class) if m.startswith("test_")}
        assert any("blocked" in m for m in method_names), f"{name} has no blocked-path test"
        assert any("unlimited" in m for m in method_names), f"{name} has no unlimited-plan test"
