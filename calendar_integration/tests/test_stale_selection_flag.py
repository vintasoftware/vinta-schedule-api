"""Calendar Pools Phase 2: surface stale calendar selections on events.

A ``CalendarEventGroupSelection`` is stale when no ``CalendarGroupSlotMembership``
row exists for its ``(slot, calendar)`` pair -- the exact predicate named by the
plan's Staleness definition. Phase 1 made this state reachable (roster removal no
longer deletes grandfathered selections); this phase adds a read-only
``is_in_current_roster`` boolean on both the REST serializer
(``CalendarEventGroupSelectionSerializer``) and the public GraphQL type
(``CalendarEventGroupSelectionGraphQLType``) so a client can warn instead of
silently re-offering a calendar it cannot re-add.

Covers:
- The flag is true for a rostered calendar, false once it is removed from the
  slot, and true again once it is re-added -- on both surfaces.
- Query-count invariance: rendering one selection costs the same number of
  queries as rendering five, on both the REST serializer path (via
  ``CalendarEventGroupSelectionVirtualModel``'s prefetch) and the GraphQL path
  (via the batched lookup in ``group_selections``), including a variant where
  the selections span multiple distinct slots rather than one.
- The nested ``group_selections`` field on ``CalendarEventSerializer`` (the
  REST field that actually makes ``is_in_current_roster`` reachable) is
  constant-query on the event list endpoint.
"""

from __future__ import annotations

import datetime
import json
import uuid
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.serializers import CalendarEventGroupSelectionSerializer
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import grant_membership_groups
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_org() -> Organization:
    return Organization.objects.create(
        name=f"Stale Selection Org {uuid.uuid4().hex[:8]}", should_sync_rooms=False
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


def _make_group_with_slot(
    org: Organization, *, required_count: int = 1
) -> tuple[CalendarGroup, CalendarGroupSlot]:
    group = CalendarGroup.objects.create(organization=org, name=f"Clinic {uuid.uuid4().hex[:8]}")
    slot = CalendarGroupSlot.objects.create(
        organization=org, group=group, name="Physicians", required_count=required_count
    )
    return group, slot


def _make_group_with_distinct_slots(
    org: Organization, *, slot_count: int = 3
) -> tuple[CalendarGroup, list[CalendarGroupSlot], list[Calendar]]:
    """A group with ``slot_count`` distinct slots, each with its own rostered calendar.

    Used by the multi-slot query-count variants: a selection landing on each
    slot exercises ``_attach_current_roster_flags``'s ``slot_fk_id__in=slot_ids``
    batching (GraphQL) and the REST ``hints.Virtual`` prefetch across more than
    one slot, which the single-slot variants next to these do not.
    """
    group = CalendarGroup.objects.create(organization=org, name=f"Clinic {uuid.uuid4().hex[:8]}")
    slots = [
        CalendarGroupSlot.objects.create(
            organization=org, group=group, name=f"Slot {i}", required_count=1
        )
        for i in range(slot_count)
    ]
    calendars = [_make_calendar(org, f"Dr {i}") for i in range(slot_count)]
    for slot, calendar in zip(slots, calendars, strict=True):
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)
    return group, slots, calendars


def _make_event(org: Organization, calendar: Calendar, group: CalendarGroup) -> CalendarEvent:
    return CalendarEvent.objects.create(
        organization=org,
        calendar=calendar,
        title="Visit",
        description="",
        external_id=f"ev-{uuid.uuid4().hex[:8]}",
        start_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2026, 10, 1, 9, 30),
        timezone="UTC",
        calendar_group=group,
    )


def _make_org_with_member() -> tuple[Organization, OrganizationMembership]:
    """A fresh org plus an active, group-carrying membership for REST auth."""
    org = _make_org()
    user = UserFactory().create_user()
    membership = grant_membership_groups(
        OrganizationMembership.objects.create(user=user, organization=org, is_active=True),
        [GROUP_ORGANIZATION_MEMBER],
    )
    return org, membership


def _rest_render_selections(queryset) -> list[dict[str, object]]:
    """Render ``queryset`` through the optimized-queryset path, matching how
    every other ``VirtualModelSerializer`` consumer in this codebase renders a
    list (see e.g. ``calendar_integration/views.py``'s
    ``Serializer(context=...).get_optimized_queryset(qs)`` call sites).

    Rendering a bare (unprefetched) instance directly would trip the
    serializer's own query-count guard -- ``VirtualModelSerializerMixin``
    defaults ``max_queries_count`` to 0 and raises on any un-hinted query
    issued while rendering, under ``DEBUG=True`` (set for tests, see
    ``pytest.ini``'s ``django_debug_mode``).
    """
    serializer = CalendarEventGroupSelectionSerializer()
    optimized = serializer.get_optimized_queryset(queryset)
    rendered = CalendarEventGroupSelectionSerializer(optimized, many=True).data
    # djangorestframework-stubs types `.data` as `ReturnDict[Any, Any]` regardless of
    # `many=True`, which actually returns a `ReturnList` of dicts at runtime.
    return rendered  # type: ignore[return-value]


def _rest_is_in_current_roster(selection: CalendarEventGroupSelection, org: Organization) -> bool:
    with organization_context(org):
        rendered = _rest_render_selections(
            CalendarEventGroupSelection.objects.filter(id=selection.id)
        )
    assert len(rendered) == 1
    return bool(rendered[0]["is_in_current_roster"])


def _make_org_wide_system_user(org: Organization):
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"stale-selection-{uuid.uuid4().hex[:8]}",
        organization=org,
    )
    ResourceAccess.objects.create(
        system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    return system_user, token


def assert_graphql_success(response) -> dict:
    assert response.status_code == 200, response.content.decode()
    data = response.json()
    assert not data.get("errors"), f"GraphQL errors: {data.get('errors')}"
    assert data.get("data") is not None, data
    return data["data"]


_CALENDAR_EVENT_WITH_ROSTER_FLAG = """
query CalendarEvents($eventId: Int) {
    calendarEvents(eventId: $eventId) {
        id
        groupSelections {
            calendar { id }
            isInCurrentRoster
        }
    }
}
"""

# Query-count-only variant: no ``calendar { id }``. That field's per-selection
# calendar lookup is a pre-existing N+1 on ``CalendarEventGroupSelectionGraphQLType
# .calendar`` unrelated to this phase (it resolves ``self.calendar``, an
# unprefetched forward relation, once per selection regardless of
# ``isInCurrentRoster``) -- including it here would make the "same query count for
# 1 vs 5 selections" assertion fail for a reason this phase does not own. Isolating
# the query this way measures exactly what ``isInCurrentRoster`` costs.
_CALENDAR_EVENT_ROSTER_FLAG_ONLY = """
query CalendarEvents($eventId: Int) {
    calendarEvents(eventId: $eventId) {
        id
        groupSelections {
            isInCurrentRoster
        }
    }
}
"""


def _post_graphql(client: APIClient, system_user, token: str, event_id: int, query: str):
    return client.post(
        "/graphql/",
        data=json.dumps({"query": query, "variables": {"eventId": event_id}}),
        content_type="application/json",
        headers={"authorization": f"Bearer {system_user.id}:{token}"},
    )


def _graphql_selection_flags(event_id: int, system_user, token: str) -> list[bool]:
    client = APIClient()
    response = _post_graphql(client, system_user, token, event_id, _CALENDAR_EVENT_WITH_ROSTER_FLAG)
    data = assert_graphql_success(response)
    events = data["calendarEvents"]
    assert len(events) == 1
    return [sel["isInCurrentRoster"] for sel in events[0]["groupSelections"]]


# ---------------------------------------------------------------------------
# Correctness: true -> false -> true again, on both surfaces
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIsInCurrentRosterFlagRest:
    def test_true_then_false_then_true_again(self):
        org = _make_org()
        calendar = _make_calendar(org, "Dr A")
        group, slot = _make_group_with_slot(org)
        membership = CalendarGroupSlotMembership.objects.create(
            organization=org, slot=slot, calendar=calendar
        )
        event = _make_event(org, calendar, group)
        selection = CalendarEventGroupSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=calendar
        )

        assert _rest_is_in_current_roster(selection, org) is True

        membership.delete()
        assert _rest_is_in_current_roster(selection, org) is False

        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)
        assert _rest_is_in_current_roster(selection, org) is True


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestIsInCurrentRosterFlagGraphQL:
    def test_true_then_false_then_true_again(self, mock_rate_limiter):
        mock_rate_limiter.return_value = iter([None, None, None])
        org = _make_org()
        calendar = _make_calendar(org, "Dr A")
        group, slot = _make_group_with_slot(org)
        membership = CalendarGroupSlotMembership.objects.create(
            organization=org, slot=slot, calendar=calendar
        )
        event = _make_event(org, calendar, group)
        CalendarEventGroupSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=calendar
        )
        system_user, token = _make_org_wide_system_user(org)

        assert _graphql_selection_flags(event.id, system_user, token) == [True]

        membership.delete()
        assert _graphql_selection_flags(event.id, system_user, token) == [False]

        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)
        assert _graphql_selection_flags(event.id, system_user, token) == [True]


# ---------------------------------------------------------------------------
# Query-count invariance: one selection costs the same as many, on both paths
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIsInCurrentRosterFlagRestQueryCount:
    def test_constant_query_count_regardless_of_selection_count(self, django_assert_num_queries):
        org = _make_org()
        group, slot = _make_group_with_slot(org, required_count=5)
        calendars = [_make_calendar(org, f"Dr {i}") for i in range(5)]
        for calendar in calendars:
            CalendarGroupSlotMembership.objects.create(
                organization=org, slot=slot, calendar=calendar
            )

        one_selection_event = _make_event(org, calendars[0], group)
        CalendarEventGroupSelection.objects.create(
            organization=org, event=one_selection_event, slot=slot, calendar=calendars[0]
        )

        many_selections_event = _make_event(org, calendars[0], group)
        for calendar in calendars:
            CalendarEventGroupSelection.objects.create(
                organization=org, event=many_selections_event, slot=slot, calendar=calendar
            )

        with organization_context(org):
            with CaptureQueriesContext(connection) as one_ctx:
                rendered_one = _rest_render_selections(
                    CalendarEventGroupSelection.objects.filter(event_fk_id=one_selection_event.id)
                )
        assert len(rendered_one) == 1
        query_count = len(one_ctx.captured_queries)

        with organization_context(org):
            with django_assert_num_queries(query_count):
                rendered_many = _rest_render_selections(
                    CalendarEventGroupSelection.objects.filter(event_fk_id=many_selections_event.id)
                )
        assert len(rendered_many) == 5

    def test_constant_query_count_across_multiple_distinct_slots(self, django_assert_num_queries):
        """Selections spanning several slots on one event cost the same as one.

        The single-slot variant above proves invariance with respect to
        selection *count* but keeps every selection against the same slot, so
        it cannot catch a regression that turns the ``hints.Virtual``
        prefetch into one query per distinct slot. Three slots, one selection
        each, closes that gap.
        """
        org = _make_org()
        group, slots, calendars = _make_group_with_distinct_slots(org, slot_count=3)

        one_selection_event = _make_event(org, calendars[0], group)
        CalendarEventGroupSelection.objects.create(
            organization=org, event=one_selection_event, slot=slots[0], calendar=calendars[0]
        )

        many_slots_event = _make_event(org, calendars[0], group)
        for slot, calendar in zip(slots, calendars, strict=True):
            CalendarEventGroupSelection.objects.create(
                organization=org, event=many_slots_event, slot=slot, calendar=calendar
            )

        with organization_context(org):
            with CaptureQueriesContext(connection) as one_ctx:
                rendered_one = _rest_render_selections(
                    CalendarEventGroupSelection.objects.filter(event_fk_id=one_selection_event.id)
                )
        assert len(rendered_one) == 1
        query_count = len(one_ctx.captured_queries)

        with organization_context(org):
            with django_assert_num_queries(query_count):
                rendered_many = _rest_render_selections(
                    CalendarEventGroupSelection.objects.filter(event_fk_id=many_slots_event.id)
                )
        assert len(rendered_many) == 3
        assert all(selection["is_in_current_roster"] for selection in rendered_many)


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestIsInCurrentRosterFlagGraphQLQueryCount:
    def test_constant_query_count_regardless_of_selection_count(
        self, mock_rate_limiter, django_assert_num_queries
    ):
        org = _make_org()
        group, slot = _make_group_with_slot(org, required_count=5)
        calendars = [_make_calendar(org, f"Dr {i}") for i in range(5)]
        for calendar in calendars:
            CalendarGroupSlotMembership.objects.create(
                organization=org, slot=slot, calendar=calendar
            )

        one_selection_event = _make_event(org, calendars[0], group)
        CalendarEventGroupSelection.objects.create(
            organization=org, event=one_selection_event, slot=slot, calendar=calendars[0]
        )

        many_selections_event = _make_event(org, calendars[0], group)
        for calendar in calendars:
            CalendarEventGroupSelection.objects.create(
                organization=org, event=many_selections_event, slot=slot, calendar=calendar
            )

        system_user, token = _make_org_wide_system_user(org)
        client = APIClient()

        mock_rate_limiter.return_value = iter([None])
        with CaptureQueriesContext(connection) as one_ctx:
            response_one = _post_graphql(
                client,
                system_user,
                token,
                one_selection_event.id,
                _CALENDAR_EVENT_ROSTER_FLAG_ONLY,
            )
        data_one = assert_graphql_success(response_one)
        assert len(data_one["calendarEvents"][0]["groupSelections"]) == 1
        query_count = len(one_ctx.captured_queries)

        mock_rate_limiter.return_value = iter([None])
        with django_assert_num_queries(query_count):
            response_many = _post_graphql(
                client,
                system_user,
                token,
                many_selections_event.id,
                _CALENDAR_EVENT_ROSTER_FLAG_ONLY,
            )
        data_many = assert_graphql_success(response_many)
        assert len(data_many["calendarEvents"][0]["groupSelections"]) == 5

    def test_constant_query_count_across_multiple_distinct_slots(
        self, mock_rate_limiter, django_assert_num_queries
    ):
        """Selections spanning several slots on one event cost the same as one.

        Mirrors the REST variant above: proves ``_attach_current_roster_flags``'s
        ``slot_fk_id__in=slot_ids`` batch is genuinely one query regardless of
        how many distinct slots the selections it processes belong to, not
        just how many selections there are on a single slot.
        """
        org = _make_org()
        group, slots, calendars = _make_group_with_distinct_slots(org, slot_count=3)

        one_selection_event = _make_event(org, calendars[0], group)
        CalendarEventGroupSelection.objects.create(
            organization=org, event=one_selection_event, slot=slots[0], calendar=calendars[0]
        )

        many_slots_event = _make_event(org, calendars[0], group)
        for slot, calendar in zip(slots, calendars, strict=True):
            CalendarEventGroupSelection.objects.create(
                organization=org, event=many_slots_event, slot=slot, calendar=calendar
            )

        system_user, token = _make_org_wide_system_user(org)
        client = APIClient()

        mock_rate_limiter.return_value = iter([None])
        with CaptureQueriesContext(connection) as one_ctx:
            response_one = _post_graphql(
                client,
                system_user,
                token,
                one_selection_event.id,
                _CALENDAR_EVENT_ROSTER_FLAG_ONLY,
            )
        data_one = assert_graphql_success(response_one)
        assert len(data_one["calendarEvents"][0]["groupSelections"]) == 1
        query_count = len(one_ctx.captured_queries)

        mock_rate_limiter.return_value = iter([None])
        with django_assert_num_queries(query_count):
            response_many = _post_graphql(
                client,
                system_user,
                token,
                many_slots_event.id,
                _CALENDAR_EVENT_ROSTER_FLAG_ONLY,
            )
        data_many = assert_graphql_success(response_many)
        assert len(data_many["calendarEvents"][0]["groupSelections"]) == 3
        assert (
            data_many["calendarEvents"][0]["groupSelections"] == [{"isInCurrentRoster": True}] * 3
        )


# ---------------------------------------------------------------------------
# The REST wiring: `group_selections` nested on `CalendarEventSerializer`
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupSelectionsFieldOnEventSerializer:
    """``CalendarEventSerializer.group_selections`` is what makes
    ``is_in_current_roster`` reachable over REST: the standalone
    ``CalendarEventGroupSelectionSerializer`` above is nested in nothing and
    mounted on no route, so a client can only see the flag through an event's
    own serialization.
    """

    def test_group_selections_reachable_on_event_list(self):
        org, membership = _make_org_with_member()
        calendar = _make_calendar(org, "Dr A")
        group, slot = _make_group_with_slot(org)
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)
        event = _make_event(org, calendar, group)
        CalendarEventGroupSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=calendar
        )

        client = APIClient()
        client.force_authenticate(user=membership.user)
        response = client.get(reverse("api:CalendarEvents-list"))

        assert response.status_code == 200, response.content
        results = response.data["results"]
        assert len(results) == 1
        selections = results[0]["group_selections"]
        assert len(selections) == 1
        assert selections[0]["is_in_current_roster"] is True


@pytest.mark.django_db
class TestGroupSelectionsFieldOnEventListQueryCount:
    def test_constant_query_count_regardless_of_event_count(self, django_assert_num_queries):
        """The event list endpoint does not gain per-event or per-selection
        queries from nesting ``group_selections``: one event carrying a
        selection costs the same number of queries as five.
        """
        org, membership = _make_org_with_member()
        group, slot = _make_group_with_slot(org, required_count=1)
        calendar = _make_calendar(org, "Dr A")
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)

        def _make_event_with_selection() -> CalendarEvent:
            event = _make_event(org, calendar, group)
            CalendarEventGroupSelection.objects.create(
                organization=org, event=event, slot=slot, calendar=calendar
            )
            return event

        _make_event_with_selection()

        client = APIClient()
        client.force_authenticate(user=membership.user)
        url = reverse("api:CalendarEvents-list")

        with CaptureQueriesContext(connection) as one_ctx:
            response_one = client.get(url)
        assert response_one.status_code == 200, response_one.content
        assert response_one.data["count"] == 1
        query_count = len(one_ctx.captured_queries)

        for _ in range(4):
            _make_event_with_selection()

        with django_assert_num_queries(query_count):
            response_many = client.get(url)
        assert response_many.status_code == 200, response_many.content
        assert response_many.data["count"] == 5
        for result in response_many.data["results"]:
            assert result["group_selections"][0]["is_in_current_roster"] is True
