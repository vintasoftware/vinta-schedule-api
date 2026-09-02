"""Integration tests for ``POST /public/booking/calendar-events/``.

Ports the seven scenarios in ``public_api/tests/test_book_with_code.py`` (the
GraphQL ``createCalendarEventWithCode`` equivalent) to the REST surface, plus
the pinned-duration and concurrency cases the plan's Phase 1 body calls for.

All requests are unauthenticated (no session/JWT). The booking code -- carried
in the ``X-Booking-Code`` header -- provides the org scope, calendar scope,
and CREATE permission.
"""

import datetime
import itertools
import threading
from unittest.mock import Mock, patch

from django.db import connection
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
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterInputData,
    CalendarEventAdapterOutputData,
)
from organizations.models import Organization


BOOKING_URL_NAME = "calendar_booking_api:booking-calendar-events-list"

BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Book-With-Code Test Org")


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar (accepts_public_scheduling=False) with managed
    availability windows -- tests seed AvailableTime rows to make a slot
    bookable; the code-as-token provides the CREATE permission so
    can_perform_scheduling returns True via the token path."""
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
def calendar_group(organization):
    return baker.make(CalendarGroup, organization=organization, name="Test Group")


@pytest.fixture
def available_window(organization, calendar):
    """A future availability window that covers the test booking slot."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        # Start at 09:00 UTC, end at 17:00 UTC -- covers the test slot (10:00-11:00).
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def booking_code(permission_service, organization, calendar):
    """A valid single-use CREATE code scoped to `calendar`."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.fixture
def reschedule_code(permission_service, organization, calendar):
    """A RESCHEDULE-only code (no CREATE) -- wrong permission for booking."""
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Existing Event",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="existing-event-external-id",
    )
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=event.id,
    )
    return token, code


@pytest.fixture
def group_booking_code(permission_service, organization, calendar_group):
    """A CREATE code scoped to a calendar GROUP (wrong scope for this endpoint)."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=calendar_group.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _post(client: APIClient, code: str | None, payload: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(reverse(BOOKING_URL_NAME), payload, format="json", headers=headers)


# ---------------------------------------------------------------------------
# Scenario 1: Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeHappyPath:
    def test_happy_path_creates_event_and_consumes_code(
        self,
        anon_client,
        booking_code,
        organization,
        calendar,
        available_window,  # noqa: ARG002 -- seeds DB rows consumed by create_event
    ):
        token, code = booking_code

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["title"] == "My Appointment"

        # Code must be consumed.
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        # The event must exist in the DB, on the right calendar/org.
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.calendar_fk_id == calendar.id
        assert event.organization_id == organization.id

    def test_event_has_external_attendee(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002
    ):
        _token, code = booking_code

        response = _post(anon_client, code, _booking_payload())

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
class TestCreateCalendarEventWithCodeReplay:
    def test_replay_returns_already_used(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002
    ):
        _token, code = booking_code
        payload = _booking_payload()

        first = _post(anon_client, code, payload)
        assert first.status_code == status.HTTP_201_CREATED, first.content

        second = _post(anon_client, code, payload)
        assert second.status_code == status.HTTP_409_CONFLICT
        assert second.json()["error_code"] == "ALREADY_USED"

        event_count = CalendarEvent.objects.filter_by_organization(organization.id).count()
        assert event_count == 1


# ---------------------------------------------------------------------------
# Scenario 3: Failed write does not consume the code
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeFailedWriteDoesNotConsume:
    def test_slot_unavailable_does_not_consume_code(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002 -- window is 09:00-17:00 UTC
        calendar,
    ):
        """Booking a slot OUTSIDE the availability window returns SLOT_UNAVAILABLE;
        code stays active."""
        token, code = booking_code

        out_of_window_payload = _booking_payload(
            start_time=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            end_time=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )

        response = _post(anon_client, code, out_of_window_payload)

        assert response.status_code == status.HTTP_409_CONFLICT
        body = response.json()
        assert body["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    def test_after_failed_write_code_can_still_be_used(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002 -- window is 09:00-17:00 UTC
        calendar,
    ):
        token, code = booking_code

        out_of_window_payload = _booking_payload(
            start_time=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            end_time=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )
        fail_response = _post(anon_client, code, out_of_window_payload)
        assert fail_response.status_code == status.HTTP_409_CONFLICT
        assert fail_response.json()["error_code"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None, "Code must remain active after failed write"

        success_response = _post(anon_client, code, _booking_payload())
        assert success_response.status_code == status.HTTP_201_CREATED, success_response.content

        token.refresh_from_db()
        assert token.used_at is not None
        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1


# ---------------------------------------------------------------------------
# Scenario 4: Wrong permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeWrongPermission:
    def test_reschedule_only_code_returns_not_permitted(
        self,
        anon_client,
        reschedule_code,
        organization,
    ):
        _token, code = reschedule_code

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        # Only the pre-existing event from the fixture, no new booking.
        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1


# ---------------------------------------------------------------------------
# Scenario 5: Wrong scope (group code)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeWrongScope:
    def test_group_code_returns_not_permitted(
        self,
        anon_client,
        group_booking_code,
        organization,
    ):
        _token, code = group_booking_code

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 6: Lifecycle rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeLifecycleRejections:
    def test_expired_code_returns_expired(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            expires_at=past,
        )

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_410_GONE
        assert response.json()["error_code"] == "EXPIRED"

    def test_revoked_code_returns_revoked(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "REVOKED"

    def test_invalid_code_returns_invalid_code(self, anon_client):
        response = _post(
            anon_client, "aW52YWxpZGJvb2tpbmdjb2Rl", _booking_payload()
        )  # "invalidbookingcode" base64

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_missing_code_returns_invalid_code(self, anon_client):
        response = _post(anon_client, None, _booking_payload())

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error_code"] == "INVALID_CODE"

    def test_used_code_returns_already_used(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        """A code marked used before this call -> ALREADY_USED."""
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        CalendarManagementToken.original_manager.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error_code"] == "ALREADY_USED"


# ---------------------------------------------------------------------------
# Scenario 7: Cross-org
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeCrossOrg:
    def test_event_created_in_code_org(
        self,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002
    ):
        token, code = booking_code

        response = _post(anon_client, code, _booking_payload())

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.organization_id == token.organization_id


# ---------------------------------------------------------------------------
# Pinned duration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodePinnedDuration:
    def test_pinned_duration_books_at_exact_span(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
        available_window,  # noqa: ARG002
    ):
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            duration=datetime.timedelta(minutes=30),
        )
        payload = _booking_payload(
            end_time=(BOOKING_START + datetime.timedelta(minutes=30)).isoformat()
        )

        response = _post(anon_client, code, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_pinned_duration_refuses_a_different_span_without_consuming(
        self,
        anon_client,
        permission_service,
        organization,
        calendar,
        available_window,  # noqa: ARG002
    ):
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            duration=datetime.timedelta(minutes=30),
        )
        # 45-minute span -- does not match the 30-minute pin.
        payload = _booking_payload(
            end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )

        response = _post(anon_client, code, payload)

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
        booking_code,
        available_window,  # noqa: ARG002
    ):
        _token, code = booking_code
        payload = _booking_payload(
            end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )

        response = _post(anon_client, code, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content


# ---------------------------------------------------------------------------
# Concurrency: two simultaneous requests, one code, exactly one event
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestCreateCalendarEventWithCodeConcurrency:
    def test_two_concurrent_requests_create_exactly_one_event(self):
        """Two concurrent bookings on one code: exactly one event, code consumed once.

        The status-code / row-count assertions below (409+201, one ``CalendarEvent``)
        do NOT, by themselves, prove the view's create-then-consume ordering matters:
        both statements run inside the same outer ``transaction.atomic()`` in
        ``booking_views.py``, so any exception raised by either one -- including
        ``consume_code``'s ``TokenAlreadyUsedError`` on a lost race -- unwinds the
        whole transaction. Swapping the two statements (consume-then-create)
        produces byte-identical status codes and row counts.

        What create-first actually changes, and what this test asserts on to make
        an inversion fail, is provider-side work: with create-first BOTH racers
        reach ``CalendarService.create_event`` and therefore call the write
        adapter, so ``fake_adapter.create_event`` is called twice. With
        consume-first, the loser blocks on ``consume_code``'s row lock, finds the
        code already used, and never reaches the adapter at all -- one call, not
        two.

        Worth knowing (not fixed here, pre-existing in the GraphQL original, out
        of scope): on a real provider-backed calendar, create-first means the
        losing racer may already have created an event at the external provider
        before the DB transaction rolls back. That provider-side event is an
        orphan the rollback cannot undo -- it only reverts the local DB rows.
        """
        organization = baker.make(Organization, name="Concurrency Test Org")
        calendar = baker.make(
            Calendar,
            organization=organization,
            name="Concurrency Calendar",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=False,
            accepts_public_scheduling=False,
        )
        permission_service = CalendarPermissionService()
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )

        # An unadapted INTERNAL-calendar create stamps a fixed `external_id=""`
        # (see `_seed_metered_occurrences`'s docstring in
        # test_event_creation_surfaces.py), which is globally unique. Two
        # genuinely concurrent creates would both attempt to insert that same
        # value and collide on the DB constraint before either request's
        # domain-level "already used" check ever runs -- a real race, but not
        # the one this test is about. A fake write adapter stamping a distinct
        # external_id per call (like the bundle fan-out tests in
        # test_event_creation_surfaces.py) isolates the row-lock/consume race
        # under test from that unrelated collision.
        external_id_counter = itertools.count()
        external_id_lock = threading.Lock()

        def _fake_create_event(
            input_data: CalendarEventAdapterInputData,
        ) -> CalendarEventAdapterOutputData:
            with external_id_lock:
                n = next(external_id_counter)
            return CalendarEventAdapterOutputData(
                calendar_external_id=calendar.external_id,
                external_id=f"concurrency-{organization.pk}-{n}",
                title=input_data.title,
                description=input_data.description,
                start_time=input_data.start_time,
                end_time=input_data.end_time,
                timezone=input_data.timezone,
                attendees=[],
                resources=[],
                original_payload={},
            )

        fake_adapter = Mock()
        fake_adapter.create_event.side_effect = _fake_create_event

        start_barrier = threading.Barrier(2, timeout=10)
        results: list[int] = []
        results_lock = threading.Lock()

        def worker() -> None:
            start_barrier.wait(timeout=10)
            try:
                client = APIClient()
                response = _post(client, code, _booking_payload())
                with results_lock:
                    results.append(response.status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        with patch.object(
            CalendarService, "_get_write_adapter_for_calendar", return_value=fake_adapter
        ):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        assert sorted(results) == [status.HTTP_201_CREATED, status.HTTP_409_CONFLICT], results
        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1
        # The assertion that actually distinguishes create-first from consume-first
        # (see the docstring above) -- both racers reach the write adapter under the
        # shipped create-then-consume ordering.
        assert fake_adapter.create_event.call_count == 2
