"""Tests for group-scoped quota rules in discovery and booking validation.

Covers:
- A calendar at its cap is hidden from discovery for that period and
  reappears the FOLLOWING period (spec UC-2, Acceptance 5's setup).
- Cancelling a booking frees quota immediately, with no further action
  (Acceptance 5).
- Two rules (daily AND weekly) on the same calendar are BOTH enforced.
- Explicit booking past the cap is rejected with
  ``GroupScopedRuleType.QUOTA_CONSUMED``.
- The headline risk group-scoped quota enforcement exists to guard against: the quota-counting
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
    EventManagementPermissions,
    GroupScopedRuleType,
    QuotaPeriod,
)
from calendar_integration.database_functions import GetCalendarGroupQuotaPeriodCountsJSON
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
    CalendarManagementToken,
)
from calendar_integration.services import slot_engine
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarGroupEventInputData,
    CalendarGroupSlotSelectionInputData,
)
from organizations.models import Organization, OrganizationMembership, WeekStart
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups
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
    # duration=30min matches every codeless booking span made through this
    # group below (``_book_via_service``'s default) -- a public group with no
    # duration fails closed in
    # CalendarPermissionService.can_perform_group_scheduling.
    return CalendarGroup.objects.create(
        organization=organization,
        name="Surgery",
        accepts_public_scheduling=True,
        duration=datetime.timedelta(minutes=30),
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


@pytest.fixture
def reschedule_admin_user(organization: Organization) -> User:
    """A real ``User`` identity (not the anonymous/public path the other
    fixtures use) -- needed only by the reschedule self-exclusion tests below,
    which must exercise the FULL ``reschedule_grouped_event`` write path
    (through ``CalendarService.update_event``'s permission check), not just
    the quota gate."""
    u = User.objects.create_user(email="quota-reschedule-admin@example.com", password="pass")
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
def reschedule_calendar_service(
    organization: Organization, reschedule_admin_user: User, calendar: Calendar
) -> CalendarService:
    """A ``CalendarService`` authenticated as ``reschedule_admin_user``, with a
    calendar-level ``CalendarManagementToken`` pre-minted so
    ``CalendarEventService.create_event``'s ``initialize_with_user`` call
    succeeds for the grouped booking's primary create (no permissions need to
    be attached to this token -- ``create_grouped_event`` sets
    ``group_authorized=True`` on the underlying create, which bypasses the
    per-calendar scheduling permission check)."""
    CalendarManagementToken.objects.create(
        organization=organization,
        calendar_fk=calendar,
        membership_user_id=reschedule_admin_user.id,
        token_hash=f"quota-reschedule-cal-{uuid.uuid4()}",
    )
    cs = CalendarService()
    cs.initialize_without_provider(user_or_token=reschedule_admin_user, organization=organization)
    return cs


@pytest.fixture
def reschedule_service(
    organization: Organization, reschedule_calendar_service: CalendarService, audit_service
) -> CalendarGroupService:
    svc = CalendarGroupService(
        calendar_service=reschedule_calendar_service,
        calendar_permission_service=CalendarPermissionService(),
        audit_service=audit_service,
    )
    svc.initialize(organization=organization)
    return svc


def _grant_reschedule_permission(
    organization: Organization, user: User, event: CalendarEvent
) -> CalendarManagementToken:
    """Mint an event-scoped token with RESCHEDULE so
    ``CalendarEventService.update_event``'s ``can_perform_update`` check
    passes for a subsequent ``reschedule_grouped_event`` call on ``event``."""
    token = CalendarManagementToken.objects.create(
        organization=organization,
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"quota-reschedule-evt-{uuid.uuid4()}",
    )
    token.permissions.create(
        organization=organization, permission=EventManagementPermissions.RESCHEDULE
    )
    return token


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
    single-calendar slot selection (mirrors the quota period-counting tests'
    ``_create_group_booking`` helper). Used to seed MULTIPLE pre-existing
    bookings without going through the full booking write pipeline, whose
    ``CalendarEvent.external_id`` is server-generated and left blank
    (globally unique, INTERNAL provider,
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
    # The configured cap (1) must NOT leak into the message either -- strip
    # out the calendar id occurrence first so a coincidental digit overlap
    # between the cap and the (DB-assigned) calendar id can't mask a
    # regression that actually embeds the cap.
    message_without_calendar_id = error_msg.replace(str(calendar.id), "")
    assert "1" not in message_without_calendar_id


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
# Reschedule self-exclusion: the event-being-moved's own still-present
# booking row must not count against itself when validating the reschedule.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reschedule_same_day_at_daily_cap_succeeds(
    reschedule_service: CalendarGroupService,
    reschedule_admin_user: User,
    organization: Organization,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """Daily cap=1, one booking at 09:00. Rescheduling THAT SAME booking to
    14:00 the same day must succeed: the candidate period (Sep 1) is the
    booking's own old period, so its own row must be excluded from the count
    used to validate the new time -- it isn't consuming any additional quota,
    just moving within the same period."""
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    event = _book_via_service(reschedule_service, surgery_slot, calendar, _utc(2025, 9, 1, 9))
    _grant_reschedule_permission(organization, reschedule_admin_user, event)

    rescheduled = reschedule_service.reschedule_grouped_event(
        event_id=event.id,
        start_time=_utc(2025, 9, 1, 14),
        end_time=_utc(2025, 9, 1, 14, 30),
        tz="UTC",
    )
    assert rescheduled.start_time == _utc(2025, 9, 1, 14)


@pytest.mark.django_db
def test_reschedule_across_boundary_into_full_period_rejected(
    reschedule_service: CalendarGroupService,
    reschedule_admin_user: User,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """Daily cap=1. The event under reschedule lives on Sep 1; Sep 2 already
    has its OWN (different) booking at the cap. Moving the Sep 1 event INTO
    Sep 2 must be rejected -- the old period (Sep 1) differs from the
    candidate period (Sep 2), so no self-exclusion applies and Sep 2's count
    (from the other booking) is evaluated as-is."""
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    event = _book_via_service(reschedule_service, surgery_slot, calendar, _utc(2025, 9, 1, 9))
    _grant_reschedule_permission(organization, reschedule_admin_user, event)
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 2, 9))

    with pytest.raises(CalendarGroupScopedRuleViolationError) as exc_info:
        reschedule_service.reschedule_grouped_event(
            event_id=event.id,
            start_time=_utc(2025, 9, 2, 14),
            end_time=_utc(2025, 9, 2, 14, 30),
            tz="UTC",
        )
    assert exc_info.value.calendar_id == calendar.id
    assert exc_info.value.rule_type == GroupScopedRuleType.QUOTA_CONSUMED


@pytest.mark.django_db
def test_reschedule_across_boundary_into_period_with_headroom_succeeds(
    reschedule_service: CalendarGroupService,
    reschedule_admin_user: User,
    organization: Organization,
    calendar: Calendar,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """Daily cap=1. The event under reschedule lives on Sep 1; Sep 2 has no
    other bookings. Moving the Sep 1 event into Sep 2 must succeed -- the old
    period differs from the candidate period (no self-exclusion needed), and
    Sep 2's count (0) is under the cap."""
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.DAY,
        cap=1,
    )
    event = _book_via_service(reschedule_service, surgery_slot, calendar, _utc(2025, 9, 1, 9))
    _grant_reschedule_permission(organization, reschedule_admin_user, event)

    rescheduled = reschedule_service.reschedule_grouped_event(
        event_id=event.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 9, 30),
        tz="UTC",
    )
    assert rescheduled.start_time == _utc(2025, 9, 2, 9)


# ---------------------------------------------------------------------------
# Sunday week start -- exercises the shift-truncate-shift bucketing branch a
# default-Monday fixture never hits.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_weekly_quota_with_sunday_week_start_hides_calendar_across_boundary(
    service: CalendarGroupService,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    """Sunday week start: the week containing Sunday 2025-09-07 runs
    [Sep 7 - Sep 13]. Two bookings inside that Sunday-started week (one on
    the Sunday itself, one on the following Monday) at cap=2 must hide the
    calendar for the REST of that week, while the calendar remains offered on
    Sep 6 (the last day of the PRECEDING Sunday-started week, [Aug 31 - Sep
    6]) -- a boundary a Monday-week-start fixture never straddles the same
    way."""
    organization = calendar.organization
    organization.week_start = WeekStart.SUNDAY
    organization.save(update_fields=["week_start"])

    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.WEEK,
        cap=2,
    )
    # Sunday 2025-09-07 and Monday 2025-09-08 are in the SAME Sunday-started
    # week [Sep 7 - Sep 13].
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 7, 9))
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 8, 9))

    # Sep 9 (Tuesday) is still inside the now-capped [Sep 7 - Sep 13] week.
    still_in_capped_week = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [(_utc(2025, 9, 9, 10), _utc(2025, 9, 9, 10, 30))],
    )
    assert still_in_capped_week[0].slots[0].available_calendar_ids == []

    # Sep 6 (Saturday) is the last day of the PRECEDING Sunday-started week
    # [Aug 31 - Sep 6] -- untouched by the cap.
    preceding_week = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [(_utc(2025, 9, 6, 10), _utc(2025, 9, 6, 10, 30))],
    )
    assert preceding_week[0].slots[0].available_calendar_ids == [calendar.id]


# ---------------------------------------------------------------------------
# Monthly quota rule, bookings across a month boundary.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_monthly_quota_hides_calendar_across_month_boundary(
    service: CalendarGroupService,
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    # The `calendar` fixture's base AvailableTime block only runs through
    # 2025-09-29 -- extend it into October so base availability isn't the
    # bottleneck for the October assertion below (mirrors the fixture's own
    # single-wide-block approach).
    AvailableTime.objects.create(
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=_utc(2025, 9, 29),
        end_time_tz_unaware=_utc(2025, 10, 3),
        timezone="UTC",
    )
    create_group_slot_quota_rule(
        organization=organization,
        group_slot=surgery_slot,
        calendar=calendar,
        period=QuotaPeriod.MONTH,
        cap=2,
    )
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 5, 9))
    _seed_booking(organization, surgery_group, surgery_slot, calendar, _utc(2025, 9, 20, 9))

    # Still September -- at cap, hidden.
    september_result = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [(_utc(2025, 9, 25, 10), _utc(2025, 9, 25, 10, 30))],
    )
    assert september_result[0].slots[0].available_calendar_ids == []

    # October -- a fresh month bucket, no bookings yet, offered again.
    october_result = service.check_group_availability(
        surgery_slot.group_fk_id,  # type: ignore[arg-type]
        [(_utc(2025, 10, 1, 10), _utc(2025, 10, 1, 10, 30))],
    )
    assert october_result[0].slots[0].available_calendar_ids == [calendar.id]


# ---------------------------------------------------------------------------
# Python/SQL bucketing pin test -- the Python-side `quota_period_start_utc`
# and the SQL `get_calendar_group_quota_period_counts_json` function must
# always bucket the SAME instant into the SAME period, or the count fetched
# via SQL will silently disagree with the period the Python side looks it up
# under.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_python_and_sql_bucketing_agree_on_period_start(
    organization: Organization,
    calendar: Calendar,
    surgery_group: CalendarGroup,
    surgery_slot: CalendarGroupSlot,
) -> None:
    seed_instants = [
        _utc(2025, 9, 3, 9),  # mid-week Wednesday
        _utc(2025, 9, 7, 23, 30),  # Sunday, near midnight
        _utc(2025, 9, 30, 9),  # end of month
    ]
    for instant in seed_instants:
        _seed_booking(organization, surgery_group, surgery_slot, calendar, instant)

    range_start = _utc(2025, 8, 25)
    range_end = _utc(2025, 10, 5)

    for period, week_start in (
        (QuotaPeriod.DAY, WeekStart.MONDAY),
        (QuotaPeriod.WEEK, WeekStart.MONDAY),
        (QuotaPeriod.WEEK, WeekStart.SUNDAY),
        (QuotaPeriod.MONTH, WeekStart.MONDAY),
    ):
        row = (
            Calendar.objects.filter_by_organization(organization.id)
            .filter(id=calendar.id)
            .annotate(
                quota_counts=GetCalendarGroupQuotaPeriodCountsJSON(
                    "id",
                    surgery_slot.id,
                    organization.id,
                    period,
                    week_start,
                    range_start,
                    range_end,
                )
            )
            .values_list("quota_counts", flat=True)
            .first()
        )
        sql_period_starts = {
            datetime.datetime.fromisoformat(bucket["period_start"]) for bucket in (row or ())
        }

        expected_period_starts = {
            slot_engine.quota_period_start_utc(instant, period, week_start)
            for instant in seed_instants
        }

        assert sql_period_starts == expected_period_starts, (period, week_start)


# ---------------------------------------------------------------------------
# `check_group_availability` quota-counting query count is fixed per
# configured (slot, period) combination, not per range.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_check_group_availability_quota_query_count_independent_of_range_count(
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

    few_ranges = [(_utc(2025, 9, 1, 9), _utc(2025, 9, 1, 9, 30))]
    many_ranges = [
        (
            WEEK1_MONDAY + datetime.timedelta(minutes=5 * i),
            WEEK1_MONDAY + datetime.timedelta(minutes=5 * i + 30),
        )
        for i in range(200)
    ]

    with CaptureQueriesContext(connection) as few:
        service.check_group_availability(
            surgery_slot.group_fk_id,  # type: ignore[arg-type]
            few_ranges,
        )
    with CaptureQueriesContext(connection) as many:
        service.check_group_availability(
            surgery_slot.group_fk_id,  # type: ignore[arg-type]
            many_ranges,
        )

    few_quota_queries = _quota_query_count(few)
    many_quota_queries = _quota_query_count(many)
    assert few_quota_queries == 1
    assert many_quota_queries == 1
    assert few_quota_queries == many_quota_queries


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
