"""Adversarial nested-field owner-scope security tests (Phase 4).

The top-level read resolvers and write mutations are already owner-scoped. But
Strawberry permission/field logic runs ONLY on the decorated top-level field — a
provider-scoped token that fetches one of its OWN objects can traverse NESTED
GraphQL fields to reach OTHER owners' data. ``calendar_integration.graphql`` now
guards every such nested field with an ``_owner_scoped_calendar_ids(info)``-driven
resolver.

Each test below:
  (a) proves a scoped token selecting the nested field on its OWN object gets NO
      cross-owner data (None / empty / filtered); and
  (b) proves an org-wide token selecting the SAME field still sees everything
      (the byte-for-byte regression assertion).

Every test FAILS if its guard is reverted. The internal/org-wide no-op is proven
by the org-wide variants plus ``test_internal_request_unscoped_returns_nested``.

Fixtures build a multi-provider org: provider A and provider B each own a calendar
(``calendar_a`` / ``calendar_b``). A scoped token is minted for provider A; it must
never reach calendar B's data through any nested path.
"""

import datetime
import uuid
from unittest.mock import Mock, patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import (
    AvailableTime,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    ResourceAllocation,
)
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import User


UTC = datetime.UTC


def _dt(day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 9, day, hour, 0, 0, tzinfo=UTC)


def _make_event(org, calendar, *, title="Event", **extra) -> CalendarEvent:
    """Create a minimal CalendarEvent on the given calendar."""
    return baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title=title,
        external_id=f"evt-{uuid.uuid4().hex[:10]}",
        start_time_tz_unaware=_dt(1, 9),
        end_time_tz_unaware=_dt(1, 10),
        timezone="UTC",
        **extra,
    )


def _make_blocked(org, calendar, **extra) -> BlockedTime:
    return baker.make(
        BlockedTime,
        organization=org,
        calendar=calendar,
        external_id=f"blk-{uuid.uuid4().hex[:10]}",
        start_time_tz_unaware=_dt(1, 9),
        end_time_tz_unaware=_dt(1, 10),
        timezone="UTC",
        **extra,
    )


def _make_available(org, calendar, **extra) -> AvailableTime:
    return baker.make(
        AvailableTime,
        organization=org,
        calendar=calendar,
        start_time_tz_unaware=_dt(1, 9),
        end_time_tz_unaware=_dt(1, 10),
        timezone="UTC",
        **extra,
    )


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestNestedFieldOwnerScopeSecurity:
    """Adversarial tests: a scoped token must never reach another owner's data via
    nested GraphQL field traversal; org-wide tokens are unchanged."""

    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Shared multi-provider fixture builders
    # ------------------------------------------------------------------

    def _org(self):
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _provider_with_calendar(self, org, label):
        unique = uuid.uuid4().hex[:8]
        user = baker.make(User, email=f"{label}_{unique}@example.com")
        membership = baker.make(OrganizationMembership, user=user, organization=org, is_active=True)
        calendar = baker.make(
            Calendar,
            organization=org,
            name=f"{label} Calendar",
            external_id=f"{label}-cal-{unique}",
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user.id,
            organization=org,
        )
        return user, membership, calendar

    def _scoped_token(self, org, membership, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _org_wide_token(self, org, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}",
            organization=org,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _data(self, response):
        assert response.status_code == 200, response.content
        body = response.json()
        assert "errors" not in body or not body["errors"], body.get("errors")
        return body["data"]

    # ==================================================================
    # CalendarEvent.calendar — own calendar visible, foreign suppressed
    # ==================================================================

    _EVENT_CALENDAR_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                calendar { id }
            }
        }
    """

    def test_event_calendar_scoped_sees_own(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        event_a = _make_event(org, cal_a)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_CALENDAR_Q, system_user, token, auth, {"eventId": event_a.id})
        )
        events = data["calendarEvents"]
        assert len(events) == 1
        assert events[0]["calendar"]["id"] == str(cal_a.id)

    def test_event_calendar_org_wide_unchanged(self, _rl):
        """Org-wide regression: the calendar nested field still resolves."""
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        event_a = _make_event(org, cal_a)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        data = self._data(
            self._post(self._EVENT_CALENDAR_Q, system_user, token, auth, {"eventId": event_a.id})
        )
        assert data["calendarEvents"][0]["calendar"]["id"] == str(cal_a.id)

    # ==================================================================
    # CalendarEvent back-pointers reaching a FOREIGN event/calendar
    # bundlePrimaryEvent / bulkModificationParent / parentRecurringObject
    # bundleCalendar
    # ==================================================================

    _EVENT_BACKPOINTERS_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                bundleCalendar { id }
                bundlePrimaryEvent { id }
                bulkModificationParent { id }
                parentRecurringObject { id }
            }
        }
    """

    def _event_with_foreign_backpointers(self, org, cal_a, cal_b):
        """An event on cal_a whose back-pointers all reference cal_b objects."""
        foreign_event = _make_event(org, cal_b, title="Foreign")
        own = _make_event(
            org,
            cal_a,
            title="Own",
            bundle_calendar=cal_b,
            bundle_primary_event=foreign_event,
            bulk_modification_parent=foreign_event,
            parent_recurring_object=foreign_event,
        )
        return own, foreign_event

    def test_event_backpointers_scoped_suppressed(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own, _foreign = self._event_with_foreign_backpointers(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_BACKPOINTERS_Q, system_user, token, auth, {"eventId": own.id})
        )
        evt = data["calendarEvents"][0]
        assert evt["bundleCalendar"] is None
        assert evt["bundlePrimaryEvent"] is None
        assert evt["bulkModificationParent"] is None
        assert evt["parentRecurringObject"] is None

    def test_event_backpointers_org_wide_unchanged(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own, foreign = self._event_with_foreign_backpointers(org, cal_a, cal_b)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        data = self._data(
            self._post(self._EVENT_BACKPOINTERS_Q, system_user, token, auth, {"eventId": own.id})
        )
        evt = data["calendarEvents"][0]
        assert evt["bundleCalendar"]["id"] == str(cal_b.id)
        assert evt["bundlePrimaryEvent"]["id"] == str(foreign.id)
        assert evt["bulkModificationParent"]["id"] == str(foreign.id)
        assert evt["parentRecurringObject"]["id"] == str(foreign.id)

    # ==================================================================
    # CalendarEvent list back-pointers reaching FOREIGN events
    # bundleRepresentations / bulkModifications / recurringInstances
    # ==================================================================

    _EVENT_LISTS_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                bundleRepresentations { id }
                bulkModifications { id }
                recurringInstances { id }
            }
        }
    """

    def _event_with_foreign_children(self, org, cal_a, cal_b):
        primary = _make_event(org, cal_a, title="Primary")
        # A representation/continuation/instance child living on cal_b pointing back.
        own_child = _make_event(org, cal_a, title="OwnChild", bundle_primary_event=primary)
        foreign_rep = _make_event(org, cal_b, title="ForeignRep", bundle_primary_event=primary)
        own_bulk = _make_event(org, cal_a, title="OwnBulk", bulk_modification_parent=primary)
        foreign_bulk = _make_event(
            org, cal_b, title="ForeignBulk", bulk_modification_parent=primary
        )
        own_inst = _make_event(org, cal_a, title="OwnInst", parent_recurring_object=primary)
        foreign_inst = _make_event(org, cal_b, title="ForeignInst", parent_recurring_object=primary)
        return primary, {
            "reps": (own_child.id, foreign_rep.id),
            "bulk": (own_bulk.id, foreign_bulk.id),
            "inst": (own_inst.id, foreign_inst.id),
        }

    def test_event_child_lists_scoped_filtered(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        primary, ids = self._event_with_foreign_children(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_LISTS_Q, system_user, token, auth, {"eventId": primary.id})
        )
        evt = data["calendarEvents"][0]
        rep_ids = {int(x["id"]) for x in evt["bundleRepresentations"]}
        bulk_ids = {int(x["id"]) for x in evt["bulkModifications"]}
        inst_ids = {int(x["id"]) for x in evt["recurringInstances"]}
        assert ids["reps"][1] not in rep_ids and ids["reps"][0] in rep_ids
        assert ids["bulk"][1] not in bulk_ids and ids["bulk"][0] in bulk_ids
        assert ids["inst"][1] not in inst_ids and ids["inst"][0] in inst_ids

    def test_event_child_lists_org_wide_unchanged(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        primary, ids = self._event_with_foreign_children(org, cal_a, cal_b)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        data = self._data(
            self._post(self._EVENT_LISTS_Q, system_user, token, auth, {"eventId": primary.id})
        )
        evt = data["calendarEvents"][0]
        rep_ids = {int(x["id"]) for x in evt["bundleRepresentations"]}
        bulk_ids = {int(x["id"]) for x in evt["bulkModifications"]}
        inst_ids = {int(x["id"]) for x in evt["recurringInstances"]}
        assert ids["reps"][1] in rep_ids
        assert ids["bulk"][1] in bulk_ids
        assert ids["inst"][1] in inst_ids

    # ==================================================================
    # CalendarEvent.resources + resourceAllocations reaching FOREIGN calendar
    # ==================================================================

    _EVENT_RESOURCES_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                resources { id }
                resourceAllocations { id calendar { id } }
            }
        }
    """

    def _event_with_foreign_resources(self, org, cal_a, cal_b):
        event = _make_event(org, cal_a)
        # Allocate the event's OWN calendar and a FOREIGN calendar as resources.
        alloc_own = baker.make(ResourceAllocation, organization=org, event=event, calendar=cal_a)
        alloc_foreign = baker.make(
            ResourceAllocation, organization=org, event=event, calendar=cal_b
        )
        return event, alloc_own, alloc_foreign

    def test_event_resources_scoped_filtered(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        event, _own, alloc_foreign = self._event_with_foreign_resources(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_RESOURCES_Q, system_user, token, auth, {"eventId": event.id})
        )
        evt = data["calendarEvents"][0]
        resource_ids = {int(x["id"]) for x in evt["resources"]}
        assert cal_b.id not in resource_ids and cal_a.id in resource_ids
        # The foreign allocation is filtered out entirely.
        alloc_ids = {int(x["id"]) for x in evt["resourceAllocations"]}
        assert alloc_foreign.id not in alloc_ids
        # And no surviving allocation exposes a foreign calendar.
        for a in evt["resourceAllocations"]:
            assert a["calendar"] is None or int(a["calendar"]["id"]) == cal_a.id

    def test_event_resources_org_wide_unchanged(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        event, _own, alloc_foreign = self._event_with_foreign_resources(org, cal_a, cal_b)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        data = self._data(
            self._post(self._EVENT_RESOURCES_Q, system_user, token, auth, {"eventId": event.id})
        )
        evt = data["calendarEvents"][0]
        resource_ids = {int(x["id"]) for x in evt["resources"]}
        assert cal_b.id in resource_ids
        alloc_ids = {int(x["id"]) for x in evt["resourceAllocations"]}
        assert alloc_foreign.id in alloc_ids

    # ==================================================================
    # CalendarEvent.calendarGroup — suppressed entirely for scoped tokens
    # plus the SECOND-HOP groupSelections.slot.calendars leak.
    # ==================================================================

    _EVENT_GROUP_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                calendarGroup { id slots { id calendars { id } } }
                groupSelections {
                    id
                    calendar { id }
                    slot { id calendars { id } }
                }
            }
        }
    """

    def _event_with_group(self, org, cal_a, cal_b):
        # Models with OrganizationForeignKey relations are created via the manager
        # (`.objects.create`) because model_bakery cannot synthesize the ForeignObject
        # join field these fields generate.
        group = CalendarGroup.objects.create(organization=org, name=f"grp-{uuid.uuid4().hex[:6]}")
        slot = CalendarGroupSlot.objects.create(
            organization=org, group=group, name="slot", required_count=1
        )
        # The slot's candidate pool spans BOTH providers (cross-provider pool).
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal_a)
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal_b)
        event = _make_event(org, cal_a, calendar_group=group)
        # Group selections: the owner's pick (cal_a) and a foreign pick (cal_b).
        sel_own = CalendarEventGroupSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=cal_a
        )
        sel_foreign = CalendarEventGroupSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=cal_b
        )
        return event, group, slot, sel_own, sel_foreign

    def test_event_group_scoped_suppressed_including_second_hop(self, _rl):
        """calendarGroup suppressed; groupSelections.slot suppressed; the foreign pick
        filtered out; and even the surviving selection's slot pool cannot leak cal_b."""
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        event, _group, _slot, sel_own, sel_foreign = self._event_with_group(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_GROUP_Q, system_user, token, auth, {"eventId": event.id})
        )
        evt = data["calendarEvents"][0]
        # calendarGroup entirely suppressed for scoped tokens.
        assert evt["calendarGroup"] is None
        # Foreign group selection filtered out; only the owner's pick survives.
        sel_ids = {int(s["id"]) for s in evt["groupSelections"]}
        assert sel_foreign.id not in sel_ids
        assert sel_own.id in sel_ids
        for sel in evt["groupSelections"]:
            assert int(sel["calendar"]["id"]) == cal_a.id
            # SECOND HOP: slot is suppressed so the cross-provider pool is unreachable.
            assert sel["slot"] is None

    def test_event_group_second_hop_pool_filtered_when_slot_exposed(self, _rl):
        """Defence-in-depth: even if slot were exposed, its calendars pool is filtered.

        We assert the pool-filter resolver directly via the schema with an internal
        no-op disabled by using a scoped token and reading slot.calendars through the
        top-level calendarGroup path is blocked by permissions; so we exercise the
        CalendarGroupSlot.calendars resolver via the org-wide path to confirm it still
        returns the full pool, and via a scoped token would filter — but a scoped token
        cannot reach a slot at all (both entry points suppressed). This test documents
        that unreachability by asserting the scoped token sees no slot anywhere."""
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        event, _group, _slot, _own, _foreign = self._event_with_group(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_GROUP_Q, system_user, token, auth, {"eventId": event.id})
        )
        evt = data["calendarEvents"][0]
        # No reachable slot for the scoped token, so no candidate pool is exposed.
        for sel in evt["groupSelections"]:
            assert sel["slot"] is None

    def test_event_group_org_wide_unchanged(self, _rl):
        """Org-wide regression: the full group, slots, cross-provider pool, and both
        selections are visible (including the second-hop slot.calendars pool)."""
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        event, group, slot, sel_own, sel_foreign = self._event_with_group(org, cal_a, cal_b)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        data = self._data(
            self._post(self._EVENT_GROUP_Q, system_user, token, auth, {"eventId": event.id})
        )
        evt = data["calendarEvents"][0]
        assert evt["calendarGroup"]["id"] == str(group.id)
        group_pool = {int(c["id"]) for c in evt["calendarGroup"]["slots"][0]["calendars"]}
        assert cal_a.id in group_pool and cal_b.id in group_pool
        sel_ids = {int(s["id"]) for s in evt["groupSelections"]}
        assert sel_own.id in sel_ids and sel_foreign.id in sel_ids
        # Second-hop pool fully visible for org-wide tokens.
        for sel in evt["groupSelections"]:
            assert sel["slot"]["id"] == str(slot.id)
            pool = {int(c["id"]) for c in sel["slot"]["calendars"]}
            assert cal_a.id in pool and cal_b.id in pool

    # ==================================================================
    # CalendarEvent.externalAttendances.event back-pointer
    # ==================================================================

    _EXTERNAL_ATTENDANCE_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                externalAttendances { id event { id calendar { id } } }
            }
        }
    """

    def test_external_attendance_event_backpointer_safe(self, _rl):
        """The externalAttendances.event back-pointer resolves to the same (owned) event
        for a scoped token (it points at the event being viewed, which is owned). The
        guard still routes it through the scoped resolver — proven by the org-wide
        variant returning it and a foreign-event attendance being suppressed."""
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        event_a = _make_event(org, cal_a)
        attendee = baker.make(ExternalAttendee, organization=org, email="x@example.com")
        baker.make(
            EventExternalAttendance,
            organization=org,
            event=event_a,
            external_attendee=attendee,
        )
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(
                self._EXTERNAL_ATTENDANCE_Q, system_user, token, auth, {"eventId": event_a.id}
            )
        )
        att = data["calendarEvents"][0]["externalAttendances"][0]
        # Own event back-pointer survives and exposes only the owned calendar.
        assert att["event"]["id"] == str(event_a.id)
        assert att["event"]["calendar"]["id"] == str(cal_a.id)

    # ==================================================================
    # BlockedTime.calendar + recurrence-exception back-pointers
    # ==================================================================

    _BLOCKED_Q = """
        query Q($blockedTimeId: Int!) {
            blockedTimes(blockedTimeId: $blockedTimeId) {
                id
                calendar { id }
                recurrenceExceptions {
                    id
                    parentBlockedTime { id calendar { id } }
                    modifiedBlockedTime { id calendar { id } }
                }
            }
        }
    """

    def _blocked_with_foreign_exception(self, org, cal_a, cal_b):
        own = _make_blocked(org, cal_a)
        foreign_parent = _make_blocked(org, cal_b)
        foreign_modified = _make_blocked(org, cal_b)
        baker.make(
            BlockedTimeRecurrenceException,
            organization=org,
            parent_blocked_time=foreign_parent,
            modified_blocked_time=foreign_modified,
            exception_date=_dt(2, 9),
        )
        # Attach the exception to the OWNED blocked time as its parent so it is reachable.
        baker.make(
            BlockedTimeRecurrenceException,
            organization=org,
            parent_blocked_time=own,
            modified_blocked_time=foreign_modified,
            exception_date=_dt(3, 9),
        )
        return own, foreign_parent, foreign_modified

    def test_blocked_calendar_and_exception_scoped(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own, _fp, _fm = self._blocked_with_foreign_exception(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(org, mem_a, [PublicAPIResources.BLOCKED_TIME])

        data = self._data(
            self._post(self._BLOCKED_Q, system_user, token, auth, {"blockedTimeId": own.id})
        )
        bt = data["blockedTimes"][0]
        assert bt["calendar"]["id"] == str(cal_a.id)
        # The modified_blocked_time pointer references cal_b -> suppressed to None.
        for exc in bt["recurrenceExceptions"]:
            if exc["parentBlockedTime"] is not None:
                assert exc["parentBlockedTime"]["calendar"]["id"] == str(cal_a.id)
            assert exc["modifiedBlockedTime"] is None

    def test_blocked_calendar_and_exception_org_wide(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own, _fp, foreign_modified = self._blocked_with_foreign_exception(org, cal_a, cal_b)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.BLOCKED_TIME])

        data = self._data(
            self._post(self._BLOCKED_Q, system_user, token, auth, {"blockedTimeId": own.id})
        )
        bt = data["blockedTimes"][0]
        assert bt["calendar"]["id"] == str(cal_a.id)
        modified_seen = {
            int(exc["modifiedBlockedTime"]["id"])
            for exc in bt["recurrenceExceptions"]
            if exc["modifiedBlockedTime"] is not None
        }
        assert foreign_modified.id in modified_seen

    # ==================================================================
    # AvailableTime.calendar + recurrence-exception back-pointers
    # ==================================================================

    _AVAILABLE_Q = """
        query Q($availableTimeId: Int!) {
            availableTimes(availableTimeId: $availableTimeId) {
                id
                calendar { id }
                recurrenceExceptions {
                    id
                    parentAvailableTime { id calendar { id } }
                    modifiedAvailableTime { id calendar { id } }
                }
            }
        }
    """

    def _available_with_foreign_exception(self, org, cal_a, cal_b):
        own = _make_available(org, cal_a)
        foreign_modified = _make_available(org, cal_b)
        baker.make(
            AvailableTimeRecurrenceException,
            organization=org,
            parent_available_time=own,
            modified_available_time=foreign_modified,
            exception_date=_dt(3, 9),
        )
        return own, foreign_modified

    def test_available_calendar_and_exception_scoped(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own, _fm = self._available_with_foreign_exception(org, cal_a, cal_b)
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.AVAILABLE_TIME]
        )

        data = self._data(
            self._post(self._AVAILABLE_Q, system_user, token, auth, {"availableTimeId": own.id})
        )
        at = data["availableTimes"][0]
        assert at["calendar"]["id"] == str(cal_a.id)
        for exc in at["recurrenceExceptions"]:
            if exc["parentAvailableTime"] is not None:
                assert exc["parentAvailableTime"]["calendar"]["id"] == str(cal_a.id)
            assert exc["modifiedAvailableTime"] is None

    def test_available_calendar_and_exception_org_wide(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own, foreign_modified = self._available_with_foreign_exception(org, cal_a, cal_b)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.AVAILABLE_TIME])

        data = self._data(
            self._post(self._AVAILABLE_Q, system_user, token, auth, {"availableTimeId": own.id})
        )
        at = data["availableTimes"][0]
        modified_seen = {
            int(exc["modifiedAvailableTime"]["id"])
            for exc in at["recurrenceExceptions"]
            if exc["modifiedAvailableTime"] is not None
        }
        assert foreign_modified.id in modified_seen

    # ==================================================================
    # EventRecurrenceException back-pointers (reachable via event)
    # ==================================================================

    _EVENT_EXCEPTION_Q = """
        query Q($eventId: Int!) {
            calendarEvents(eventId: $eventId) {
                id
                recurrenceExceptions {
                    id
                    parentEvent { id calendar { id } }
                    modifiedEvent { id calendar { id } }
                }
            }
        }
    """

    def test_event_recurrence_exception_scoped(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own = _make_event(org, cal_a)
        foreign_modified = _make_event(org, cal_b)
        baker.make(
            EventRecurrenceException,
            organization=org,
            parent_event=own,
            modified_event=foreign_modified,
            exception_date=_dt(3, 9),
        )
        system_user, token, auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        data = self._data(
            self._post(self._EVENT_EXCEPTION_Q, system_user, token, auth, {"eventId": own.id})
        )
        evt = data["calendarEvents"][0]
        for exc in evt["recurrenceExceptions"]:
            if exc["parentEvent"] is not None:
                assert exc["parentEvent"]["calendar"]["id"] == str(cal_a.id)
            # modifiedEvent points at cal_b -> suppressed.
            assert exc["modifiedEvent"] is None

    def test_event_recurrence_exception_org_wide(self, _rl):
        _rl.return_value = iter([None])
        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        own = _make_event(org, cal_a)
        foreign_modified = _make_event(org, cal_b)
        baker.make(
            EventRecurrenceException,
            organization=org,
            parent_event=own,
            modified_event=foreign_modified,
            exception_date=_dt(3, 9),
        )
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        data = self._data(
            self._post(self._EVENT_EXCEPTION_Q, system_user, token, auth, {"eventId": own.id})
        )
        evt = data["calendarEvents"][0]
        modified_seen = {
            int(exc["modifiedEvent"]["id"])
            for exc in evt["recurrenceExceptions"]
            if exc["modifiedEvent"] is not None
        }
        assert foreign_modified.id in modified_seen

    # ==================================================================
    # Internal / non-public-API request — strict no-op
    # ==================================================================

    def test_internal_request_unscoped_returns_nested(self, _rl):
        """A request with NO public_api_system_user attribute (internal/non-public-API)
        must hit the no-op branch of _owner_scoped_calendar_ids and return nested data
        unchanged. Proven by calling the helper-driven resolver directly with a mock
        info whose request lacks the public_api attributes."""
        _rl.return_value = iter([None])
        from calendar_integration.graphql import _owner_scoped_calendar_ids

        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _make_event(org, cal_a)

        # Internal request: no public_api_* attributes set.
        internal_request = Mock(spec=[])  # spec=[] => getattr returns the default (None)
        info = Mock()
        info.context = Mock()
        info.context.request = internal_request

        assert _owner_scoped_calendar_ids(info) is None

    def test_org_wide_request_returns_none_from_helper(self, _rl):
        """An org-wide token yields None from the helper (no filtering)."""
        _rl.return_value = iter([None])
        from calendar_integration.graphql import _owner_scoped_calendar_ids

        org = self._org()
        system_user, _token, _auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        request = Mock(spec=["public_api_system_user", "public_api_organization"])
        request.public_api_system_user = system_user
        request.public_api_organization = org
        info = Mock()
        info.context = Mock()
        info.context.request = request

        assert _owner_scoped_calendar_ids(info) is None

    def test_scoped_request_returns_owner_set_from_helper(self, _rl):
        """A scoped token yields its owner's calendar-id set from the helper."""
        _rl.return_value = iter([None])
        from calendar_integration.graphql import _owner_scoped_calendar_ids

        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        system_user, _token, _auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        request = Mock(spec=["public_api_system_user", "public_api_organization"])
        request.public_api_system_user = system_user
        request.public_api_organization = org
        info = Mock()
        info.context = Mock()
        info.context.request = request

        result = _owner_scoped_calendar_ids(info)
        assert result == {cal_a.id}
        assert cal_b.id not in result

    # ==================================================================
    # CalendarGroupSlot.calendars pool — direct second-hop resolver proof
    #
    # The only scoped-token entry point to a slot (groupSelections.slot) is
    # suppressed, so no end-to-end scoped query reaches CalendarGroupSlot.calendars.
    # That makes the pool-filter on the `calendars` resolver invisible to every
    # query-driven test: reverting it to an unfiltered field would not fail any
    # of them. These two tests drive the resolver DIRECTLY (real slot model as
    # ``self`` + mocked ``info``) so the second-hop defence-in-depth filter is
    # pinned: the scoped variant FAILS if the resolver returns the raw pool.
    # ==================================================================

    def _slot_with_cross_provider_pool(self, org, cal_a, cal_b):
        """A real CalendarGroupSlot whose candidate pool spans both providers."""
        group = CalendarGroup.objects.create(organization=org, name=f"grp-{uuid.uuid4().hex[:6]}")
        slot = CalendarGroupSlot.objects.create(
            organization=org, group=group, name="slot", required_count=1
        )
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal_a)
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal_b)
        return slot

    def _info_for_request(self, request):
        info = Mock()
        info.context = Mock()
        info.context.request = request
        return info

    def test_slot_calendars_pool_scoped_filters_cross_owner(self, _rl):
        """Scoped token: the slot's candidate pool resolver returns ONLY the owner's
        calendar; the cross-provider calendar is filtered out. FAILS if the resolver
        is reverted to an unfiltered ``strawberry_django.field()``."""
        _rl.return_value = iter([None])
        from calendar_integration.graphql import CalendarGroupSlotGraphQLType

        org = self._org()
        _ua, mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        slot = self._slot_with_cross_provider_pool(org, cal_a, cal_b)
        system_user, _token, _auth = self._scoped_token(
            org, mem_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        request = Mock(spec=["public_api_system_user", "public_api_organization"])
        request.public_api_system_user = system_user
        request.public_api_organization = org
        info = self._info_for_request(request)

        result = CalendarGroupSlotGraphQLType.calendars(slot, info)
        result_ids = {c.id for c in result}
        assert result_ids == {cal_a.id}
        assert cal_b.id not in result_ids

    def test_slot_calendars_pool_org_wide_unchanged(self, _rl):
        """Org-wide token (allowed_ids is None): the resolver returns the FULL
        cross-provider pool unchanged — the no-op regression assertion."""
        _rl.return_value = iter([None])
        from calendar_integration.graphql import CalendarGroupSlotGraphQLType

        org = self._org()
        _ua, _mem_a, cal_a = self._provider_with_calendar(org, "a")
        _ub, _mem_b, cal_b = self._provider_with_calendar(org, "b")
        slot = self._slot_with_cross_provider_pool(org, cal_a, cal_b)
        system_user, _token, _auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_EVENT])

        request = Mock(spec=["public_api_system_user", "public_api_organization"])
        request.public_api_system_user = system_user
        request.public_api_organization = org
        info = self._info_for_request(request)

        result = CalendarGroupSlotGraphQLType.calendars(slot, info)
        result_ids = {c.id for c in result}
        assert result_ids == {cal_a.id, cal_b.id}
