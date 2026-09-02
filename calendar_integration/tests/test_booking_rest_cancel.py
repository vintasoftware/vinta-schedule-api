"""Integration tests for ``POST /public/booking/events/cancel/``.

Ports the scenarios in ``public_api/tests/test_cancel_with_code.py`` (the
GraphQL ``cancelEventWithCode`` equivalent) to the REST surface. Covers BOTH
the calendar-bound (non-grouped) path and the calendar-group-bound (grouped
event) path via the SAME endpoint, exactly as the GraphQL original does.

All requests are unauthenticated (no session/JWT). The booking code -- carried
in the ``X-Booking-Code`` header -- provides the org scope, event scope, and
CANCEL permission.
"""

import datetime
from unittest.mock import patch

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization
from public_api.models import SystemUser


CANCEL_URL_NAME = "calendar_booking_api:booking-events-cancel-list"


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Cancel-With-Code Test Org")


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    return APIClient()


def _post(client: APIClient, code: str | None):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(reverse(CANCEL_URL_NAME), {}, format="json", headers=headers)


# ---------------------------------------------------------------------------
# Fixtures -- single-calendar path
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def existing_event(organization, calendar):
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Appointment",
        description="A scheduled appointment.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
        calendar_group=None,
    )


@pytest.fixture
def cancel_code(permission_service, organization, calendar, existing_event):
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def reschedule_code(permission_service, organization, calendar, existing_event):
    """A RESCHEDULE-only code -- wrong permission for cancellation."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def create_code(permission_service, organization, calendar):
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Fixtures -- group path
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar",
        external_id="primary-cal-cancel-rest-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def secondary_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Room Calendar",
        external_id="room-cal-cancel-rest-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def group(organization, primary_calendar, secondary_calendar):
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Physicians", order=0, required_count=1
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Rooms", order=1, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=primary_calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_b, calendar=secondary_calendar
    )
    return grp


@pytest.fixture
def grouped_event(organization, group, primary_calendar, secondary_calendar):
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=primary_calendar,
        calendar_group=group,
        title="Group Appointment",
        description="A grouped appointment.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )

    slot_a = CalendarGroupSlot.objects.filter_by_organization(organization.id).get(
        group=group, name="Physicians"
    )
    slot_b = CalendarGroupSlot.objects.filter_by_organization(organization.id).get(
        group=group, name="Rooms"
    )
    CalendarEventGroupSelection.objects.create(
        organization=organization, event=event, slot=slot_a, calendar=primary_calendar
    )
    CalendarEventGroupSelection.objects.create(
        organization=organization, event=event, slot=slot_b, calendar=secondary_calendar
    )

    BlockedTime.objects.create(
        organization=organization,
        calendar=secondary_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        timezone="UTC",
        reason=f"Group booking: {event.title}",
        external_id=f"group-event-{event.id}-cal-{secondary_calendar.id}",
    )

    return event


@pytest.fixture
def group_cancel_code(permission_service, organization, group, grouped_event):
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_group_id=group.id,
        event_id=grouped_event.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Scenario 1: Calendar cancel happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeCalendarHappyPath:
    def test_happy_path_cancels_event_and_consumes_code(
        self, anon_client, cancel_code, existing_event
    ):
        token, code = cancel_code
        event_id = existing_event.id

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.content

        assert not CalendarEvent.original_manager.filter(id=event_id).exists()
        # Token is gone: the event FK has on_delete=CASCADE, so deleting the
        # event also removes the token. Non-existence proves the cancel was
        # atomic (consume succeeded, then event+token were removed together).
        assert not CalendarManagementToken.original_manager.filter(pk=token.pk).exists()

    def test_cancels_exactly_the_bound_event(
        self, anon_client, cancel_code, organization, calendar
    ):
        token, code = cancel_code

        other_event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title="Other Event",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2030, 6, 2, 10, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 2, 11, 0),
            external_id="other-event-cancel-rest-001",
        )

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_204_NO_CONTENT

        assert not CalendarEvent.original_manager.filter(id=token.event_fk_id).exists()
        assert CalendarEvent.original_manager.filter(id=other_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 2: Group cancel happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeGroupHappyPath:
    def test_happy_path_cancels_grouped_event(
        self, anon_client, group_cancel_code, organization, secondary_calendar, grouped_event
    ):
        token, code = group_cancel_code
        event_id = grouped_event.id

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.content

        assert not CalendarEvent.original_manager.filter(id=event_id).exists()
        assert not CalendarEventGroupSelection.original_manager.filter(
            event_fk_id=event_id
        ).exists()
        assert not BlockedTime.original_manager.filter(
            external_id__startswith=f"group-event-{event_id}-cal-"
        ).exists()
        assert not CalendarManagementToken.original_manager.filter(pk=token.pk).exists()

    def test_non_primary_blocked_times_deleted_not_orphaned(
        self, anon_client, group_cancel_code, secondary_calendar, grouped_event
    ):
        _token, code = group_cancel_code
        event_id = grouped_event.id

        assert BlockedTime.original_manager.filter(
            external_id=f"group-event-{event_id}-cal-{secondary_calendar.id}"
        ).exists()

        _post(anon_client, code)

        assert not BlockedTime.original_manager.filter(
            external_id__startswith=f"group-event-{event_id}-cal-"
        ).exists()


# ---------------------------------------------------------------------------
# Scenario 3: Replay -> INVALID_CODE (token cascade-deleted with the event)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeReplay:
    def test_replay_returns_invalid_code_after_cancel(self, anon_client, cancel_code):
        _token, code = cancel_code

        first = _post(anon_client, code)
        assert first.status_code == status.HTTP_204_NO_CONTENT

        second = _post(anon_client, code)
        assert second.status_code == status.HTTP_404_NOT_FOUND
        assert second.json()["error_code"] == "INVALID_CODE"

    def test_group_cancel_replay_returns_invalid_code(self, anon_client, group_cancel_code):
        _token, code = group_cancel_code

        first = _post(anon_client, code)
        assert first.status_code == status.HTTP_204_NO_CONTENT

        second = _post(anon_client, code)
        assert second.status_code == status.HTTP_404_NOT_FOUND
        assert second.json()["error_code"] == "INVALID_CODE"


# ---------------------------------------------------------------------------
# Scenario 4: Wrong permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeWrongPermission:
    def test_reschedule_only_code_returns_not_permitted(
        self, anon_client, reschedule_code, existing_event
    ):
        _token, code = reschedule_code

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()

    def test_create_only_code_returns_not_permitted(self, anon_client, create_code, existing_event):
        _token, code = create_code

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 5: Lifecycle rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeLifecycleRejections:
    def test_expired_code_returns_expired(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
            event_id=existing_event.id,
            expires_at=past,
        )

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_410_GONE
        assert response.json()["error_code"] == "EXPIRED"
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()

    def test_revoked_code_returns_revoked(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        minter = baker.make(SystemUser, organization=organization, is_active=True)
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
            event_id=existing_event.id,
            minted_by=minter,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "REVOKED"
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()

    def test_invalid_code_returns_invalid_code(self, anon_client):
        response = _post(anon_client, "aW52YWxpZGNhbmNlbGNvZGU=")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_missing_code_returns_invalid_code(self, anon_client):
        response = _post(anon_client, None)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_already_used_code_returns_already_used(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        CalendarManagementToken.original_manager.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "ALREADY_USED"
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 6: Atomicity -- consume rolls back when delete fails
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeAtomicity:
    def test_delete_failure_leaves_code_live_and_event_intact(
        self, anon_client, cancel_code, existing_event
    ):
        token, code = cancel_code

        with patch(
            "calendar_integration.services.calendar_service.CalendarService.delete_event",
            side_effect=RuntimeError("Simulated delete failure"),
        ):
            with pytest.raises(RuntimeError):
                _post(anon_client, code)

        refreshed = CalendarManagementToken.original_manager.filter(pk=token.pk).first()
        assert refreshed is not None, "Token was unexpectedly deleted (consume was not rolled back)"
        assert refreshed.used_at is None, "Token was consumed despite the delete failure"

        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists(), (
            "Event was deleted despite the delete failure rolling back"
        )


# ---------------------------------------------------------------------------
# Scenario 7: "Not bound to a specific event" -- the 403 branch that no
# wrong-permission fixture reaches (both existing wrong-permission fixtures
# fail earlier, at the permission check inside resolve_and_authorize_write).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeNotBoundToEvent:
    def test_code_without_event_binding_returns_not_permitted(
        self, anon_client, permission_service, organization, calendar, existing_event
    ):
        """A code that carries CANCEL and a calendar_id, but no event_id,
        passes the permission check inside ``resolve_and_authorize_write`` and
        only then trips ``if token.event is None`` (booking_views.py ~L960)."""
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
        )

        response = _post(anon_client, code)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert body["detail"] == "This code is not bound to a specific event."
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()
