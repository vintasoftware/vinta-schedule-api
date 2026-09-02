"""Calendar Pools Phase 6: the stale-selection sweep query.

``CalendarGroupService.find_stale_selections`` lists every `(event, slot,
calendar)` triple booked under a group whose calendar has since left its
slot's roster -- the exact predicate Phase 2 already surfaced per-selection
as ``is_in_current_roster``: no ``CalendarGroupSlotMembership`` row exists for
the selection's ``(slot, calendar)`` pair, regardless of whether that row was
inline or projected from a ``CalendarPool`` (Phase 3). This phase adds the
ops-sweep counterpart -- list the whole backlog for a group in one query --
on the service, REST, and public GraphQL surfaces.

Covers:
- The service returns exactly the stale triples, excludes fully-rostered
  events, honours the date window, returns nothing for an untouched group,
  and reports a calendar that departed a POOL (not an inline roster) as
  stale -- proving the single predicate covers both origins.
- Query-count invariance: the same query, measured at two stale-selection
  counts, costs the same number of round trips.
- REST (``CalendarGroupViewSet.stale_selections``): reachable by a
  participating member, honours the window, 401 for anonymous, 404 for a
  same-org non-participant (REST has no "wrong resource" axis -- the
  permission gate is participant vs not, verified by DB state, not just the
  status code).
- GraphQL (``calendarGroupStaleSelections``): org-wide sees a group's
  backlog, a scoped-member sees it only for groups it participates in (empty,
  not an error, for one it does not), refused for both an anonymous caller
  and a token missing the ``CALENDAR_GROUP`` resource, and every new field
  name is present in ``FIELD_TO_RESOURCE_MAPPING``.
"""

from __future__ import annotations

import datetime
import json
import uuid

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_ownership, create_calendar_pool
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarPoolMembership,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import grant_membership_groups, make_membership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.permissions import OrganizationResourceAccess
from public_api.services import PublicAPIAuthService
from users.factories import UserFactory
from users.models import User


_DEFAULT_START = datetime.datetime(2026, 10, 1, 9, 0)


# ---------------------------------------------------------------------------
# Shared, fixture-free helpers (mirror test_stale_selection_flag.py)
# ---------------------------------------------------------------------------


def _make_org() -> Organization:
    return Organization.objects.create(
        name=f"Stale Sweep Org {uuid.uuid4().hex[:8]}", should_sync_rooms=False
    )


def _make_calendar(org: Organization, label: str) -> Calendar:
    return Calendar.objects.create(
        organization=org,
        name=label,
        external_id=f"{label}-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


def _make_event(
    org: Organization,
    calendar: Calendar,
    group: CalendarGroup,
    start: datetime.datetime = _DEFAULT_START,
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        organization=org,
        calendar=calendar,
        title="Visit",
        description="",
        external_id=f"ev-{uuid.uuid4().hex[:8]}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=30),
        timezone="UTC",
        calendar_group=group,
    )


def _select(
    org: Organization, event: CalendarEvent, slot: CalendarGroupSlot, calendar: Calendar
) -> CalendarEventGroupSelection:
    return CalendarEventGroupSelection.objects.create(
        organization=org, event=event, slot=slot, calendar=calendar
    )


def _drop_membership(org: Organization, slot: CalendarGroupSlot, calendar: Calendar) -> None:
    """Make `calendar` leave `slot`'s roster -- simulates the state Phase 1's
    lenient removal makes reachable, without going through the full
    ``update_group`` reconcile (the query under test does not care how the
    row disappeared, only that it did)."""
    CalendarGroupSlotMembership.objects.filter_by_organization(org.id).filter(
        slot=slot, calendar=calendar
    ).delete()


def _make_group_with_service(
    org: Organization, *, slot_name: str = "Physicians", calendar_ids: list[int], pool_ids=None
) -> tuple[CalendarGroupService, CalendarGroup, CalendarGroupSlot]:
    service = CalendarGroupService()
    service.initialize(organization=org)
    group = service.create_group(
        CalendarGroupInputData(
            name=f"Clinic {uuid.uuid4().hex[:6]}",
            slots=[
                CalendarGroupSlotInputData(
                    name=slot_name,
                    calendar_ids=calendar_ids,
                    pool_ids=pool_ids,
                    required_count=1,
                ),
            ],
        )
    )
    slot = group.slots.get(name=slot_name)
    return service, group, slot


# ---------------------------------------------------------------------------
# Service: correctness
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFindStaleSelectionsService:
    def test_returns_exactly_departed_calendars_excludes_fully_rostered(self):
        org = _make_org()
        phys_a = _make_calendar(org, "Dr A")
        phys_b = _make_calendar(org, "Dr B")
        service, group, slot = _make_group_with_service(org, calendar_ids=[phys_a.id, phys_b.id])

        rostered_event = _make_event(org, phys_a, group)
        _select(org, rostered_event, slot, phys_a)

        stale_event = _make_event(org, phys_b, group)
        _select(org, stale_event, slot, phys_b)

        _drop_membership(org, slot, phys_b)

        results = service.find_stale_selections(group_id=group.id)
        assert [(r.event_id, r.slot_id, r.calendar_id) for r in results] == [
            (stale_event.id, slot.id, phys_b.id)
        ]

    def test_honours_the_date_window(self):
        org = _make_org()
        phys_b = _make_calendar(org, "Dr B")
        service, group, slot = _make_group_with_service(org, calendar_ids=[phys_b.id])

        early_event = _make_event(org, phys_b, group, datetime.datetime(2026, 1, 1, 9, 0))
        _select(org, early_event, slot, phys_b)
        late_event = _make_event(org, phys_b, group, datetime.datetime(2026, 12, 1, 9, 0))
        _select(org, late_event, slot, phys_b)

        _drop_membership(org, slot, phys_b)

        window_start = datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC)
        window_end = datetime.datetime(2026, 12, 31, tzinfo=datetime.UTC)
        results = service.find_stale_selections(
            group_id=group.id, window_start=window_start, window_end=window_end
        )
        assert [r.event_id for r in results] == [late_event.id]

        # No window: both stale selections are returned.
        unbounded_results = service.find_stale_selections(group_id=group.id)
        assert {r.event_id for r in unbounded_results} == {early_event.id, late_event.id}

    def test_returns_nothing_for_a_group_whose_rosters_never_changed(self):
        org = _make_org()
        phys_a = _make_calendar(org, "Dr A")
        service, group, slot = _make_group_with_service(org, calendar_ids=[phys_a.id])

        event = _make_event(org, phys_a, group)
        _select(org, event, slot, phys_a)

        assert service.find_stale_selections(group_id=group.id) == []

    def test_calendar_that_left_a_pool_is_reported_stale(self):
        """The projection means one predicate covers both origins: this pins
        the POOL half (Phase 3 projects a pool's roster into the same
        ``CalendarGroupSlotMembership`` table an inline calendar uses), the
        sibling correctness test above already pins the inline half."""
        org = _make_org()
        phys_a = _make_calendar(org, "Dr A")
        nurse = _make_calendar(org, "Nurse")
        pool = create_calendar_pool(organization=org, name="Nurses", calendars=[nurse])
        service, group, slot = _make_group_with_service(
            org, calendar_ids=[phys_a.id], pool_ids=[pool.id]
        )

        # Sanity: the pool's calendar really did get projected into the slot.
        assert (
            CalendarGroupSlotMembership.objects.filter_by_organization(org.id)
            .filter(slot=slot, calendar=nurse)
            .exists()
        )

        event = _make_event(org, nurse, group)
        _select(org, event, slot, nurse)

        # The nurse leaves the POOL -- not the slot -- which reprojects via
        # calendar_integration.signals.reconcile_pools and deletes the row.
        CalendarPoolMembership.objects.filter_by_organization(org.id).filter(
            pool=pool, calendar=nurse
        ).delete()
        assert not (
            CalendarGroupSlotMembership.objects.filter_by_organization(org.id)
            .filter(slot=slot, calendar=nurse)
            .exists()
        )

        results = service.find_stale_selections(group_id=group.id)
        assert [(r.event_id, r.slot_id, r.calendar_id) for r in results] == [
            (event.id, slot.id, nurse.id)
        ]

    def test_cross_organization_group_id_not_found(self):
        org = _make_org()
        other_org = _make_org()
        other_cal = _make_calendar(other_org, "Foreign")
        _other_service, other_group, _other_slot = _make_group_with_service(
            other_org, calendar_ids=[other_cal.id]
        )

        service = CalendarGroupService()
        service.initialize(organization=org)
        with pytest.raises(CalendarGroup.DoesNotExist):
            service.find_stale_selections(group_id=other_group.id)


# ---------------------------------------------------------------------------
# Service: query-count invariance
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFindStaleSelectionsQueryCount:
    def _make_group_with_n_stale(
        self, org: Organization, n: int
    ) -> tuple[CalendarGroupService, CalendarGroup]:
        calendars = [_make_calendar(org, f"Dr {uuid.uuid4().hex[:6]}") for _ in range(n)]
        service, group, slot = _make_group_with_service(org, calendar_ids=[c.id for c in calendars])
        for cal in calendars:
            event = _make_event(org, cal, group)
            _select(org, event, slot, cal)
            _drop_membership(org, slot, cal)
        return service, group

    def test_query_count_independent_of_result_size(self):
        org = _make_org()

        small_service, small_group = self._make_group_with_n_stale(org, 1)
        with CaptureQueriesContext(connection) as small_ctx:
            small_results = small_service.find_stale_selections(group_id=small_group.id)
        assert len(small_results) == 1

        big_service, big_group = self._make_group_with_n_stale(org, 20)
        with CaptureQueriesContext(connection) as big_ctx:
            big_results = big_service.find_stale_selections(group_id=big_group.id)
        assert len(big_results) == 20

        small_count = len(small_ctx.captured_queries)
        big_count = len(big_ctx.captured_queries)
        assert small_count == big_count, (
            f"N+1: {small_count} queries for 1 stale selection vs {big_count} for 20"
        )


# ---------------------------------------------------------------------------
# REST: calendar-groups/{id}/stale-selections/
# ---------------------------------------------------------------------------


def _assert_status(response, expected):
    assert response.status_code == expected, (
        f"{response.status_code} != {expected}\n"
        f"Response: {json.dumps(response.json() if response.content else {}, indent=2, default=str)}"
    )


@pytest.fixture
def organization(user):
    org = baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")
    baker.make(OrganizationMembership, user=user, organization=org)
    return org


@pytest.fixture
def admin_user(user, organization):
    membership = OrganizationMembership.objects.get(user=user, organization=organization)
    grant_membership_groups(membership, [GROUP_ORGANIZATION_ADMIN])
    return user


@pytest.fixture
def rest_calendars(organization):
    return {
        "phys_a": _make_calendar(organization, "Dr A"),
        "phys_b": _make_calendar(organization, "Dr B"),
    }


@pytest.fixture
def rest_group_with_stale_selection(user, organization, rest_calendars):
    """A group `user` participates in (owns phys_a), with one rostered
    selection (phys_a) and one stale selection (phys_b, since removed)."""
    create_calendar_ownership(calendar=rest_calendars["phys_a"], user=user)
    _service, group, slot = _make_group_with_service(
        organization,
        calendar_ids=[rest_calendars["phys_a"].id, rest_calendars["phys_b"].id],
    )
    rostered_event = _make_event(organization, rest_calendars["phys_a"], group)
    _select(organization, rostered_event, slot, rest_calendars["phys_a"])
    stale_event = _make_event(organization, rest_calendars["phys_b"], group)
    _select(organization, stale_event, slot, rest_calendars["phys_b"])
    _drop_membership(organization, slot, rest_calendars["phys_b"])
    return group, slot, stale_event, rest_calendars["phys_b"]


@pytest.mark.django_db
class TestStaleSelectionsRest:
    def test_returns_exactly_the_stale_triple(self, auth_client, rest_group_with_stale_selection):
        group, slot, stale_event, stale_calendar = rest_group_with_stale_selection
        url = reverse("api:CalendarGroups-stale-selections", kwargs={"pk": group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data == [
            {"event_id": stale_event.id, "slot_id": slot.id, "calendar_id": stale_calendar.id}
        ]

    def test_honours_the_date_window(self, auth_client, user, organization, rest_calendars):
        # Own phys_a too, and never drop its membership: `only_member_of`
        # scopes on CURRENTLY rostered calendars, so without a surviving
        # anchor the user would lose visibility into the group the moment
        # phys_b (the only calendar they'd otherwise own here) goes stale.
        create_calendar_ownership(calendar=rest_calendars["phys_a"], user=user)
        _service, group, slot = _make_group_with_service(
            organization,
            calendar_ids=[rest_calendars["phys_a"].id, rest_calendars["phys_b"].id],
        )
        early = _make_event(
            organization, rest_calendars["phys_b"], group, datetime.datetime(2026, 1, 1, 9, 0)
        )
        _select(organization, early, slot, rest_calendars["phys_b"])
        late = _make_event(
            organization, rest_calendars["phys_b"], group, datetime.datetime(2026, 12, 1, 9, 0)
        )
        _select(organization, late, slot, rest_calendars["phys_b"])
        _drop_membership(organization, slot, rest_calendars["phys_b"])

        url = reverse("api:CalendarGroups-stale-selections", kwargs={"pk": group.id})
        response = auth_client.get(
            url,
            {"window_start": "2026-11-01T00:00:00Z", "window_end": "2026-12-31T00:00:00Z"},
        )
        _assert_status(response, status.HTTP_200_OK)
        assert [row["event_id"] for row in response.data] == [late.id]

    def test_unauthenticated_refused(self, anonymous_client, rest_group_with_stale_selection):
        group, _slot, stale_event, stale_calendar = rest_group_with_stale_selection
        url = reverse("api:CalendarGroups-stale-selections", kwargs={"pk": group.id})
        response = anonymous_client.get(url)
        _assert_status(response, status.HTTP_401_UNAUTHORIZED)
        # The row genuinely exists -- the refusal is the auth gate, not an
        # accident of the fixture producing no data.
        assert (
            CalendarEventGroupSelection.objects.filter_by_organization(group.organization_id)
            .filter(event_fk=stale_event, calendar_fk=stale_calendar)
            .exists()
        )

    def test_non_participant_member_gets_404(self, auth_client, user, organization, rest_calendars):
        """Same-org group `user` does not participate in -- REST has no
        resource-scope axis, so the fail-closed gate here is participant vs
        not, enforced by `get_queryset()`: absent from the queryset, 404
        rather than 403 (matches `CalendarGroupViewSet`'s existing contract).
        """
        _service, foreign_group, foreign_slot = _make_group_with_service(
            organization, calendar_ids=[rest_calendars["phys_b"].id]
        )
        event = _make_event(organization, rest_calendars["phys_b"], foreign_group)
        _select(organization, event, foreign_slot, rest_calendars["phys_b"])
        _drop_membership(organization, foreign_slot, rest_calendars["phys_b"])
        # Prove the stale row is really there -- the 404 below must be the
        # permission gate, not an empty fixture.
        assert (
            CalendarEventGroupSelection.objects.filter_by_organization(organization.id)
            .filter(event_fk=event)
            .exists()
        )

        url = reverse("api:CalendarGroups-stale-selections", kwargs={"pk": foreign_group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_404_NOT_FOUND)

    def test_scoped_member_sees_stale_selections_only_for_participating_groups(
        self, auth_client, user, organization, rest_calendars, rest_group_with_stale_selection
    ):
        group, slot, stale_event, stale_calendar = rest_group_with_stale_selection

        foreign_cal = _make_calendar(organization, "Foreign")
        _service, foreign_group, foreign_slot = _make_group_with_service(
            organization, calendar_ids=[foreign_cal.id]
        )
        foreign_event = _make_event(organization, foreign_cal, foreign_group)
        _select(organization, foreign_event, foreign_slot, foreign_cal)
        _drop_membership(organization, foreign_slot, foreign_cal)

        own_url = reverse("api:CalendarGroups-stale-selections", kwargs={"pk": group.id})
        own_response = auth_client.get(own_url)
        _assert_status(own_response, status.HTTP_200_OK)
        assert own_response.data == [
            {"event_id": stale_event.id, "slot_id": slot.id, "calendar_id": stale_calendar.id}
        ]

        foreign_url = reverse(
            "api:CalendarGroups-stale-selections", kwargs={"pk": foreign_group.id}
        )
        foreign_response = auth_client.get(foreign_url)
        _assert_status(foreign_response, status.HTTP_404_NOT_FOUND)

    def test_admin_sees_stale_selections_for_a_group_they_do_not_participate_in(
        self, auth_client, admin_user, organization, rest_calendars
    ):
        _service, group, slot = _make_group_with_service(
            organization, calendar_ids=[rest_calendars["phys_b"].id]
        )
        event = _make_event(organization, rest_calendars["phys_b"], group)
        _select(organization, event, slot, rest_calendars["phys_b"])
        _drop_membership(organization, slot, rest_calendars["phys_b"])

        url = reverse("api:CalendarGroups-stale-selections", kwargs={"pk": group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data == [
            {
                "event_id": event.id,
                "slot_id": slot.id,
                "calendar_id": rest_calendars["phys_b"].id,
            }
        ]


# ---------------------------------------------------------------------------
# GraphQL: calendarGroupStaleSelections
# ---------------------------------------------------------------------------


STALE_SELECTIONS_QUERY = """
query StaleSelections($groupId: Int!) {
    calendarGroupStaleSelections(groupId: $groupId) {
        eventId
        slotId
        calendarId
    }
}
"""

STALE_SELECTIONS_WINDOWED_QUERY = """
query StaleSelections($groupId: Int!, $windowStart: DateTime, $windowEnd: DateTime) {
    calendarGroupStaleSelections(
        groupId: $groupId, windowStart: $windowStart, windowEnd: $windowEnd
    ) {
        eventId
    }
}
"""


def _org_wide_token(org: Organization, resources: list[str]):
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def _scoped_token(org: Organization, membership: OrganizationMembership, resources: list[str]):
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
        organization=org,
        scoped_to_membership=membership,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def _post(client, query, system_user, token, auth_service, variables):
    from di_core.containers import container

    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": variables},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


def _post_anon(client, query, variables):
    return client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )


@pytest.mark.django_db
class TestCalendarGroupStaleSelectionsGraphQL:
    def setup_method(self):
        self.client = APIClient()

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _member(
        self, org: Organization, *, groups: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER,)
    ) -> tuple[User, OrganizationMembership]:
        unique = uuid.uuid4().hex[:8]
        member = UserFactory().create_user()
        member.email = f"member_{unique}@example.com"
        member.save(update_fields=["email"])
        membership = make_membership(user=member, organization=org, groups=groups, is_active=True)
        return member, membership

    def _make_group_with_stale_selection(
        self, org: Organization, *, owner=None
    ) -> tuple[CalendarGroup, CalendarGroupSlot, CalendarEvent, Calendar]:
        """A group with two rostered calendars: `anchor` stays rostered for
        the whole test (so `owner`, if given, remains a `only_member_of`
        participant), `calendar` is the one whose membership is dropped,
        producing exactly one stale selection. Without the anchor, an owner
        who only owned the calendar that goes stale would lose visibility
        into the group at the same moment -- `only_member_of` scopes on
        CURRENTLY rostered calendars, not on selection history.
        """
        anchor = _make_calendar(org, "Anchor")
        calendar = _make_calendar(org, "Dr A")
        if owner is not None:
            create_calendar_ownership(calendar=anchor, user=owner)
        _service, group, slot = _make_group_with_service(org, calendar_ids=[anchor.id, calendar.id])
        event = _make_event(org, calendar, group)
        _select(org, event, slot, calendar)
        _drop_membership(org, slot, calendar)
        return group, slot, event, calendar

    def test_org_wide_sees_stale_selections_for_the_group(self):
        org = self._org()
        group, slot, event, calendar = self._make_group_with_stale_selection(org)
        system_user, token, auth = _org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])

        response = _post(
            self.client, STALE_SELECTIONS_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarGroupStaleSelections"]
        assert rows == [{"eventId": event.id, "slotId": slot.id, "calendarId": calendar.id}]

    def test_honours_the_date_window(self):
        org = self._org()
        calendar = _make_calendar(org, "Dr A")
        _service, group, slot = _make_group_with_service(org, calendar_ids=[calendar.id])
        early = _make_event(org, calendar, group, datetime.datetime(2026, 1, 1, 9, 0))
        _select(org, early, slot, calendar)
        late = _make_event(org, calendar, group, datetime.datetime(2026, 12, 1, 9, 0))
        _select(org, late, slot, calendar)
        _drop_membership(org, slot, calendar)

        system_user, token, auth = _org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])
        response = _post(
            self.client,
            STALE_SELECTIONS_WINDOWED_QUERY,
            system_user,
            token,
            auth,
            {
                "groupId": group.id,
                "windowStart": "2026-11-01T00:00:00Z",
                "windowEnd": "2026-12-31T00:00:00Z",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert [row["eventId"] for row in data["data"]["calendarGroupStaleSelections"]] == [late.id]

    def test_scoped_member_sees_stale_selections_only_for_participating_groups(self):
        org = self._org()
        member, membership = self._member(org)
        group, slot, event, calendar = self._make_group_with_stale_selection(org, owner=member)

        foreign_group, _foreign_slot, _foreign_event, _foreign_calendar = (
            self._make_group_with_stale_selection(org)
        )

        system_user, token, auth = _scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_GROUP]
        )

        own_response = _post(
            self.client, STALE_SELECTIONS_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert own_response.status_code == 200
        own_data = own_response.json()
        assert own_data.get("errors", []) == []
        assert own_data["data"]["calendarGroupStaleSelections"] == [
            {"eventId": event.id, "slotId": slot.id, "calendarId": calendar.id}
        ]

        foreign_response = _post(
            self.client,
            STALE_SELECTIONS_QUERY,
            system_user,
            token,
            auth,
            {"groupId": foreign_group.id},
        )
        assert foreign_response.status_code == 200
        foreign_data = foreign_response.json()
        assert foreign_data.get("errors", []) == []
        # Fail closed, not an error -- the row genuinely exists (proven by
        # the org-wide test above using the identical builder), it's just
        # not visible to a token scoped to a different member.
        assert foreign_data["data"]["calendarGroupStaleSelections"] == []

    def test_scoped_member_inactive_membership_sees_none(self):
        org = self._org()
        member, membership = self._member(org)
        group, _slot, _event, _calendar = self._make_group_with_stale_selection(org, owner=member)

        system_user, token, auth = _scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_GROUP]
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])

        response = _post(
            self.client, STALE_SELECTIONS_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarGroupStaleSelections"] == []

    def test_unauthenticated_refused(self):
        org = self._org()
        group, _slot, event, calendar = self._make_group_with_stale_selection(org)

        response = _post_anon(self.client, STALE_SELECTIONS_QUERY, {"groupId": group.id})
        assert response.status_code == 200
        data = response.json()
        assert data["data"] is None
        assert data["errors"][0]["message"] == "You must be authenticated to access this resource."
        # The row genuinely exists -- confirms the refusal is the auth gate.
        assert (
            CalendarEventGroupSelection.objects.filter_by_organization(org.id)
            .filter(event_fk_id=event.id, calendar_fk_id=calendar.id)
            .exists()
        )

    def test_wrong_resource_token_refused(self):
        org = self._org()
        group, _slot, _event, _calendar = self._make_group_with_stale_selection(org)
        # Holds CALENDAR_POOL but not the CALENDAR_GROUP this field is mapped to.
        system_user, token, auth = _org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = _post(
            self.client, STALE_SELECTIONS_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"] is None
        assert data["errors"][0]["message"] == "You don't have access to query this resource."

    def _query_count(self, org, group_id, n, system_user, token, auth):
        with CaptureQueriesContext(connection) as ctx:
            response = _post(
                self.client, STALE_SELECTIONS_QUERY, system_user, token, auth, {"groupId": group_id}
            )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert len(data["data"]["calendarGroupStaleSelections"]) == n
        return len(ctx.captured_queries)

    def test_query_count_independent_of_result_size(self):
        org = self._org()

        def _make_group_with_n_stale(n: int) -> CalendarGroup:
            calendars = [_make_calendar(org, f"Dr {uuid.uuid4().hex[:6]}") for _ in range(n)]
            _service, group, slot = _make_group_with_service(
                org, calendar_ids=[c.id for c in calendars]
            )
            for cal in calendars:
                event = _make_event(org, cal, group)
                _select(org, event, slot, cal)
                _drop_membership(org, slot, cal)
            return group

        system_user, token, auth = _org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])

        small_group = _make_group_with_n_stale(1)
        small = self._query_count(org, small_group.id, 1, system_user, token, auth)

        big_group = _make_group_with_n_stale(20)
        big = self._query_count(org, big_group.id, 20, system_user, token, auth)

        assert small == big, f"N+1: {small} queries for 1 stale selection vs {big} for 20"


class TestStaleSelectionsFieldMapped:
    def test_field_is_present_in_resource_mapping(self):
        mapped = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING
        assert "calendarGroupStaleSelections" in mapped
        assert mapped["calendarGroupStaleSelections"] == PublicAPIResources.CALENDAR_GROUP
