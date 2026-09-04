"""Integration tests for the codeless, slug-addressed public-group discovery reads.

Phase 9 adds two read endpoints under the same
``calendar-groups/<public_slug>/`` prefix the Phase 3b codeless write already
uses, carrying no ``X-Booking-Code`` at all:

- ``GET  /public/booking/calendar-groups/<public_slug>/bookable-slots/``
- ``POST /public/booking/calendar-groups/<public_slug>/availability/``

They are gated on the addressed group's own ``accepts_public_scheduling``,
mirroring the codeless write's 404/403 contract exactly (unknown slug ->
404, non-public group -> 403) -- see
``test_booking_rest_codeless_group.py`` for that write-side contract and
``test_booking_rest_reads.py`` for the token-scoped Phase 5 reads these are
NOT the same addressing scheme as.

``availability-windows`` / ``unavailable-windows`` are deliberately NOT
shipped on this codeless surface -- see the "Codeless, slug-addressed
public-group reads" section at the bottom of
``calendar_integration/booking_read_views.py`` for why (no group-level,
non-attributing primitive exists for a continuous availability/busy
WINDOW). ``TestWindowReadsAreNotShipped`` below locks that decision in as a
404, so a future change that silently adds one of these routes without
revisiting that decision gets caught here.
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    EventManagementPermissions,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


SEARCH_WINDOW_START = "2030-06-01T09:00:00Z"
SEARCH_WINDOW_END = "2030-06-01T11:00:00Z"
GROUP_DURATION = datetime.timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _bookable_slots_url(public_slug: str) -> str:
    return f"/public/booking/calendar-groups/{public_slug}/bookable-slots/"


def _availability_url(public_slug: str) -> str:
    return f"/public/booking/calendar-groups/{public_slug}/availability/"


def _events_url(public_slug: str) -> str:
    return f"/public/booking/calendar-groups/{public_slug}/events/"


def _get_slots(client: APIClient, public_slug: str, params: dict | None = None):
    return client.get(_bookable_slots_url(public_slug), params or {})


def _post_availability(client: APIClient, public_slug: str, body: dict):
    return client.post(_availability_url(public_slug), body, format="json")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Codeless Group-Reads Test Org")


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    return APIClient()


def _make_slot_calendar(organization: Organization, external_id: str) -> Calendar:
    # Unmanaged (manage_available_windows=False): available by default unless
    # blocked, so the fixture needs no separately-seeded AvailableTime rows --
    # mirrors ``group_calendar`` in ``test_booking_rest_reads.py``.
    return baker.make(
        Calendar,
        organization=organization,
        external_id=external_id,
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


def _make_group(
    organization: Organization,
    *,
    accepts_public_scheduling: bool,
    slot_calendar: Calendar,
    name: str,
    duration: datetime.timedelta | None,
) -> CalendarGroup:
    grp = baker.make(
        CalendarGroup,
        organization=organization,
        name=name,
        accepts_public_scheduling=accepts_public_scheduling,
        duration=duration,
    )
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Physicians", order=0, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=slot_calendar
    )
    return grp


@pytest.fixture
def slot_calendar(organization):
    return _make_slot_calendar(organization, "public-group-reads-slot-cal")


@pytest.fixture
def public_group(organization, slot_calendar):
    """A CalendarGroup open to codeless discovery and booking."""
    return _make_group(
        organization,
        accepts_public_scheduling=True,
        slot_calendar=slot_calendar,
        name="Public Discovery Group",
        duration=GROUP_DURATION,
    )


@pytest.fixture
def private_group(organization, slot_calendar):
    """A CalendarGroup that does NOT accept public scheduling."""
    return _make_group(
        organization,
        accepts_public_scheduling=False,
        slot_calendar=slot_calendar,
        name="Private Discovery Group",
        duration=GROUP_DURATION,
    )


@pytest.fixture
def public_group_with_no_duration(organization, slot_calendar):
    """A grandfathered public group with no duration configured.

    ``CalendarGroupService.create_group`` / ``update_group`` refuse to set
    ``accepts_public_scheduling=True`` without a duration going forward, but
    there is no DB constraint stopping a pre-existing row from carrying this
    combination (see the "Public implies length-constrained" Guiding
    Decision) -- ``baker.make`` bypasses the service layer entirely, exactly
    like a grandfathered row would.
    """
    return _make_group(
        organization,
        accepts_public_scheduling=True,
        slot_calendar=slot_calendar,
        name="Grandfathered Public Group",
        duration=None,
    )


@pytest.fixture
def group_code(permission_service, organization, public_group):
    """A Phase 5 CREATE code scoped to ``public_group`` -- lets a test compare
    the codeless read against its code-gated counterpart for the same group."""
    _token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=public_group.id,
    )
    return code


def _slot_selections(group: CalendarGroup, slot_calendar: Calendar) -> list[dict]:
    slot = group.slots.get(name="Physicians")
    return [{"slot_id": slot.id, "calendar_ids": [slot_calendar.id]}]


def _group_booking_payload(start_time: str, end_time: str, slot_selections: list[dict]) -> dict:
    return {
        "title": "Codeless Discovery Booking",
        "description": "",
        "start_time": start_time,
        "end_time": end_time,
        "timezone": "UTC",
        "slot_selections": slot_selections,
        "external_attendee": {"email": "patient@example.com", "name": "Pat Patient"},
    }


# ---------------------------------------------------------------------------
# Scenario 1: Bookable slots (codeless, slug-addressed)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicBookableSlotsRead:
    def test_public_group_slots_readable_with_no_credential(self, anon_client, public_group):
        response = _get_slots(
            anon_client,
            public_group.public_booking_slug,
            {
                "search_window_start": SEARCH_WINDOW_START,
                "search_window_end": SEARCH_WINDOW_END,
                "slot_step_seconds": 1800,
            },
        )

        assert response.status_code == status.HTTP_200_OK, response.content
        body = response.json()
        assert len(body) > 0
        # Only start_time/end_time -- no calendar id, no slot id, nothing
        # that attributes a proposal to a specific member calendar.
        assert set(body[0].keys()) == {"start_time", "end_time"}

    def test_private_group_returns_403(self, anon_client, private_group):
        response = _get_slots(
            anon_client,
            private_group.public_booking_slug,
            {"search_window_start": SEARCH_WINDOW_START, "search_window_end": SEARCH_WINDOW_END},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "does not accept public scheduling" in response.json()["detail"].lower()

    def test_unknown_slug_returns_404(self, anon_client):
        response = _get_slots(
            anon_client,
            "well-formed-but-nonexistent-slug",
            {"search_window_start": SEARCH_WINDOW_START, "search_window_end": SEARCH_WINDOW_END},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_null_duration_returns_403_rather_than_guessing_a_length(
        self, anon_client, public_group_with_no_duration
    ):
        response = _get_slots(
            anon_client,
            public_group_with_no_duration.public_booking_slug,
            {"search_window_start": SEARCH_WINDOW_START, "search_window_end": SEARCH_WINDOW_END},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_flipping_accepts_public_scheduling_off_disables_the_read(
        self, anon_client, public_group
    ):
        params = {
            "search_window_start": SEARCH_WINDOW_START,
            "search_window_end": SEARCH_WINDOW_END,
        }
        before = _get_slots(anon_client, public_group.public_booking_slug, params)
        assert before.status_code == status.HTTP_200_OK, before.content

        public_group.accepts_public_scheduling = False
        public_group.save()

        after = _get_slots(anon_client, public_group.public_booking_slug, params)
        assert after.status_code == status.HTTP_403_FORBIDDEN

    def test_duration_seconds_query_param_is_not_accepted_and_has_no_effect(
        self, anon_client, public_group
    ):
        """``duration_seconds`` is not a query parameter on this endpoint at
        all -- the search always uses ``group.duration``. Sending one (even a
        value the group's actual pin would refuse at write time) must not
        change the result: the response with a bogus value must be
        byte-identical to the response with none supplied."""
        params = {
            "search_window_start": SEARCH_WINDOW_START,
            "search_window_end": SEARCH_WINDOW_END,
            "slot_step_seconds": 1800,
        }
        without_param = _get_slots(anon_client, public_group.public_booking_slug, params)

        with_bogus_param = _get_slots(
            anon_client,
            public_group.public_booking_slug,
            {**params, "duration_seconds": 999999},
        )

        assert without_param.status_code == status.HTTP_200_OK, without_param.content
        assert with_bogus_param.status_code == status.HTTP_200_OK, with_bogus_param.content
        assert without_param.json() == with_bogus_param.json()

    def test_matches_the_phase5_code_gated_read_for_the_same_group(
        self, anon_client, public_group, group_code
    ):
        """The codeless surface and the Phase 5 code-gated surface are two
        different addressing schemes over the SAME underlying service call
        (``CalendarGroupService.find_bookable_slots``) -- for the same group,
        the same window, and the same (here, matching) duration, they must
        return the same proposals."""
        codeless_response = _get_slots(
            anon_client,
            public_group.public_booking_slug,
            {
                "search_window_start": SEARCH_WINDOW_START,
                "search_window_end": SEARCH_WINDOW_END,
                "slot_step_seconds": 1800,
            },
        )
        assert codeless_response.status_code == status.HTTP_200_OK, codeless_response.content

        coded_response = anon_client.get(
            "/public/booking/calendar-group-bookable-slots/",
            {
                "search_window_start": SEARCH_WINDOW_START,
                "search_window_end": SEARCH_WINDOW_END,
                "duration_seconds": int(GROUP_DURATION.total_seconds()),
                "slot_step_seconds": 1800,
            },
            headers={"X-Booking-Code": group_code},
        )
        assert coded_response.status_code == status.HTTP_200_OK, coded_response.content

        assert codeless_response.json() == coded_response.json()
        assert len(codeless_response.json()) > 0

    def test_every_proposal_is_actually_bookable_through_the_codeless_write(
        self, anon_client, organization, public_group, slot_calendar
    ):
        response = _get_slots(
            anon_client,
            public_group.public_booking_slug,
            {
                "search_window_start": SEARCH_WINDOW_START,
                "search_window_end": SEARCH_WINDOW_END,
                "slot_step_seconds": 1800,
            },
        )
        assert response.status_code == status.HTTP_200_OK, response.content
        proposals = response.json()
        assert len(proposals) > 0
        first_proposal = proposals[0]

        selections = _slot_selections(public_group, slot_calendar)
        booking_response = anon_client.post(
            _events_url(public_group.public_booking_slug),
            _group_booking_payload(
                first_proposal["start_time"], first_proposal["end_time"], selections
            ),
            format="json",
        )

        assert booking_response.status_code == status.HTTP_201_CREATED, booking_response.content
        event_id = booking_response.json()["id"]
        assert (
            CalendarEvent.objects.filter_by_organization(organization.id)
            .filter(id=event_id)
            .exists()
        )


# ---------------------------------------------------------------------------
# Scenario 2: Range availability (codeless, slug-addressed, POST)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicAvailabilityRead:
    def _ranges_body(self) -> dict:
        return {
            "ranges": [
                {"start_time": "2030-06-01T09:00:00Z", "end_time": "2030-06-01T09:30:00Z"},
            ]
        }

    def test_public_group_availability_readable_with_no_credential(self, anon_client, public_group):
        response = _post_availability(
            anon_client, public_group.public_booking_slug, self._ranges_body()
        )

        assert response.status_code == status.HTTP_200_OK, response.content
        body = response.json()
        assert len(body) == 1
        assert body[0]["slots"][0]["required_count"] == 1

    def test_private_group_returns_403(self, anon_client, private_group):
        response = _post_availability(
            anon_client, private_group.public_booking_slug, self._ranges_body()
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "does not accept public scheduling" in response.json()["detail"].lower()

    def test_unknown_slug_returns_404(self, anon_client):
        response = _post_availability(
            anon_client, "well-formed-but-nonexistent-slug", self._ranges_body()
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_null_duration_returns_403_rather_than_reporting_availability(
        self, anon_client, public_group_with_no_duration
    ):
        """A group the write side always refuses (fail closed, no duration
        configured) must not report itself as available either -- the read
        and the write must agree."""
        response = _post_availability(
            anon_client, public_group_with_no_duration.public_booking_slug, self._ranges_body()
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_flipping_accepts_public_scheduling_off_disables_the_read(
        self, anon_client, public_group
    ):
        before = _post_availability(
            anon_client, public_group.public_booking_slug, self._ranges_body()
        )
        assert before.status_code == status.HTTP_200_OK, before.content

        public_group.accepts_public_scheduling = False
        public_group.save()

        after = _post_availability(
            anon_client, public_group.public_booking_slug, self._ranges_body()
        )
        assert after.status_code == status.HTTP_403_FORBIDDEN

    def test_matches_the_phase5_code_gated_read_for_the_same_group(
        self, anon_client, public_group, group_code
    ):
        codeless_response = _post_availability(
            anon_client, public_group.public_booking_slug, self._ranges_body()
        )
        assert codeless_response.status_code == status.HTTP_200_OK, codeless_response.content

        coded_response = anon_client.post(
            "/public/booking/calendar-group-availability/",
            self._ranges_body(),
            format="json",
            headers={"X-Booking-Code": group_code},
        )
        assert coded_response.status_code == status.HTTP_200_OK, coded_response.content

        assert codeless_response.json() == coded_response.json()

    def test_available_calendar_ids_are_retained_for_slot_selection(
        self, anon_client, public_group, slot_calendar
    ):
        """Unlike the bookable-slots read, this endpoint deliberately keeps
        ``available_calendar_ids`` per slot -- group booking's
        ``slot_selections`` genuinely needs those ids to build a valid
        create request. See the "Codeless discovery is group-aggregated"
        Guiding Decision, which calls this retention out explicitly."""
        response = _post_availability(
            anon_client, public_group.public_booking_slug, self._ranges_body()
        )

        assert response.status_code == status.HTTP_200_OK, response.content
        slot_result = response.json()[0]["slots"][0]
        assert slot_result["available_calendar_ids"] == [slot_calendar.id]


# ---------------------------------------------------------------------------
# Scenario 3: window reads are deliberately not shipped on this surface.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWindowReadsAreNotShipped:
    def test_availability_windows_route_does_not_exist(self, anon_client, public_group):
        response = anon_client.get(
            f"/public/booking/calendar-groups/{public_group.public_booking_slug}"
            "/availability-windows/"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unavailable_windows_route_does_not_exist(self, anon_client, public_group):
        response = anon_client.get(
            f"/public/booking/calendar-groups/{public_group.public_booking_slug}"
            "/unavailable-windows/"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
