"""Integration tests for group-slot scoping on AvailableTime/BlockedTime (Phase 0 of
CALENDAR_GROUP_SCOPED_AVAILABILITY).

Phase 0 adds a nullable ``group_slot`` reference to both models and makes the
default manager exclude scoped rows. Nothing yet *writes* the column and no
service consumes it as a scoping signal — Phase 0's job is only to prove that a
group-scoped row, inserted directly (as a stand-in for what a later phase's
write path will do), is invisible on every existing read path with zero call
site edits: the availability service (single-calendar), the calendar-group
service (group discovery / availability), and the REST/public-API queryset
pattern (``.objects.filter_by_organization(...)``).
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.recurrence_manager import RecurrenceManager
from tenancy.models import Organization


class _FakeAvailabilityHost:
    """Minimal AvailabilityServiceHost — no events, no side-effect writes needed here."""

    def get_calendar_events_expanded(self, calendar, start_date, end_date):
        return []

    def bulk_create_manual_blocked_times(self, calendar, blocked_times):
        return []

    def _create_recurrence_rule_if_needed(self, rrule_string):
        return None


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Phase0 Scoping Org", should_sync_rooms=False)


@pytest.fixture
def managed_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="phase0-cal",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture
def group_slot(organization: Organization, managed_calendar: Calendar) -> CalendarGroupSlot:
    group = CalendarGroup.objects.create(organization=organization, name="Surgery")
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Lead")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=managed_calendar
    )
    return slot


def _search_window() -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 9, 2, 17, 0, tzinfo=datetime.UTC)
    return start, end


@pytest.mark.django_db
def test_group_scoped_available_time_invisible_to_single_calendar_availability_service(
    organization: Organization,
    managed_calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    """A group-scoped-only window must not make a managed calendar available on the
    single-calendar read path — that path has no group context and must not gain one
    (spec non-goal: "no changes to single-calendar booking").
    """
    start, end = _search_window()

    # Directly insert a group-scoped row covering the whole search window. No base
    # (group_slot IS NULL) row exists for this calendar.
    AvailableTime.objects.unscoped().create(
        organization=organization,
        calendar=managed_calendar,
        group_slot=group_slot,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    context = CalendarServiceContext(
        organization=organization,
        user_or_token=None,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    service = AvailabilityService(
        context=context,
        recurrence_manager=RecurrenceManager(),
        host=_FakeAvailabilityHost(),
    )

    windows = list(service.get_availability_windows_in_range(managed_calendar, start, end))

    assert windows == []


@pytest.mark.django_db
def test_group_scoped_available_time_invisible_to_group_availability_check(
    organization: Organization,
    managed_calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    """A group-scoped-only window must not make the calendar count toward its own
    slot's availability in group discovery — Phase 0 does not wire group-scoped
    configuration into group reads yet, so this must behave exactly as "nothing
    configured" (the calendar has no *base* availability).
    """
    start, end = _search_window()

    AvailableTime.objects.unscoped().create(
        organization=organization,
        calendar=managed_calendar,
        group_slot=group_slot,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    group_service = CalendarGroupService()
    group_service.initialize(organization=organization)

    [availability] = group_service.check_group_availability(group_slot.group_fk_id, [(start, end)])
    [slot_availability] = availability.slots
    assert slot_availability.available_calendar_ids == []
    assert not slot_availability.is_satisfied_for_required_count

    proposals = group_service.find_bookable_slots(
        group_id=group_slot.group_fk_id,
        search_window_start=start,
        search_window_end=end,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(minutes=30),
    )
    assert proposals == []


@pytest.mark.django_db
def test_group_scoped_available_time_invisible_to_group_availability_check_even_with_base_row(
    organization: Organization,
    managed_calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    """Sanity check that the *base* row is what group discovery actually reads —
    proves the previous test's empty result comes from scoping, not from some
    unrelated reason the calendar never appears (e.g. a broken fixture).
    """
    start, end = _search_window()

    AvailableTime.objects.create(
        organization=organization,
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    group_service = CalendarGroupService()
    group_service.initialize(organization=organization)

    [availability] = group_service.check_group_availability(group_slot.group_fk_id, [(start, end)])
    [slot_availability] = availability.slots
    assert slot_availability.available_calendar_ids == [managed_calendar.id]


@pytest.mark.django_db
def test_group_scoped_available_time_invisible_to_rest_and_public_api_queryset_pattern(
    organization: Organization,
    managed_calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    """Mirrors the exact queryset construction used by the REST viewset
    (``AvailableTimeViewSet.get_queryset``) and the public GraphQL API
    (``public_api/queries.py``): ``AvailableTime.objects.filter_by_organization(...)``.
    Both call sites go through the default manager with zero changes required
    for this phase, so a group-scoped row must not appear in either.
    """
    start, end = _search_window()

    base_row = AvailableTime.objects.create(
        organization=organization,
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )
    group_scoped_row = AvailableTime.objects.unscoped().create(
        organization=organization,
        calendar=managed_calendar,
        group_slot=group_slot,
        start_time_tz_unaware=start + datetime.timedelta(hours=1),
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    visible_ids = set(
        AvailableTime.objects.filter_by_organization(organization.id).values_list("id", flat=True)
    )
    assert base_row.id in visible_ids
    assert group_scoped_row.id not in visible_ids
