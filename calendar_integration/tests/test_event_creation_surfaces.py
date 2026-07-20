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

import ast
import base64
import datetime
import pathlib
from unittest.mock import Mock, patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

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
    """``count`` already-metered occurrences in the subscription's current billing
    period -- what ``_count_event_occurrences`` reads back as usage.

    Each row points at a **real** ``CalendarEvent``. An earlier version invented
    ``event_id=800000 + i`` for rows whose events did not exist; that only survived
    because Django declares FK constraints ``DEFERRABLE INITIALLY DEFERRED`` and a
    test transaction is rolled back rather than committed, so the check never ran. A
    fixture that depends on never committing is a fixture that breaks the first time
    somebody writes a ``transactional_db`` test against it.
    """
    seed_calendar = baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        external_id=f"usage-seed-cal-{organization.pk}",
    )
    period_start = subscription.current_period_start
    events = CalendarEvent.objects.bulk_create(
        [
            CalendarEvent(
                organization=organization,
                calendar_fk=seed_calendar,
                title=f"Seeded usage {i}",
                description="",
                start_time_tz_unaware=(period_start + datetime.timedelta(hours=i)).replace(
                    tzinfo=None
                ),
                end_time_tz_unaware=(
                    period_start + datetime.timedelta(hours=i, minutes=30)
                ).replace(tzinfo=None),
                timezone="UTC",
                # `CalendarEvent.external_id` is globally unique and an un-adapted
                # create stamps `""`, so the seeds must not also use `""` or the very
                # first real create in the test collides with them.
                external_id=f"usage-seed-{organization.pk}-{i}",
            )
            for i in range(count)
        ]
    )
    MeteredOccurrence.objects.bulk_create(
        [
            MeteredOccurrence(
                organization=organization,
                subscription=subscription,
                event_id=event.pk,
                occurrence_start=period_start + datetime.timedelta(hours=i),
                billing_period_start=period_start,
                is_within_allowance=True,
                unit_price=0,
            )
            for i, event in enumerate(events)
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
        _seed_metered_occurrences(organization, subscription, 1)
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
        _seed_metered_occurrences(organization, subscription, 1)
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
    # Built through the container rather than `PublicAPIAuthService()`: its
    # `audit_service` is a required constructor argument, satisfied by DI at runtime
    # but not by a bare call (which mypy correctly rejects). Imported inside the
    # function because `di_core.containers.container` is None until app startup wires
    # it -- a module-level `from ... import container` would capture the None.
    from di_core.containers import container

    assert container is not None
    auth_service = container.public_api_auth_service()
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
        _seed_metered_occurrences(organization, subscription, 1)
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
        _seed_metered_occurrences(organization, subscription, 1)
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
        _seed_metered_occurrences(organization, subscription, 1)
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
        _seed_metered_occurrences(organization, subscription, 1)
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
# Inertness, observed rather than inferred
# ----------------------------------------------------------------------------------


@pytest.mark.django_db
class TestUnlimitedPlanTakesNoLock:
    """The ``test_unlimited_plan_is_unchanged`` probes above assert a 201/success,
    which is necessary but blind to *how* the guard reached it.

    An earlier revision of ``check_postpaid_allowance`` took ``SELECT ... FOR UPDATE``
    on the billing root's ``Subscription`` row **before** resolving the limit, so every
    event creation for every organization -- all of which are on ``unlimited`` for this
    whole rollout -- took an organization-wide row lock, inside ``create_event``'s
    transaction, held across the external provider round-trip. Two users booking
    different calendars of the same organization serialized on it. Every one of those
    tests still passed, because the request still returned 201.

    So this asserts the property directly: on the unlimited path the guard issues no
    row lock at all. A future change cannot silently put one back.
    """

    def test_no_row_lock_is_taken_on_the_unlimited_path(self):
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            accepts_public_scheduling=True,
            manage_available_windows=False,
            external_id=f"nolock-cal-{organization.pk}",
        )
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = subscription.current_period_start + datetime.timedelta(days=1)
        with CaptureQueriesContext(connection) as captured:
            facade.create_event(
                calendar.id,
                CalendarEventInputData(
                    title="Unlimited, unlocked",
                    description="",
                    start_time=start,
                    end_time=start + datetime.timedelta(hours=1),
                    timezone="UTC",
                    attendances=[],
                    external_attendances=[],
                    resource_allocations=[],
                ),
            )

        locking = [
            query["sql"]
            for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"].upper()
            and "PAYMENTS_SUBSCRIPTION" in query["sql"].upper()
        ]
        assert not locking, (
            "check_postpaid_allowance took SELECT ... FOR UPDATE on payments_subscription "
            "for an organization with a NULL event_occurrences ceiling. Every organization "
            "is on `unlimited` for this rollout and create_event is @transaction.atomic "
            "around a provider round-trip, so this serializes every booking in the "
            f"organization on one row for a ceiling that cannot block anybody: {locking}"
        )

    def test_a_finite_ceiling_still_takes_the_lock(self):
        """The negative control: the lock is *deferred* until a real ceiling exists,
        not deleted. Without this, "take no lock" would pass by removing the guard's
        concurrency protection entirely."""
        organization, subscription = _organization_with_postpaid_limit(50, BillingState.ACTIVE)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            accepts_public_scheduling=True,
            manage_available_windows=False,
            external_id=f"lock-cal-{organization.pk}",
        )
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = subscription.current_period_start + datetime.timedelta(days=1)
        with CaptureQueriesContext(connection) as captured:
            facade.create_event(
                calendar.id,
                CalendarEventInputData(
                    title="Finite ceiling, locked",
                    description="",
                    start_time=start,
                    end_time=start + datetime.timedelta(hours=1),
                    timezone="UTC",
                    attendances=[],
                    external_attendances=[],
                    resource_allocations=[],
                ),
            )

        assert any(
            "FOR UPDATE" in query["sql"].upper() and "PAYMENTS_SUBSCRIPTION" in query["sql"].upper()
            for query in captured.captured_queries
        )


# ----------------------------------------------------------------------------------
# Recurring masters: the guard must charge occurrences, not masters
# ----------------------------------------------------------------------------------


def _bookable_calendar(organization: Organization, slug: str) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        accepts_public_scheduling=True,
        manage_available_windows=False,
        external_id=f"{slug}-{organization.pk}",
    )


@pytest.mark.django_db
class TestRecurringMasterCostsItsOccurrences:
    """``current_usage`` counts ``MeteredOccurrence`` rows -- one per *occurrence*.
    A recurring master is one row in ``CalendarEvent`` but ~30 occurrences a month in
    the meter, so a guard that charges 1 per master lets an organization one unit
    below its ceiling create an open-ended daily series and accrue unbillable usage
    forever, never tripping the guard on that series again.

    The delta comes from ``MeteringService.occurrence_starts_of`` -- the meter's own
    expansion, called rather than re-implemented.
    """

    def test_a_daily_series_costs_its_occurrences_not_one(self):
        organization, subscription = _organization_with_postpaid_limit(10, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 9)
        calendar = _bookable_calendar(organization, "recurring-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        # 9 used of 10: a *single* event would fit (9 + 1 <= 10). A daily series of 5
        # does not (9 + 5 > 10), and the old master-counting guard would have allowed it.
        start = timezone.now() + datetime.timedelta(days=1)
        with pytest.raises(OverLimitError) as exc_info:
            facade.create_event(
                calendar.id,
                CalendarEventInputData(
                    title="Daily standup",
                    description="",
                    start_time=start,
                    end_time=start + datetime.timedelta(minutes=30),
                    timezone="UTC",
                    recurrence_rule="RRULE:FREQ=DAILY;COUNT=5",
                    attendances=[],
                    external_attendances=[],
                    resource_allocations=[],
                ),
            )

        assert exc_info.value.resource_key == LimitedResource.EVENT_OCCURRENCES
        assert exc_info.value.remedy == LimitRemedy.ADD_PAYMENT_METHOD
        # Stage 2 runs after the insert and rolls it back; nothing survives.
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()

    def test_a_single_event_of_the_same_shape_still_fits(self):
        """The control for the test above: with 9 of 10 used, one booking is allowed.
        Without this, the block above could just be an over-eager guard."""
        organization, subscription = _organization_with_postpaid_limit(10, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 9)
        calendar = _bookable_calendar(organization, "single-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = timezone.now() + datetime.timedelta(days=1)
        event = facade.create_event(
            calendar.id,
            CalendarEventInputData(
                title="One booking",
                description="",
                start_time=start,
                end_time=start + datetime.timedelta(minutes=30),
                timezone="UTC",
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            ),
        )

        assert event.pk is not None

    def test_a_series_that_fits_is_created(self):
        organization, subscription = _organization_with_postpaid_limit(10, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 4)
        calendar = _bookable_calendar(organization, "fits-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = timezone.now() + datetime.timedelta(days=1)
        event = facade.create_event(
            calendar.id,
            CalendarEventInputData(
                title="Daily standup that fits",
                description="",
                start_time=start,
                end_time=start + datetime.timedelta(minutes=30),
                timezone="UTC",
                recurrence_rule="RRULE:FREQ=DAILY;COUNT=5",
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            ),
        )

        assert event.pk is not None
        assert event.is_recurring

    def test_create_recurring_event_shortcut_is_guarded_the_same_way(self):
        """``create_recurring_event`` is a thin shortcut over ``create_event``; it must
        not be a way around the occurrence-counting guard."""
        organization, subscription = _organization_with_postpaid_limit(10, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 9)
        calendar = _bookable_calendar(organization, "shortcut-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = timezone.now() + datetime.timedelta(days=1)
        with pytest.raises(OverLimitError):
            facade.create_recurring_event(
                calendar_id=calendar.id,
                title="Daily standup",
                description="",
                start_time=start,
                end_time=start + datetime.timedelta(minutes=30),
                timezone="UTC",
                recurrence_rule="RRULE:FREQ=DAILY;COUNT=5",
            )

    def test_the_unlimited_plan_is_unchanged_for_a_series(self):
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = _bookable_calendar(organization, "unlimited-recurring-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = timezone.now() + datetime.timedelta(days=1)
        with CaptureQueriesContext(connection) as captured:
            event = facade.create_event(
                calendar.id,
                CalendarEventInputData(
                    title="Unlimited daily standup",
                    description="",
                    start_time=start,
                    end_time=start + datetime.timedelta(minutes=30),
                    timezone="UTC",
                    recurrence_rule="RRULE:FREQ=DAILY;COUNT=5",
                    attendances=[],
                    external_attendances=[],
                    resource_allocations=[],
                ),
            )

        assert event.pk is not None
        # The expansion is behind the unlimited early-return, so an unlimited
        # organization does not pay for it -- nor for a row lock.
        assert not [
            query["sql"]
            for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"].upper()
            and "PAYMENTS_SUBSCRIPTION" in query["sql"].upper()
        ]


# ----------------------------------------------------------------------------------
# Internal re-entries into create_event, and the bypass switch
# ----------------------------------------------------------------------------------


@pytest.mark.django_db
class TestInternalCreateEventReentries:
    def test_bulk_modification_continuation_is_guarded(self):
        """Splitting a series creates a *continuation* master through ``create_event``.
        It is a genuinely new master the meter enumerates in its own right, so it is
        checked -- and, being recurring, checked for its occurrences."""
        organization, subscription = _organization_with_postpaid_limit(20, BillingState.FREE)
        calendar = _bookable_calendar(organization, "bulkmod-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        # Microsecond-free: `OccurrenceValidator.validate_modification_date` compares
        # the split date against `dateutil.rrule` output, which truncates microseconds,
        # so a `now()`-derived start would never match its own second occurrence.
        start = (timezone.now() + datetime.timedelta(days=1)).replace(microsecond=0)
        parent = facade.create_event(
            calendar.id,
            CalendarEventInputData(
                title="Weekly sync",
                description="",
                start_time=start,
                end_time=start + datetime.timedelta(minutes=30),
                timezone="UTC",
                recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=5",
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            ),
        )

        # Fill the allowance to the brim before splitting, so the continuation's own
        # create is the thing that has to fail.
        _seed_metered_occurrences(organization, subscription, 20)

        # The split first *updates* the parent (to truncate it), which runs the
        # ordinary permission-token check -- unrelated to the guard under test and
        # needing a full event-token fixture to satisfy. Mocked open; the guard and
        # every write stay real.
        with (
            patch.object(CalendarPermissionService, "can_perform_update", return_value=True),
            pytest.raises(OverLimitError) as exc_info,
        ):
            facade.create_recurring_event_bulk_modification(
                parent_event=parent,
                modification_start_date=parent.start_time + datetime.timedelta(weeks=1),
                modified_title="Weekly sync (moved)",
            )

        assert exc_info.value.resource_key == LimitedResource.EVENT_OCCURRENCES

    def test_transfer_between_calendars_is_not_charged(self):
        """A transfer is a **move**: ``transfer_event`` creates the event on
        the target calendar and deletes it from the source, so it is net-zero on
        billable masters. Charging it would compare ``delta=1`` against a usage count
        that already includes the moved event's own occurrences and hand an
        organization at its allowance a 402 for no net new billable capacity."""
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        source = _bookable_calendar(organization, "transfer-src")
        target = _bookable_calendar(organization, "transfer-dst")

        start = subscription.current_period_start + datetime.timedelta(days=1)
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=source,
            title="Movable",
            description="",
            start_time_tz_unaware=start.replace(tzinfo=None),
            end_time_tz_unaware=(start + datetime.timedelta(hours=1)).replace(tzinfo=None),
            timezone="UTC",
            external_id="transfer-external-id",
        )

        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)
        # `transfer_event` requires an authenticated service (it reads the
        # source event back off the provider). Everything below the guard is real.
        adapter = Mock()
        adapter.get_event.return_value = CalendarEventAdapterOutputData(
            calendar_external_id=source.external_id,
            external_id=event.external_id,
            title="Movable",
            description="",
            start_time=start,
            end_time=start + datetime.timedelta(hours=1),
            timezone="UTC",
            attendees=[],
            resources=[],
            original_payload={},
        )
        adapter.create_event.return_value = CalendarEventAdapterOutputData(
            calendar_external_id=target.external_id,
            external_id="transferred-external-id",
            title="Movable",
            description="",
            start_time=start,
            end_time=start + datetime.timedelta(hours=1),
            timezone="UTC",
            attendees=[],
            resources=[],
            original_payload={},
        )
        facade.account = Mock(name="fake-social-account")
        facade.calendar_adapter = adapter

        # At the allowance (1 of 1) with no payment method: a *creation* would be
        # blocked here (that is what TestRestSurface asserts). A move must not be.
        #
        # The transfer's second half (`delete_event` on the source) runs the ordinary
        # permission-token update check, which has nothing to do with the post-paid
        # guard under test and would need a full event-token fixture to satisfy. It is
        # mocked open; the guard, both creates, and every DB write stay real.
        with patch.object(CalendarPermissionService, "can_perform_update", return_value=True):
            moved = facade.transfer_event(event, target)

        assert moved.pk is not None
        assert moved.calendar_fk_id == target.id
        assert not CalendarEvent.objects.filter(pk=event.pk).exists()

    def test_bypass_limits_skips_the_guard(self):
        """The plan's Enforcement-bypass Guiding Decision: every guarded write takes
        ``bypass_limits`` so a management command or a support repair can run against
        an organization that is over its allowance."""
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = _bookable_calendar(organization, "bypass-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)

        start = subscription.current_period_start + datetime.timedelta(days=1)
        event_data = CalendarEventInputData(
            title="Bypassed",
            description="",
            start_time=start,
            end_time=start + datetime.timedelta(hours=1),
            timezone="UTC",
            attendances=[],
            external_attendances=[],
            resource_allocations=[],
        )

        # Control: without the bypass this exact call is blocked.
        with pytest.raises(OverLimitError):
            facade.create_event(calendar.id, event_data)

        event = facade.create_event(calendar.id, event_data, bypass_limits=True)
        assert event.pk is not None

    def test_a_service_in_bypass_mode_skips_the_guard(self):
        """``CalendarService.authenticate(bypass_limits=True)`` puts the whole service
        in bypass mode. The provider-entitlement gate honours it; so must this guard,
        or a service explicitly placed in bypass mode is still post-paid-blocked."""
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = _bookable_calendar(organization, "bypassmode-cal")
        facade = CalendarService()
        facade.initialize_without_provider(organization=organization)
        facade._bypass_entitlement_limits = True

        start = subscription.current_period_start + datetime.timedelta(days=1)
        event = facade.create_event(
            calendar.id,
            CalendarEventInputData(
                title="Bypass mode",
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


@pytest.mark.django_db
class TestSyncRecoversOnceHeadroomIsRestored:
    """An over-allowance sync is refused, recorded as ``FAILED``, and retried on the
    next scheduled pass. Nothing in the ``CalendarSync`` row distinguishes that from a
    provider outage, so the refusal is logged with the organization and the remedy --
    and, critically, it must **self-heal**: once headroom exists, the very next sync
    over the same window must succeed with no manual replay."""

    def test_the_same_sync_succeeds_once_headroom_exists(self):
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
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
            "events": [_recurring_master_adapter_event("sync-recovers")],
            "next_sync_token": None,
        }

        sync_service.sync_events(calendar_sync)
        calendar_sync.refresh_from_db()
        assert calendar_sync.status == "failed"

        # Headroom restored (here by attaching a payment method -- the remedy the
        # refusal reports). No replay, no manual intervention: just the next pass.
        subscription.billing_state = BillingState.ACTIVE
        subscription.save(update_fields=["billing_state"])

        sync_service.sync_events(calendar_sync)
        calendar_sync.refresh_from_db()
        assert calendar_sync.status == "success"
        assert CalendarEvent.objects.filter(calendar=calendar, external_id="sync-recovers").exists()


# ----------------------------------------------------------------------------------
# Coverage registry: every module that reaches the guard has a probe here
# ----------------------------------------------------------------------------------

#: Repository root, for the source walk below.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The three modules that *implement* the guarded write chain rather than consume it,
#: excluded from the derived set below. Each has a reason, not a convenience:
#:
#: - ``calendar_event_service.py`` holds the guard itself, and its internal
#:   re-entries into ``create_event`` (``create_recurring_event``, the
#:   bulk-modification continuation, ``transfer_event``) are covered by
#:   ``TestInternalCreateEventReentries`` below rather than by a "surface" probe.
#: - ``calendar_service.py`` is the facade that forwards to it.
#: - ``calendar_bundle_service.py`` is the fan-out, covered by
#:   ``TestBundleEventFanOutHeadroom``.
_GUARD_IMPLEMENTATION_MODULES = {
    "calendar_integration/services/calendar_event_service.py",
    "calendar_integration/services/calendar_service.py",
    "calendar_integration/services/calendar_bundle_service.py",
}

#: Call names that mean "this module can trip the post-paid guard": the two service
#: entry points that route into ``CalendarEventService.create_event``, and a direct
#: call to the guard for a writer (the sync path) that does not go through it.
_GUARD_REACHING_CALLS = {"create_event", "create_recurring_event", "check_postpaid_allowance"}

#: Receivers whose ``create_event`` is the *provider* adapter's (a Google/Microsoft
#: API write), not this service's. ``ExternalEventChangeRequestService`` calls
#: ``write_adapter.create_event`` to re-create an event on the provider after an
#: undone deletion -- no local ``CalendarEvent`` is created and nothing is metered,
#: so it is not a guarded surface.
_ADAPTER_RECEIVER_MARKERS = ("adapter", "client")


def _receiver_name(node: ast.Attribute) -> str:
    """The identifier the guarded call is made *on* (``x.y.create_event`` -> ``y``)."""
    value = node.value
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Name):
        return value.id
    return ""


#: Every module the walk below is expected to find, mapped to the test class(es) that
#: drive it. The **keys are asserted against the source tree**, in both directions --
#: this is the difference between this test and the hand-written literal it replaced,
#: which compared a dict in this file against a list in this file and would therefore
#: have passed unchanged when a seventh entry point appeared.
GUARDED_SURFACES: dict[str, tuple[type, ...]] = {
    # REST (`CalendarEventViewSet`) and the token REST surface share one serializer,
    # which is the module that actually issues the create.
    "calendar_integration/serializers.py": (TestRestSurface, TestTokenSurface),
    # `createCalendarEventWithCode` (direct) and `createCalendarGroupEventWithCode`
    # (via `calendar_group_service`, below).
    "calendar_integration/mutations.py": (
        TestBookingCodeEventSurface,
        TestBookingCodeGroupEventSurface,
    ),
    "public_api/mutations.py": (TestPublicApiScheduleEventSurface,),
    "calendar_integration/services/calendar_group_service.py": (TestBookingCodeGroupEventSurface,),
    "calendar_integration/services/calendar_sync_service.py": (TestBulkSyncWriterSurface,),
}


def _modules_reaching_the_guard() -> set[str]:
    """Every module in the tree that can trip the post-paid guard, read out of the
    source with ``ast`` rather than listed by hand.

    Tests, migrations, factories, and the calendar *adapters* (whose own
    ``create_event`` talks to Google/Microsoft, not to this service) are excluded, as
    are the three guard-implementation modules above.

    Scoped to this project's own **first-party Django app packages** (resolved from
    ``django.apps``, kept to those living under the repo), not a blind ``rglob`` of the
    repo root. Two reasons, one correctness and one operational: a blind walk would
    read non-source trees like ``mediafiles`` / ``templates`` / a nested
    ``.claude/worktrees`` checkout of this same repo -- making the result depend on
    what else is on disk -- and parsing every one of them is slow enough to trip this
    test's ``pytest-timeout`` under parallel load. Walking only the app packages is
    both the correct set and an order of magnitude less work.
    """
    from django.apps import apps

    app_dirs: list[pathlib.Path] = []
    for config in apps.get_app_configs():
        app_path = pathlib.Path(config.path).resolve()
        try:
            app_path.relative_to(_REPO_ROOT)
        except ValueError:
            continue  # third-party app installed outside the repo
        app_dirs.append(app_path)

    found: set[str] = set()
    for app_dir in app_dirs:
        for path in app_dir.rglob("*.py"):
            parts = path.relative_to(_REPO_ROOT).parts
            relative = path.relative_to(_REPO_ROOT).as_posix()
            if (
                any(part.startswith(".") for part in parts)
                or "tests" in parts
                or "migrations" in parts
                or "calendar_adapters" in parts
                or path.name == "factories.py"
                or relative in _GUARD_IMPLEMENTATION_MODULES
            ):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our source
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _GUARD_REACHING_CALLS
                    and not any(
                        marker in _receiver_name(node.func) for marker in _ADAPTER_RECEIVER_MARKERS
                    )
                ):
                    found.add(relative)
                    break
    return found


def test_every_module_reaching_the_guard_has_a_probe():
    """Derived from the source tree, and asserted both ways.

    A seventh entry point -- a new viewset, a new mutation, a new service that calls
    ``create_event`` -- appears in ``discovered`` the moment it is written and fails
    here until somebody registers a probe for it. And a registration left behind for
    a module that no longer creates events fails the other assertion, so the registry
    cannot rot in the direction of *claiming* coverage it no longer has.
    """
    discovered = _modules_reaching_the_guard()

    unprobed = discovered - GUARDED_SURFACES.keys()
    assert not unprobed, (
        f"These modules reach CalendarEventService.create_event (or the post-paid guard "
        f"directly) but have no probe in this file: {sorted(unprobed)}. Add one, or add "
        "the module to _GUARD_IMPLEMENTATION_MODULES with a reason if it implements the "
        "guarded chain rather than consuming it."
    )

    stale = GUARDED_SURFACES.keys() - discovered
    assert not stale, (
        f"GUARDED_SURFACES registers {sorted(stale)}, which no longer reach the guard. "
        "Remove the registration, or fix the call path if the surface was meant to stay "
        "guarded -- otherwise this file reports coverage of a path nothing routes through."
    )


def test_every_registered_surface_has_a_blocked_and_unlimited_probe():
    """A registered probe class that lost its blocked-path or unlimited-path test
    would leave the module above nominally covered and actually unchecked."""
    for module, test_classes in GUARDED_SURFACES.items():
        for test_class in test_classes:
            method_names = {m for m in dir(test_class) if m.startswith("test_")}
            assert any("blocked" in m for m in method_names), (
                f"{module} -> {test_class.__name__} has no blocked-path test"
            )
            assert any("unlimited" in m for m in method_names), (
                f"{module} -> {test_class.__name__} has no unlimited-plan test"
            )
