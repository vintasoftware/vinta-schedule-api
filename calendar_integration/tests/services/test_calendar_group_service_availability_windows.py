"""Tests for group-scoped availability window writes on ``CalendarGroupService``
(Phase 1a of ``CALENDAR_GROUP_SCOPED_AVAILABILITY``).

Covers create/update/delete through the explicit group-scoped accessor,
recurrence + per-window timezone round-trip, audit emission with before/after
diffs on update, permission gating (owner-within-group or org admin, with a
member unable to learn a group exists through the error shape), and
orphaned-booking detection on a narrowing update.
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
    AvailableTime,
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
from organizations.models import Organization, OrganizationMembership, OrganizationRole
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
    return Organization.objects.create(name="Windows Test Org", should_sync_rooms=False)


@pytest.fixture
def audit_service() -> AuditService:
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
def owner_user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="owner@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u, organization=organization, role=OrganizationRole.MEMBER
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
        user=u, organization=organization, role=OrganizationRole.MEMBER
    )
    return u


@pytest.fixture
def stranger_user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="stranger@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u, organization=organization, role=OrganizationRole.MEMBER
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
# create_group_scoped_availability_window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_group_scoped_availability_window_admin_happy_path(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            result = service.create_group_scoped_availability_window(
                acting_user=admin_user,
                group_slot_id=group_slot.id,
                calendar_id=calendar.id,
                start_time=_utc(2025, 9, 2, 9),
                end_time=_utc(2025, 9, 2, 17),
                tz="UTC",
            )

    window = result.window
    assert window is not None
    assert result.orphaned_bookings == []
    assert window.group_slot_fk_id == group_slot.id
    assert window.calendar_fk_id == calendar.id

    # Invisible on the default (base-rows-only) manager...
    assert (
        not AvailableTime.objects.filter_by_organization(service.organization.id)
        .filter(id=window.id)
        .exists()
    )
    # ...and visible through the explicit group-scoped accessor.
    assert (
        AvailableTime.objects.for_group_slot(group_slot.id)
        .filter_by_organization(service.organization.id)
        .get(id=window.id)
        == window
    )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.CREATE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.AvailableTime"
    assert payloads[0]["subject"]["subject_id"] == str(window.pk)


@pytest.mark.django_db
def test_create_group_scoped_availability_window_owner_happy_path(
    service: CalendarGroupService,
    owner_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    result = service.create_group_scoped_availability_window(
        acting_user=owner_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    assert result.window is not None
    assert result.window.calendar_fk_id == calendar.id


@pytest.mark.django_db
def test_create_group_scoped_availability_window_denies_non_owner_without_disclosing_group(
    service: CalendarGroupService,
    stranger_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    with pytest.raises(CalendarGroupSlotConfigNotFoundError) as excinfo:
        service.create_group_scoped_availability_window(
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
        service.create_group_scoped_availability_window(
            acting_user=stranger_user,
            group_slot_id=group_slot.id,
            calendar_id=calendar.id + 999_999,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    assert str(excinfo_missing.value) == stranger_message


@pytest.mark.django_db
def test_create_group_scoped_availability_window_denies_owner_outside_target_calendar(
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
        service.create_group_scoped_availability_window(
            acting_user=other_owner_user,
            group_slot_id=group_slot.id,
            calendar_id=calendar.id,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    assert not AvailableTime.objects.unscoped().filter(group_slot_fk=group_slot).exists()


@pytest.mark.django_db
def test_create_group_scoped_availability_window_recurrence_and_timezone_round_trip(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    result = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),  # 2025-09-02 is a Tuesday
        end_time=_utc(2025, 9, 2, 17),
        tz="America/Sao_Paulo",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    window = result.window
    assert window is not None
    assert window.timezone == "America/Sao_Paulo"
    assert window.recurrence_rule is not None
    assert window.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TU,TH"

    # Read back through the group-scoped accessor and expand recurrence over two
    # weeks -- must land on Tuesdays and Thursdays only. Annotating BEFORE calling
    # get_occurrences_in_range caches `recurring_occurrences` on the instance, so
    # the read never falls through RecurringMixin's internal re-fetch via the
    # DEFAULT (base-rows-only) manager -- see the Phase 0 carry-forward note.
    range_start = _utc(2025, 9, 1, 0)
    range_end = _utc(2025, 9, 15, 0)
    master = (
        AvailableTime.objects.for_group_slot(group_slot.id)
        .filter_by_organization(service.organization.id)
        .annotate_recurring_occurrences_on_date_range(range_start, range_end)
        .get(id=window.id)
    )
    occurrences = master.get_occurrences_in_range(range_start, range_end, include_self=True)
    weekdays = sorted({o.start_time.weekday() for o in occurrences})
    assert weekdays == [1, 3]  # Tuesday, Thursday
    assert len(occurrences) == 4  # two Tuesdays + two Thursdays in the range


# ---------------------------------------------------------------------------
# update_group_scoped_availability_window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_group_scoped_availability_window_records_diff(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            result = service.update_group_scoped_availability_window(
                acting_user=admin_user,
                window_id=window_id,
                rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TH",
            )

    assert result.window is not None
    assert result.window.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TH"

    payloads = _payloads(mock_task)
    update_payloads = [p for p in payloads if p["action"] == AuditAction.UPDATE]
    assert len(update_payloads) == 1
    diff = update_payloads[0]["diff"]
    assert diff is not None
    assert "rrule" in diff
    assert diff["rrule"]["old"] == "FREQ=WEEKLY;BYDAY=TU,TH"
    assert diff["rrule"]["new"] == "FREQ=WEEKLY;BYDAY=TH"


@pytest.mark.django_db
def test_update_group_scoped_availability_window_timezone_round_trip(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    result = service.update_group_scoped_availability_window(
        acting_user=admin_user,
        window_id=window_id,
        tz="America/Sao_Paulo",
    )
    assert result.window is not None
    assert result.window.timezone == "America/Sao_Paulo"

    reloaded = (
        AvailableTime.objects.unscoped()
        .filter_by_organization(service.organization.id)
        .get(id=window_id)
    )
    assert reloaded.timezone == "America/Sao_Paulo"


@pytest.mark.django_db
def test_update_group_scoped_availability_window_denies_non_owner(
    service: CalendarGroupService,
    admin_user: User,
    stranger_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with pytest.raises(CalendarGroupSlotConfigNotFoundError):
        service.update_group_scoped_availability_window(
            acting_user=stranger_user, window_id=window_id, tz="America/Sao_Paulo"
        )

    reloaded = (
        AvailableTime.objects.unscoped()
        .filter_by_organization(service.organization.id)
        .get(id=window_id)
    )
    assert reloaded.timezone == "UTC"  # untouched


@pytest.mark.django_db
def test_update_group_scoped_availability_window_narrowing_returns_orphaned_bookings_untouched(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """UC-6: narrowing Tuesday+Thursday down to Thursday-only returns the future
    Tuesday booking as orphaned, and modifies neither the event nor the group
    selection."""
    now = django_timezone.now()
    # The window's own recurrence anchor is `tuesday` -- recurrence only generates
    # occurrences forward from its own start, so `thursday` must fall in the SAME
    # week, strictly after `tuesday` (picking it independently via `_next_weekday`
    # could land it chronologically BEFORE the window's anchor, e.g. if "today" is
    # a Wednesday -- the nearest future Thursday would then precede the nearest
    # future Tuesday, and no occurrence could ever generate for it).
    tuesday = _next_weekday(now, weekday=1)
    thursday = tuesday + datetime.timedelta(days=2)

    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(tuesday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(tuesday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    tuesday_event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Operation",
        description="",
        external_id="ev_tuesday",
        start_time_tz_unaware=datetime.datetime.combine(
            tuesday, datetime.time(10), tzinfo=datetime.UTC
        ),
        end_time_tz_unaware=datetime.datetime.combine(
            tuesday, datetime.time(11), tzinfo=datetime.UTC
        ),
        timezone="UTC",
        calendar_group=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=tuesday_event,
        slot=group_slot,
        calendar=calendar,
    )

    thursday_event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Operation",
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
        event=thursday_event,
        slot=group_slot,
        calendar=calendar,
    )

    result = service.update_group_scoped_availability_window(
        acting_user=admin_user,
        window_id=window_id,
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TH",
        now=now,
    )

    orphaned_ids = {e.id for e in result.orphaned_bookings}
    assert orphaned_ids == {tuesday_event.id}

    # Nothing about either booking was touched.
    tuesday_event.refresh_from_db()
    thursday_event.refresh_from_db()
    assert tuesday_event.title == "Operation"
    assert CalendarEvent.objects.filter_by_organization(service.organization.id).count() == 2
    assert (
        CalendarEventGroupSelection.objects.filter_by_organization(service.organization.id).count()
        == 2
    )


# ---------------------------------------------------------------------------
# delete_group_scoped_availability_window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_group_scoped_availability_window_admin(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    django_capture_on_commit_callbacks,
) -> None:
    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.delete_group_scoped_availability_window(
                acting_user=admin_user, window_id=window_id
            )

    assert not AvailableTime.objects.unscoped().filter(id=window_id).exists()
    payloads = _payloads(mock_task)
    delete_payloads = [p for p in payloads if p["action"] == AuditAction.DELETE]
    assert len(delete_payloads) == 1
    assert delete_payloads[0]["subject"]["subject_id"] == str(window_id)


@pytest.mark.django_db
def test_delete_group_scoped_availability_window_denies_non_owner(
    service: CalendarGroupService,
    admin_user: User,
    stranger_user: User,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with pytest.raises(CalendarGroupSlotConfigNotFoundError):
        service.delete_group_scoped_availability_window(
            acting_user=stranger_user, window_id=window_id
        )
    assert AvailableTime.objects.unscoped().filter(id=window_id).exists()


# ---------------------------------------------------------------------------
# Cascade through this service path (schema-enforced by Phase 0)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_slot_through_update_group_cascades_group_scoped_windows(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    created = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]
    assert AvailableTime.objects.unscoped().filter(id=window_id).exists()

    # Reconcile the group with no slots at all -- CalendarGroupService.update_group
    # deletes the now-absent "Lead Surgeon" slot, which cascades (on_delete=CASCADE
    # on AvailableTime.group_slot, established in Phase 0) to every group-scoped
    # window that referenced it.
    service.update_group(group.id, CalendarGroupInputData(name=group.name, slots=[]))

    assert (
        not CalendarGroupSlot.objects.filter_by_organization(service.organization.id)
        .filter(id=group_slot.id)
        .exists()
    )
    assert not AvailableTime.objects.unscoped().filter(id=window_id).exists()


# ---------------------------------------------------------------------------
# FIX 1: Removing a calendar from a slot removes its group-scoped windows
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_removing_calendar_from_slot_removes_group_scoped_windows(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    other_calendar: Calendar,
    group: CalendarGroup,
    django_capture_on_commit_callbacks,
) -> None:
    """FIX 1 (BLOCKER): When removing a calendar from a slot's membership,
    its group-scoped availability windows must be deleted (not orphaned).
    A second calendar's windows in the same slot survive.
    Each deleted window is audited with a DELETE action naming the actor.
    """
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

    # Create group-scoped windows for BOTH calendars.
    window1_result = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window1_id = window1_result.window.id  # type: ignore[union-attr]

    window2_result = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=slot.id,
        calendar_id=other_calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window2_id = window2_result.window.id  # type: ignore[union-attr]

    # Verify both windows exist.
    assert AvailableTime.objects.unscoped().filter(id=window1_id).exists()
    assert AvailableTime.objects.unscoped().filter(id=window2_id).exists()

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

    # The first calendar's window must be deleted.
    assert not AvailableTime.objects.unscoped().filter(id=window1_id).exists()
    # The second calendar's window must survive.
    assert AvailableTime.objects.unscoped().filter(id=window2_id).exists()
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

    # Verify that a DELETE audit record was emitted for the deleted window.
    payloads = _payloads(mock_task)
    delete_payloads = [p for p in payloads if p["action"] == AuditAction.DELETE]
    # Should have at least one DELETE for window1 (may also have UPDATE for group).
    window_delete_payloads = [
        p
        for p in delete_payloads
        if p["subject"]["subject_type"] == "calendar_integration.AvailableTime"
    ]
    assert len(window_delete_payloads) == 1
    assert window_delete_payloads[0]["subject"]["subject_id"] == str(window1_id)


# ---------------------------------------------------------------------------
# FIX 2: Creating first window detects orphaned bookings
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_group_scoped_availability_window_first_detects_orphaned_bookings(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """FIX 2 (SHOULD-FIX): Creating the FIRST group-scoped window for a
    (calendar, slot) flips from fall-through (base availability) to narrowed
    evaluation, which can orphan pre-existing bookings. Return them in
    orphaned_bookings; do NOT modify/cancel them.
    """
    now = django_timezone.now()
    # Pick a date in the future that will be a Thursday.
    thursday = _next_weekday(now, weekday=3)

    # Create a booking for Thursday outside the window we'll create.
    booking = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Operation",
        description="",
        external_id="ev_thursday",
        start_time_tz_unaware=datetime.datetime.combine(
            thursday,
            datetime.time(18),
            tzinfo=datetime.UTC,  # 6pm = outside window
        ),
        end_time_tz_unaware=datetime.datetime.combine(
            thursday, datetime.time(19), tzinfo=datetime.UTC
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

    # Create the FIRST group-scoped window for this (calendar, slot):
    # 9am-5pm on Thursdays. The 6pm booking is now orphaned.
    result = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(thursday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(thursday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )

    # The booking must be returned as orphaned.
    orphaned_ids = {e.id for e in result.orphaned_bookings}
    assert orphaned_ids == {booking.id}

    # The booking itself must be untouched (not cancelled).
    booking.refresh_from_db()
    assert booking.title == "Operation"
    assert CalendarEvent.objects.filter_by_organization(service.organization.id).count() == 1


@pytest.mark.django_db
def test_create_group_scoped_availability_window_second_plus_no_orphans(
    service: CalendarGroupService,
    admin_user: User,
    calendar: Calendar,
    group: CalendarGroup,
    group_slot: CalendarGroupSlot,
) -> None:
    """When creating a SECOND (or later) group-scoped window, it only widens
    the union and cannot orphan bookings. Verify orphaned_bookings=[] even
    if a booking sits outside this specific window (but within an existing one).
    """
    now = django_timezone.now()
    thursday = _next_weekday(now, weekday=3)
    tuesday = thursday - datetime.timedelta(days=2)

    # Create the FIRST window: Tuesday 9am-5pm.
    service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(tuesday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(tuesday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )

    # Create a booking on Thursday (outside the first window).
    booking = CalendarEvent.objects.create(
        organization=service.organization,
        calendar=calendar,
        title="Operation",
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

    # Create the SECOND window: Thursday 9am-5pm. Union now covers both days.
    result = service.create_group_scoped_availability_window(
        acting_user=admin_user,
        group_slot_id=group_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(thursday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(thursday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )

    # No booking is orphaned (widening the union cannot orphan).
    assert result.orphaned_bookings == []
