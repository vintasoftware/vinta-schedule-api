"""Integration tests for ``POST /public/booking/calendar-groups/<group_id>/events/``.

Ports the scenarios in ``public_api/tests/test_book_group_with_code.py`` (the
GraphQL ``createCalendarGroupEventWithCode`` equivalent) to the REST surface,
plus the Phase 2-specific path/token scope-mismatch, single-calendar-code
rejection, and pinned-duration cases the plan's Phase 2 body calls for.

All requests are unauthenticated (no session/JWT). The booking code -- carried
in the ``X-Booking-Code`` header -- provides the org scope, group scope, and
CREATE permission. ``group_id`` in the path is a routing convenience only; the
real scope comes from the resolved token.
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)


def _booking_url(group_id: int) -> str:
    return f"/public/booking/calendar-groups/{group_id}/events/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Group-Book-With-Code Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def primary_calendar(organization):
    """The primary (first-slot) calendar. RESTRICTED: accepts_public_scheduling=False.

    Using a restricted calendar ensures the test exercises the group-scoped
    branch of ``can_perform_scheduling`` / ``can_perform_group_scheduling``. A
    public calendar would mask a permission regression.
    """
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar (Dr. A)",
        external_id="rest-primary-cal-group-code-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def secondary_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Room Calendar",
        external_id="rest-room-cal-group-code-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup with two slots: slot_a (primary_calendar) and slot_b (secondary_calendar)."""
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization,
        group=grp,
        name="Physicians",
        order=0,
        required_count=1,
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization,
        group=grp,
        name="Rooms",
        order=1,
        required_count=1,
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=primary_calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_b, calendar=secondary_calendar
    )
    return grp


@pytest.fixture
def other_group(other_organization):
    """A CalendarGroup in a DIFFERENT organization -- used to prove the path
    <group_id> does not leak the code's real group across a mismatch."""
    return baker.make(CalendarGroup, organization=other_organization, name="Other Org Group")


@pytest.fixture
def availability_windows(organization, primary_calendar, secondary_calendar):
    """Availability windows for both calendars: 09:00-17:00 UTC on 2030-06-01."""
    windows = []
    for cal in (primary_calendar, secondary_calendar):
        windows.append(
            AvailableTime.objects.create(
                organization=organization,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
                timezone="UTC",
            )
        )
    return windows


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def group_booking_code(permission_service, organization, group):
    """A valid single-use CREATE code scoped to the group."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.fixture
def calendar_scoped_code(permission_service, organization, primary_calendar):
    """A CREATE code scoped to a single calendar -- wrong scope for this endpoint."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=primary_calendar.id,
    )
    return token, code


@pytest.fixture
def reschedule_group_code(permission_service, organization, group):
    """A RESCHEDULE-only group code -- wrong permission for booking."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot_selections(
    group: CalendarGroup, primary_calendar: Calendar, secondary_calendar: Calendar
):
    slot_a = group.slots.get(name="Physicians")
    slot_b = group.slots.get(name="Rooms")
    return [
        {"slot_id": slot_a.id, "calendar_ids": [primary_calendar.id]},
        {"slot_id": slot_b.id, "calendar_ids": [secondary_calendar.id]},
    ]


def _group_booking_payload(slot_selections: list[dict], **overrides) -> dict:
    base = {
        "title": "Group Appointment",
        "description": "A group booking",
        "start_time": BOOKING_START.isoformat(),
        "end_time": BOOKING_END.isoformat(),
        "timezone": "UTC",
        "slot_selections": slot_selections,
        "external_attendee": {
            "email": "patient@example.com",
            "name": "Pat Patient",
        },
    }
    base.update(overrides)
    return base


def _post(client: APIClient, group_id: int, code: str | None, payload: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(_booking_url(group_id), payload, format="json", headers=headers)


# ---------------------------------------------------------------------------
# Scenario 1: Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeHappyPath:
    """Scenario 1: Valid group code + restricted primary calendar + available slots.

    RESTRICTED primary calendar (accepts_public_scheduling=False) exercises the
    group-scoped branch of ``can_perform_scheduling`` / ``can_perform_group_scheduling``.
    """

    def test_happy_path_creates_grouped_event_and_consumes_code(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002 -- seeds DB rows
    ):
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["title"] == "Group Appointment"

        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.calendar_fk_id == primary_calendar.id
        assert event.calendar_group_fk_id == group.id
        assert event.organization_id == organization.id

    def test_event_has_external_attendee(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        external_attendances = list(event.external_attendances.select_related("external_attendee"))
        assert len(external_attendances) == 1
        assert external_attendances[0].external_attendee.email == "patient@example.com"
        assert external_attendances[0].external_attendee.name == "Pat Patient"


# ---------------------------------------------------------------------------
# Scenario 2: Replay
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeReplay:
    def test_replay_returns_already_used(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        payload = _group_booking_payload(selections)

        first = _post(anon_client, group.id, code, payload)
        assert first.status_code == status.HTTP_201_CREATED, first.content

        second = _post(anon_client, group.id, code, payload)
        assert second.status_code == status.HTTP_409_CONFLICT
        assert second.json()["error_code"] == "ALREADY_USED"

        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1


# ---------------------------------------------------------------------------
# Scenario 3: Failed write does not consume the code
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeFailedWriteDoesNotConsume:
    def test_slot_outside_availability_does_not_consume_code(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002 -- window is 09:00-17:00 UTC
    ):
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        out_of_window_payload = _group_booking_payload(
            selections,
            start_time=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            end_time=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )

        response = _post(anon_client, group.id, code, out_of_window_payload)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    def test_after_failed_write_code_can_still_be_used(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        out_of_window_payload = _group_booking_payload(
            selections,
            start_time=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            end_time=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )
        fail_response = _post(anon_client, group.id, code, out_of_window_payload)
        assert fail_response.status_code == status.HTTP_409_CONFLICT

        token.refresh_from_db()
        assert token.used_at is None, "Code must remain active after failed write"

        success_response = _post(anon_client, group.id, code, _group_booking_payload(selections))
        assert success_response.status_code == status.HTTP_201_CREATED, success_response.content

        token.refresh_from_db()
        assert token.used_at is not None
        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1

    def test_invalid_slot_selection_does_not_consume_code(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        token, code = group_booking_code
        bad_selections = [{"slot_id": 999999, "calendar_ids": [primary_calendar.id]}]

        response = _post(anon_client, group.id, code, _group_booking_payload(bad_selections))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 4: Wrong scope (calendar-scoped code)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeWrongScope:
    def test_calendar_scoped_code_returns_not_permitted(
        self,
        anon_client,
        calendar_scoped_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        _token, code = calendar_scoped_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 5: Missing CREATE permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeMissingPermission:
    def test_reschedule_code_returns_not_permitted(
        self,
        anon_client,
        reschedule_group_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        _token, code = reschedule_group_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 6: Lifecycle rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeLifecycleRejections:
    def test_expired_code_returns_expired(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
            expires_at=past,
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_410_GONE
        assert response.json()["error_code"] == "EXPIRED"

    def test_revoked_code_returns_revoked(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "REVOKED"

    def test_invalid_code_returns_invalid_code(self, anon_client, group):
        response = _post(
            anon_client,
            group.id,
            "aW52YWxpZGJvb2tpbmdjb2Rl",
            _group_booking_payload([]),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_missing_code_returns_invalid_code(self, anon_client, group):
        response = _post(anon_client, group.id, None, _group_booking_payload([]))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_used_code_returns_already_used(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        CalendarManagementToken.original_manager.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "ALREADY_USED"


# ---------------------------------------------------------------------------
# Scenario 7: Cross-org
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeCrossOrg:
    def test_org_a_code_books_only_org_a_resources(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
        other_organization,
    ):
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.organization_id == token.organization_id
        assert not CalendarEvent.objects.filter_by_organization(other_organization.id).exists()

    def test_org_a_code_cannot_book_org_b_calendar(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        availability_windows,  # noqa: ARG002
        other_organization,
    ):
        """Injecting a foreign-org calendar id into a slot selection is rejected
        (SLOT_UNAVAILABLE, not a member of the org-A group's slots) and the code
        stays active."""
        foreign_calendar = baker.make(
            Calendar,
            organization=other_organization,
            external_id="rest-cross-org-b-cal",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        slot_a = group.slots.get(name="Physicians")
        bad_selections = [{"slot_id": slot_a.id, "calendar_ids": [foreign_calendar.id]}]

        response = _post(anon_client, group.id, code, _group_booking_payload(bad_selections))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()
        assert not CalendarEvent.objects.filter_by_organization(other_organization.id).exists()


# ---------------------------------------------------------------------------
# Path <group_id> vs token scope: no enumeration oracle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodePathGroupMismatch:
    def test_path_group_mismatch_returns_403_not_404(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        """A code for `group` presented against a DIFFERENT group id in the path
        must return 403 NOT_PERMITTED, never 404 -- a 404 would confirm the
        code's real group to whoever is probing."""
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        wrong_group_id = group.id + 999999

        response = _post(anon_client, wrong_group_id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    def test_path_group_mismatch_books_nothing_in_either_group(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        other_group,
        other_organization,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        """A code for org-A's `group` presented against a real but DIFFERENT
        group (belonging to another organization entirely) neither books in the
        token's real group nor discloses/uses the path group -- it is rejected
        before any group is touched."""
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, other_group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"

        token.refresh_from_db()
        assert token.used_at is None
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()
        assert not CalendarEvent.objects.filter_by_organization(other_organization.id).exists()

    def test_matching_path_group_id_still_succeeds(
        self,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        """Sanity check: the mismatch guard does not accidentally reject the
        legitimate, matching path id."""
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post(anon_client, group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content


# ---------------------------------------------------------------------------
# Pinned duration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodePinnedDuration:
    def test_pinned_duration_books_at_exact_span(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
            duration=datetime.timedelta(minutes=30),
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        payload = _group_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=30)).isoformat()
        )

        response = _post(anon_client, group.id, code, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_pinned_duration_refuses_a_different_span_across_multi_slot_selection(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        """The pin applies to the grouped event's own times, not per member
        calendar -- a multi-slot selection at the wrong span is refused just
        the same as a single-calendar booking."""
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
            duration=datetime.timedelta(minutes=30),
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        # 45-minute span -- does not match the 30-minute pin.
        payload = _group_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )

        response = _post(anon_client, group.id, code, payload)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "30 minute" in body["detail"]

        token.refresh_from_db()
        assert token.used_at is None
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    def test_unpinned_code_accepts_any_span(
        self,
        anon_client,
        group_booking_code,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        payload = _group_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )

        response = _post(anon_client, group.id, code, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content
