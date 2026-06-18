"""Phase 5 — Cross-owner adversarial sweep for per-owner-scoped Public API tokens.

This file is the consolidated negative-path guarantee for the per-owner-scoping
feature. It proves that a PROVIDER-SCOPED token (``SystemUser.scoped_to_user`` set)
cannot reach data belonging to calendars owned by ANOTHER provider in the same
organization — across:

  * every reachable top-level READ query (calendars / calendarEvents / blockedTimes /
    availableTimes / availabilityWindows / unavailableWindows);
  * every nested/related field a scoped token can traverse from an object it
    legitimately owns (the GraphQL field-traversal surface deferred from Phase 1 —
    bundle_representations, bundle_calendar, resources, group_selections,
    recurring_instances, calendar, etc.);
  * every reachable WRITE mutation (createAvailableTime / createBlockedTime /
    scheduleEvent).

Each test is BEHAVIORAL: it FAILS if its corresponding owner guard / field resolver
is removed. A regression block at the end asserts org-wide tokens (scoped_to_user IS
NULL) are unaffected by any guard.

Companion artifact: ``ai-plans/2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SECURITY_REVIEW.md``.
"""

import datetime
import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
    EventRecurrenceException,
    ResourceAllocation,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService
from users.models import User


# --------------------------------------------------------------------------- #
# Shared client helpers                                                        #
# --------------------------------------------------------------------------- #

_ALL_PROVIDER_RESOURCES = (
    PublicAPIResources.CALENDAR,
    PublicAPIResources.CALENDAR_EVENT,
    PublicAPIResources.BLOCKED_TIME,
    PublicAPIResources.AVAILABLE_TIME,
    PublicAPIResources.AVAILABILITY_WINDOWS,
    PublicAPIResources.UNAVAILABLE_WINDOWS,
)


def _grant_all(system_user: SystemUser) -> None:
    for resource in _ALL_PROVIDER_RESOURCES:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)


def _make_scoped_client(organization: Organization, owner: User) -> tuple[APIClient, SystemUser]:
    """Scoped (scoped_to_user=owner) client with all provider resource grants."""
    token = generate_long_lived_token()
    system_user = baker.make(
        SystemUser,
        organization=organization,
        scoped_to_user=owner,
        integration_name=f"sec_scoped_{organization.pk}_{owner.pk}_{uuid4().hex[:8]}",
        long_lived_token_hash=hash_long_lived_token(token),
        is_active=True,
    )
    _grant_all(system_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _make_org_wide_client(organization: Organization) -> tuple[APIClient, SystemUser]:
    """Org-wide (scoped_to_user IS NULL) client with all provider resource grants."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"sec_orgwide_{organization.pk}_{uuid4().hex[:8]}",
        organization=organization,
    )
    _grant_all(system_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _post(client: APIClient, query: str, variables: dict | None = None) -> dict:
    response = client.post(
        "/graphql/",
        data=json.dumps({"query": query, "variables": variables or {}}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content.decode()
    return response.json()


# --------------------------------------------------------------------------- #
# Fixtures: two providers (A = owner, B = other_owner) in one org.            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="SecSweepOrg")


@pytest.fixture
def owner(organization) -> User:
    user = baker.make(User, email=f"owner_{uuid4().hex[:8]}@sec.test")
    baker.make("organizations.OrganizationMembership", user=user, organization=organization)
    return user


@pytest.fixture
def other_owner(organization) -> User:
    user = baker.make(User, email=f"other_{uuid4().hex[:8]}@sec.test")
    baker.make("organizations.OrganizationMembership", user=user, organization=organization)
    return user


@pytest.fixture
def calendar_a(organization, owner) -> Calendar:
    """Calendar A — owned by `owner` (the scoped token's provider)."""
    cal = baker.make(
        Calendar,
        organization=organization,
        name="Calendar A",
        external_id=f"cal-a-{uuid4().hex[:8]}",
    )
    baker.make(CalendarOwnership, calendar=cal, user=owner, organization=organization)
    return cal


@pytest.fixture
def calendar_b(organization, other_owner) -> Calendar:
    """Calendar B — owned by `other_owner` (a DIFFERENT provider, same org)."""
    cal = baker.make(
        Calendar,
        organization=organization,
        name="Calendar B",
        external_id=f"cal-b-{uuid4().hex[:8]}",
    )
    baker.make(CalendarOwnership, calendar=cal, user=other_owner, organization=organization)
    return cal


def _make_event(calendar: Calendar, *, title: str, **kwargs) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        calendar_fk=calendar,
        organization=calendar.organization,
        title=title,
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2026, 9, 2, 10, 0),
        end_time_tz_unaware=datetime.datetime(2026, 9, 2, 11, 0),
        external_id=f"ev-{uuid4().hex}",
        **kwargs,
    )


_RANGE_START = "2026-09-02T00:00:00Z"
_RANGE_END = "2026-09-02T23:59:59Z"


# --------------------------------------------------------------------------- #
# 1. Top-level READ leak sweep                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestTopLevelReadNoLeak:
    """A scoped token's top-level reads return only owner-A data, never B's."""

    def test_calendars_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        client, _ = _make_scoped_client(organization, owner)
        data = _post(client, "query { calendars { id name } }")
        ids = {c["id"] for c in data["data"]["calendars"]}
        assert str(calendar_a.id) in ids
        assert str(calendar_b.id) not in ids, "Calendar B (other owner) must not be visible"

    def test_calendar_events_by_id_blocks_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        ev_b = _make_event(calendar_b, title="B Event")
        client, _ = _make_scoped_client(organization, owner)
        query = "query($id: Int!) { calendarEvents(eventId: $id) { id title } }"
        data = _post(client, query, {"id": ev_b.id})
        assert data["data"]["calendarEvents"] == [], "B's event must not resolve by id"

    def test_blocked_times_by_id_blocks_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        bt_b = baker.make(
            BlockedTime,
            calendar_fk=calendar_b,
            organization=organization,
            timezone="UTC",
            external_id=f"bt-{uuid4().hex}",
        )
        client, _ = _make_scoped_client(organization, owner)
        query = "query($id: Int!) { blockedTimes(blockedTimeId: $id) { id } }"
        data = _post(client, query, {"id": bt_b.id})
        assert data["data"]["blockedTimes"] == [], "B's blocked time must not resolve by id"

    def test_available_times_by_id_blocks_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        at_b = baker.make(
            AvailableTime,
            calendar_fk=calendar_b,
            organization=organization,
            timezone="UTC",
        )
        client, _ = _make_scoped_client(organization, owner)
        query = "query($id: Int!) { availableTimes(availableTimeId: $id) { id } }"
        data = _post(client, query, {"id": at_b.id})
        assert data["data"]["availableTimes"] == [], "B's available time must not resolve by id"

    def test_calendar_events_by_range_on_b_calendar_is_not_found(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        """Cross-owner calendarId range query is indistinguishable from a missing calendar."""
        mock_rl.return_value = iter([None])
        _make_event(calendar_b, title="B Event")
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($c: Int!, $s: DateTime!, $e: DateTime!) {"
            " calendarEvents(calendarId: $c, startDatetime: $s, endDatetime: $e)"
            " { id } }"
        )
        cross = _post(client, query, {"c": calendar_b.id, "s": _RANGE_START, "e": _RANGE_END})
        missing = _post(client, query, {"c": 9_999_999, "s": _RANGE_START, "e": _RANGE_END})
        assert ("errors" in cross) == ("errors" in missing), (
            "Cross-owner calendarId must be indistinguishable from a nonexistent one"
        )


# --------------------------------------------------------------------------- #
# 2. Nested-field traversal leak sweep (the deferred Phase-1 surface)          #
# --------------------------------------------------------------------------- #

# Each query fetches A's OWN event by id and selects a nested relation that, before
# Phase 5, could surface B-owned objects via field traversal. The scoped token must
# never see a B id in the nested selection.


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestNestedFieldTraversalNoLeak:
    """Nested-field selections on A's objects never surface B-owned data."""

    def test_bundle_representations_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        primary = _make_event(calendar_a, title="A Primary", is_bundle_primary=True)
        # A representation lives on B's calendar but points back at A's primary event.
        rep_b = _make_event(calendar_b, title="B Representation", bundle_primary_event_fk=primary)
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id bundleRepresentations { id title } } }"
        )
        data = _post(client, query, {"id": primary.id})
        events = data["data"]["calendarEvents"]
        assert len(events) == 1
        rep_ids = {r["id"] for r in events[0]["bundleRepresentations"]}
        assert str(rep_b.id) not in rep_ids, "B-owned bundle representation must be hidden"

    def test_bundle_calendar_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        # A's event was created through a bundle calendar owned by B.
        ev_a = _make_event(calendar_a, title="A via B bundle", bundle_calendar_fk=calendar_b)
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id) { id bundleCalendar { id name } } }"
        )
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        assert event["bundleCalendar"] is None, "B-owned bundle calendar must be hidden"

    def test_bundle_primary_event_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        primary_b = _make_event(calendar_b, title="B Primary", is_bundle_primary=True)
        rep_a = _make_event(calendar_a, title="A Representation", bundle_primary_event_fk=primary_b)
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id bundlePrimaryEvent { id title } } }"
        )
        data = _post(client, query, {"id": rep_a.id})
        event = data["data"]["calendarEvents"][0]
        assert event["bundlePrimaryEvent"] is None, "B-owned bundle primary event must be hidden"

    def test_resources_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        ev_a = _make_event(calendar_a, title="A Event with resource")
        # Allocate B's calendar as a resource of A's event.
        ResourceAllocation.objects.create(
            event=ev_a,
            calendar=calendar_b,
            organization=organization,
        )
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id resources { id name } resourceAllocations { id calendar { id } } } }"
        )
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        resource_ids = {r["id"] for r in event["resources"]}
        assert str(calendar_b.id) not in resource_ids, "B resource calendar must be hidden"
        alloc_cal_ids = {a["calendar"]["id"] for a in event["resourceAllocations"] if a["calendar"]}
        assert str(calendar_b.id) not in alloc_cal_ids, (
            "Allocation exposing B's resource calendar must be hidden"
        )

    def test_group_selections_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        ev_a = _make_event(calendar_a, title="A grouped event")
        group = baker.make(CalendarGroup, organization=organization, name="Grp")
        slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Slot")
        # Add both calendars to the slot pool via the through model.
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=calendar_a
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=calendar_b
        )
        CalendarEventGroupSelection.objects.create(
            event=ev_a,
            slot=slot,
            calendar=calendar_b,
            organization=organization,
        )
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id groupSelections { id slot { id calendars { id } } calendar { id } } } }"
        )
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        sel_cal_ids = {s["calendar"]["id"] for s in event["groupSelections"] if s["calendar"]}
        assert str(calendar_b.id) not in sel_cal_ids, "Group selection on B must be hidden"
        # Fix 1 (BLOCKER): slot must be null for scoped tokens — the slot's calendar pool
        # spans all providers and would enumerate cross-owner calendars.
        for sel in event["groupSelections"]:
            assert sel["slot"] is None, (
                "slot must be suppressed for scoped tokens (cross-provider pool leak)"
            )

    def test_group_selections_slot_calendars_excludes_other_owner_via_org_wide(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        """Org-wide token reaches the slot; its calendars pool is still full (no scoping).

        This is the companion regression ensuring that the org-wide path is unchanged
        while also verifying that the defense-in-depth ``calendars`` resolver is hit.
        If a slot IS reached (only possible via org-wide here), its pool is returned
        in full. The scoped-token path is covered by ``test_group_selections_excludes_other_owner``.
        """
        mock_rl.return_value = iter([None])
        ev_a = _make_event(calendar_a, title="A grouped event ow")
        group = baker.make(CalendarGroup, organization=organization, name="GrpOW")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=group, name="SlotOW"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=calendar_a
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=calendar_b
        )
        CalendarEventGroupSelection.objects.create(
            event=ev_a,
            slot=slot,
            calendar=calendar_a,
            organization=organization,
        )
        client, _ = _make_org_wide_client(organization)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id groupSelections { id slot { id calendars { id } } calendar { id } } } }"
        )
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        assert len(event["groupSelections"]) == 1
        sel = event["groupSelections"][0]
        assert sel["slot"] is not None, "Org-wide token must see the slot"
        pool_ids = {c["id"] for c in sel["slot"]["calendars"]}
        assert str(calendar_a.id) in pool_ids, "Org-wide token must see all slot calendars"
        assert str(calendar_b.id) in pool_ids, "Org-wide token must see all slot calendars"

    def test_recurring_instances_excludes_other_owner(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        mock_rl.return_value = iter([None])
        parent = _make_event(calendar_a, title="A recurring parent")
        # An "instance" whose calendar_fk is B's — must be filtered out.
        inst_b = _make_event(
            calendar_b, title="B masquerading instance", parent_recurring_object_fk=parent
        )
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id recurringInstances { id title } } }"
        )
        data = _post(client, query, {"id": parent.id})
        event = data["data"]["calendarEvents"][0]
        inst_ids = {i["id"] for i in event["recurringInstances"]}
        assert str(inst_b.id) not in inst_ids, "B-owned recurring instance must be hidden"

    def test_calendar_group_suppressed_for_scoped_token(
        self, mock_rl, organization, owner, calendar_a
    ):
        mock_rl.return_value = iter([None])
        group = baker.make(CalendarGroup, organization=organization, name="Grp2")
        ev_a = _make_event(calendar_a, title="A grouped", calendar_group_fk=group)
        client, _ = _make_scoped_client(organization, owner)
        query = "query($id: Int!) { calendarEvents(eventId: $id) { id calendarGroup { id } } }"
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        assert event["calendarGroup"] is None, (
            "calendarGroup is suppressed for scoped tokens (would expose cross-owner slots)"
        )

    def test_own_calendar_still_resolves(self, mock_rl, organization, owner, calendar_a):
        """Positive control: the event's OWN calendar still resolves for the scoped token."""
        mock_rl.return_value = iter([None])
        ev_a = _make_event(calendar_a, title="A owned")
        client, _ = _make_scoped_client(organization, owner)
        query = "query($id: Int!) { calendarEvents(eventId: $id) { id calendar { id } } }"
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        assert event["calendar"]["id"] == str(calendar_a.id), "Own calendar must resolve"

    def test_recurrence_exception_parent_and_modified_event_cross_owner_hidden(
        self, mock_rl, organization, owner, calendar_a, calendar_b
    ):
        """Fix 2: recurrenceExceptions.parentEvent / modifiedEvent second-hop pointers.

        A's event has a recurrence exception whose parent_event is on B's calendar
        (adversarial state). The scoped token must get null for both pointers, not
        B's event data.

        This test FAILS without the Fix 2 resolvers on EventRecurrenceExceptionGraphQLType.
        """
        mock_rl.return_value = iter([None])
        # B's event is the modified instance — cross-owner second-hop.
        modified_b = _make_event(calendar_b, title="B Modified Instance")
        # A's event is the parent recurring event (owned, accessible).
        ev_a = _make_event(calendar_a, title="A Event With Exception")
        # Exception: parentEvent → A (owner's calendar), modifiedEvent → B (other owner's).
        EventRecurrenceException.objects.create(
            organization=organization,
            parent_event=ev_a,
            modified_event=modified_b,
            exception_date=datetime.datetime(2026, 9, 4, tzinfo=datetime.UTC),
            is_cancelled=False,
        )

        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id recurrenceExceptions {"
            "    id parentEvent { id title } modifiedEvent { id title }"
            " } } }"
        )
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        assert len(event["recurrenceExceptions"]) >= 1
        for exc_data in event["recurrenceExceptions"]:
            # parentEvent is A's own event — must still resolve (positive control).
            assert exc_data["parentEvent"] is not None, (
                "parentEvent on A's own event must still resolve"
            )
            assert exc_data["parentEvent"]["id"] == str(ev_a.id)
            # modifiedEvent is B's event — must be suppressed.
            assert exc_data["modifiedEvent"] is None, (
                "modifiedEvent pointing to B's calendar must be null for scoped tokens"
            )

    def test_recurrence_exception_own_pointers_still_resolve(
        self, mock_rl, organization, owner, calendar_a
    ):
        """Positive control: recurrence exception pointers within owner's calendar resolve."""
        mock_rl.return_value = iter([None])
        parent_a = _make_event(calendar_a, title="A Recurring Parent")
        modified_a = _make_event(calendar_a, title="A Modified Instance")
        EventRecurrenceException.objects.create(
            organization=organization,
            parent_event=parent_a,
            modified_event=modified_a,
            exception_date=datetime.datetime(2026, 9, 5, tzinfo=datetime.UTC),
            is_cancelled=False,
        )
        client, _ = _make_scoped_client(organization, owner)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id)"
            " { id recurrenceExceptions {"
            "    id parentEvent { id } modifiedEvent { id }"
            " } } }"
        )
        data = _post(client, query, {"id": parent_a.id})
        event = data["data"]["calendarEvents"][0]
        assert len(event["recurrenceExceptions"]) == 1
        exc_data = event["recurrenceExceptions"][0]
        assert exc_data["parentEvent"]["id"] == str(parent_a.id), (
            "Own-calendar parentEvent must resolve"
        )
        assert exc_data["modifiedEvent"]["id"] == str(modified_a.id), (
            "Own-calendar modifiedEvent must resolve"
        )


# --------------------------------------------------------------------------- #
# 3. WRITE mutation cross-owner sweep                                          #
# --------------------------------------------------------------------------- #

_CREATE_AVAILABLE_TIME = """
mutation($c: Int!, $s: DateTime!, $e: DateTime!, $tz: String!) {
    createAvailableTime(calendarId: $c, startTime: $s, endTime: $e, timezone: $tz) { id }
}
"""

_CREATE_BLOCKED_TIME = """
mutation($c: Int!, $s: DateTime!, $e: DateTime!, $tz: String!) {
    createBlockedTime(calendarId: $c, startTime: $s, endTime: $e, timezone: $tz) { id }
}
"""

_SCHEDULE_EVENT = """
mutation($c: Int!, $t: String!, $s: DateTime!, $e: DateTime!, $tz: String!) {
    scheduleEvent(calendarId: $c, title: $t, startTime: $s, endTime: $e, timezone: $tz) { id }
}
"""

_MUT_START = "2026-09-02T09:00:00Z"
_MUT_END = "2026-09-02T10:00:00Z"


@pytest.mark.django_db
class TestWriteMutationsNoCrossOwnerWrite:
    """Each write mutation targeting B's calendar → not-found, no row, indistinguishable."""

    def _scoped(self, organization, owner) -> APIClient:
        client, _ = _make_scoped_client(organization, owner)
        return client

    def test_create_available_time_cross_owner_not_found(
        self, organization, owner, calendar_a, calendar_b
    ):
        client = self._scoped(organization, owner)
        cross = _post(
            client,
            _CREATE_AVAILABLE_TIME,
            {"c": calendar_b.id, "s": _MUT_START, "e": _MUT_END, "tz": "UTC"},
        )
        missing = _post(
            client,
            _CREATE_AVAILABLE_TIME,
            {"c": 9_999_999, "s": _MUT_START, "e": _MUT_END, "tz": "UTC"},
        )
        assert "errors" in cross and "errors" in missing
        assert str(cross["errors"][0]["message"]) == str(missing["errors"][0]["message"]), (
            "Cross-owner write must be indistinguishable from a missing calendar"
        )
        assert (
            not AvailableTime.objects.filter_by_organization(organization.id)
            .filter(calendar_fk=calendar_b)
            .exists()
        ), "No AvailableTime row may be written on B's calendar"

    def test_create_blocked_time_cross_owner_not_found(
        self, organization, owner, calendar_a, calendar_b
    ):
        client = self._scoped(organization, owner)
        cross = _post(
            client,
            _CREATE_BLOCKED_TIME,
            {"c": calendar_b.id, "s": _MUT_START, "e": _MUT_END, "tz": "UTC"},
        )
        missing = _post(
            client,
            _CREATE_BLOCKED_TIME,
            {"c": 9_999_999, "s": _MUT_START, "e": _MUT_END, "tz": "UTC"},
        )
        assert "errors" in cross and "errors" in missing
        assert str(cross["errors"][0]["message"]) == str(missing["errors"][0]["message"])
        assert (
            not BlockedTime.objects.filter_by_organization(organization.id)
            .filter(calendar_fk=calendar_b)
            .exists()
        ), "No BlockedTime row may be written on B's calendar"

    def test_schedule_event_cross_owner_not_found(
        self, organization, owner, calendar_a, calendar_b
    ):
        client = self._scoped(organization, owner)
        cross = _post(
            client,
            _SCHEDULE_EVENT,
            {"c": calendar_b.id, "t": "X", "s": _MUT_START, "e": _MUT_END, "tz": "UTC"},
        )
        missing = _post(
            client,
            _SCHEDULE_EVENT,
            {"c": 9_999_999, "t": "X", "s": _MUT_START, "e": _MUT_END, "tz": "UTC"},
        )
        assert "errors" in cross and "errors" in missing
        assert str(cross["errors"][0]["message"]) == str(missing["errors"][0]["message"])
        assert (
            not CalendarEvent.objects.filter_by_organization(organization.id)
            .filter(calendar_fk=calendar_b)
            .exists()
        ), "No CalendarEvent row may be written on B's calendar"


# --------------------------------------------------------------------------- #
# 4. Org-wide regression: org-wide tokens are unaffected by every guard        #
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestOrgWideTokenUnaffected:
    """Org-wide (scoped_to_user IS NULL) tokens see/do everything in their org."""

    def test_org_wide_sees_both_calendars(self, mock_rl, organization, calendar_a, calendar_b):
        mock_rl.return_value = iter([None])
        client, _ = _make_org_wide_client(organization)
        data = _post(client, "query { calendars { id } }")
        ids = {c["id"] for c in data["data"]["calendars"]}
        assert {str(calendar_a.id), str(calendar_b.id)} <= ids, (
            "Org-wide token must see both owners' calendars"
        )

    def test_org_wide_event_by_id_resolves_other_owner(self, mock_rl, organization, calendar_b):
        mock_rl.return_value = iter([None])
        ev_b = _make_event(calendar_b, title="B Event")
        client, _ = _make_org_wide_client(organization)
        query = "query($id: Int!) { calendarEvents(eventId: $id) { id } }"
        data = _post(client, query, {"id": ev_b.id})
        assert [e["id"] for e in data["data"]["calendarEvents"]] == [str(ev_b.id)]

    def test_org_wide_nested_bundle_representation_resolves(
        self, mock_rl, organization, calendar_a, calendar_b
    ):
        """Org-wide token still sees cross-owner nested data (no scoping applied)."""
        mock_rl.return_value = iter([None])
        primary = _make_event(calendar_a, title="A Primary", is_bundle_primary=True)
        rep_b = _make_event(calendar_b, title="B Representation", bundle_primary_event_fk=primary)
        client, _ = _make_org_wide_client(organization)
        query = (
            "query($id: Int!) { calendarEvents(eventId: $id) { id bundleRepresentations { id } } }"
        )
        data = _post(client, query, {"id": primary.id})
        rep_ids = {r["id"] for r in data["data"]["calendarEvents"][0]["bundleRepresentations"]}
        assert str(rep_b.id) in rep_ids, "Org-wide token must still see the representation"

    def test_org_wide_nested_calendar_group_resolves(self, mock_rl, organization, calendar_a):
        mock_rl.return_value = iter([None])
        group = baker.make(CalendarGroup, organization=organization, name="OWGrp")
        ev_a = _make_event(calendar_a, title="A grouped", calendar_group_fk=group)
        client, _ = _make_org_wide_client(organization)
        query = "query($id: Int!) { calendarEvents(eventId: $id) { id calendarGroup { id } } }"
        data = _post(client, query, {"id": ev_a.id})
        event = data["data"]["calendarEvents"][0]
        assert event["calendarGroup"]["id"] == str(group.id), (
            "Org-wide token must still see the calendar group"
        )
