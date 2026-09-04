"""Integration tests for Phase 8's patient self-service management codes.

A successful booking-code create or reschedule mints a fresh ``RESCHEDULE`` +
``CANCEL`` booking-code pair for the event it just wrote, and returns both
plaintexts in a ``management`` object on the ``201`` -- so the patient who
booked can manage their own appointment without anyone minting a code for
them by hand. Covers:

- Single-calendar create -> the returned ``reschedule_code`` reschedules, and
  the RE-ISSUED code from that response reschedules again (the chain
  continues); the returned ``cancel_code`` cancels.
- Group create (coded and codeless) -> the issued codes carry
  ``kind=BOOKING_CODE`` and are revokable.
- A failed booking (slot unavailable) mints nothing: no ``management`` key
  on the error body, and no new ``CalendarManagementToken`` rows.
- A re-issued group reschedule code still refuses a different span -- the
  duration pin lives on ``CalendarGroup.duration``, not on the code, so it
  survives re-issue.
- The issued codes are bound to exactly the one event they were minted for --
  a second event on the same calendar is untouched.

See ``test_booking_rest_create_event.py`` / ``test_booking_rest_create_group_event.py``
/ ``test_booking_rest_codeless_group.py`` / ``test_booking_rest_reschedule.py`` /
``test_booking_rest_cancel.py`` for this surface's pre-existing coverage --
this file adds only the Phase 8 ``management`` object behavior on top.
"""

import datetime

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.constants import (
    CalendarManagementTokenKind,
    CalendarProvider,
    CalendarType,
    EventManagementPermissions,
)
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


BOOKING_URL_NAME = "calendar_booking_api:booking-calendar-events-list"
RESCHEDULE_URL_NAME = "calendar_booking_api:booking-events-reschedule-list"
GROUP_RESCHEDULE_URL_NAME = "calendar_booking_api:booking-group-events-reschedule-list"
CANCEL_URL_NAME = "calendar_booking_api:booking-events-cancel-list"

BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)
NEW_START = datetime.datetime(2030, 6, 1, 14, 0, tzinfo=datetime.UTC)
NEW_END = datetime.datetime(2030, 6, 1, 15, 0, tzinfo=datetime.UTC)
NEWER_START = datetime.datetime(2030, 6, 1, 16, 0, tzinfo=datetime.UTC)
NEWER_END = datetime.datetime(2030, 6, 1, 17, 0, tzinfo=datetime.UTC)
OOW_START = datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC)
OOW_END = datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC)


def _group_booking_url(public_slug: str) -> str:
    return f"/public/booking/calendar-groups/{public_slug}/events/"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Management Codes Test Org")


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


def _post(client: APIClient, url_name: str, code: str | None, payload: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(reverse(url_name), payload, format="json", headers=headers)


def _post_group(client: APIClient, public_slug: str, code: str | None, payload: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(_group_booking_url(public_slug), payload, format="json", headers=headers)


def _booking_payload(**overrides) -> dict:
    base = {
        "title": "My Appointment",
        "description": "A test booking",
        "start_time": BOOKING_START.isoformat(),
        "end_time": BOOKING_END.isoformat(),
        "timezone": "UTC",
        "external_attendee": {
            "email": "patient@example.com",
            "name": "Pat Patient",
        },
    }
    base.update(overrides)
    return base


def _reschedule_payload(**overrides) -> dict:
    base = {
        "start_time": NEW_START.isoformat(),
        "end_time": NEW_END.isoformat(),
        "timezone": "UTC",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures -- single-calendar path
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar with managed availability windows."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def available_window(organization, calendar):
    """Availability window covering the original booking and both reschedule targets."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 18, 0),
        timezone="UTC",
    )


@pytest.fixture
def booking_code(permission_service, organization, calendar):
    """A valid single-use CREATE code scoped to `calendar`."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Single-calendar: reschedule chain continues
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestManagementCodesSingleCalendarRescheduleChain:
    def test_book_returns_working_reschedule_and_cancel_codes(
        self,
        anon_client,
        permission_service,
        booking_code,
        organization,
        calendar,
        available_window,  # noqa: ARG002
    ):
        _token, code = booking_code

        response = _post(anon_client, BOOKING_URL_NAME, code, _booking_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        management = body["management"]
        assert management["reschedule_code"]
        assert management["cancel_code"]
        assert management["reschedule_code"] != management["cancel_code"]

        # Both codes must be resolvable, bound to the just-created event and
        # calendar, and carry exactly the permission their name implies.
        event_id = body["id"]
        reschedule_token = permission_service.resolve_code(management["reschedule_code"])
        assert reschedule_token.event_fk_id == event_id
        assert reschedule_token.calendar_fk_id == calendar.id
        assert reschedule_token.calendar_group_fk_id is None
        assert {p.permission for p in reschedule_token.permissions.all()} == {
            EventManagementPermissions.RESCHEDULE
        }
        # expires_at defaults to the event's own end_time.
        assert reschedule_token.expires_at is not None
        assert reschedule_token.expires_at.replace(tzinfo=None) == BOOKING_END.replace(tzinfo=None)

        cancel_token = permission_service.resolve_code(management["cancel_code"])
        assert cancel_token.event_fk_id == event_id
        assert cancel_token.calendar_fk_id == calendar.id
        assert {p.permission for p in cancel_token.permissions.all()} == {
            EventManagementPermissions.CANCEL
        }

    def test_reschedule_with_issued_code_then_reissued_code_chains(
        self,
        anon_client,
        booking_code,
        available_window,  # noqa: ARG002
    ):
        """Book, reschedule with the issued code, then reschedule AGAIN with the
        code re-issued by that first reschedule -- the chain continues."""
        _token, code = booking_code

        book_response = _post(anon_client, BOOKING_URL_NAME, code, _booking_payload())
        assert book_response.status_code == status.HTTP_201_CREATED, book_response.content
        first_reschedule_code = book_response.json()["management"]["reschedule_code"]

        first_response = _post(
            anon_client, RESCHEDULE_URL_NAME, first_reschedule_code, _reschedule_payload()
        )
        assert first_response.status_code == status.HTTP_201_CREATED, first_response.content
        first_body = first_response.json()
        assert first_body["id"] == book_response.json()["id"]
        second_reschedule_code = first_body["management"]["reschedule_code"]
        assert second_reschedule_code != first_reschedule_code

        # The FIRST code is now consumed and must not work a second time.
        replay_response = _post(
            anon_client, RESCHEDULE_URL_NAME, first_reschedule_code, _reschedule_payload()
        )
        assert replay_response.status_code == status.HTTP_409_CONFLICT
        assert replay_response.json()["error_code"] == "ALREADY_USED"

        # The RE-ISSUED code from the first reschedule's response works.
        second_response = _post(
            anon_client,
            RESCHEDULE_URL_NAME,
            second_reschedule_code,
            _reschedule_payload(start_time=NEWER_START.isoformat(), end_time=NEWER_END.isoformat()),
        )
        assert second_response.status_code == status.HTTP_201_CREATED, second_response.content
        second_body = second_response.json()
        assert second_body["id"] == first_body["id"]
        # A third pair is issued -- the chain keeps continuing.
        assert second_body["management"]["reschedule_code"] != second_reschedule_code

    def test_cancel_with_issued_code_deletes_event(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002
    ):
        _token, code = booking_code

        book_response = _post(anon_client, BOOKING_URL_NAME, code, _booking_payload())
        assert book_response.status_code == status.HTTP_201_CREATED, book_response.content
        event_id = book_response.json()["id"]
        cancel_code = book_response.json()["management"]["cancel_code"]

        cancel_response = _post(anon_client, CANCEL_URL_NAME, cancel_code, {})
        assert cancel_response.status_code == status.HTTP_204_NO_CONTENT, cancel_response.content

        assert (
            not CalendarEvent.objects.filter_by_organization(organization.id)
            .filter(id=event_id)
            .exists()
        )


# ---------------------------------------------------------------------------
# Failed booking issues nothing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestManagementCodesFailedBookingIssuesNothing:
    def test_slot_unavailable_issues_no_codes_and_no_token_rows(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002 -- window is 09:00-18:00 UTC
    ):
        token, code = booking_code

        before_count = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = _post(
            anon_client,
            BOOKING_URL_NAME,
            code,
            _booking_payload(start_time=OOW_START.isoformat(), end_time=OOW_END.isoformat()),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        body = response.json()
        assert body["error_code"] == "SLOT_UNAVAILABLE"
        assert "management" not in body

        token.refresh_from_db()
        assert token.used_at is None
        after_count = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert after_count == before_count, "A failed booking must mint no token rows at all."


# ---------------------------------------------------------------------------
# Issued codes are scoped to exactly the one event they were minted for
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestManagementCodesScopedToOwnEventOnly:
    def test_reschedule_code_only_affects_its_own_event(
        self,
        anon_client,
        booking_code,
        organization,
        calendar,
        available_window,  # noqa: ARG002
    ):
        _token, code = booking_code

        book_response = _post(anon_client, BOOKING_URL_NAME, code, _booking_payload())
        assert book_response.status_code == status.HTTP_201_CREATED, book_response.content
        own_event_id = book_response.json()["id"]
        reschedule_code = book_response.json()["management"]["reschedule_code"]

        # A second, unrelated event on the SAME calendar.
        other_event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title="Other Event",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2030, 6, 2, 10, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 2, 11, 0),
            external_id="other-event-management-codes-001",
        )

        response = _post(anon_client, RESCHEDULE_URL_NAME, reschedule_code, _reschedule_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        assert response.json()["id"] == own_event_id

        other_event.refresh_from_db()
        assert other_event.start_time_tz_unaware.replace(tzinfo=None) == datetime.datetime(
            2030, 6, 2, 10, 0
        )


# ---------------------------------------------------------------------------
# Group booking (coded and codeless) -- issued codes are revokable
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar",
        external_id="primary-cal-mgmt-codes-rest-test",
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
        external_id="room-cal-mgmt-codes-rest-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


def _make_group_with_two_slots(
    organization: Organization,
    *,
    primary_calendar: Calendar,
    secondary_calendar: Calendar,
    accepts_public_scheduling: bool = False,
    name: str = "Test Group",
    duration: datetime.timedelta | None = None,
) -> CalendarGroup:
    grp = baker.make(
        CalendarGroup,
        organization=organization,
        name=name,
        accepts_public_scheduling=accepts_public_scheduling,
        duration=duration,
    )
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
def group(organization, primary_calendar, secondary_calendar):
    return _make_group_with_two_slots(
        organization, primary_calendar=primary_calendar, secondary_calendar=secondary_calendar
    )


@pytest.fixture
def public_group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup that accepts public (codeless) scheduling, pinned to 1 hour."""
    return _make_group_with_two_slots(
        organization,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        accepts_public_scheduling=True,
        name="Public Group",
        duration=datetime.timedelta(hours=1),
    )


@pytest.fixture
def group_availability_windows(organization, primary_calendar, secondary_calendar):
    windows = []
    for cal in (primary_calendar, secondary_calendar):
        windows.append(
            AvailableTime.objects.create(
                organization=organization,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 18, 0),
                timezone="UTC",
            )
        )
    return windows


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


@pytest.fixture
def group_booking_code(permission_service, organization, group):
    """A valid single-use CREATE code scoped to `group`."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.mark.django_db
class TestManagementCodesGroupBookingRevokable:
    def test_coded_group_booking_issued_codes_are_revokable(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        group_booking_code,
        group_availability_windows,  # noqa: ARG002
    ):
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        response = _post_group(
            anon_client, group.public_booking_slug, code, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        management = response.json()["management"]

        reschedule_token = permission_service.resolve_code(management["reschedule_code"])
        assert reschedule_token.kind == CalendarManagementTokenKind.BOOKING_CODE
        assert reschedule_token.calendar_group_fk_id == group.id
        assert (
            permission_service.revoke_token(
                organization_id=organization.id, token_id=reschedule_token.id
            )
            is True
        )

        cancel_token = permission_service.resolve_code(management["cancel_code"])
        assert cancel_token.kind == CalendarManagementTokenKind.BOOKING_CODE
        assert (
            permission_service.revoke_token(
                organization_id=organization.id, token_id=cancel_token.id
            )
            is True
        )

    def test_codeless_group_booking_issued_codes_are_revokable(
        self,
        anon_client,
        permission_service,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
        group_availability_windows,  # noqa: ARG002
    ):
        """The codeless branch presents no credential at all, so this is the
        ONLY way the patient can ever manage the appointment they just booked
        -- Phase 7's explicit `kind` discriminator exists specifically so a
        codeless mint (no user, no system user) is still revokable."""
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post_group(
            anon_client, public_group.public_booking_slug, None, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        management = response.json()["management"]

        reschedule_token = permission_service.resolve_code(management["reschedule_code"])
        assert reschedule_token.kind == CalendarManagementTokenKind.BOOKING_CODE
        assert reschedule_token.calendar_group_fk_id == public_group.id
        assert reschedule_token.minted_by_system_user_id is None
        assert reschedule_token.minted_by_membership_user_id is None
        assert (
            permission_service.revoke_token(
                organization_id=organization.id, token_id=reschedule_token.id
            )
            is True
        )


# ---------------------------------------------------------------------------
# Group reschedule: re-issued code still honors the group's duration pin
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestManagementCodesGroupReissuedReschedulePinSurvives:
    def test_reissued_reschedule_code_refuses_a_different_span(
        self,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        group_availability_windows,  # noqa: ARG002
    ):
        """A 30-minute group appointment cannot be rescheduled into a 60-minute
        one, even though the RE-ISSUED code itself pins nothing -- the
        constraint lives on `CalendarGroup.duration` and is enforced fresh
        every time the code is presented."""
        group.duration = datetime.timedelta(minutes=30)
        group.save()
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        booking_response = _post_group(
            anon_client,
            group.public_booking_slug,
            code,
            _group_booking_payload(
                selections,
                end_time=(BOOKING_START + datetime.timedelta(minutes=30)).isoformat(),
            ),
        )
        assert booking_response.status_code == status.HTTP_201_CREATED, booking_response.content
        reschedule_code = booking_response.json()["management"]["reschedule_code"]

        # First reschedule (30 -> 30 minutes) succeeds and re-issues a fresh pair.
        first_reschedule = _post(
            anon_client,
            GROUP_RESCHEDULE_URL_NAME,
            reschedule_code,
            _reschedule_payload(end_time=(NEW_START + datetime.timedelta(minutes=30)).isoformat()),
        )
        assert first_reschedule.status_code == status.HTTP_201_CREATED, first_reschedule.content
        reissued_reschedule_code = first_reschedule.json()["management"]["reschedule_code"]
        assert reissued_reschedule_code != reschedule_code

        # The RE-ISSUED code refuses a move to a 60-minute span -- the pin
        # survived re-issue without living on the code itself.
        second_reschedule = _post(
            anon_client,
            GROUP_RESCHEDULE_URL_NAME,
            reissued_reschedule_code,
            _reschedule_payload(
                start_time=NEWER_START.isoformat(),
                end_time=(NEWER_START + datetime.timedelta(minutes=60)).isoformat(),
            ),
        )
        assert second_reschedule.status_code == status.HTTP_403_FORBIDDEN
        body = second_reschedule.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "30 minute" in body["detail"]

        # The refused attempt must not have consumed the re-issued code.
        surviving_token = permission_service.resolve_code(reissued_reschedule_code)
        assert surviving_token.used_at is None
