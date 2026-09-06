"""Tests for appointment-type-scoped availability window writes on ``AppointmentTypeService``.

Covers create/update/delete through the explicit appointment-type-scoped accessor,
recurrence + per-window timezone round-trip, audit emission with before/after
diffs on update, permission gating (owner-within-appointment-type or org admin, with a
member unable to learn an appointment type exists through the error shape), and
orphaned-booking detection on a narrowing update.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

from django.utils import timezone as django_timezone

import pytest

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import AppointmentTypeSlotConfigNotFoundError
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarEventAppointmentTypeSelection,
    CalendarOwnership,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.dataclasses import (
    AppointmentTypeInputData,
    AppointmentTypeSlotInputData,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
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
    return Organization.objects.create(name="Windows Test Org", should_sync_rooms=False)


@pytest.fixture
def audit_service() -> OrganizationAuditService:
    from di_core.containers import get_container

    return get_container().audit_service()


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
    being a member of the org (and even owning some calendar in the appointment type) is
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
    """A second calendar in the same appointment type (different slot) -- owned by
    `other_owner_user`, so that fixture can "see" the appointment type without owning
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
def appointment_type(organization: Organization) -> AppointmentType:
    return AppointmentType.objects.create(organization=organization, name="Surgery")


@pytest.fixture
def appointment_type_slot(
    organization: Organization, appointment_type: AppointmentType, calendar: Calendar
) -> AppointmentTypeSlot:
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Lead Surgeon"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def other_slot(
    organization: Organization, appointment_type: AppointmentType, other_calendar: Calendar
) -> AppointmentTypeSlot:
    """A second slot in the same appointment type, populated with `other_calendar` -- makes
    `other_owner_user` a genuine member of the appointment type without owning `calendar`."""
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Assist"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=other_calendar
    )
    return slot


@pytest.fixture
def service(
    organization: Organization, audit_service: OrganizationAuditService
) -> AppointmentTypeService:
    svc = AppointmentTypeService(
        calendar_permission_service=CalendarPermissionService(),
        audit_service=audit_service,
    )
    svc.initialize(organization=organization)
    return svc


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# create_appointment_type_scoped_availability_window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_admin_happy_path(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
    django_capture_on_commit_callbacks,
) -> None:
    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            result = service.create_appointment_type_scoped_availability_window(
                acting_user=admin_user,
                appointment_type_slot_id=appointment_type_slot.id,
                calendar_id=calendar.id,
                start_time=_utc(2025, 9, 2, 9),
                end_time=_utc(2025, 9, 2, 17),
                tz="UTC",
            )

    window = result.window
    assert window is not None
    assert result.orphaned_bookings == []
    assert window.appointment_type_slot_fk_id == appointment_type_slot.id
    assert window.calendar_fk_id == calendar.id

    # Invisible on the default (base-rows-only) manager...
    assert (
        not AvailableTime.objects.filter_by_organization(service.organization_id)
        .filter(id=window.id)
        .exists()
    )
    # ...and visible through the explicit appointment-type-scoped accessor.
    assert (
        AvailableTime.objects.for_appointment_type_slot(appointment_type_slot.id)
        .filter_by_organization(service.organization_id)
        .get(id=window.id)
        == window
    )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action_key"] == AuditAction.CREATE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.availabletime"
    assert payloads[0]["subject"]["subject_id"] == str(window.pk)


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_owner_happy_path(
    service: AppointmentTypeService,
    owner_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    result = service.create_appointment_type_scoped_availability_window(
        acting_user=owner_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    assert result.window is not None
    assert result.window.calendar_fk_id == calendar.id


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_denies_non_owner_without_disclosing_appointment_type(
    service: AppointmentTypeService,
    stranger_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    with pytest.raises(AppointmentTypeSlotConfigNotFoundError) as excinfo:
        service.create_appointment_type_scoped_availability_window(
            acting_user=stranger_user,
            appointment_type_slot_id=appointment_type_slot.id,
            calendar_id=calendar.id,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    stranger_message = str(excinfo.value)

    # A genuinely missing (appointment_type_slot_id, calendar_id) pairing must raise the
    # exact same exception, message included -- a caller cannot distinguish
    # "forbidden" from "does not exist" from the error alone.
    with pytest.raises(AppointmentTypeSlotConfigNotFoundError) as excinfo_missing:
        service.create_appointment_type_scoped_availability_window(
            acting_user=stranger_user,
            appointment_type_slot_id=appointment_type_slot.id,
            calendar_id=calendar.id + 999_999,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    assert str(excinfo_missing.value) == stranger_message


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_denies_owner_outside_target_calendar(
    service: AppointmentTypeService,
    other_owner_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
    other_slot: AppointmentTypeSlot,
) -> None:
    """`other_owner_user` owns a calendar in the SAME appointment type (a different slot),
    so they can see the appointment type -- but they do not own `calendar`, so they must
    still be denied, with the same not-found-shaped error."""
    with pytest.raises(AppointmentTypeSlotConfigNotFoundError):
        service.create_appointment_type_scoped_availability_window(
            acting_user=other_owner_user,
            appointment_type_slot_id=appointment_type_slot.id,
            calendar_id=calendar.id,
            start_time=_utc(2025, 9, 2, 9),
            end_time=_utc(2025, 9, 2, 17),
            tz="UTC",
        )
    assert (
        not AvailableTime.objects.unscoped()
        .filter(appointment_type_slot_fk=appointment_type_slot)
        .exists()
    )


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_recurrence_and_timezone_round_trip(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    result = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
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

    # Read back through the appointment-type-scoped accessor and expand recurrence over two
    # weeks -- must land on Tuesdays and Thursdays only. Annotating BEFORE calling
    # get_occurrences_in_range caches `recurring_occurrences` on the instance, so
    # the read never falls through RecurringMixin's internal re-fetch via the
    # DEFAULT (base-rows-only) manager -- see the note on the default manager
    # excluding appointment-type-scoped rows.
    range_start = _utc(2025, 9, 1, 0)
    range_end = _utc(2025, 9, 15, 0)
    master = (
        AvailableTime.objects.for_appointment_type_slot(appointment_type_slot.id)
        .filter_by_organization(service.organization_id)
        .annotate_recurring_occurrences_on_date_range(range_start, range_end)
        .get(id=window.id)
    )
    occurrences = master.get_occurrences_in_range(range_start, range_end, include_self=True)
    weekdays = sorted({o.start_time.weekday() for o in occurrences})
    assert weekdays == [1, 3]  # Tuesday, Thursday
    assert len(occurrences) == 4  # two Tuesdays + two Thursdays in the range


# ---------------------------------------------------------------------------
# update_appointment_type_scoped_availability_window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_appointment_type_scoped_availability_window_records_diff(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
    django_capture_on_commit_callbacks,
) -> None:
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            result = service.update_appointment_type_scoped_availability_window(
                acting_user=admin_user,
                window_id=window_id,
                rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TH",
            )

    assert result.window is not None
    assert result.window.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TH"

    payloads = _payloads(mock_task)
    update_payloads = [p for p in payloads if p["action_key"] == AuditAction.UPDATE]
    assert len(update_payloads) == 1
    diff = update_payloads[0]["diff"]
    assert diff is not None
    assert "rrule" in diff
    assert diff["rrule"]["old"] == "FREQ=WEEKLY;BYDAY=TU,TH"
    assert diff["rrule"]["new"] == "FREQ=WEEKLY;BYDAY=TH"


@pytest.mark.django_db
def test_update_appointment_type_scoped_availability_window_timezone_round_trip(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    result = service.update_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        window_id=window_id,
        tz="America/Sao_Paulo",
    )
    assert result.window is not None
    assert result.window.timezone == "America/Sao_Paulo"

    reloaded = (
        AvailableTime.objects.unscoped()
        .filter_by_organization(service.organization_id)
        .get(id=window_id)
    )
    assert reloaded.timezone == "America/Sao_Paulo"


@pytest.mark.django_db
def test_update_appointment_type_scoped_availability_window_explicit_none_clears_recurrence(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    """Explicit ``rrule_string=None`` is the tri-state "clear" case -- distinct
    from the default sentinel (omitted), which leaves recurrence untouched."""
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    result = service.update_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        window_id=window_id,
        rrule_string=None,
    )
    assert result.window is not None
    assert result.window.recurrence_rule is None
    assert result.window.is_recurring is False

    reloaded = (
        AvailableTime.objects.unscoped()
        .filter_by_organization(service.organization_id)
        .get(id=window_id)
    )
    assert reloaded.recurrence_rule is None
    assert reloaded.is_recurring is False


@pytest.mark.django_db
def test_update_appointment_type_scoped_availability_window_omitted_rrule_string_leaves_it_unchanged(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    """Omitting ``rrule_string`` (the ``_UNCHANGED`` sentinel default) must
    leave an existing recurrence untouched."""
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
        rrule_string="RRULE:FREQ=WEEKLY;BYDAY=TU,TH",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    result = service.update_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        window_id=window_id,
        tz="America/Sao_Paulo",
    )
    assert result.window is not None
    assert result.window.recurrence_rule is not None
    assert result.window.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;BYDAY=TU,TH"


@pytest.mark.django_db
def test_update_appointment_type_scoped_availability_window_denies_non_owner(
    service: AppointmentTypeService,
    admin_user: User,
    stranger_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with pytest.raises(AppointmentTypeSlotConfigNotFoundError):
        service.update_appointment_type_scoped_availability_window(
            acting_user=stranger_user, window_id=window_id, tz="America/Sao_Paulo"
        )

    reloaded = (
        AvailableTime.objects.unscoped()
        .filter_by_organization(service.organization_id)
        .get(id=window_id)
    )
    assert reloaded.timezone == "UTC"  # untouched


@pytest.mark.django_db
def test_update_appointment_type_scoped_availability_window_narrowing_returns_orphaned_bookings_untouched(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type: AppointmentType,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    """UC-6: narrowing Tuesday+Thursday down to Thursday-only returns the future
    Tuesday booking as orphaned, and modifies neither the event nor the appointment type
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

    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
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
        appointment_type=appointment_type,
    )
    CalendarEventAppointmentTypeSelection.objects.create(
        organization=service.organization,
        event=tuesday_event,
        slot=appointment_type_slot,
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
        appointment_type=appointment_type,
    )
    CalendarEventAppointmentTypeSelection.objects.create(
        organization=service.organization,
        event=thursday_event,
        slot=appointment_type_slot,
        calendar=calendar,
    )

    result = service.update_appointment_type_scoped_availability_window(
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
    assert CalendarEvent.objects.filter_by_organization(service.organization_id).count() == 2
    assert (
        CalendarEventAppointmentTypeSelection.objects.filter_by_organization(
            service.organization_id
        ).count()
        == 2
    )


# ---------------------------------------------------------------------------
# delete_appointment_type_scoped_availability_window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_appointment_type_scoped_availability_window_admin(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
    django_capture_on_commit_callbacks,
) -> None:
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.delete_appointment_type_scoped_availability_window(
                acting_user=admin_user, window_id=window_id
            )

    assert not AvailableTime.objects.unscoped().filter(id=window_id).exists()
    payloads = _payloads(mock_task)
    delete_payloads = [p for p in payloads if p["action_key"] == AuditAction.DELETE]
    assert len(delete_payloads) == 1
    assert delete_payloads[0]["subject"]["subject_id"] == str(window_id)


@pytest.mark.django_db
def test_delete_appointment_type_scoped_availability_window_denies_non_owner(
    service: AppointmentTypeService,
    admin_user: User,
    stranger_user: User,
    calendar: Calendar,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]

    with pytest.raises(AppointmentTypeSlotConfigNotFoundError):
        service.delete_appointment_type_scoped_availability_window(
            acting_user=stranger_user, window_id=window_id
        )
    assert AvailableTime.objects.unscoped().filter(id=window_id).exists()


# ---------------------------------------------------------------------------
# Cascade through this service path (schema-enforced by on_delete=CASCADE)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_slot_through_update_appointment_type_cascades_appointment_type_scoped_windows(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type: AppointmentType,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    created = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window_id = created.window.id  # type: ignore[union-attr]
    assert AvailableTime.objects.unscoped().filter(id=window_id).exists()

    # Reconcile the appointment type with no slots at all -- AppointmentTypeService.update_appointment_type
    # deletes the now-absent "Lead Surgeon" slot, which cascades (on_delete=CASCADE
    # on AvailableTime.appointment_type_slot) to every appointment-type-scoped window that referenced it.
    service.update_appointment_type(
        appointment_type.id, AppointmentTypeInputData(name=appointment_type.name, slots=[])
    )

    assert (
        not AppointmentTypeSlot.objects.filter_by_organization(service.organization_id)
        .filter(id=appointment_type_slot.id)
        .exists()
    )
    assert not AvailableTime.objects.unscoped().filter(id=window_id).exists()


# ---------------------------------------------------------------------------
# Removing a calendar from a slot keeps its appointment-type-scoped windows
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_removing_calendar_from_slot_keeps_appointment_type_scoped_windows(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    other_calendar: Calendar,
    appointment_type: AppointmentType,
    django_capture_on_commit_callbacks,
) -> None:
    """Contract change (Calendar Pools Phase 1): when removing a calendar from
    a slot's membership, its appointment-type-scoped availability window is kept, not
    deleted -- roster removal is lenient and never destroys configuration.
    Both calendars' windows survive, and no window DELETE is audited.
    """
    # Create a slot with TWO calendars.
    slot = AppointmentTypeSlot.objects.create(
        organization=service.organization, appointment_type=appointment_type, name="Test Slot"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=service.organization, slot=slot, calendar=calendar
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=service.organization, slot=slot, calendar=other_calendar
    )

    # Create appointment-type-scoped windows for BOTH calendars.
    window1_result = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=slot.id,
        calendar_id=calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window1_id = window1_result.window.id  # type: ignore[union-attr]

    window2_result = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=slot.id,
        calendar_id=other_calendar.id,
        start_time=_utc(2025, 9, 2, 9),
        end_time=_utc(2025, 9, 2, 17),
        tz="UTC",
    )
    window2_id = window2_result.window.id  # type: ignore[union-attr]

    # Verify both windows exist.
    assert AvailableTime.objects.unscoped().filter(id=window1_id).exists()
    assert AvailableTime.objects.unscoped().filter(id=window2_id).exists()

    # Remove ONLY the first calendar from the slot (via update_appointment_type → _reconcile_slot).
    with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_appointment_type(
                appointment_type.id,
                AppointmentTypeInputData(
                    name=appointment_type.name,
                    slots=[
                        AppointmentTypeSlotInputData(
                            name=slot.name,
                            calendar_ids=[other_calendar.id],
                            required_count=1,
                        )
                    ],
                ),
            )

    # Both windows survive the roster removal.
    assert AvailableTime.objects.unscoped().filter(id=window1_id).exists()
    assert AvailableTime.objects.unscoped().filter(id=window2_id).exists()
    # The first calendar's membership is gone.
    assert (
        not AppointmentTypeSlotMembership.objects.filter_by_organization(service.organization_id)
        .filter(slot_fk=slot, calendar_fk_id=calendar.id)
        .exists()
    )
    # The second calendar's membership remains.
    assert (
        AppointmentTypeSlotMembership.objects.filter_by_organization(service.organization_id)
        .filter(slot_fk=slot, calendar_fk_id=other_calendar.id)
        .exists()
    )

    # No window DELETE is audited -- nothing was deleted.
    payloads = _payloads(mock_task)
    delete_payloads = [p for p in payloads if p["action_key"] == AuditAction.DELETE]
    window_delete_payloads = [
        p
        for p in delete_payloads
        if p["subject"]["subject_type"] == "calendar_integration.availabletime"
    ]
    assert window_delete_payloads == []


# ---------------------------------------------------------------------------
# FIX 2: Creating first window detects orphaned bookings
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_first_detects_orphaned_bookings(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type: AppointmentType,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    """FIX 2 (SHOULD-FIX): Creating the FIRST appointment-type-scoped window for a
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
        appointment_type=appointment_type,
    )
    CalendarEventAppointmentTypeSelection.objects.create(
        organization=service.organization,
        event=booking,
        slot=appointment_type_slot,
        calendar=calendar,
    )

    # Create the FIRST appointment-type-scoped window for this (calendar, slot):
    # 9am-5pm on Thursdays. The 6pm booking is now orphaned.
    result = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
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
    assert CalendarEvent.objects.filter_by_organization(service.organization_id).count() == 1


@pytest.mark.django_db
def test_create_appointment_type_scoped_availability_window_second_plus_no_orphans(
    service: AppointmentTypeService,
    admin_user: User,
    calendar: Calendar,
    appointment_type: AppointmentType,
    appointment_type_slot: AppointmentTypeSlot,
) -> None:
    """When creating a SECOND (or later) appointment-type-scoped window, it only widens
    the union and cannot orphan bookings. Verify orphaned_bookings=[] even
    if a booking sits outside this specific window (but within an existing one).
    """
    now = django_timezone.now()
    thursday = _next_weekday(now, weekday=3)
    tuesday = thursday - datetime.timedelta(days=2)

    # Create the FIRST window: Tuesday 9am-5pm.
    service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
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
        appointment_type=appointment_type,
    )
    CalendarEventAppointmentTypeSelection.objects.create(
        organization=service.organization,
        event=booking,
        slot=appointment_type_slot,
        calendar=calendar,
    )

    # Create the SECOND window: Thursday 9am-5pm. Union now covers both days.
    result = service.create_appointment_type_scoped_availability_window(
        acting_user=admin_user,
        appointment_type_slot_id=appointment_type_slot.id,
        calendar_id=calendar.id,
        start_time=datetime.datetime.combine(thursday, datetime.time(9), tzinfo=datetime.UTC),
        end_time=datetime.datetime.combine(thursday, datetime.time(17), tzinfo=datetime.UTC),
        tz="UTC",
        now=now,
    )

    # No booking is orphaned (widening the union cannot orphan).
    assert result.orphaned_bookings == []
