"""Integration tests for the six code-gated read endpoints under ``public/booking/``.

Ports the scenarios in ``public_api/tests/test_code_gated_reads.py`` (the GraphQL
``*WithCode`` query fields) to REST, plus this phase's two headline security
properties:

- **Non-disclosure**: every one of the six endpoints returns the exact same
  ``403 {"detail": "Invalid or expired code."}`` for every kind of code
  failure -- invalid, expired, already-used, revoked, and wrong-scope.
- **Range validation precedes code resolution**: a bad range is a ``400``
  reachable with an *invalid* code, proving response status cannot be used
  to time-probe a code's state.

All requests are unauthenticated (no session/JWT). The booking code -- carried
in the ``X-Booking-Code`` header -- provides the org scope and read scope.
None of these endpoints ever call ``consume_code`` -- reads are repeatable.
"""

import datetime

from django.urls import reverse

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


AVAILABLE_TIMES_URL = "calendar_booking_api:booking-available-times-list"
AVAILABILITY_WINDOWS_URL = "calendar_booking_api:booking-availability-windows-list"
UNAVAILABLE_WINDOWS_URL = "calendar_booking_api:booking-unavailable-windows-list"
CALENDAR_BOOKABLE_SLOTS_URL = "calendar_booking_api:booking-calendar-bookable-slots-list"
CALENDAR_GROUP_BOOKABLE_SLOTS_URL = (
    "calendar_booking_api:booking-calendar-group-bookable-slots-list"
)
CALENDAR_GROUP_AVAILABILITY_URL = "calendar_booking_api:booking-calendar-group-availability-list"
CALENDAR_EVENTS_URL = "calendar_booking_api:booking-calendar-events-list"

OPAQUE_BODY = {"detail": "Invalid or expired code."}
INVALID_RANGE_BODY = {"detail": "Invalid time range."}
RANGE_TOO_LARGE_BODY = {"detail": "Requested time range is too large."}


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _get(client: APIClient, url_name: str, code: str | None, params: dict | None = None):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.get(reverse(url_name), params or {}, headers=headers)


def _post(client: APIClient, url_name: str, code: str | None, body: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(reverse(url_name), body, format="json", headers=headers)


# ---------------------------------------------------------------------------
# Code-minting helpers
# ---------------------------------------------------------------------------


def _mint_code(permission_service: CalendarPermissionService, organization: Organization, **kwargs):
    _token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        **kwargs,
    )
    return code


def _mint_expired_code(
    permission_service: CalendarPermissionService, organization: Organization, **kwargs
):
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    _token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        expires_at=past,
        **kwargs,
    )
    return code


def _mint_used_code(
    permission_service: CalendarPermissionService, organization: Organization, **kwargs
):
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        **kwargs,
    )
    CalendarManagementToken.original_manager.filter(id=token.id).update(
        used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
    )
    return code


def _mint_revoked_code(
    permission_service: CalendarPermissionService, organization: Organization, **kwargs
):
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        **kwargs,
    )
    permission_service.revoke_token(organization_id=organization.id, token_id=token.id)
    return code


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Code-Gated Reads Test Org")


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    return APIClient()


@pytest.fixture
def calendar(organization):
    """A managed calendar -- availability comes from seeded ``AvailableTime`` rows."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        external_id="reads-rest-calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def available_window(organization, calendar):
    """A broad declared-availability window: 2030-06-01 09:00-17:00 UTC."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def blocking_event(organization, calendar):
    """A busy span inside ``available_window``: 12:00-12:30."""
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Busy",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 12, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 12, 30),
        external_id="blocking-event-reads-rest",
    )


@pytest.fixture
def calendar_code(permission_service, organization, calendar):
    """A CREATE code scoped to ``calendar`` -- valid for the four calendar-scoped reads."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.fixture
def group_calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Group Slot Calendar",
        external_id="reads-rest-group-calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def calendar_group(organization, group_calendar):
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Physicians", order=0, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=group_calendar
    )
    return grp


@pytest.fixture
def group_code(permission_service, organization, calendar_group):
    """A CREATE code scoped to ``calendar_group`` -- valid for the two group-scoped reads."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=calendar_group.id,
    )
    return token, code


@pytest.fixture
def grouped_event(organization, calendar, calendar_group):
    """Simulates ``CalendarGroupService.create_grouped_event``'s persistence: the
    actual ``CalendarEvent`` always lands on a real, single primary calendar
    (``calendar``) even though it was booked through ``calendar_group`` -- so
    ``event.calendar`` is always populated for a grouped booking, same as
    ``event.calendar_group``.
    """
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        calendar_group=calendar_group,
        title="Grouped Booking",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 30),
        external_id="grouped-event-reads-rest",
    )


@pytest.fixture
def group_reschedule_code(permission_service, organization, calendar_group, grouped_event):
    """A RESCHEDULE code scoped to ``calendar_group`` + ``event_id`` -- no
    ``calendar_id`` -- mirroring
    ``create_calendar_group_reschedule_booking_code``'s mint shape
    (``calendar_integration/mutations.py``).
    """
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=calendar_group.id,
        event_id=grouped_event.id,
    )
    return token, code


@pytest.fixture
def calendar_reschedule_code(permission_service, organization, calendar, grouped_event):
    """A RESCHEDULE code scoped to ``calendar`` + ``event_id`` -- no
    ``calendar_group_id`` -- the symmetric single-calendar reschedule/cancel
    code shape.
    """
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=grouped_event.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Scenario 1: Available times (calendar-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAvailableTimesRead:
    def test_happy_path_and_repeatable_without_consuming(
        self, anon_client, calendar_code, available_window
    ):
        token, code = calendar_code
        params = {
            "start_datetime": "2030-06-01T00:00:00Z",
            "end_datetime": "2030-06-02T00:00:00Z",
        }

        first = _get(anon_client, AVAILABLE_TIMES_URL, code, params)
        assert first.status_code == status.HTTP_200_OK, first.content
        body = first.json()
        assert len(body) == 1
        assert body[0]["id"] == available_window.id

        second = _get(anon_client, AVAILABLE_TIMES_URL, code, params)
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == body

        token.refresh_from_db()
        assert token.used_at is None, "a read must never consume the code"

    def test_group_code_rejected_with_uniform_403(self, anon_client, group_code, available_window):
        _token, code = group_code
        response = _get(
            anon_client,
            AVAILABLE_TIMES_URL,
            code,
            {"start_datetime": "2030-06-01T00:00:00Z", "end_datetime": "2030-06-02T00:00:00Z"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# Scenario 2: Availability windows (calendar-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAvailabilityWindowsRead:
    def test_happy_path_and_repeatable_without_consuming(
        self, anon_client, calendar_code, available_window, blocking_event
    ):
        token, code = calendar_code
        params = {
            "start_datetime": "2030-06-01T00:00:00Z",
            "end_datetime": "2030-06-02T00:00:00Z",
        }

        first = _get(anon_client, AVAILABILITY_WINDOWS_URL, code, params)
        assert first.status_code == status.HTTP_200_OK, first.content
        body = first.json()
        # The 12:00-12:30 event splits the 09:00-17:00 window into two free spans.
        assert len(body) == 2

        second = _get(anon_client, AVAILABILITY_WINDOWS_URL, code, params)
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == body

        token.refresh_from_db()
        assert token.used_at is None

    def test_group_code_rejected_with_uniform_403(self, anon_client, group_code, available_window):
        _token, code = group_code
        response = _get(
            anon_client,
            AVAILABILITY_WINDOWS_URL,
            code,
            {"start_datetime": "2030-06-01T00:00:00Z", "end_datetime": "2030-06-02T00:00:00Z"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# Scenario 3: Unavailable windows (calendar-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUnavailableWindowsRead:
    def test_happy_path_and_repeatable_without_consuming(
        self, anon_client, calendar_code, blocking_event
    ):
        token, code = calendar_code
        params = {
            "start_datetime": "2030-06-01T00:00:00Z",
            "end_datetime": "2030-06-02T00:00:00Z",
        }

        first = _get(anon_client, UNAVAILABLE_WINDOWS_URL, code, params)
        assert first.status_code == status.HTTP_200_OK, first.content
        body = first.json()
        assert len(body) == 1
        assert body[0]["reason"] == "calendar_event"

        second = _get(anon_client, UNAVAILABLE_WINDOWS_URL, code, params)
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == body

        token.refresh_from_db()
        assert token.used_at is None

    def test_group_code_rejected_with_uniform_403(self, anon_client, group_code, blocking_event):
        _token, code = group_code
        response = _get(
            anon_client,
            UNAVAILABLE_WINDOWS_URL,
            code,
            {"start_datetime": "2030-06-01T00:00:00Z", "end_datetime": "2030-06-02T00:00:00Z"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# Scenario 4: Calendar bookable slots (calendar-scoped, first REST surface)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCalendarBookableSlotsRead:
    def test_happy_path_and_repeatable_without_consuming(
        self, anon_client, calendar_code, available_window, blocking_event
    ):
        token, code = calendar_code
        params = {
            "search_window_start": "2030-06-01T09:00:00Z",
            "search_window_end": "2030-06-01T13:00:00Z",
            "duration_seconds": 1800,
            "slot_step_seconds": 1800,
        }

        first = _get(anon_client, CALENDAR_BOOKABLE_SLOTS_URL, code, params)
        assert first.status_code == status.HTTP_200_OK, first.content
        body = first.json()
        starts = [p["start_time"] for p in body]
        assert "2030-06-01T12:00:00+00:00" not in starts

        second = _get(anon_client, CALENDAR_BOOKABLE_SLOTS_URL, code, params)
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == body

        token.refresh_from_db()
        assert token.used_at is None

    def test_group_code_rejected_with_uniform_403(self, anon_client, group_code, available_window):
        _token, code = group_code
        response = _get(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_URL,
            code,
            {
                "search_window_start": "2030-06-01T09:00:00Z",
                "search_window_end": "2030-06-01T13:00:00Z",
                "duration_seconds": 1800,
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# Scenario 5: Calendar group bookable slots (group-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCalendarGroupBookableSlotsRead:
    def test_happy_path_and_repeatable_without_consuming(self, anon_client, group_code):
        token, code = group_code
        params = {
            "search_window_start": "2030-06-01T09:00:00Z",
            "search_window_end": "2030-06-01T11:00:00Z",
            "duration_seconds": 1800,
            "slot_step_seconds": 1800,
        }

        first = _get(anon_client, CALENDAR_GROUP_BOOKABLE_SLOTS_URL, code, params)
        assert first.status_code == status.HTTP_200_OK, first.content
        body = first.json()
        assert len(body) > 0

        second = _get(anon_client, CALENDAR_GROUP_BOOKABLE_SLOTS_URL, code, params)
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == body

        token.refresh_from_db()
        assert token.used_at is None

    def test_calendar_code_rejected_with_uniform_403(self, anon_client, calendar_code):
        _token, code = calendar_code
        response = _get(
            anon_client,
            CALENDAR_GROUP_BOOKABLE_SLOTS_URL,
            code,
            {
                "search_window_start": "2030-06-01T09:00:00Z",
                "search_window_end": "2030-06-01T11:00:00Z",
                "duration_seconds": 1800,
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# Scenario 6: Calendar group availability (group-scoped, POST)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCalendarGroupAvailabilityRead:
    def test_happy_path_and_repeatable_without_consuming(self, anon_client, group_code):
        token, code = group_code
        body_payload = {
            "ranges": [
                {"start_time": "2030-06-01T09:00:00Z", "end_time": "2030-06-01T09:30:00Z"},
            ]
        }

        first = _post(anon_client, CALENDAR_GROUP_AVAILABILITY_URL, code, body_payload)
        assert first.status_code == status.HTTP_200_OK, first.content
        body = first.json()
        assert len(body) == 1
        assert body[0]["slots"][0]["required_count"] == 1

        second = _post(anon_client, CALENDAR_GROUP_AVAILABILITY_URL, code, body_payload)
        assert second.status_code == status.HTTP_200_OK
        assert second.json() == body

        token.refresh_from_db()
        assert token.used_at is None

    def test_calendar_code_rejected_with_uniform_403(self, anon_client, calendar_code):
        _token, code = calendar_code
        response = _post(
            anon_client,
            CALENDAR_GROUP_AVAILABILITY_URL,
            code,
            {
                "ranges": [
                    {"start_time": "2030-06-01T09:00:00Z", "end_time": "2030-06-01T09:30:00Z"}
                ]
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# A group reschedule/cancel code must never leak the specific calendar its
# event landed on, and symmetrically a single-calendar reschedule/cancel code
# must never leak group scope. Regression coverage for the fallback-to-
# ``token.event.calendar`` / ``token.event.calendar_group`` disclosure bug:
# ``CalendarGroupService.create_grouped_event`` always creates the underlying
# event on a real single primary calendar, so ``token.event.calendar`` is
# always populated for a grouped booking -- a naive fallback there would leak
# that specific calendar's availability to a patient holding only a group
# code (and the group abstraction exists precisely so they never learn that).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupScopedCodeCannotLeakSpecificCalendar:
    @pytest.mark.parametrize(
        "url_name,params",
        [
            (
                AVAILABLE_TIMES_URL,
                {"start_datetime": "2030-06-01T00:00:00Z", "end_datetime": "2030-06-02T00:00:00Z"},
            ),
            (
                AVAILABILITY_WINDOWS_URL,
                {"start_datetime": "2030-06-01T00:00:00Z", "end_datetime": "2030-06-02T00:00:00Z"},
            ),
            (
                UNAVAILABLE_WINDOWS_URL,
                {"start_datetime": "2030-06-01T00:00:00Z", "end_datetime": "2030-06-02T00:00:00Z"},
            ),
            (
                CALENDAR_BOOKABLE_SLOTS_URL,
                {
                    "search_window_start": "2030-06-01T09:00:00Z",
                    "search_window_end": "2030-06-01T13:00:00Z",
                    "duration_seconds": 1800,
                },
            ),
        ],
    )
    def test_group_reschedule_code_rejected_on_calendar_scoped_reads(
        self, anon_client, group_reschedule_code, available_window, blocking_event, url_name, params
    ):
        """A group-scoped RESCHEDULE code (``calendar_group_id`` + ``event_id``,
        no ``calendar_id``) must get the uniform 403 on every calendar-scoped
        read, even though ``token.event.calendar`` resolves to a real
        calendar with real availability data.
        """
        _token, code = group_reschedule_code
        response = _get(anon_client, url_name, code, params)
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
        assert response.json() == OPAQUE_BODY

    @pytest.mark.parametrize(
        "url_name,method,params",
        [
            (
                CALENDAR_GROUP_BOOKABLE_SLOTS_URL,
                "get",
                {
                    "search_window_start": "2030-06-01T09:00:00Z",
                    "search_window_end": "2030-06-01T11:00:00Z",
                    "duration_seconds": 1800,
                },
            ),
            (
                CALENDAR_GROUP_AVAILABILITY_URL,
                "post",
                {
                    "ranges": [
                        {"start_time": "2030-06-01T09:00:00Z", "end_time": "2030-06-01T09:30:00Z"}
                    ]
                },
            ),
        ],
    )
    def test_single_calendar_reschedule_code_rejected_on_group_scoped_reads(
        self, anon_client, calendar_reschedule_code, url_name, method, params
    ):
        """Symmetric case: a single-calendar RESCHEDULE code (``calendar_id`` +
        ``event_id``, no ``calendar_group_id``) must get the uniform 403 on
        every group-scoped read, even though ``token.event.calendar_group``
        resolves to a real group.
        """
        _token, code = calendar_reschedule_code
        response = (
            _get(anon_client, url_name, code, params)
            if method == "get"
            else _post(anon_client, url_name, code, params)
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
        assert response.json() == OPAQUE_BODY


# ---------------------------------------------------------------------------
# Non-disclosure matrix: byte-identical 403 across every failure kind, on
# every one of the six endpoints. This is the property the whole phase exists
# for -- see the module docstring.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNonDisclosureMatrix:
    def test_every_failure_kind_is_byte_identical_on_every_endpoint(
        self,
        anon_client,
        organization,
        permission_service,
        calendar,
        calendar_group,
        grouped_event,
    ):
        invalid_code = "dGhpc19pc19ub3RfYV9yZWFsX2NvZGU="  # garbage base64, no matching token

        # A group reschedule/cancel code: `calendar_group_id` + `event_id`, no
        # `calendar_id` -- `grouped_event.calendar` is a real single calendar
        # (mirrors `CalendarGroupService.create_grouped_event`'s persistence),
        # so this exercises the `token.event.calendar` fallback specifically,
        # not just a scopeless CREATE code. See
        # `TestGroupScopedCodeCannotLeakSpecificCalendar`.
        _group_reschedule_token, group_reschedule_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_group_id=calendar_group.id,
            event_id=grouped_event.id,
        )
        # Symmetric: a single-calendar reschedule/cancel code -- `calendar_id`
        # + `event_id`, no `calendar_group_id` -- exercising the
        # `token.event.calendar_group` fallback.
        (
            _calendar_reschedule_token,
            calendar_reschedule_code,
        ) = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=grouped_event.id,
        )

        calendar_failure_codes = {
            "invalid": invalid_code,
            "missing_header": None,
            "empty_header": "",
            "expired": _mint_expired_code(
                permission_service, organization, calendar_id=calendar.id
            ),
            "already_used": _mint_used_code(
                permission_service, organization, calendar_id=calendar.id
            ),
            "revoked": _mint_revoked_code(
                permission_service, organization, calendar_id=calendar.id
            ),
            # Wrong scope: a group-bound code presented to a calendar-scoped read.
            "wrong_scope": _mint_code(
                permission_service, organization, calendar_group_id=calendar_group.id
            ),
            # Wrong scope via the event fallback: a group reschedule/cancel code
            # whose bound event sits on a real calendar.
            "wrong_scope_via_event_fallback": group_reschedule_code,
        }
        group_failure_codes = {
            "invalid": invalid_code,
            "missing_header": None,
            "empty_header": "",
            "expired": _mint_expired_code(
                permission_service, organization, calendar_group_id=calendar_group.id
            ),
            "already_used": _mint_used_code(
                permission_service, organization, calendar_group_id=calendar_group.id
            ),
            "revoked": _mint_revoked_code(
                permission_service, organization, calendar_group_id=calendar_group.id
            ),
            # Wrong scope: a calendar-bound code presented to a group-scoped read.
            "wrong_scope": _mint_code(permission_service, organization, calendar_id=calendar.id),
            # Wrong scope via the event fallback: a single-calendar
            # reschedule/cancel code whose bound event also sits on a group.
            "wrong_scope_via_event_fallback": calendar_reschedule_code,
        }

        far_start = "2030-06-01T09:00:00Z"
        far_end = "2030-06-01T10:00:00Z"

        calendar_requests = [
            (AVAILABLE_TIMES_URL, "get", {"start_datetime": far_start, "end_datetime": far_end}),
            (
                AVAILABILITY_WINDOWS_URL,
                "get",
                {"start_datetime": far_start, "end_datetime": far_end},
            ),
            (
                UNAVAILABLE_WINDOWS_URL,
                "get",
                {"start_datetime": far_start, "end_datetime": far_end},
            ),
            (
                CALENDAR_BOOKABLE_SLOTS_URL,
                "get",
                {
                    "search_window_start": far_start,
                    "search_window_end": far_end,
                    "duration_seconds": 1800,
                },
            ),
        ]
        group_requests = [
            (
                CALENDAR_GROUP_BOOKABLE_SLOTS_URL,
                "get",
                {
                    "search_window_start": far_start,
                    "search_window_end": far_end,
                    "duration_seconds": 1800,
                },
            ),
            (
                CALENDAR_GROUP_AVAILABILITY_URL,
                "post",
                {"ranges": [{"start_time": far_start, "end_time": far_end}]},
            ),
        ]

        response_bodies: set[bytes] = set()
        assertion_count = 0

        for url_name, method, params in calendar_requests:
            for kind, code in calendar_failure_codes.items():
                response = (
                    _get(anon_client, url_name, code, params)
                    if method == "get"
                    else _post(anon_client, url_name, code, params)
                )
                assert response.status_code == status.HTTP_403_FORBIDDEN, (
                    url_name,
                    kind,
                    response.content,
                )
                assert response.json() == OPAQUE_BODY, (url_name, kind, response.content)
                assertion_count += 2
                response_bodies.add(bytes(response.content))

        for url_name, method, params in group_requests:
            for kind, code in group_failure_codes.items():
                response = (
                    _get(anon_client, url_name, code, params)
                    if method == "get"
                    else _post(anon_client, url_name, code, params)
                )
                assert response.status_code == status.HTTP_403_FORBIDDEN, (
                    url_name,
                    kind,
                    response.content,
                )
                assert response.json() == OPAQUE_BODY, (url_name, kind, response.content)
                assertion_count += 2
                response_bodies.add(bytes(response.content))

        # Byte-identical across ALL SIX endpoints and ALL EIGHT failure kinds.
        assert len(response_bodies) == 1, response_bodies
        assert assertion_count == 6 * 8 * 2  # 6 endpoints x 8 failure kinds x 2 assertions


# ---------------------------------------------------------------------------
# Range validation precedes code resolution: a bad range is a 400 reachable
# with an INVALID code, proving status cannot be used to time-probe.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRangeValidationPrecedesCodeResolution:
    INVALID_CODE = "dGhpc19pc19ub3RfYV9yZWFsX2NvZGU="

    @pytest.mark.parametrize(
        "url_name,method,build_params",
        [
            (
                AVAILABLE_TIMES_URL,
                "get",
                lambda start, end: {"start_datetime": start, "end_datetime": end},
            ),
            (
                AVAILABILITY_WINDOWS_URL,
                "get",
                lambda start, end: {"start_datetime": start, "end_datetime": end},
            ),
            (
                UNAVAILABLE_WINDOWS_URL,
                "get",
                lambda start, end: {"start_datetime": start, "end_datetime": end},
            ),
            (
                CALENDAR_BOOKABLE_SLOTS_URL,
                "get",
                lambda start, end: {
                    "search_window_start": start,
                    "search_window_end": end,
                    "duration_seconds": 1800,
                },
            ),
            (
                CALENDAR_GROUP_BOOKABLE_SLOTS_URL,
                "get",
                lambda start, end: {
                    "search_window_start": start,
                    "search_window_end": end,
                    "duration_seconds": 1800,
                },
            ),
        ],
    )
    def test_backwards_range_is_400_even_with_invalid_code(
        self, anon_client, url_name, method, build_params
    ):
        # end BEFORE start -- backwards range.
        params = build_params("2030-06-01T10:00:00Z", "2030-06-01T09:00:00Z")
        response = _get(anon_client, url_name, self.INVALID_CODE, params)
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json() == INVALID_RANGE_BODY

    @pytest.mark.parametrize(
        "url_name,method,build_params",
        [
            (
                AVAILABLE_TIMES_URL,
                "get",
                lambda start, end: {"start_datetime": start, "end_datetime": end},
            ),
            (
                CALENDAR_BOOKABLE_SLOTS_URL,
                "get",
                lambda start, end: {
                    "search_window_start": start,
                    "search_window_end": end,
                    "duration_seconds": 1800,
                },
            ),
        ],
    )
    def test_too_large_range_is_400_even_with_invalid_code(
        self, anon_client, url_name, method, build_params
    ):
        start = "2030-06-01T00:00:00Z"
        end = "2032-01-01T00:00:00Z"  # > 366 days
        params = build_params(start, end)
        response = _get(anon_client, url_name, self.INVALID_CODE, params)
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json() == RANGE_TOO_LARGE_BODY

    def test_group_availability_backwards_range_is_400_even_with_invalid_code(self, anon_client):
        response = _post(
            anon_client,
            CALENDAR_GROUP_AVAILABILITY_URL,
            self.INVALID_CODE,
            {
                "ranges": [
                    {"start_time": "2030-06-01T10:00:00Z", "end_time": "2030-06-01T09:00:00Z"}
                ]
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert response.json() == INVALID_RANGE_BODY


# ---------------------------------------------------------------------------
# Timezone-naive datetimes must be rejected, not silently interpreted in the
# server's default timezone. Reachable with an invalid code (400 precedes
# code resolution, same as the other range-validation cases).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNaiveDatetimeRejected:
    INVALID_CODE = "dGhpc19pc19ub3RfYV9yZWFsX2NvZGU="

    @pytest.mark.parametrize(
        "url_name,build_params",
        [
            (
                AVAILABLE_TIMES_URL,
                lambda naive: {"start_datetime": naive, "end_datetime": "2030-06-02T00:00:00Z"},
            ),
            (
                AVAILABILITY_WINDOWS_URL,
                lambda naive: {"start_datetime": naive, "end_datetime": "2030-06-02T00:00:00Z"},
            ),
            (
                UNAVAILABLE_WINDOWS_URL,
                lambda naive: {"start_datetime": naive, "end_datetime": "2030-06-02T00:00:00Z"},
            ),
            (
                CALENDAR_BOOKABLE_SLOTS_URL,
                lambda naive: {
                    "search_window_start": naive,
                    "search_window_end": "2030-06-02T00:00:00Z",
                    "duration_seconds": 1800,
                },
            ),
            (
                CALENDAR_GROUP_BOOKABLE_SLOTS_URL,
                lambda naive: {
                    "search_window_start": naive,
                    "search_window_end": "2030-06-02T00:00:00Z",
                    "duration_seconds": 1800,
                },
            ),
        ],
    )
    def test_naive_datetime_rejected_with_400(self, anon_client, url_name, build_params):
        # No offset -- would otherwise be interpreted in the server's default
        # timezone instead of being rejected.
        params = build_params("2030-06-01T00:00:00")
        response = _get(anon_client, url_name, self.INVALID_CODE, params)
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
        assert "must include a UTC offset" in str(response.json())


# ---------------------------------------------------------------------------
# Pinned duration: silent override, byte-identical across duration_seconds
# variants, and every proposal returned is actually bookable.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPinnedDurationSilentOverride:
    def test_byte_identical_across_duration_seconds_variants_and_every_slot_bookable(
        self, anon_client, organization, permission_service, calendar, available_window
    ):
        # A CREATE code pinned to a 30-minute duration.
        _pinned_token, pinned_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            duration=datetime.timedelta(minutes=30),
        )

        base_params = {
            "search_window_start": "2030-06-01T09:00:00Z",
            "search_window_end": "2030-06-01T11:00:00Z",
            # Matches the pin so proposals are back-to-back, not overlapping --
            # needed to book every one of them without a conflict below.
            "slot_step_seconds": 1800,
        }

        response_wrong = _get(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_URL,
            pinned_code,
            {**base_params, "duration_seconds": 3600},
        )
        response_right = _get(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_URL,
            pinned_code,
            {**base_params, "duration_seconds": 1800},
        )
        # Malformed (non-numeric, but PRESENT) -- still silently overridden by
        # the pin. Presence itself is still required regardless of pin state
        # (see ``test_pinned_code_also_requires_duration_seconds_presence``);
        # only the parsed VALUE is unconditionally ignored here.
        response_malformed = _get(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_URL,
            pinned_code,
            {**base_params, "duration_seconds": "not-a-number"},
        )

        assert response_wrong.status_code == status.HTTP_200_OK, response_wrong.content
        assert response_right.status_code == status.HTTP_200_OK, response_right.content
        assert response_malformed.status_code == status.HTTP_200_OK, response_malformed.content

        assert response_wrong.content == response_right.content == response_malformed.content

        proposals = response_right.json()
        assert len(proposals) > 0

        # Every proposal returned by the pinned read must actually be bookable
        # through the Phase 1 endpoint -- a fresh code per proposal, since a
        # booking code is single-use.
        for proposal in proposals:
            _token, booking_code = permission_service.create_booking_token(
                organization_id=organization.id,
                permissions=[EventManagementPermissions.CREATE],
                calendar_id=calendar.id,
                duration=datetime.timedelta(minutes=30),
            )
            payload = {
                "title": "Pinned Slot Booking",
                "description": "",
                "start_time": proposal["start_time"],
                "end_time": proposal["end_time"],
                "timezone": "UTC",
                "external_attendee": {"email": "patient@example.com", "name": "Pat Patient"},
            }
            booking_response = _post(anon_client, CALENDAR_EVENTS_URL, booking_code, payload)
            assert booking_response.status_code == status.HTTP_201_CREATED, (
                proposal,
                booking_response.content,
            )
            # The INTERNAL provider has no write adapter, so every booked event
            # keeps a blank external_id -- and that column is globally unique.
            # Remove each event right after confirming it booked so the next
            # proposal's booking (also blank external_id) does not collide; this
            # is a test-isolation workaround for that pre-existing constraint,
            # not a property of the endpoint under test.
            CalendarEvent.objects.filter_by_organization(organization.id).filter(
                id=booking_response.json()["id"]
            ).delete()

    def test_unpinned_code_still_requires_duration_seconds(
        self, anon_client, organization, permission_service, calendar, available_window
    ):
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        response = _get(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_URL,
            code,
            {
                "search_window_start": "2030-06-01T09:00:00Z",
                "search_window_end": "2030-06-01T11:00:00Z",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content

    def test_pinned_code_also_requires_duration_seconds_presence(
        self, anon_client, organization, permission_service, calendar, available_window
    ):
        """A missing ``duration_seconds`` must be a 400 for a PINNED code too --
        the response status must never be the oracle that discloses pin state
        (see FIX 2 / the "Duration pinning -- reads" Guiding Decision update).
        """
        _token, pinned_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            duration=datetime.timedelta(minutes=30),
        )
        response = _get(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_URL,
            pinned_code,
            {
                "search_window_start": "2030-06-01T09:00:00Z",
                "search_window_end": "2030-06-01T11:00:00Z",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.content
