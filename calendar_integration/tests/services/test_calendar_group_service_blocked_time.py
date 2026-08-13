"""Tests for group-scoped blocked-time writes on ``CalendarGroupService``
(Phase 2a of ``CALENDAR_GROUP_SCOPED_AVAILABILITY``).

Covers create/update/delete through the explicit group-scoped accessor,
recurrence + per-block timezone round-trip, audit emission with before/after
diffs on update, permission gating (owner-within-group or org admin, with a
member unable to learn a group exists through the error shape), and
orphaned-booking detection -- on every create (not just the first, unlike
windows) and on update.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

from django.utils import timezone as django_timezone

import pytest

from audit.constants import AuditAction
from audit.services import AuditService
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import CalendarGroupSlotConfigNotFoundError
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization, OrganizationMembership
from organizations.tests.helpers import grant_membership_groups
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


def _next_weekday(after: datetime.datetime, weekday: int) -> datetime.date:
    """Next date (strictly after `after`'s date) landing on ISO `weekday`
    (Monday=0 ... Sunday=6)."""
    days_ahead = (weekday - after.weekday()) % 7
    days_ahead = days_ahead or 7
    return (after + datetime.timedelta(days=days_ahead)).date()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Blocks Test Org", should_sync_rooms=False)


@pytest.fixture
def audit_service() -> AuditService:
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
        )
    )
    return u


@pytest.fixture
def owner_user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="owner@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u,
        organization=organization,
    )
    return u


@pytest.fixture
def other_owner_user(db: Any, organization: Organization) -> User:
    """Owns a DIFFERENT calendar (not the one under test) -- used to prove that
    being a member of the org (and even owning some calendar in the group) is
    not enough; only the target calendar's own owner may edit it."""
    u = User.objects.create_user(email="other_owner@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u,
        organization=organization,
    )
    return u


@pytest.fixture
def stranger_user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="stranger@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u,
        organization=organization,
    )
    return u


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="dr_reyes",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture
def other_calendar(organization: Organization) -> Calendar:
    """A second calendar in the same group (different slot) -- owned by
    `other_owner_user`, so that fixture can "see" the group without owning
    `calendar`."""
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Costa",
        external_id="dr_costa",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


@pytest.fixture(autouse=True)
def _ownerships(
    organization: Organization,
    owner_user: User,
    other_owner_user: User,
    calendar: Calendar,
    other_calendar: Calendar,
) -> None:
    CalendarOwnership.objects.create(
        organization=organization, calendar=calendar, membership_user_id=owner_user.id
    )
    CalendarOwnership.objects.create(
        organization=organization,
        calendar=other_calendar,
        membership_user_id=other_owner_user.id,
    )


@pytest.fixture
def group(organization: Organization) -> CalendarGroup:
    return CalendarGroup.objects.create(organization=organization, name="Surgery")


@pytest.fixture
def group_slot(
    organization: Organization, group: CalendarGroup, calendar: Calendar
) -> CalendarGroupSlot:
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Lead Surgeon"
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def other_slot(
    organization: Organization, group: CalendarGroup, other_calendar: Calendar
) -> CalendarGroupSlot:
    """A second slot in the same group, populated with `other_calendar` -- makes
    `other_owner_user` a genuine member of the group without owning `calendar`."""
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Assist")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=other_calendar
    )
    return slot


@pytest.fixture
def service(organization: Organization, audit_service: AuditService) -> CalendarGroupService:
    svc = CalendarGroupService(
        calendar_permission_service=CalendarPermissionService(),
        audit_service=audit_service,
    )
    svc.initialize(organization=organization)
    return svc


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# create_group_scoped_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_admin_happy_path(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            result = service.create_group_scoped_blocked_time(
                acting_user=admin_user,
                group_slot_id=group_slot.id,
                calendar_id=calendar.id,
                start_time=_utc(2025, 9, 2, 9),
                end_time=_utc(2025, 9, 2, 17),
                tz="UTC",
                reason="Conference",
            )

    block = result.block
    assert block is not None
    assert result.orphaned_bookings == []
    assert block.group_slot_fk_id == group_slot.id
    assert block.calendar_fk_id == calendar.id
    assert block.reason == "Conference"

    # Invisible on the default (base-rows-only) manager...
    assert (
        not BlockedTime.objects.filter_by_organization(service.organization.id)
        .filter(id=block.id)
        .exists()
    )
    # ...and visible through the explicit group-scoped accessor.
    assert (
        BlockedTime.objects.for_group_slot(group_slot.id)
        .filter_by_organization(service.organization.id)
        .get(id=block.id)
        == block
    )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.CREATE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.BlockedTime"
    assert payloads[0]["subject"]["subject_id"] == str(block.pk)


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_owner_happy_path(
    service: CalendarGroupService,
    owner_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    result = service.create_group_scoped_blocked_time(
        acting_user=owner_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    assert result.block is not None
    assert result.block.calendar_fk_id == calendar.id


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_denies_non_owner_without_disclosing_group(
    service: CalendarGroupService,
    stranger_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    with pytest.raises(CalendarGroupSlotConfigNotFoundError) as excinfo:
        service.create_group_scoped_blocked_time(
            acting_user=stranger_user,
            group_slot_id=group_slot.id,
            calendar_id=calendar.id,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    stranger_message = str(excinfo.value)

    # A genuinely missing (group_slot_id, calendar_id) pairing must raise the
    # exact same exception, message included -- a caller cannot distinguish
    # "forbidden" from "does not exist" from the error alone.
    with pytest.raises(CalendarGroupSlotConfigNotFoundError) as excinfo_missing:
        service.create_group_scoped_blocked_time(
            acting_user=stranger_user,
            group_slot_id=group_slot.id,
            calendar_id=calendar.id + 999_999,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    assert str(excinfo_missing.value) == stranger_message


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_denies_owner_outside_target_calendar(
    service: CalendarGroupService,
    other_owner_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    other_slot: CalendarGroupSlot,
) -> None:
    """`other_owner_user` owns a calendar in the SAME group (a different slot),
    so they can see the group -- but they do not own `calendar`, so they must
    still be denied, with the same not-found-shaped error."""
    with pytest.raises(CalendarGroupSlotConfigNotFoundError):
        service.create_group_scoped_blocked_time(
            acting_user=other_owner_user,
            group_slot_id=group_slot.id,
            calendar_id=calendar.id,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    assert not BlockedTime.objects.unscoped().filter(group_slot_fk=group_slot).exists()


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_recurrence_and_timezone_round_trip(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    result = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),  # 2025-09-02 is a Tuesday
        end_time=_utc(2025, 9, 2, 17),
        tz="America/Sao_Paulo",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    block = result.block
    assert block is not None
    assert block.timezone == "America/Sao_Paulo"
    assert block.recurrence_rule is not None
    assert block.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TU,TH"

    range_start = _utc(2025, 9, 1, 0)
    range_end = _utc(2025, 9, 15, 0)
    master = (
        BlockedTime.objects.for_group_slot(group_slot.id)
        .filter_by_organization(service.organization.id)
        .annotate_recurring_occurrences_on_date_range(range_start, range_end)
        .get(id=block.id)
    )
    occurrences = master.get_occurrences_in_range(range_start, range_end, include_self=True)
    weekdays = sorted({o.start_time.weekday() for o in occurrences})
    assert weekdays == [1, 3]  # Tuesday, Thursday
    assert len(occurrences) == 4  # two Tuesdays + two Thursdays in the range


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_unique_external_id_per_calendar(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    """Two blocks on the same calendar must not collide on the
    (calendar, external_id) uniqueness constraint."""
    first = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 12),
        tz="UTC",
    )
    second = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 4, 9),
        end_time=_utc(2025, 9, 4, 12),
        tz="UTC",
    )
    assert first.block.id != second.block.id  # type: ignore[union-attr]


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_detects_orphaned_bookings_on_every_create(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """Unlike windows (only the FIRST one can orphan), every group-scoped
    BLOCK independently subtracts time, so orphaned-booking detection must
    run on every create -- verified here on a SECOND block."""
    now = django_timezone.now()
    tuesday = _next_weekday(now, weekday=1)
    thursday = tuesday + datetime.timedelta(days=2)

    # First block: Tuesday 9-17 -- no bookings yet, nothing orphaned.
    first = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(tuesday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(tuesday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )
    assert first.orphaned_bookings == []

    # A confirmed future booking on Thursday, outside the first block.
    booking = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Consult",
        description="",
        external_id="ev_thursday",
        start_time_tz_unaware=datetime.datetime.combine(
            thursday, datetime.time(10), tzinfo=datetime.UTC
        ),
        end_time_tz_unaware=datetime.datetime.combine(
            thursday, datetime.time(11), tzinfo=datetime.UTC
        ),
        timezone="UTC",
        calendar_group=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=booking,
        slot=group_slot,
        calendar=calendar,
    )

    # Second block: Thursday 9-17 -- now overlaps the booking. Must be reported.
    second = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(thursday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(thursday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )
    orphaned_ids = {e.id for e in second.orphaned_bookings}
    assert orphaned_ids == {booking.id}

    # The booking itself must be untouched (not cancelled).
    booking.refresh_from_db()
    assert booking.title == "Consult"
    assert CalendarEvent.objects.filter_by_organization(service.organization.id).count() == 1


# ---------------------------------------------------------------------------
# update_group_scoped_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_group_scoped_blocked_time_records_diff(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        reason="Conference",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            result = service.update_group_scoped_blocked_time(
                acting_user=admin_user,
                block_id=block_id,
                reason="Conference (extended)",
                rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TH",
            )

    assert result.block is not None
    assert result.block.reason == "Conference (extended)"
    assert result.block.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TH"

    payloads = _payloads(mock_task)
    update_payloads = [p for p in payloads if p["action"] == AuditAction.UPDATE]
    assert len(update_payloads) == 1
    diff = update_payloads[0]["diff"]
    assert diff is not None
    assert diff["reason"]["old"] == "Conference"
    assert diff["reason"]["new"] == "Conference (extended)"
    assert diff["rrule"]["old"] == "FREQ=WEEKLY;BYDAY=TU,TH"
    assert diff["rrule"]["new"] == "FREQ=WEEKLY;BYDAY=TH"


@pytest.mark.django_db
def test_update_group_scoped_blocked_time_timezone_round_trip(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    result = service.update_group_scoped_blocked_time(
        acting_user=admin_user,
        block_id=block_id,
        tz="America/Sao_Paulo",
    )
    assert result.block is not None
    assert result.block.timezone == "America/Sao_Paulo"

    reloaded = (
        BlockedTime.objects.unscoped()
        .filter_by_organization(service.organization.id)
        .get(id=block_id)
    )
    assert reloaded.timezone == "America/Sao_Paulo"


@pytest.mark.django_db
def test_update_group_scoped_blocked_time_explicit_none_clears_recurrence(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    result = service.update_group_scoped_blocked_time(
        acting_user=admin_user,
        block_id=block_id,
        rrule_string=None,
    )
    assert result.block is not None
    assert result.block.recurrence_rule is None
    assert result.block.is_recurring is False

    reloaded = (
        BlockedTime.objects.unscoped()
        .filter_by_organization(service.organization.id)
        .get(id=block_id)
    )
    assert reloaded.recurrence_rule is None
    assert reloaded.is_recurring is False


@pytest.mark.django_db
def test_update_group_scoped_blocked_time_omitted_rrule_string_leaves_it_unchanged(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    result = service.update_group_scoped_blocked_time(
        acting_user=admin_user,
        block_id=block_id,
        tz="America/Sao_Paulo",
    )
    assert result.block is not None
    assert result.block.recurrence_rule is not None
    assert result.block.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TU,TH"


@pytest.mark.django_db
def test_update_group_scoped_blocked_time_denies_non_owner(
    service: CalendarGroupService,
    admin_user: User,
    stranger_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    with pytest.raises(CalendarGroupSlotConfigNotFoundError):
        service.update_group_scoped_blocked_time(
            acting_user=stranger_user, block_id=block_id, tz="America/Sao_Paulo"
        )

    reloaded = (
        BlockedTime.objects.unscoped()
        .filter_by_organization(service.organization.id)
        .get(id=block_id)
    )
    assert reloaded.timezone == "UTC"  # untouched


@pytest.mark.django_db
def test_update_group_scoped_blocked_time_reports_newly_orphaned_booking(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """Extending a block to now overlap a previously-unaffected booking reports
    it as orphaned, and modifies neither the event nor the group selection."""
    now = django_timezone.now()
    tuesday = _next_weekday(now, weekday=1)

    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(tuesday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(tuesday, datetime.time(12), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )
    block_id = created.block.id  # type: ignore[union-attr]

    booking = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Consult",
        description="",
        external_id="ev_afternoon",
        start_time_tz_unaware=datetime.datetime.combine(
            tuesday, datetime.time(14), tzinfo=datetime.UTC
        ),
        end_time_tz_unaware=datetime.datetime.combine(
            tuesday, datetime.time(15), tzinfo=datetime.UTC
        ),
        timezone="UTC",
        calendar_group=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=booking,
        slot=group_slot,
        calendar=calendar,
    )

    # Extend the block to 9-17, now covering the 14:00 booking.
    result = service.update_group_scoped_blocked_time(
        acting_user=admin_user,
        block_id=block_id,
        end_time=datetime.datetime.combine(tuesday, datetime.time(17), tzinfo=datetime.UTC),
        now=now,
    )

    orphaned_ids = {e.id for e in result.orphaned_bookings}
    assert orphaned_ids == {booking.id}

    booking.refresh_from_db()
    assert booking.title == "Consult"
    assert CalendarEvent.objects.filter_by_organization(service.organization.id).count() == 1
    assert (
        CalendarEventGroupSelection.objects.filter_by_organization(service.organization.id).count()
        == 1
    )


# ---------------------------------------------------------------------------
# delete_group_scoped_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_group_scoped_blocked_time_admin(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.delete_group_scoped_blocked_time(acting_user=admin_user, block_id=block_id)

    assert not BlockedTime.objects.unscoped().filter(id=block_id).exists()
    payloads = _payloads(mock_task)
    delete_payloads = [p for p in payloads if p["action"] == AuditAction.DELETE]
    assert len(delete_payloads) == 1
    assert delete_payloads[0]["subject"]["subject_id"] == str(block_id)


@pytest.mark.django_db
def test_delete_group_scoped_blocked_time_denies_non_owner(
    service: CalendarGroupService,
    admin_user: User,
    stranger_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block_id = created.block.id  # type: ignore[union-attr]

    with pytest.raises(CalendarGroupSlotConfigNotFoundError):
        service.delete_group_scoped_blocked_time(acting_user=stranger_user, block_id=block_id)
    assert BlockedTime.objects.unscoped().filter(id=block_id).exists()


# ---------------------------------------------------------------------------
# Cascade through this service path (schema-enforced by Phase 0)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_slot_through_update_group_cascades_group_scoped_blocks(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block_id = created.block.id  # type: ignore[union-attr]
    assert BlockedTime.objects.unscoped().filter(id=block_id).exists()

    # Reconcile the group with no slots at all -- CalendarGroupService.update_group
    # deletes the now-absent "Lead Surgeon" slot, which cascades (on_delete=CASCADE
    # on BlockedTime.group_slot, established in Phase 0) to every group-scoped
    # block that referenced it.
    service.update_group(group.id, CalendarGroupInputData(name=group.name, slots=[]))

    assert (
        not CalendarGroupSlot.objects.filter_by_organization(service.organization.id)
        .filter(id=group_slot.id)
        .exists()
    )
    assert not BlockedTime.objects.unscoped().filter(id=block_id).exists()


# ---------------------------------------------------------------------------
# Removing a calendar from a slot removes its group-scoped blocks
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_removing_calendar_from_slot_removes_group_scoped_blocks(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    other_calendar: Calendar,
    group: CalendarGroup,
    django_capture_on_commit_callbacks,
) -> None:
    """When removing a calendar from a slot's membership, its group-scoped
    blocked time must be deleted (not orphaned). A second calendar's blocks
    in the same slot survive. Each deleted block is audited with a DELETE
    action naming the actor."""
    # Create a slot with TWO calendars.
    slot = CalendarGroupSlot.objects.create(
        organization=service.organization, group=group, name="Test Slot"
    )
    CalendarGroupSlotMembership.objects.create(
        organization=service.organization, slot=slot, calendar=calendar
    )
    CalendarGroupSlotMembership.objects.create(
        organization=service.organization, slot=slot, calendar=other_calendar
    )

    # Create group-scoped blocks for BOTH calendars.
    block1_result = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block1_id = block1_result.block.id  # type: ignore[union-attr]

    block2_result = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=slot.id,
        calendar_id=other_calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    block2_id = block2_result.block.id  # type: ignore[union-attr]

    # Verify both blocks exist.
    assert BlockedTime.objects.unscoped().filter(id=block1_id).exists()
    assert BlockedTime.objects.unscoped().filter(id=block2_id).exists()

    # Remove ONLY the first calendar from the slot (via update_group → _reconcile_slot).
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_group(
                group.id,
                CalendarGroupInputData(
                    name=group.name,
                    slots=[
                        CalendarGroupSlotInputData(
                            name=slot.name,
                            calendar_ids=[other_calendar.id],
                            required_count=1,
                        )
                    ],
                ),
            )

    # The first calendar's block must be deleted.
    assert not BlockedTime.objects.unscoped().filter(id=block1_id).exists()
    # The second calendar's block must survive.
    assert BlockedTime.objects.unscoped().filter(id=block2_id).exists()
    # The first calendar's membership is gone.
    assert (
        not CalendarGroupSlotMembership.objects.filter_by_organization(service.organization.id)
        .filter(slot_fk=slot, calendar_fk_id=calendar.id)
        .exists()
    )
    # The second calendar's membership remains.
    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(service.organization.id)
        .filter(slot_fk=slot, calendar_fk_id=other_calendar.id)
        .exists()
    )

    # Verify that a DELETE audit record was emitted for the deleted block.
    payloads = _payloads(mock_task)
    delete_payloads = [p for p in payloads if p["action"] == AuditAction.DELETE]
    block_delete_payloads = [
        p
        for p in delete_payloads
        if p["subject"]["subject_type"] == "calendar_integration.BlockedTime"
    ]
    assert len(block_delete_payloads) == 1
    assert block_delete_payloads[0]["subject"]["subject_id"] == str(block1_id)


# ---------------------------------------------------------------------------
# Recurring block orphan detection + partial-overlap orphan detection
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_group_scoped_recurring_blocked_time_detects_orphaned_bookings_on_future_occurrence(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """A recurring block whose LATER occurrence (not the first) overlaps a
    confirmed future booking must report that booking as orphaned on creation,
    before the booking existed. Tests that recurrence-aware expansion catches
    all occurrences."""
    # Use a fixed date to avoid timezone issues: 2025-09-02 is a Tuesday.
    base_date = datetime.date(2025, 9, 2)
    block_start = datetime.datetime.combine(base_date, datetime.time(9), tzinfo=datetime.UTC)
    block_end = datetime.datetime.combine(base_date, datetime.time(17), tzinfo=datetime.UTC)
    now = block_start - datetime.timedelta(days=7)  # Ensure "now" is before the block

    # Create a recurring block: every Tuesday and Thursday, 9-17.
    recurring_block = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=block_start,
        end_time=block_end,
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
        now=now,
    )
    assert recurring_block.block is not None
    assert recurring_block.orphaned_bookings == []

    # Create a booking on the SECOND Thursday (9 days after 2025-09-02).
    # 2025-09-02 = Tuesday
    # 2025-09-04 = Thursday (first Thursday)
    # 2025-09-11 = Thursday (second Thursday)
    second_thursday = base_date + datetime.timedelta(days=9)
    booking = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Future Consult",
        description="",
        external_id="ev_second_thursday",
        start_time_tz_unaware=datetime.datetime.combine(
            second_thursday, datetime.time(10), tzinfo=datetime.UTC
        ),
        end_time_tz_unaware=datetime.datetime.combine(
            second_thursday, datetime.time(11), tzinfo=datetime.UTC
        ),
        timezone="UTC",
        calendar_group=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=booking,
        slot=group_slot,
        calendar=calendar,
    )

    # The EXISTING recurring block (created before the booking) should be reported
    # as orphaning the booking (the second Thursday occurrence overlaps it).
    # We do this by creating a NEW block that overlaps the booking.
    overlapping_block = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=block_start,
        end_time=block_end,
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
        now=now,
    )
    # Now the new block creation should detect the booking as orphaned
    # (because the recurring occurrences overlap it).
    orphaned_ids = {e.id for e in overlapping_block.orphaned_bookings}
    assert orphaned_ids == {booking.id}


@pytest.mark.django_db
def test_create_group_scoped_blocked_time_detects_partially_overlapping_booking_as_orphaned(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """A block that PARTIALLY overlaps a booking (not fully contained) must
    still report the booking as orphaned. The orphan check uses
    `intervals_overlap`, which returns true for any overlap."""
    now = django_timezone.now()
    tuesday = _next_weekday(now, weekday=1)

    # Create a booking: 11:00-13:00
    booking = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Consult",
        description="",
        external_id="ev_partial",
        start_time_tz_unaware=datetime.datetime.combine(
            tuesday, datetime.time(11), tzinfo=datetime.UTC
        ),
        end_time_tz_unaware=datetime.datetime.combine(
            tuesday, datetime.time(13), tzinfo=datetime.UTC
        ),
        timezone="UTC",
        calendar_group=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=booking,
        slot=group_slot,
        calendar=calendar,
    )

    # Create a block that PARTIALLY overlaps: 12:00-14:00 (overlaps 12:00-13:00).
    result = service.create_group_scoped_blocked_time(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(tuesday, datetime.time(12), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(tuesday, datetime.time(14), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )

    # The booking should be reported as orphaned despite only partial overlap.
    orphaned_ids = {e.id for e in result.orphaned_bookings}
    assert orphaned_ids == {booking.id}

    # The booking itself must be untouched (not cancelled).
    booking.refresh_from_db()
    assert booking.title == "Consult"
    assert CalendarEvent.objects.filter_by_organization(service.organization.id).count() == 1
