"""Integration tests for the codeless branch of
``POST /public/booking/calendar-groups/<group_id>/events/``.

Phase 3 adds a second authorization path to the Phase 2 endpoint: when
``X-Booking-Code`` is absent, the request is authorized entirely by the path
group's own ``accepts_public_scheduling`` flag, mirroring GraphQL's codeless
``createCalendarGroupEvent`` mutation. See
``test_booking_rest_create_group_event.py`` for the coded-path coverage this
complements -- the two files together cover the endpoint's full contract.

All requests here are unauthenticated (no session/JWT, no header at all).
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
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
    CalendarManagementToken,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


BOOKING_START = datetime.datetime(2030, 7, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 7, 1, 11, 0, tzinfo=datetime.UTC)


def _booking_url(group_id: int) -> str:
    return f"/public/booking/calendar-groups/{group_id}/events/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Codeless Group-Book Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


def _make_calendar(organization: Organization, external_id: str) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        external_id=external_id,
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        accepts_public_scheduling=False,
    )


def _make_group_with_two_slots(
    organization: Organization,
    *,
    accepts_public_scheduling: bool,
    primary_calendar: Calendar,
    secondary_calendar: Calendar,
    name: str = "Test Group",
) -> CalendarGroup:
    grp = baker.make(
        CalendarGroup,
        organization=organization,
        name=name,
        accepts_public_scheduling=accepts_public_scheduling,
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
def primary_calendar(organization):
    return _make_calendar(organization, "rest-codeless-primary-cal")


@pytest.fixture
def secondary_calendar(organization):
    return _make_calendar(organization, "rest-codeless-room-cal")


@pytest.fixture
def public_group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup that accepts public (codeless) scheduling."""
    return _make_group_with_two_slots(
        organization,
        accepts_public_scheduling=True,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        name="Public Group",
    )


@pytest.fixture
def private_group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup that does NOT accept public scheduling -- codeless requests
    against it must be denied."""
    return _make_group_with_two_slots(
        organization,
        accepts_public_scheduling=False,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        name="Private Group",
    )


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def public_group_booking_code(permission_service, organization, public_group):
    """A valid single-use CREATE code scoped to `public_group` -- used to prove
    the coded path wins even though the group itself accepts public scheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=public_group.id,
    )
    return token, code


@pytest.fixture
def anon_client():
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
        "title": "Codeless Group Appointment",
        "description": "A codeless group booking",
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
# Scenario 1: Codeless happy path against a public group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventHappyPath:
    def test_public_group_books_with_no_header(
        self,
        anon_client,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post(anon_client, public_group.id, None, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["title"] == "Codeless Group Appointment"

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.calendar_fk_id == primary_calendar.id
        assert event.calendar_group_fk_id == public_group.id
        assert event.organization_id == organization.id

    def test_no_code_is_consumed(
        self,
        anon_client,
        permission_service,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """No code is presented, so none can be consumed -- asserted explicitly.

        A valid group booking code exists for `public_group` (it could have been
        used to book this exact request) but is never sent. The codeless request
        must still succeed via the group's own ``accepts_public_scheduling``, and
        that unrelated, unpresented code must remain completely untouched."""
        unused_token, _unused_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=public_group.id,
        )

        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        response = _post(anon_client, public_group.id, None, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content

        unused_token.refresh_from_db()
        assert unused_token.used_at is None
        assert unused_token.consumed_source_ip is None

    def test_existing_tokens_in_the_organization_are_left_untouched(
        self,
        anon_client,
        permission_service,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A codeless booking must not read, consume, or otherwise mutate any
        PRE-EXISTING CalendarManagementToken row -- there is no code in the
        request to resolve one against. Seed a handful of unrelated live
        booking-code tokens (calendar-scoped and group-scoped) and prove every
        one of them is byte-identical after the codeless request.

        This does not assert the organization's token count is unchanged:
        ``create_grouped_event`` always mints a fresh per-attendee RSVP
        management token for the new event's external attendee, regardless of
        whether the booking was coded or codeless -- that is an unrelated,
        expected side effect of event creation, not a booking code being
        consumed."""
        calendar_token, _ = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=primary_calendar.id,
        )
        group_token, _ = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=public_group.id,
        )
        pre_existing_ids = {calendar_token.id, group_token.id}
        before = {
            token.id: (token.used_at, token.consumed_source_ip, token.revoked_at)
            for token in CalendarManagementToken.objects.filter_by_organization(
                organization.id
            ).filter(id__in=pre_existing_ids)
        }
        assert len(before) == 2

        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        response = _post(anon_client, public_group.id, None, _group_booking_payload(selections))
        assert response.status_code == status.HTTP_201_CREATED, response.content

        after = {
            token.id: (token.used_at, token.consumed_source_ip, token.revoked_at)
            for token in CalendarManagementToken.objects.filter_by_organization(
                organization.id
            ).filter(id__in=pre_existing_ids)
        }
        assert after == before

        calendar_token.refresh_from_db()
        group_token.refresh_from_db()
        assert calendar_token.used_at is None
        assert group_token.used_at is None


# ---------------------------------------------------------------------------
# Scenario 2: Codeless denial against a private group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventPrivateGroupDenied:
    def test_private_group_returns_not_permitted(
        self,
        anon_client,
        organization,
        private_group,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(private_group, primary_calendar, secondary_calendar)

        response = _post(anon_client, private_group.id, None, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "does not accept public scheduling" in body["detail"].lower()

    def test_private_group_books_nothing(
        self,
        anon_client,
        organization,
        private_group,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(private_group, primary_calendar, secondary_calendar)

        response = _post(anon_client, private_group.id, None, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 3: Missing group returns 404 (not a secret on this path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventMissingGroup:
    def test_nonexistent_group_id_returns_404(self, anon_client):
        response = _post(anon_client, 999_999_999, None, _group_booking_payload([]))

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Scenario 4: The coded branch wins when the header is present, even against a
# group that itself accepts public scheduling.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodedBranchWinsOverCodeless:
    def test_valid_code_against_public_group_still_consumes_the_code(
        self,
        anon_client,
        public_group_booking_code,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A group that accepts public scheduling AND is handed a valid group
        code still books through the coded path -- and that code IS consumed.
        The coded branch wins whenever the header is present."""
        token, code = public_group_booking_code
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post(anon_client, public_group.id, code, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None


# ---------------------------------------------------------------------------
# Scenario 5: Cross-organization isolation on the codeless path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventCrossOrgIsolation:
    def test_codeless_booking_stays_scoped_to_its_own_organization(
        self,
        anon_client,
        organization,
        other_organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A second organization existing at all must not let a codeless
        booking against `organization`'s public group leak into, or be
        satisfied by, `other_organization`'s data -- the group id alone
        determines the organization, and nothing else can redirect it."""
        other_primary = _make_calendar(other_organization, "rest-codeless-other-primary")
        other_secondary = _make_calendar(other_organization, "rest-codeless-other-room")
        other_group = _make_group_with_two_slots(
            other_organization,
            accepts_public_scheduling=True,
            primary_calendar=other_primary,
            secondary_calendar=other_secondary,
            name="Other Org Public Group",
        )

        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        response = _post(anon_client, public_group.id, None, _group_booking_payload(selections))

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.organization_id == organization.id
        assert not CalendarEvent.objects.filter_by_organization(other_organization.id).exists()

        other_group.refresh_from_db()  # sanity: untouched, no event linked to it
        assert (
            not CalendarEvent.objects.filter_by_organization(other_organization.id)
            .filter(calendar_group_fk_id=other_group.id)
            .exists()
        )


# ---------------------------------------------------------------------------
# Scenario 6: ambiguous X-Booking-Code header values -- empty string vs.
# whitespace-only. ``booking_code_header`` does ``return value or None``, so
# these two must NOT be treated the same: an empty string is falsy (codeless),
# a whitespace-only string is truthy (coded).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAmbiguousHeaderValues:
    def test_empty_header_value_is_treated_as_codeless(
        self,
        anon_client,
        organization,
        public_group,
        private_group,
        primary_calendar,
        secondary_calendar,
    ):
        """``booking_code_header`` normalizes an empty-string header to
        ``None`` (``value or None``), so an empty ``X-Booking-Code`` takes the
        codeless branch exactly like an absent header -- proven here against
        both a PUBLIC group (books) and a PRIVATE group (denied via the same
        403 NOT_PERMITTED the fully-absent-header case gets), so the branch
        choice is unambiguous either way."""
        public_selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        public_response = _post(
            anon_client, public_group.id, "", _group_booking_payload(public_selections)
        )
        assert public_response.status_code == status.HTTP_201_CREATED, public_response.content

        private_selections = _slot_selections(private_group, primary_calendar, secondary_calendar)
        private_response = _post(
            anon_client, private_group.id, "", _group_booking_payload(private_selections)
        )
        assert private_response.status_code == status.HTTP_403_FORBIDDEN
        assert private_response.json()["error_code"] == "NOT_PERMITTED"

    def test_whitespace_header_is_treated_as_a_code_not_codeless(
        self,
        anon_client,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A whitespace-only ``X-Booking-Code`` (``" "``) is truthy, so
        ``booking_code_header`` returns it unchanged and the request takes the
        CODED branch -- never the codeless one, even against a group that
        itself accepts public scheduling. This matters: if a whitespace
        header fell through to codeless, a caller could bypass every one of
        the coded path's checks (resolve/authorize/scope/pin) just by sending
        a blank-looking header instead of omitting it -- that would be a
        bypass of the coded path's guarantees, not a convenience. Instead,
        the coded branch tries to resolve `" "` as a code and fails.

        Observed (not assumed): ``resolve_code`` cannot decode a whitespace
        string into a valid ``token_id:token_str`` pair, so it raises
        ``InvalidTokenError`` -> ``InvalidCodeAPIException`` -> ``404
        INVALID_CODE``.
        """
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post(anon_client, public_group.id, " ", _group_booking_payload(selections))

        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content
        assert response.json()["error_code"] == "INVALID_CODE"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()
