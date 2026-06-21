"""Audit-emission tests for AvailabilityService business write paths.

Each test constructs a real AvailabilityService (with a real AuditService bound on
the context) and asserts that the expected audit record(s) are enqueued. We patch
``audit.services.persist_audit_record`` and execute the on_commit callbacks so the
enqueue happens, then inspect the serialized payloads.

The AvailabilityService is built directly (bypassing the CalendarService facade)
using a real CalendarServiceContext plus the lightweight FakeHost from
test_availability_service.py.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

import pytest

from audit.constants import AuditAction
from audit.services import AuditService
from calendar_integration.constants import CalendarProvider
from calendar_integration.models import BlockedTime, Calendar
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.recurrence_manager import RecurrenceManager
from calendar_integration.tests.services.test_availability_service import FakeHost
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Audit Test Org")


@pytest.fixture
def user(db: Any, organization: Organization) -> User:
    u = User.objects.create_user(email="audit_avail@example.com", password="pass")
    Profile.objects.create(user=u)
    OrganizationMembership.objects.create(
        user=u, organization=organization, role=OrganizationRole.ADMIN
    )
    return u


@pytest.fixture
def audit_service() -> AuditService:
    from di_core.containers import container

    return container.audit_service()


@pytest.fixture
def calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Audit Calendar",
        external_id="audit_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def managed_calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Audit Managed Calendar",
        external_id="audit_cal_managed",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
        manage_available_windows=True,
    )


@pytest.fixture
def context(
    organization: Organization, user: User, audit_service: AuditService
) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
        audit_service=audit_service,
    )


@pytest.fixture
def service(context: CalendarServiceContext, organization: Organization) -> AvailabilityService:
    host = FakeHost(organization=organization)
    return AvailabilityService(context=context, recurrence_manager=RecurrenceManager(), host=host)


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# create_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_blocked_time_records_create(
    service: AvailabilityService,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            bt = service.create_blocked_time(
                calendar=calendar,
                start_time=_utc(2025, 7, 1, 9),
                end_time=_utc(2025, 7, 1, 10),
                timezone="UTC",
                reason="Focus",
            )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.CREATE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.BlockedTime"
    assert payloads[0]["subject"]["subject_id"] == str(bt.pk)
    assert payloads[0]["subject"]["subject_label"] == "Focus"


# ---------------------------------------------------------------------------
# update_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_blocked_time_records_update_with_reason_diff(
    service: AvailabilityService,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    bt = service.create_blocked_time(
        calendar=calendar,
        start_time=_utc(2025, 7, 1, 9),
        end_time=_utc(2025, 7, 1, 10),
        timezone="UTC",
        reason="Old reason",
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_blocked_time(
                calendar=calendar,
                blocked_time_id=bt.pk,
                reason="New reason",
            )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.UPDATE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.BlockedTime"
    assert payloads[0]["diff"] == {"reason": {"old": "Old reason", "new": "New reason"}}


@pytest.mark.django_db
def test_update_blocked_time_time_only_records_update_with_null_diff(
    service: AvailabilityService,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    bt = service.create_blocked_time(
        calendar=calendar,
        start_time=_utc(2025, 7, 1, 9),
        end_time=_utc(2025, 7, 1, 10),
        timezone="UTC",
        reason="Same",
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.update_blocked_time(
                calendar=calendar,
                blocked_time_id=bt.pk,
                start_time=_utc(2025, 7, 1, 11),
                end_time=_utc(2025, 7, 1, 12),
            )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.UPDATE
    # reason unchanged -> compute_diff returns None.
    assert payloads[0]["diff"] is None


# ---------------------------------------------------------------------------
# delete_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_blocked_time_records_delete(
    service: AvailabilityService,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    bt = service.create_blocked_time(
        calendar=calendar,
        start_time=_utc(2025, 7, 1, 9),
        end_time=_utc(2025, 7, 1, 10),
        timezone="UTC",
        reason="To delete",
    )
    bt_pk = bt.pk

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.delete_blocked_time(calendar=calendar, blocked_time_id=bt_pk)

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.DELETE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.BlockedTime"
    assert payloads[0]["subject"]["subject_id"] == str(bt_pk)
    assert not BlockedTime.objects.filter(pk=bt_pk).exists()


# ---------------------------------------------------------------------------
# create_available_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_available_time_records_create(
    service: AvailabilityService,
    managed_calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            at = service.create_available_time(
                calendar=managed_calendar,
                start_time=_utc(2025, 7, 1, 10),
                end_time=_utc(2025, 7, 1, 12),
                timezone="UTC",
            )

    payloads = _payloads(mock_task)
    assert len(payloads) == 1
    assert payloads[0]["action"] == AuditAction.CREATE
    assert payloads[0]["subject"]["subject_type"] == "calendar_integration.AvailableTime"
    assert payloads[0]["subject"]["subject_id"] == str(at.pk)
    # AvailableTime has no human-readable scalar -> no label.
    assert payloads[0]["subject"]["subject_label"] is None


# ---------------------------------------------------------------------------
# recurring exceptions / bulk modifications -> UPDATE on the parent, diff=None
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_recurring_blocked_time_exception_records_update_on_parent(
    service: AvailabilityService,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    parent = service.create_blocked_time(
        calendar=calendar,
        start_time=_utc(2025, 7, 1, 9),
        end_time=_utc(2025, 7, 1, 10),
        timezone="UTC",
        reason="Recurring block",
        rrule_string="RRULE:FREQ=DAILY;COUNT=5",
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.create_recurring_blocked_time_exception(
                parent_blocked_time=parent,
                exception_date=datetime.date(2025, 7, 3),
                is_cancelled=True,
            )

    # The parent series gets an UPDATE record. Nested create_blocked_time calls (from
    # the recurrence engine split) may also emit CREATE records; assert the parent
    # UPDATE is present.
    parent_subject_id = str(parent.pk)
    update_records = [
        p
        for p in _payloads(mock_task)
        if p["action"] == AuditAction.UPDATE
        and p["subject"]["subject_type"] == "calendar_integration.BlockedTime"
        and p["subject"]["subject_id"] == parent_subject_id
    ]
    assert len(update_records) == 1
    assert update_records[0]["diff"] is None


@pytest.mark.django_db
def test_create_recurring_available_time_exception_records_update_on_parent(
    service: AvailabilityService,
    managed_calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    parent = service.create_available_time(
        calendar=managed_calendar,
        start_time=_utc(2025, 7, 1, 10),
        end_time=_utc(2025, 7, 1, 12),
        timezone="UTC",
        rrule_string="RRULE:FREQ=DAILY;COUNT=5",
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.create_recurring_available_time_exception(
                parent_available_time=parent,
                exception_date=datetime.date(2025, 7, 3),
                is_cancelled=True,
            )

    parent_subject_id = str(parent.pk)
    update_records = [
        p
        for p in _payloads(mock_task)
        if p["action"] == AuditAction.UPDATE
        and p["subject"]["subject_type"] == "calendar_integration.AvailableTime"
        and p["subject"]["subject_id"] == parent_subject_id
    ]
    assert len(update_records) == 1
    assert update_records[0]["diff"] is None


@pytest.mark.django_db
def test_create_recurring_blocked_time_bulk_modification_records_update_on_parent(
    service: AvailabilityService,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    parent = service.create_blocked_time(
        calendar=calendar,
        start_time=_utc(2025, 7, 1, 9),
        end_time=_utc(2025, 7, 1, 10),
        timezone="UTC",
        reason="Recurring block",
        rrule_string="RRULE:FREQ=DAILY;COUNT=5",
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.create_recurring_blocked_time_bulk_modification(
                parent_blocked_time=parent,
                modification_start_date=_utc(2025, 7, 3, 9),
                is_bulk_cancelled=True,
            )

    parent_subject_id = str(parent.pk)
    update_records = [
        p
        for p in _payloads(mock_task)
        if p["action"] == AuditAction.UPDATE
        and p["subject"]["subject_type"] == "calendar_integration.BlockedTime"
        and p["subject"]["subject_id"] == parent_subject_id
    ]
    assert len(update_records) == 1
    assert update_records[0]["diff"] is None


@pytest.mark.django_db
def test_create_recurring_available_time_bulk_modification_records_update_on_parent(
    service: AvailabilityService,
    managed_calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    parent = service.create_available_time(
        calendar=managed_calendar,
        start_time=_utc(2025, 7, 1, 10),
        end_time=_utc(2025, 7, 1, 12),
        timezone="UTC",
        rrule_string="RRULE:FREQ=DAILY;COUNT=5",
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service.create_recurring_available_time_bulk_modification(
                parent_available_time=parent,
                modification_start_date=_utc(2025, 7, 3, 10),
                is_bulk_cancelled=True,
            )

    parent_subject_id = str(parent.pk)
    update_records = [
        p
        for p in _payloads(mock_task)
        if p["action"] == AuditAction.UPDATE
        and p["subject"]["subject_type"] == "calendar_integration.AvailableTime"
        and p["subject"]["subject_id"] == parent_subject_id
    ]
    assert len(update_records) == 1
    assert update_records[0]["diff"] is None


# ---------------------------------------------------------------------------
# None-guard: no audit_service on context -> no emission, no error
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_audit_service_skips_emission(
    organization: Organization,
    user: User,
    calendar: Calendar,
    django_capture_on_commit_callbacks,
) -> None:
    context = CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
        audit_service=None,
    )
    service = AvailabilityService(
        context=context,
        recurrence_manager=RecurrenceManager(),
        host=FakeHost(organization=organization),
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            bt = service.create_blocked_time(
                calendar=calendar,
                start_time=_utc(2025, 7, 1, 9),
                end_time=_utc(2025, 7, 1, 10),
                timezone="UTC",
                reason="No audit",
            )

    assert bt.pk is not None
    mock_task.delay.assert_not_called()
