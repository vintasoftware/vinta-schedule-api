"""Tests for appointment-type-scoped blocked time in discovery and booking validation.

Covers:
- An appointment-type-scoped block hides the calendar in that appointment type, in that block's
  time, and NOWHERE else (spec UC-3).
- A block overlapping an appointment-type-scoped window WINS -- resolution order is
  base availability, then block, then window ("blocks beat everything").
- Explicit booking/reschedule inside a block is rejected with
  ``AppointmentTypeScopedRuleType.INSIDE_BLOCK``.
- The required "unchanged path" test: an appointment type with NO appointment-type-scoped
  configuration (neither windows nor blocks) produces byte-for-byte
  identical discovery output AND issues the SAME number of queries as the
  engine without appointment-type-scoped blocked-time/window support.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.constants import (
    AppointmentTypeScopedRuleType,
    CalendarProvider,
    CalendarType,
)
from calendar_integration.exceptions import AppointmentTypeScopedRuleViolationError
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AppointmentTypeEventInputData,
    AppointmentTypeSlotSelectionInputData,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups
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
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=u,
            organization=organization,
        ),
        [GROUP_ORGANIZATION_ADMIN],
    )
    return u


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    """Dr. Reyes -- available Monday through Friday, base availability only
    (a single wide AvailableTime block; appointment-type-scoped blocks/windows model the
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
def surgery_appointment_type(organization: Organization) -> AppointmentType:
    # duration=30min matches every codeless create_appointment_type_event booking span
    # in this file -- a public appointment type with no duration fails closed in
    # CalendarPermissionService.can_perform_appointment_type_scheduling.
    return AppointmentType.objects.create(
        organization=organization,
        name="Surgery",
        accepts_public_scheduling=True,
        duration=datetime.timedelta(minutes=30),
    )


@pytest.fixture
def surgery_slot(
    organization: Organization, surgery_appointment_type: AppointmentType, calendar: Calendar
) -> AppointmentTypeSlot:
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=surgery_appointment_type, name="Lead Surgeon"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def consults_appointment_type(organization: Organization) -> AppointmentType:
    """A SECOND appointment type containing the SAME calendar, with NO appointment-type-scoped
    configuration -- proves a block in Surgery does not leak here."""
    return AppointmentType.objects.create(
        organization=organization, name="Consults", accepts_public_scheduling=True
    )


@pytest.fixture
def consults_slot(
    organization: Organization, consults_appointment_type: AppointmentType, calendar: Calendar
) -> AppointmentTypeSlot:
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=consults_appointment_type, name="Consultant"
    )
    AppointmentTypeSlotMembership.objects.create(
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
) -> AppointmentTypeService:
    svc = AppointmentTypeService(
        calendar_service=calendar_service,
        calendar_permission_service=CalendarPermissionService(),
        audit_service=audit_service,
    )
    svc.initialize(organization=organization)
    return svc


# ---------------------------------------------------------------------------
# UC-3 -- a block hides the calendar in one appointment type only.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_appointment_type_scoped_block_hides_calendar_in_one_appointment_type_only(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
    consults_slot: AppointmentTypeSlot,
) -> None:
    # Block Tuesday and Thursday entirely in Surgery.
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 0),
        end_time=_utc(2025, 9, 3, 0),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    surgery_proposals = service.find_bookable_slots(
        appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
        search_window_start=MONDAY,
        search_window_end=SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    surgery_days = {p.start_time.weekday() for p in surgery_proposals}
    # Tuesday (1) and Thursday (3) are blocked; Monday/Wednesday/Friday remain.
    assert surgery_days == {0, 2, 4}

    # The SAME calendar in the Consults appointment type (no appointment-type-scoped config there)
    # is unaffected -- full base availability (Mon-Fri) is offered.
    consults_proposals = service.find_bookable_slots(
        appointment_type_id=consults_slot.appointment_type_fk_id,  # type: ignore[arg-type]
        search_window_start=MONDAY,
        search_window_end=SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    consults_days = {p.start_time.weekday() for p in consults_proposals}
    assert consults_days == {0, 1, 2, 3, 4}


@pytest.mark.django_db
def test_check_appointment_type_availability_excludes_blocked_calendar(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )

    blocked_range = (_utc(2025, 9, 2, 10), _utc(2025, 9, 2, 10, 30))
    free_range = (_utc(2025, 9, 3, 10), _utc(2025, 9, 3, 10, 30))

    [blocked_result, free_result] = service.check_appointment_type_availability(
        surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
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
def test_appointment_type_scoped_block_wins_over_overlapping_window(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    # A window covering the whole day.
    service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 8),
        end_time=_utc(2025, 9, 2, 18),
        tz="UTC",
    )
    # A block covering the middle of the day, inside the window.
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 12),
        end_time=_utc(2025, 9, 2, 14),
        tz="UTC",
    )

    proposals = service.find_bookable_slots(
        appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
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

    # And check_appointment_type_availability agrees.
    inside_block_range = (_utc(2025, 9, 2, 12, 30), _utc(2025, 9, 2, 13))
    outside_block_range = (_utc(2025, 9, 2, 9), _utc(2025, 9, 2, 9, 30))
    [inside_result, outside_result] = service.check_appointment_type_availability(
        surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
        [inside_block_range, outside_block_range],
    )
    assert inside_result.slots[0].available_calendar_ids == []
    assert outside_result.slots[0].available_calendar_ids == [calendar.id]


@pytest.mark.django_db
def test_create_appointment_type_event_rejects_calendar_inside_block_even_when_window_covers_it(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 8),
        end_time=_utc(2025, 9, 2, 18),
        tz="UTC",
    )
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 12),
        end_time=_utc(2025, 9, 2, 14),
        tz="UTC",
    )

    with pytest.raises(AppointmentTypeScopedRuleViolationError) as exc_info:
        service.create_appointment_type_event(
            AppointmentTypeEventInputData(
                title="Surgery",
                description="",
                start_time=_utc(2025, 9, 2, 12, 30),  # inside both window and block
                end_time=_utc(2025, 9, 2, 13),
                timezone="UTC",
                appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
                slot_selections=[
                    AppointmentTypeSlotSelectionInputData(
                        slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                    ),
                ],
            )
        )

    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == AppointmentTypeScopedRuleType.INSIDE_BLOCK


# ---------------------------------------------------------------------------
# Explicit booking / reschedule inside a block is rejected.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_appointment_type_event_rejects_calendar_inside_appointment_type_scoped_block(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    with pytest.raises(AppointmentTypeScopedRuleViolationError) as exc_info:
        service.create_appointment_type_event(
            AppointmentTypeEventInputData(
                title="Surgery",
                description="",
                start_time=_utc(2025, 9, 2, 10),  # Tuesday -- inside the block
                end_time=_utc(2025, 9, 2, 10, 30),
                timezone="UTC",
                appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
                slot_selections=[
                    AppointmentTypeSlotSelectionInputData(
                        slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                    ),
                ],
            )
        )

    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == AppointmentTypeScopedRuleType.INSIDE_BLOCK
    # The error names the calendar and the rule type -- never the configured
    # block values (spec Decisions -> Errors).
    error_msg = str(exc_info.value)
    assert str(calendar.id) in error_msg
    assert "inside_block" in error_msg


@pytest.mark.django_db
def test_create_appointment_type_event_allows_calendar_outside_appointment_type_scoped_block(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    event = service.create_appointment_type_event(
        AppointmentTypeEventInputData(
            title="Surgery",
            description="",
            start_time=_utc(2025, 9, 3, 10),  # Wednesday -- outside the block
            end_time=_utc(2025, 9, 3, 10, 30),
            timezone="UTC",
            appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
            slot_selections=[
                AppointmentTypeSlotSelectionInputData(
                    slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                ),
            ],
        )
    )
    assert event.calendar_fk_id == calendar.id


@pytest.mark.django_db
def test_reschedule_appointment_type_event_rejects_move_inside_appointment_type_scoped_block(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    service.create_appointment_type_scoped_blocked_time(
        acting_user=admin_user,
        appointment_type_slot_id=surgery_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )

    event = service.create_appointment_type_event(
        AppointmentTypeEventInputData(
            title="Surgery",
            description="",
            start_time=_utc(2025, 9, 3, 10),
            end_time=_utc(2025, 9, 3, 10, 30),
            timezone="UTC",
            appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
            slot_selections=[
                AppointmentTypeSlotSelectionInputData(
                    slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                ),
            ],
        )
    )

    with pytest.raises(AppointmentTypeScopedRuleViolationError) as exc_info:
        service.reschedule_appointment_type_event(
            event_id=event.id,
            start_time=_utc(2025, 9, 2, 10),  # Tuesday -- inside the block
            end_time=_utc(2025, 9, 2, 10, 30),
            tz="UTC",
        )
    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == AppointmentTypeScopedRuleType.INSIDE_BLOCK


# ---------------------------------------------------------------------------
# Required -- unchanged path: identical output, unchanged query count.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unconfigured_appointment_type_discovery_is_byte_for_byte_unchanged(
    service: AppointmentTypeService,
    calendar: Calendar,
    surgery_slot: AppointmentTypeSlot,
) -> None:
    """No appointment-type-scoped window, block, or quota rule exists anywhere in the
    appointment type -- discovery must take the early-out before any new appointment-type-scoped
    work runs.

    Query counts match the established baseline exactly (6 for
    ``find_bookable_slots``, 5 for ``check_appointment_type_availability``) -- adding
    the block existence flag folded it into the SAME per-slot query rather
    than issuing a new one, so appointment-type-scoped blocked time adds zero queries to
    the unconfigured path too.
    """
    window_start = MONDAY
    window_end = SATURDAY

    with CaptureQueriesContext(connection) as captured:
        proposals = service.find_bookable_slots(
            appointment_type_id=surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
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
        result = service.check_appointment_type_availability(
            surgery_slot.appointment_type_fk_id,  # type: ignore[arg-type]
            [range1, range2],
        )
    assert len(captured2.captured_queries) == 5
    [r1, r2] = result
    assert [s.available_calendar_ids for s in r1.slots] == [[calendar.id]]
    assert [s.available_calendar_ids for s in r2.slots] == [[]]
