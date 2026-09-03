"""Integration tests for ``POST /booking-codes/`` and ``DELETE /booking-codes/<id>/``.

This is the authenticated minting/revocation surface -- unlike every other
booking-code test file, requests here carry a session (``force_authenticate``),
not an ``X-Booking-Code`` header. The header appears only on the SECOND half of
each parity test, when the code minted through this endpoint is presented to a
Phase 1-5 endpoint to prove it is indistinguishable from a GraphQL-minted one.

Covers:
- The six ``purpose`` x target combinations, each minted here and then USED
  against the matching Phase 1-4 write endpoint (the real parity assertion).
- End-to-end duration pinning: this endpoint mints no ``duration_seconds`` of
  its own -- duration pinning lives on ``CalendarGroup.duration``, not on the
  token. A code minted for a group that already carries a duration is
  enforced on write and silently overrides the client's ``duration_seconds``
  on the Phase 5 group-scoped bookable-slots read; a code minted for a
  calendar carries no duration constraint at all.
- Authorization matrix: admin mints for any target; a member mints for an owned
  calendar / a participated group; a member is refused for a non-owned calendar
  / non-participated group; a cross-organization target is 404, never 403.
- Validation matrix.
- Mint attribution: ``minted_by_membership_user_id`` set, ``minted_by_system_user``
  null, audit actor names the user.
- Revoke idempotency, a revoked code failing a Phase 1 write with 403 REVOKED,
  and revoking another organization's code leaving that row untouched.
"""

import datetime
from unittest.mock import patch

from django.urls import reverse

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
from calendar_integration.factories import create_calendar_ownership
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
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import grant_membership_groups
from users.factories import UserFactory


MINT_LIST_URL = "api:BookingCodes-list"
MINT_DETAIL_URL = "api:BookingCodes-detail"

BOOKING_CALENDAR_EVENTS_URL = "calendar_booking_api:booking-calendar-events-list"
BOOKING_RESCHEDULE_URL = "calendar_booking_api:booking-events-reschedule-list"
BOOKING_GROUP_RESCHEDULE_URL = "calendar_booking_api:booking-group-events-reschedule-list"
BOOKING_CANCEL_URL = "calendar_booking_api:booking-events-cancel-list"
BOOKING_BOOKABLE_SLOTS_URL = "calendar_booking_api:booking-calendar-bookable-slots-list"
BOOKING_GROUP_BOOKABLE_SLOTS_URL = "calendar_booking_api:booking-calendar-group-bookable-slots-list"

BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 6, 1, 10, 30, tzinfo=datetime.UTC)
NEW_START = datetime.datetime(2030, 6, 1, 14, 0, tzinfo=datetime.UTC)
NEW_END = datetime.datetime(2030, 6, 1, 15, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_url() -> str:
    return reverse(MINT_LIST_URL)


def _revoke_url(token_id: int) -> str:
    return reverse(MINT_DETAIL_URL, args=[token_id])


def _mint(client: APIClient, payload: dict):
    return client.post(_mint_url(), payload, format="json")


def _revoke(client: APIClient, token_id: int):
    return client.delete(_revoke_url(token_id))


def _group_booking_url(public_slug: str) -> str:
    return f"/public/booking/calendar-groups/{public_slug}/events/"


def _make_member(org: Organization, *, is_admin: bool = False) -> OrganizationMembership:
    user = UserFactory().create_user()
    return grant_membership_groups(
        OrganizationMembership.objects.create(user=user, organization=org, is_active=True),
        [GROUP_ORGANIZATION_ADMIN if is_admin else GROUP_ORGANIZATION_MEMBER],
    )


def _auth_client(membership: OrganizationMembership) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=membership.user)
    return client


def _book_payload(**overrides) -> dict:
    base = {
        "title": "My Appointment",
        "description": "",
        "start_time": BOOKING_START.isoformat(),
        "end_time": BOOKING_END.isoformat(),
        "timezone": "UTC",
        "external_attendee": {"email": "patient@example.com", "name": "Pat Patient"},
    }
    base.update(overrides)
    return base


def _slot_selections(group: CalendarGroup, primary: Calendar, secondary: Calendar) -> list[dict]:
    slot_a = group.slots.get(name="Physicians")
    slot_b = group.slots.get(name="Rooms")
    return [
        {"slot_id": slot_a.id, "calendar_ids": [primary.id]},
        {"slot_id": slot_b.id, "calendar_ids": [secondary.id]},
    ]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Mint Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def admin_membership(organization):
    return _make_member(organization, is_admin=True)


@pytest.fixture
def admin_client(admin_membership):
    return _auth_client(admin_membership)


@pytest.fixture
def anon_client():
    """APIClient with no session -- used to present the minted code."""
    return APIClient()


@pytest.fixture
def calendar(organization):
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        external_id="rest-mint-primary-cal",
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
        external_id="rest-mint-secondary-cal",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def available_window(organization, calendar):
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def group(organization, calendar, secondary_calendar):
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Physicians", order=0, required_count=1
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Rooms", order=1, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_b, calendar=secondary_calendar
    )
    return grp


@pytest.fixture
def group_availability_windows(organization, calendar, secondary_calendar):
    windows = []
    for cal in (calendar, secondary_calendar):
        windows.append(
            baker.make(
                AvailableTime,
                organization=organization,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
                timezone="UTC",
            )
        )
    return windows


# ---------------------------------------------------------------------------
# The real parity test: six purpose x target combinations, minted here and
# USED against the matching Phase 1-4 endpoint.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSixPurposeTargetCombinations:
    """A REST-minted code must be indistinguishable from a GraphQL-minted one.

    Each test mints through ``POST /booking-codes/`` and then presents the
    returned plaintext to the matching Phase 1-4 endpoint via the
    ``X-Booking-Code`` header, proving the code actually works end to end --
    not merely that a token row with the right shape was created.
    """

    def test_book_calendar(
        self, admin_client, anon_client, organization, calendar, available_window
    ):
        mint = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        assert set(token.permissions.values_list("permission", flat=True)) == {
            EventManagementPermissions.CREATE
        }

        response = anon_client.post(
            reverse(BOOKING_CALENDAR_EVENTS_URL),
            _book_payload(),
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(
            id=response.json()["id"]
        )
        assert event.calendar_fk_id == calendar.id

    def test_book_calendar_group(
        self,
        admin_client,
        anon_client,
        organization,
        calendar,
        secondary_calendar,
        group,
        group_availability_windows,  # noqa: ARG002 -- seeds DB rows consumed by create_event
    ):
        mint = _mint(admin_client, {"purpose": "book", "calendar_group": group.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        assert set(token.permissions.values_list("permission", flat=True)) == {
            EventManagementPermissions.CREATE
        }

        payload = _book_payload(
            slot_selections=_slot_selections(group, calendar, secondary_calendar)
        )
        response = anon_client.post(
            _group_booking_url(group.public_booking_slug),
            payload,
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(
            id=response.json()["id"]
        )
        assert event.calendar_group_fk_id == group.id

    def test_reschedule_calendar(
        self,
        admin_client,
        anon_client,
        organization,
        calendar,
        available_window,  # noqa: ARG002 -- seeds DB rows consumed by can_perform_update
    ):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title="Existing Event",
            timezone="UTC",
            start_time_tz_unaware=BOOKING_START.replace(tzinfo=None),
            end_time_tz_unaware=BOOKING_END.replace(tzinfo=None),
            external_id="",
        )

        mint = _mint(
            admin_client,
            {"purpose": "reschedule", "calendar": calendar.id, "event": event.id},
        )
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        assert set(token.permissions.values_list("permission", flat=True)) == {
            EventManagementPermissions.RESCHEDULE
        }

        response = anon_client.post(
            reverse(BOOKING_RESCHEDULE_URL),
            {
                "start_time": NEW_START.isoformat(),
                "end_time": NEW_END.isoformat(),
                "timezone": "UTC",
            },
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content
        event.refresh_from_db()
        assert event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(tzinfo=None)

    def test_reschedule_calendar_group(
        self,
        admin_client,
        anon_client,
        organization,
        calendar,
        secondary_calendar,
        group,
        group_availability_windows,  # noqa: ARG002
    ):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            calendar_group=group,
            title="Existing Group Event",
            timezone="UTC",
            start_time_tz_unaware=BOOKING_START.replace(tzinfo=None),
            end_time_tz_unaware=BOOKING_END.replace(tzinfo=None),
            external_id="",
        )

        mint = _mint(
            admin_client,
            {"purpose": "reschedule", "calendar_group": group.id, "event": event.id},
        )
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        assert set(token.permissions.values_list("permission", flat=True)) == {
            EventManagementPermissions.RESCHEDULE
        }

        response = anon_client.post(
            reverse(BOOKING_GROUP_RESCHEDULE_URL),
            {
                "start_time": NEW_START.isoformat(),
                "end_time": NEW_END.isoformat(),
                "timezone": "UTC",
            },
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content
        event.refresh_from_db()
        assert event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(tzinfo=None)

    def test_cancel_calendar(self, admin_client, anon_client, organization, calendar):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title="Existing Event",
            timezone="UTC",
            start_time_tz_unaware=BOOKING_START.replace(tzinfo=None),
            end_time_tz_unaware=BOOKING_END.replace(tzinfo=None),
            external_id="",
        )

        mint = _mint(
            admin_client, {"purpose": "cancel", "calendar": calendar.id, "event": event.id}
        )
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        assert set(token.permissions.values_list("permission", flat=True)) == {
            EventManagementPermissions.CANCEL
        }

        response = anon_client.post(
            reverse(BOOKING_CANCEL_URL), {}, format="json", headers={BOOKING_CODE_HEADER: code}
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.content
        assert not CalendarEvent.original_manager.filter(id=event.id).exists()

    def test_cancel_calendar_group(
        self, admin_client, anon_client, organization, calendar, secondary_calendar, group
    ):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            calendar_group=group,
            title="Existing Group Event",
            timezone="UTC",
            start_time_tz_unaware=BOOKING_START.replace(tzinfo=None),
            end_time_tz_unaware=BOOKING_END.replace(tzinfo=None),
            external_id="",
        )

        mint = _mint(
            admin_client,
            {"purpose": "cancel", "calendar_group": group.id, "event": event.id},
        )
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        assert set(token.permissions.values_list("permission", flat=True)) == {
            EventManagementPermissions.CANCEL
        }

        response = anon_client.post(
            reverse(BOOKING_CANCEL_URL), {}, format="json", headers={BOOKING_CODE_HEADER: code}
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.content
        assert not CalendarEvent.original_manager.filter(id=event.id).exists()


# ---------------------------------------------------------------------------
# End-to-end duration pinning
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDurationPinEndToEnd:
    """Duration pinning lives on ``CalendarGroup.duration``, not on the
    minted token: the mint endpoint accepts no ``duration_seconds`` of its
    own, so these tests set the pin on the GROUP target directly, before
    minting a code for it -- unlike GraphQL's mint mutations, which are
    deliberately unchanged.

    A calendar-scoped code carries no duration constraint at all (no
    ``Calendar.duration`` exists) -- see
    ``test_calendar_scoped_code_accepts_any_span`` below.
    """

    def test_pinned_group_code_enforced_on_write_and_silently_overrides_read(
        self,
        admin_client,
        anon_client,
        organization,
        calendar,
        secondary_calendar,
        group,
        group_availability_windows,  # noqa: ARG002 -- seeds DB rows consumed by create_event
    ):
        group.duration = datetime.timedelta(minutes=30)
        group.save()

        mint = _mint(admin_client, {"purpose": "book", "calendar_group": group.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        body = mint.json()
        code = body["code"]

        slot_selections = _slot_selections(group, calendar, secondary_calendar)

        # Wrong span (45 min instead of the pinned 30) is refused and does NOT
        # consume the code -- the pin check runs before create/consume.
        wrong_payload = _book_payload(
            end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat(),
            slot_selections=slot_selections,
        )
        wrong_response = anon_client.post(
            _group_booking_url(group.public_booking_slug),
            wrong_payload,
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert wrong_response.status_code == status.HTTP_403_FORBIDDEN, wrong_response.content
        assert wrong_response.json()["error_code"] == "NOT_PERMITTED"

        # Phase 5 read: the client asks for a DIFFERENT duration_seconds: the
        # group's pin silently overrides it, so every proposal spans exactly
        # 30 minutes.
        read_response = anon_client.get(
            reverse(BOOKING_GROUP_BOOKABLE_SLOTS_URL),
            {
                "search_window_start": "2030-06-01T09:00:00Z",
                "search_window_end": "2030-06-01T11:00:00Z",
                "duration_seconds": 3600,
                "slot_step_seconds": 1800,
            },
            headers={BOOKING_CODE_HEADER: code},
        )
        assert read_response.status_code == status.HTTP_200_OK, read_response.content
        proposals = read_response.json()
        assert len(proposals) > 0
        for proposal in proposals:
            start = datetime.datetime.fromisoformat(proposal["start_time"])
            end = datetime.datetime.fromisoformat(proposal["end_time"])
            assert end - start == datetime.timedelta(minutes=30)

        # Correct span (30 min, matching the pin) succeeds and consumes the code.
        right_payload = _book_payload(slot_selections=slot_selections)
        right_response = anon_client.post(
            _group_booking_url(group.public_booking_slug),
            right_payload,
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert right_response.status_code == status.HTTP_201_CREATED, right_response.content

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=mint.json()["id"]
        )
        assert token.used_at is not None

    def test_unpinned_group_code_accepts_any_span(
        self,
        admin_client,
        anon_client,
        organization,
        calendar,
        secondary_calendar,
        group,
        group_availability_windows,  # noqa: ARG002 -- seeds DB rows consumed by create_event
    ):
        mint = _mint(admin_client, {"purpose": "book", "calendar_group": group.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        code = mint.json()["code"]

        payload = _book_payload(
            end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat(),
            slot_selections=_slot_selections(group, calendar, secondary_calendar),
        )
        response = anon_client.post(
            _group_booking_url(group.public_booking_slug),
            payload,
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_calendar_scoped_code_accepts_any_span(
        self, admin_client, anon_client, organization, calendar, available_window
    ):
        """A calendar-scoped code has no ``CalendarGroup`` to pin a duration
        on at all -- unlike the group-scoped cases above, any span is
        accepted."""
        mint = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        code = mint.json()["code"]

        payload = _book_payload(
            end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )
        response = anon_client.post(
            reverse(BOOKING_CALENDAR_EVENTS_URL),
            payload,
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthorizationMatrix:
    def test_admin_mints_for_any_calendar(self, admin_client, calendar):
        response = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_admin_mints_for_any_group(self, admin_client, group):
        response = _mint(admin_client, {"purpose": "book", "calendar_group": group.id})
        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_member_mints_for_owned_calendar(self, organization, calendar):
        member = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=member.user)
        client = _auth_client(member)

        response = _mint(client, {"purpose": "book", "calendar": calendar.id})
        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_member_mints_for_participated_group(self, organization, calendar, group):
        member = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=member.user)
        client = _auth_client(member)

        response = _mint(client, {"purpose": "book", "calendar_group": group.id})
        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_member_refused_for_non_owned_calendar(self, organization, calendar):
        member = _make_member(organization)
        client = _auth_client(member)

        response = _mint(client, {"purpose": "book", "calendar": calendar.id})
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content

    def test_member_refused_for_non_participated_group(self, organization, group):
        member = _make_member(organization)
        client = _auth_client(member)

        response = _mint(client, {"purpose": "book", "calendar_group": group.id})
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content

    def test_member_refused_for_different_group_despite_owning_calendar_in_another_group(
        self, organization, calendar, group
    ):
        """A member who owns a calendar inside group `group` (G1)'s slot pools
        must still be refused for a DIFFERENT group G2 in the same org.

        ``test_member_refused_for_non_participated_group`` above uses a member
        with no ownership at all, so it would pass even if
        ``can_view_calendar_group`` degenerated to "owns any calendar
        anywhere" -- this test would catch that regression.
        """
        member = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=member.user)
        client = _auth_client(member)

        other_group = baker.make(CalendarGroup, organization=organization, name="Other Group")
        CalendarGroupSlot.objects.create(
            organization=organization, group=other_group, name="Slot", order=0, required_count=1
        )

        response = _mint(client, {"purpose": "book", "calendar_group": other_group.id})
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content

    def test_cross_org_calendar_target_is_404_not_403(self, admin_client, other_organization):
        other_calendar = baker.make(Calendar, organization=other_organization)
        response = _mint(admin_client, {"purpose": "book", "calendar": other_calendar.id})
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content

    def test_cross_org_group_target_is_404_not_403(self, admin_client, other_organization):
        other_group = baker.make(CalendarGroup, organization=other_organization)
        response = _mint(admin_client, {"purpose": "book", "calendar_group": other_group.id})
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content


# ---------------------------------------------------------------------------
# _resolve_event_target's three 404 branches (mismatch is always 404, never
# 403 -- the event id is as sensitive as the calendar/group id it belongs to).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolveEventTarget404s:
    def test_cross_org_event_target_is_404(
        self, admin_client, organization, calendar, other_organization
    ):
        other_calendar = baker.make(Calendar, organization=other_organization)
        other_event = baker.make(
            CalendarEvent,
            organization=other_organization,
            calendar=other_calendar,
            timezone="UTC",
        )

        response = _mint(
            admin_client,
            {"purpose": "reschedule", "calendar": calendar.id, "event": other_event.id},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content

    def test_event_on_different_calendar_same_org_is_404(
        self, admin_client, organization, calendar, secondary_calendar
    ):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=secondary_calendar,
            timezone="UTC",
        )

        response = _mint(
            admin_client,
            {"purpose": "reschedule", "calendar": calendar.id, "event": event.id},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content

    def test_grouped_event_addressed_with_calendar_instead_of_group_is_404(
        self, admin_client, organization, calendar, secondary_calendar, group
    ):
        event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            calendar_group=group,
            timezone="UTC",
        )

        response = _mint(
            admin_client,
            {"purpose": "reschedule", "calendar": calendar.id, "event": event.id},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content


# ---------------------------------------------------------------------------
# Validation matrix
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestValidationMatrix:
    def test_both_targets_supplied(self, admin_client, calendar, group):
        response = _mint(
            admin_client,
            {"purpose": "book", "calendar": calendar.id, "calendar_group": group.id},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_neither_target_supplied(self, admin_client):
        response = _mint(admin_client, {"purpose": "book"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_event_forbidden_for_purpose_book(self, admin_client, organization, calendar):
        event = baker.make(
            CalendarEvent, organization=organization, calendar=calendar, timezone="UTC"
        )
        response = _mint(
            admin_client, {"purpose": "book", "calendar": calendar.id, "event": event.id}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_event_required_for_purpose_reschedule(self, admin_client, calendar):
        response = _mint(admin_client, {"purpose": "reschedule", "calendar": calendar.id})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_expires_at_in_past(self, admin_client, calendar):
        past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).isoformat()
        response = _mint(
            admin_client, {"purpose": "book", "calendar": calendar.id, "expires_at": past}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Mint attribution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMintAttribution:
    def test_minted_by_membership_set_system_user_null(
        self, admin_client, admin_membership, organization, calendar
    ):
        response = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        assert response.status_code == status.HTTP_201_CREATED, response.content

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=response.json()["id"]
        )
        assert token.minted_by_membership_user_id == admin_membership.user_id
        assert token.minted_by_system_user_id is None

    def test_audit_entry_names_user_actor(
        self, admin_client, admin_membership, calendar, django_capture_on_commit_callbacks
    ):
        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})

        assert response.status_code == status.HTTP_201_CREATED, response.content

        payloads = [call.args[0] for call in mock_task.delay.call_args_list]
        token_payloads = [
            p
            for p in payloads
            if p["subject"]["subject_type"] == "calendar_integration.calendarmanagementtoken"
        ]
        assert len(token_payloads) == 1
        payload = token_payloads[0]
        assert payload["action_key"] == "create"
        assert payload["actor"]["identity_type"] == "membership"
        assert payload["actor"]["identity_key"] == str(admin_membership.user_id)


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevoke:
    def test_revoke_is_idempotent(self, admin_client, calendar):
        mint = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        token_id = mint.json()["id"]

        first = _revoke(admin_client, token_id)
        second = _revoke(admin_client, token_id)

        assert first.status_code == status.HTTP_204_NO_CONTENT
        assert second.status_code == status.HTTP_204_NO_CONTENT

    def test_revoked_code_fails_phase1_write_with_403_revoked(
        self, admin_client, anon_client, calendar, available_window
    ):
        mint = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        token_id = mint.json()["id"]
        code = mint.json()["code"]

        revoke_response = _revoke(admin_client, token_id)
        assert revoke_response.status_code == status.HTTP_204_NO_CONTENT

        response = anon_client.post(
            reverse(BOOKING_CALENDAR_EVENTS_URL),
            _book_payload(),
            format="json",
            headers={BOOKING_CODE_HEADER: code},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.content
        assert response.json()["error_code"] == "REVOKED"

    def test_revoking_another_orgs_code_returns_204_and_leaves_row_unchanged(
        self, admin_client, other_organization
    ):
        other_membership = _make_member(other_organization, is_admin=True)
        other_client = _auth_client(other_membership)
        other_calendar = baker.make(Calendar, organization=other_organization)

        mint = _mint(other_client, {"purpose": "book", "calendar": other_calendar.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        other_token_id = mint.json()["id"]

        response = _revoke(admin_client, other_token_id)
        assert response.status_code == status.HTTP_204_NO_CONTENT

        other_token = CalendarManagementToken.objects.filter_by_organization(
            other_organization.id
        ).get(id=other_token_id)
        assert other_token.revoked_at is None


# ---------------------------------------------------------------------------
# Revoke authorization -- a plain member must not be able to revoke a token
# that isn't theirs, whether it's a booking code or an owner/attendee token
# minted through a completely different surface (create_calendar_owner_token /
# create_attendee_token). Phase 6 reviewer BLOCKER: DELETE previously applied
# no authorization at all, beyond organization scoping.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevokeAuthorization:
    def test_member_cannot_revoke_another_users_calendar_owner_token(self, organization, calendar):
        """A calendar-owner token (create_calendar_owner_token) is not a booking
        code -- it is not reachable through the mint endpoint at all -- and a
        plain member with no stake in it must not be able to revoke it via
        DELETE /booking-codes/<id>/. Before the fix, `destroy` applied no
        authorization check beyond organization scoping, so this call
        succeeded and permanently locked the owner out (create_calendar_owner_token
        re-mints via get_or_create with no revoked_at in the lookup, so the
        owner's existing revoked row is returned forever).
        """
        owner_membership = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=owner_membership.user)

        service = CalendarPermissionService()
        owner_token = service.create_calendar_owner_token(
            organization_id=organization.id,
            user=owner_membership.user,
            calendar_id=calendar.id,
        )

        member = _make_member(organization)
        client = _auth_client(member)

        response = _revoke(client, owner_token.id)
        assert response.status_code == status.HTTP_204_NO_CONTENT

        owner_token.refresh_from_db()
        assert owner_token.revoked_at is None

        # The owner can still act on their own calendar afterwards.
        service.initialize_with_user(
            owner_membership.user,
            organization_id=organization.id,
            calendar_id=calendar.id,
        )
        assert service.token is not None
        assert service.token.id == owner_token.id

    def test_member_cannot_revoke_booking_code_for_calendar_they_do_not_own(
        self, organization, calendar
    ):
        owner_membership = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=owner_membership.user)
        owner_client = _auth_client(owner_membership)

        mint = _mint(owner_client, {"purpose": "book", "calendar": calendar.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        token_id = mint.json()["id"]

        other_member = _make_member(organization)
        other_client = _auth_client(other_member)

        response = _revoke(other_client, token_id)
        assert response.status_code == status.HTTP_204_NO_CONTENT

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_id
        )
        assert token.revoked_at is None

    def test_owner_can_revoke_own_calendars_booking_code(self, organization, calendar):
        owner_membership = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=owner_membership.user)
        owner_client = _auth_client(owner_membership)

        mint = _mint(owner_client, {"purpose": "book", "calendar": calendar.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        token_id = mint.json()["id"]

        response = _revoke(owner_client, token_id)
        assert response.status_code == status.HTTP_204_NO_CONTENT

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_id
        )
        assert token.revoked_at is not None

    def test_admin_can_revoke_any_booking_code_in_org(self, admin_client, organization, calendar):
        owner_membership = _make_member(organization)
        create_calendar_ownership(calendar=calendar, user=owner_membership.user)
        owner_client = _auth_client(owner_membership)

        mint = _mint(owner_client, {"purpose": "book", "calendar": calendar.id})
        assert mint.status_code == status.HTTP_201_CREATED, mint.content
        token_id = mint.json()["id"]

        response = _revoke(admin_client, token_id)
        assert response.status_code == status.HTTP_204_NO_CONTENT

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_id
        )
        assert token.revoked_at is not None

    def test_audit_entry_names_the_revoking_user(
        self, admin_client, admin_membership, calendar, django_capture_on_commit_callbacks
    ):
        mint = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        token_id = mint.json()["id"]

        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = _revoke(admin_client, token_id)

        assert response.status_code == status.HTTP_204_NO_CONTENT

        payloads = [call.args[0] for call in mock_task.delay.call_args_list]
        token_payloads = [
            p
            for p in payloads
            if p["subject"]["subject_type"] == "calendar_integration.calendarmanagementtoken"
        ]
        assert len(token_payloads) == 1
        payload = token_payloads[0]
        assert payload["action_key"] == "update"
        assert payload["actor"]["identity_type"] == "membership"
        assert payload["actor"]["identity_key"] == str(admin_membership.user_id)


# ---------------------------------------------------------------------------
# The plaintext code is one-time-only
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPlaintextCodeOneTimeOnly:
    def test_code_in_create_response_never_persisted(self, admin_client, organization, calendar):
        response = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["code"]

        token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=body["id"]
        )
        # Only the hash is stored -- the plaintext is never persisted anywhere,
        # so it cannot appear verbatim as (or within) the stored hash.
        assert body["code"] != token.token_hash
        assert body["code"] not in token.token_hash

    def test_no_list_or_retrieve_action_exists(self, admin_client, organization, calendar):
        mint = _mint(admin_client, {"purpose": "book", "calendar": calendar.id})
        token_id = mint.json()["id"]

        list_response = admin_client.get(_mint_url())
        assert list_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        retrieve_response = admin_client.get(_revoke_url(token_id))
        assert retrieve_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        put_response = admin_client.put(_revoke_url(token_id), {}, format="json")
        assert put_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        patch_response = admin_client.patch(_revoke_url(token_id), {}, format="json")
        assert patch_response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_unauthenticated_mint_is_401(self, anon_client, calendar):
        response = _mint(anon_client, {"purpose": "book", "calendar": calendar.id})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.content
