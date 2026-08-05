"""Tests for group-scoped quota rules in discovery and booking validation
(Phase 3b of ``CALENDAR_GROUP_SCOPED_AVAILABILITY``).

Covers:
- A calendar at its cap is hidden from discovery for that period and
  reappears the FOLLOWING period (spec UC-2, Acceptance 5's setup).
- Cancelling a booking frees quota immediately, with no further action
  (Acceptance 5).
- Two rules (daily AND weekly) on the same calendar are BOTH enforced.
- Explicit booking past the cap is rejected with
  ``GroupScopedRuleType.QUOTA_CONSUMED``.
- The headline risk this phase exists to guard against: the quota-counting
  query count is a FIXED function of the roster/config (bounded by the
  number of distinct ``(slot, period)`` combinations actually configured),
  never a function of how many candidate times discovery walks.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    GroupScopedRuleType,
    QuotaPeriod,
)
from calendar_integration.exceptions import CalendarGroupScopedRuleViolationError
from calendar_integration.factories import create_group_slot_quota_rule
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
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

QUOTA_COUNTING_FUNCTION = "get_calendar_group_quota_period_counts_json"


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


# 2025-09-01 is a Monday. Weeks: [Sep 1-7], [Sep 8-14], [Sep 15-21].
WEEK1_MONDAY = _utc(2025, 9, 1)
WEEK1_TUESDAY = _utc(2025, 9, 2)
WEEK1_WEDNESDAY = _utc(2025, 9, 3)
WEEK1_SATURDAY = _utc(2025, 9, 6)
WEEK2_MONDAY = _utc(2025, 9, 8)
WEEK2_SATURDAY = _utc(2025, 9, 13)


def _quota_query_count(captured: CaptureQueriesContext) -> int:
    return sum(1 for q in captured.captured_queries if QUOTA_COUNTING_FUNCTION in q["sql"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Quota Discovery Org", should_sync_rooms=False)


@pytest.fixture
def audit_service():
    from di_core.containers import container

    return container.audit_service()


@pytest.fixture
def admin_user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="quota-admin@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u, organization=organization, role=OrganizationRole.ADMIN
    )
    return u


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    """Dr. Reyes -- available every day for several weeks (a single wide
    AvailableTime block); base availability is never the bottleneck under
    test here, only quota. MANAGED (``manage_available_windows=True``), so
    base availability derives purely from ``AvailableTime`` coverage -- the
    existing bookings created below never conflict with each other or with
    later candidates on base-availability grounds, letting the tests isolate
    the quota gate."""
    cal = Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="dr_reyes_quota",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=cal,
        start_time_tz_unaware=WEEK1_MONDAY,
        end_time_tz_unaware=_utc(2025, 9, 29),
        timezone="UTC",
    )
    return cal


@pytest.fixture
def other_calendar(organization: Organization) -> Calendar:
    cal = Calendar.objects.create(
        organization=organization,
        name="Dr. Costa",
        external_id="dr_costa_quota",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=cal,
        start_time_tz_unaware=WEEK1_MONDAY,
        end_time_tz_unaware=_utc(2025, 9, 29),
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


def _seed_booking(
    organization: Organization,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
    calendar: Calendar,
    start: datetime.datetime,
    duration_minutes: int = 30,
) -> CalendarEvent:
    """Directly create a LIVE booking "made through" `surgery_slot` -- a
    ``CalendarEvent`` plus its ``CalendarEventGroupSelection`` link, exactly
    what ``CalendarGroupService.create_grouped_event`` would persist for a
    single-calendar slot selection (mirrors the Phase 3a counting-function
    test helper). Used to seed MULTIPLE pre-existing bookings without going
    through the full booking write pipeline, whose ``CalendarEvent.external_id``
    is server-generated and left blank (globally unique, INTERNAL provider,
    no write adapter under ``initialize_without_provider``) -- going through
    ``create_grouped_event`` more than once per test would collide on that
    constraint. The REJECTION/ACCEPTANCE tests below still exercise the real
    write path via ``create_grouped_event`` (only ONE successful write each).
    """
    end = start + datetime.timedelta(minutes=duration_minutes)
    event = CalendarEvent.objects.create(
        organization=organization,
        calendar=calendar,
        title="Surgery",
        external_id=f"quota-seed-{uuid.uuid4()}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        calendar_group=surgery_group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=organization, event=event, slot=surgery_slot, calendar=calendar
    )
    return event


def _book_via_service(
    service: CalendarGroupService,
    surgery_slot: CalendarGroupSlot,
    calendar: Calendar,
    start: datetime.datetime,
    duration_minutes: int = 30,
):
    """Book through the real write path (``create_grouped_event``) -- used
    for the tests that exercise booking-time validation itself, never more
    than once per test (see ``_seed_booking``'s docstring)."""
    return service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Surgery",
            description="",
            start_time=start,
            end_time=start + datetime.timedelta(minutes=duration_minutes),
            timezone="UTC",
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=surgery_slot.id, calendar_ids=[calendar.id]
                ),
            ],
        )
    )


# ---------------------------------------------------------------------------
# UC-2 / Acceptance 5 (setup) -- capped calendar hidden for the period, offered
# again the following period.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_capped_calendar_hidden_for_period_and_offered_next_period(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 1, 9))
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 2, 9))
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 3, 9))

    week1_proposals = service.find_bookable_slots(
        group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=WEEK1_MONDAY,
        search_window_end=WEEK1_SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    assert week1_proposals == []

    week2_proposals = service.find_bookable_slots(
        group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=WEEK2_MONDAY,
        search_window_end=WEEK2_SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    assert len(week2_proposals) > 0
    assert {p.start_time.weekday() for p in week2_proposals} == {0, 1, 2, 3, 4}


@pytest.mark.django_db
def test_check_group_availability_excludes_capped_calendar(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=1,
    )
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 1, 9))

    capped_range = (_utc(2025, 9, 2, 10), _utc(2025, 9, 2, 10, 30))
    free_range = (_utc(2025, 9, 8, 10), _utc(2025, 9, 8, 10, 30))  # next week

    [capped_result, free_result] = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [capped_range, free_range],
    )
    assert capped_result.slots[0].available_calendar_ids == []
    assert free_result.slots[0].available_calendar_ids == [calendar.id]


# ---------------------------------------------------------------------------
# Acceptance 5 -- cancelling a booking frees quota immediately.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cancelling_booking_frees_quota_with_no_further_action(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 1, 9))
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 2, 9))
    third = _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 3, 9))

    assert (
        service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=WEEK1_SATURDAY,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(hours=2),
        )
        == []
    )

    # Cancelling a grouped booking deletes the CalendarEvent row (mirrors
    # CalendarGroupService.cancel_grouped_event, which this test bypasses to
    # avoid needing a fully-permissioned actor -- the fixtures here have no
    # user or public-scheduling token attached to the calendar_service, so
    # the real write-path permission check would reject the delete for
    # reasons unrelated to quota). The CASCADE on
    # CalendarEventGroupSelection.event_fk removes the group-selection link
    # too, so the counting function sees one fewer live booking -- exactly
    # what cancel_grouped_event's own deletion of the primary event achieves.
    third.delete()

    proposals_after_cancel = service.find_bookable_slots(
        group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=WEEK1_MONDAY,
        search_window_end=WEEK1_SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    assert len(proposals_after_cancel) > 0


# ---------------------------------------------------------------------------
# Two rules (daily AND weekly) both enforced.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_daily_and_weekly_rules_both_enforced(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """A calendar under its weekly cap but at its daily cap is not offered
    THAT day, but is offered other days in the week."""
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=5,
    )

    _seed_booking(organization, surgery_group, surgery_slot, calendar, WEEK1_MONDAY.replace(hour=9))

    proposals = service.find_bookable_slots(
        group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
        search_window_start=WEEK1_MONDAY,
        search_window_end=WEEK1_SATURDAY,
        duration=datetime.timedelta(minutes=30),
        slot_step=datetime.timedelta(hours=2),
    )
    days_offered = {p.start_time.weekday() for p in proposals}
    # Monday (0) is at its daily cap -- absent. Tuesday-Friday (1-4) are
    # under both caps -- present. The weekly cap (5) is never hit.
    assert 0 not in days_offered
    assert days_offered == {1, 2, 3, 4}


# ---------------------------------------------------------------------------
# Explicit booking past the cap is rejected with QUOTA_CONSUMED.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_grouped_event_rejects_calendar_over_quota(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 1, 9))

    with pytest.raises(CalendarGroupScopedRuleViolationError) as exc_info:
        _book_via_service(service, surgery_slot, calendar, _utc(2025, 9, 1, 14))

    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == GroupScopedRuleType.QUOTA_CONSUMED
    # The error names the calendar and the rule type -- never the configured
    # cap or current count (spec Decisions -> Errors).
    error_msg = str(exc_info.value)
    assert str(calendar.id) in error_msg
    assert "quota_consumed" in error_msg


@pytest.mark.django_db
def test_create_grouped_event_allows_calendar_under_quota(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=2,
    )
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 1, 9))
    event = _book_via_service(service, surgery_slot, calendar, _utc(2025, 9, 1, 14))
    assert event.calendar_fk_id == calendar.id


# ---------------------------------------------------------------------------
# Headline risk: quota-counting query count is independent of candidate
# count, and bounded by (slot, period) combinations actually configured --
# never by roster size or candidate count.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_quota_query_count_independent_of_candidate_count(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    # FEW candidates: 2-hour steps over a single day.
    with CaptureQueriesContext(connection) as few:
        few_proposals = service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=WEEK1_TUESDAY,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(hours=2),
        )

    # MANY candidates: 5-minute steps over three weeks -- orders of magnitude
    # more candidate windows than the FEW case above.
    with CaptureQueriesContext(connection) as many:
        many_proposals = service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=_utc(2025, 9, 22),
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(minutes=5),
        )

    assert len(many_proposals) > len(few_proposals) * 50  # sanity: MANY more candidates

    few_quota_queries = _quota_query_count(few)
    many_quota_queries = _quota_query_count(many)
    assert few_quota_queries == 1
    assert many_quota_queries == 1
    assert few_quota_queries == many_quota_queries

    # The TOTAL query count (not just the quota-counting slice) is also
    # identical -- discovery does not issue one round trip per candidate for
    # ANY of its fetches, quota included.
    assert len(few.captured_queries) == len(many.captured_queries)


@pytest.mark.django_db
def test_quota_query_count_bounded_by_period_combinations_not_calendar_count(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    other_calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """Two calendars sharing the SAME (slot, period) -- the counting call is
    ONE annotated query covering both, not one per calendar."""
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=surgery_slot, calendar=other_calendar
    )
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=other_calendar,
        period=QuotaPeriod.WEEK,
        cap=3,
    )

    with CaptureQueriesContext(connection) as captured:
        service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=WEEK1_SATURDAY,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(hours=2),
        )
    assert _quota_query_count(captured) == 1


@pytest.mark.django_db
def test_quota_query_count_scales_with_distinct_periods_not_candidates(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """One calendar with TWO period types configured (day + week) -- TWO
    counting queries, bounded by the distinct period types actually
    configured, still independent of candidate count."""
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=5,
    )

    with CaptureQueriesContext(connection) as few:
        service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=WEEK1_TUESDAY,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(hours=2),
        )
    with CaptureQueriesContext(connection) as many:
        service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=WEEK1_SATURDAY,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(minutes=5),
        )
    assert _quota_query_count(few) == 2
    assert _quota_query_count(many) == 2


# ---------------------------------------------------------------------------
# Required -- unchanged path: identical output, unchanged query count (no
# quota-counting query issued at all when nothing is configured).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unconfigured_group_issues_no_quota_counting_query(
    service: CalendarGroupService,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    with CaptureQueriesContext(connection) as captured:
        proposals = service.find_bookable_slots(
            group_id=surgery_slot.group_fk_id,  # type: ignore[arg-type]
            search_window_start=WEEK1_MONDAY,
            search_window_end=WEEK1_SATURDAY,
            duration=datetime.timedelta(minutes=30),
            slot_step=datetime.timedelta(hours=2),
        )
    assert len(proposals) > 0
    assert _quota_query_count(captured) == 0
