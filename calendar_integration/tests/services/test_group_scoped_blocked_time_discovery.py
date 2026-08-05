"""Tests for group-scoped blocked time in discovery and booking validation
(Phase 2a of ``CALENDAR_GROUP_SCOPED_AVAILABILITY``).

Covers:
- A group-scoped block hides the calendar in that group, in that block's
  time, and NOWHERE else (spec UC-3).
- A block overlapping a group-scoped window WINS -- resolution order is
  base availability, then block, then window ("blocks beat everything").
- Explicit booking/reschedule inside a block is rejected with
  ``GroupScopedRuleType.INSIDE_BLOCK``.
- The required "unchanged path" test: a group with NO group-scoped
  configuration (neither windows nor blocks) produces byte-for-byte
  identical discovery output AND issues the SAME number of queries as the
  pre-Phase-1b/2a engine.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType, GroupScopedRuleType
from calendar_integration.exceptions import CalendarGroupScopedRuleViolationError
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarGroupEventInputData,
    CalendarGroupSlotSelectionInputData,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


# 2025-09-01 is a Monday.
MONDAY = _utc(2025, 9, 1)
TUESDAY = _utc(2025, 9, 2)
WEDNESDAY = _utc(2025, 9, 3)
THURSDAY = _utc(2025, 9, 4)
FRIDAY = _utc(2025, 9, 5)
SATURDAY = _utc(2025, 9, 6)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Discovery Blocks Org", should_sync_rooms=False)


@pytest.fixture
def audit_service():
    from di_core.containers import container

    return container.audit_service()


@pytest.fixture
def admin_user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="admin@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u, organization=organization, role=OrganizationRole.ADMIN
    )
    return u


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    """Dr. Reyes -- available Monday through Friday, base availability only
    (a single wide AvailableTime block; group-scoped blocks/windows model the
    narrowing under test)."""
    cal = Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="dr_reyes",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=cal,
        start_time_tz_unaware=MONDAY,
        end_time_tz_unaware=SATURDAY,
        timezone="UTC",
    )
    return cal


@pytest.fixture
def surgery_group(organization: Organization) -> CalendarGroup:
    return CalendarGroup.objects.create(
        organization=organization, name="Surgery", accepts_public_scheduling=True
    )


@pytest.fixture
def surgery_slot(
    organization: Organization, surgery_group: CalendarGroup, calendar: Calendar
) -> CalendarGroupSlot:
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=surgery_group, name="Lead Surgeon"
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def consults_group(organization: Organization) -> CalendarGroup:
    """A SECOND group containing the SAME calendar, with NO group-scoped
    configuration -- proves a block in Surgery does not leak here."""
    return CalendarGroup.objects.create(
        organization=organization, name="Consults", accepts_public_scheduling=True
    )


@pytest.fixture
def consults_slot(
    organization: Organization, consults_group: CalendarGroup, calendar: Calendar
) -> CalendarGroupSlot:
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=consults_group, name="Consultant"
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def calendar_service(organization: Organization) -> CalendarService:
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    return cs


@pytest.fixture
def service(
    organization: Organization, calendar_service: CalendarService, audit_service
) -> CalendarGroupService:
    svc = CalendarGroupService(
        calendar_service=calendar_service,
        calendar_permission_service=CalendarPermissionService(),
        audit_service=audit_service,
    )
    svc.initialize(organization=organization)
    return svc


# ---------------------------------------------------------------------------
# UC-3 -- a block hides the calendar in one group only.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_scoped_block_hides_calendar_in_one_group_only(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
    consults_slot: CalendarGroupSlot,
) -> None:
    # Block Tuesday and Thursday entirely in Surgery.
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 0),
        end_time=_utc(2025, 9, 3, 0),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    surgery_proposals = service.find_bookable_slots(
        group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=MONDAY,
        search_window_end=SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    surgery_days = {p.start_time.weekday() for p in surgery_proposals}
    # Tuesday (1) and Thursday (3) are blocked; Monday/Wednesday/Friday remain.
    assert surgery_days == {0, 2, 4}

    # The SAME calendar in the Consults group (no group-scoped config there)
    # is unaffected -- full base availability (Mon-Fri) is offered.
    consults_proposals = service.find_bookable_slots(
        group_id=consults_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=MONDAY,
        search_window_end=SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    consults_days = {p.start_time.weekday() for p in consults_proposals}
    assert consults_days == {0, 1, 2, 3, 4}


@pytest.mark.django_db
def test_check_group_availability_excludes_blocked_calendar(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )

    blocked_range = (_utc(2025, 9, 2, 10), _utc(2025, 9, 2, 10, 30))
    free_range = (_utc(2025, 9, 3, 10), _utc(2025, 9, 3, 10, 30))

    [blocked_result, free_result] = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [blocked_range, free_range],
    )
    [blocked_slot] = blocked_result.slots
    [free_slot] = free_result.slots
    assert blocked_slot.available_calendar_ids == []
    assert free_slot.available_calendar_ids == [calendar.id]


# ---------------------------------------------------------------------------
# Blocks beat everything: a block overlapping a window still wins.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_scoped_block_wins_over_overlapping_window(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    # A window covering the whole day.
    service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 8),
        end_time=_utc(2025, 9, 2, 18),
        tz="UTC",
    )
    # A block covering the middle of the day, inside the window.
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 12),
        end_time=_utc(2025, 9, 2, 14),
        tz="UTC",
    )

    proposals = service.find_bookable_slots(
        group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=_utc(2025, 9, 2, 8),
        search_window_end=_utc(2025, 9, 2, 18),
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(minutes=30),
    )
    proposal_starts = {p.start_time.hour + p.start_time.minute / 60 for p in proposals}

    # The window covers 8-18; the block removes 12-14. The calendar must be
    # offered before and after the block, but NEVER inside it -- even though
    # the window covers that time too.
    assert any(h < 12 for h in proposal_starts)
    assert any(h >= 14 for h in proposal_starts)
    assert not any(12 <= h < 14 for h in proposal_starts)

    # And check_group_availability agrees.
    inside_block_range = (_utc(2025, 9, 2, 12, 30), _utc(2025, 9, 2, 13))
    outside_block_range = (_utc(2025, 9, 2, 9), _utc(2025, 9, 2, 9, 30))
    [inside_result, outside_result] = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [inside_block_range, outside_block_range],
    )
    assert inside_result.slots[0].available_calendar_ids == []
    assert outside_result.slots[0].available_calendar_ids == [calendar.id]


@pytest.mark.django_db
def test_create_grouped_event_rejects_calendar_inside_block_even_when_window_covers_it(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 8),
        end_time=_utc(2025, 9, 2, 18),
        tz="UTC",
    )
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 12),
        end_time=_utc(2025, 9, 2, 14),
        tz="UTC",
    )

    with pytest.raises(CalendarGroupScopedRuleViolationError) as exc_info:
        service.create_grouped_event(
            CalendarGroupEventInputData(
                title="Surgery",
                description="",
                start_time=_utc(2025, 9, 2, 12, 30),  # inside both window and block
                end_time=_utc(2025, 9, 2, 13),
                timezone="UTC",
                group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
                slot_selections=[
                    CalendarGroupSlotSelectionInputData(
                        slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                    ),
                ],
            )
        )

    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == GroupScopedRuleType.INSIDE_BLOCK


# ---------------------------------------------------------------------------
# Explicit booking / reschedule inside a block is rejected.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_grouped_event_rejects_calendar_inside_group_scoped_block(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    with pytest.raises(CalendarGroupScopedRuleViolationError) as exc_info:
        service.create_grouped_event(
            CalendarGroupEventInputData(
                title="Surgery",
                description="",
                start_time=_utc(2025, 9, 2, 10),  # Tuesday -- inside the block
                end_time=_utc(2025, 9, 2, 10, 30),
                timezone="UTC",
                group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
                slot_selections=[
                    CalendarGroupSlotSelectionInputData(
                        slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                    ),
                ],
            )
        )

    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == GroupScopedRuleType.INSIDE_BLOCK
    # The error names the calendar and the rule type -- never the configured
    # block values (spec Decisions -> Errors).
    error_msg = str(exc_info.value)
    assert str(calendar.id) in error_msg
    assert "inside_block" in error_msg


@pytest.mark.django_db
def test_create_grouped_event_allows_calendar_outside_group_scoped_block(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    event = service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Surgery",
            description="",
            start_time=_utc(2025, 9, 3, 10),  # Wednesday -- outside the block
            end_time=_utc(2025, 9, 3, 10, 30),
            timezone="UTC",
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                ),
            ],
        )
    )
    assert event.calendar_fk_id == calendar.id


@pytest.mark.django_db
def test_reschedule_grouped_event_rejects_move_inside_group_scoped_block(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    event = service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Surgery",
            description="",
            start_time=_utc(2025, 9, 3, 10),
            end_time=_utc(2025, 9, 3, 10, 30),
            timezone="UTC",
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                ),
            ],
        )
    )

    with pytest.raises(CalendarGroupScopedRuleViolationError) as exc_info:
        service.reschedule_grouped_event(
            event_id=event.id,
            start_time=_utc(2025, 9, 2, 10),  # Tuesday -- inside the block
            end_time=_utc(2025, 9, 2, 10, 30),
            tz="UTC",
        )
    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == GroupScopedRuleType.INSIDE_BLOCK


# ---------------------------------------------------------------------------
# Required -- unchanged path: identical output, unchanged query count.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unconfigured_group_discovery_is_byte_for_byte_unchanged(
    service: CalendarGroupService,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """No group-scoped window, block, or quota rule exists anywhere in the
    group -- discovery must take the early-out before any new (Phase 1b/2a)
    work runs.

    Query counts match the Phase 1b baseline exactly (6 for
    ``find_bookable_slots``, 5 for ``check_group_availability``) -- adding
    the block existence flag folded it into the SAME per-slot query rather
    than issuing a new one, so this phase adds zero queries to the
    unconfigured path too.
    """
    window_start = MONDAY
    window_end = SATURDAY

    with CaptureQueriesContext(connection) as captured:
        proposals = service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=window_start,
            search_window_end=window_end,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(hours=2),
        )
    assert len(captured.captured_queries) == 6
    assert len(proposals) == 60
    assert {p.start_time.weekday() for p in proposals} == {0, 1, 2, 3, 4}

    range1 = (_utc(2025, 9, 2, 10), _utc(2025, 9, 2, 10, 30))
    range2 = (_utc(2025, 9, 6, 10), _utc(2025, 9, 6, 10, 30))  # Saturday -- outside base
    with CaptureQueriesContext(connection) as captured2:
        result = service.check_group_availability(
            surgery_slot.group_fk_id,  # type: ignore[arg-type]
            [range1, range2],
        )
    assert len(captured2.captured_queries) == 5
    [r1, r2] = result
    assert [s.available_calendar_ids for s in r1.slots] == [[calendar.id]]
    assert [s.available_calendar_ids for s in r2.slots] == [[]]
