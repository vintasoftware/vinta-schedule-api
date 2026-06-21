"""Integration tests for Phase 6 — FORBIDDEN mode auto-undo during sync.

Tests exercise ``CalendarSyncService._process_existing_event`` under the ``FORBIDDEN``
policy and the ``ExternalEventChangeRequestService.auto_undo_inbound_change`` method
that implements the shared outbound-undo path.

The provider write adapter is MOCKED — these tests never touch a real Google Calendar.
DB writes are exercised against the real test database.

Test matrix:
- FORBIDDEN + inbound UPDATE: local event NOT mutated; outbound ``update_event`` called
  with retained values (incl. attendees/recurrence preserved); an ``AUTO_UNDONE`` row
  recorded; external id in ``matched_event_ids``; audit ``EXTERNAL_CHANGE_AUTO_UNDONE``.
- FORBIDDEN + inbound DELETE (cancelled): local event NOT deleted; outbound
  ``create_event`` called; local ``external_id`` rebound to new id; ``AUTO_UNDONE`` row;
  audit entry recorded.
- FORBIDDEN + service None → ``ImproperlyConfigured``.
- FORBIDDEN + adapter None → ``ImproperlyConfigured``.
- Compensation: provider create succeeds but local commit fails → compensating
  ``delete_event`` called, no orphan (mirrors the reject compensation test).
- ALLOW policy: inbound edit still applies directly and inbound cancellation still deletes
  (backward-compat — load-bearing check that FORBIDDEN wiring doesn't regress ALLOW).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured

import pytest
from allauth.socialaccount.models import SocialAccount

from audit.constants import AuditAction, AuditActorType
from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.factories import create_event_attendance
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarSync,
    EventExternalAttendance,
    ExternalAttendee,
    ExternalEventChangeRequest,
    RecurrenceRule,
)
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from organizations.models import ExternalEventUpdatePolicy, Organization, OrganizationMembership
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fake sync host (matches FakeHost from other sync test modules)
# ---------------------------------------------------------------------------


class FakeHost:
    """Minimal SyncServiceHost for tests — records calls, no-ops the actions."""

    def __init__(self) -> None:
        self.remove_calls: list[tuple[Any, ...]] = []
        self.grant_calls: list[Calendar] = []
        self.request_calendar_sync_calls: list[dict[str, Any]] = []
        self.execute_org_import_calls: list[tuple[datetime.datetime, datetime.datetime]] = []

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self,
        calendar_id: int,
        blocked_times: Any,
        events: Any,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        self.remove_calls.append(
            (calendar_id, list(blocked_times), list(events), start_time, end_time)
        )

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None:
        self.grant_calls.append(calendar)

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source: Any = None,
    ) -> None:
        self.request_calendar_sync_calls.append(
            {
                "calendar": calendar,
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
                "should_update_events": should_update_events,
                "trigger_source": trigger_source,
            }
        )
        return None

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> list:
        self.execute_org_import_calls.append((start_time, end_time))
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization_forbidden(db: Any) -> Organization:
    """Organization with FORBIDDEN policy."""
    return Organization.objects.create(
        name="Forbidden Policy Org",
        external_event_update_policy=ExternalEventUpdatePolicy.FORBIDDEN,
    )


@pytest.fixture
def organization_allow(db: Any) -> Organization:
    """Organization with ALLOW policy (direct-apply, legacy behavior)."""
    return Organization.objects.create(
        name="Allow Policy Org",
        external_event_update_policy=ExternalEventUpdatePolicy.ALLOW,
    )


@pytest.fixture
def user(db: Any) -> User:
    u = User.objects.create_user(  # noqa: S106
        email="sync_forbidden@example.com", password="pass"
    )
    Profile.objects.create(user=u)
    return u


@pytest.fixture
def social_account(db: Any, user: User) -> SocialAccount:
    return SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid="forbidden_test_111"
    )


@pytest.fixture
def calendar_forbidden(db: Any, organization_forbidden: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Forbidden Calendar",
        external_id="forbidden_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization_forbidden,
    )


@pytest.fixture
def calendar_allow(db: Any, organization_allow: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Allow Calendar",
        external_id="allow_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization_allow,
    )


@pytest.fixture
def fake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.provider = CalendarProvider.GOOGLE
    return adapter


@pytest.fixture
def context_forbidden(
    organization_forbidden: Organization, user: User, fake_adapter: MagicMock
) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization_forbidden,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


@pytest.fixture
def context_allow(
    organization_allow: Organization, user: User, fake_adapter: MagicMock
) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization_allow,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


@pytest.fixture
def change_request_service() -> ExternalEventChangeRequestService:
    """Service with audit_service=None (no audit assertions needed here)."""
    return ExternalEventChangeRequestService(audit_service=None)


@pytest.fixture
def change_request_service_with_audit() -> ExternalEventChangeRequestService:
    """Service with a real AuditService for audit-assertion tests."""
    from di_core.containers import container

    return container.external_event_change_request_service()


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_existing_event(
    calendar: Calendar,
    external_id: str,
    title: str = "Original Title",
    description: str = "Original description",
    start_time: datetime.datetime | None = None,
    end_time: datetime.datetime | None = None,
) -> CalendarEvent:
    """Create a pre-existing CalendarEvent in the DB to serve as the sync target."""
    if start_time is None:
        start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    if end_time is None:
        end_time = datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC)
    return CalendarEvent.objects.create(
        calendar=calendar,
        title=title,
        description=description,
        start_time_tz_unaware=start_time.replace(tzinfo=None),
        end_time_tz_unaware=end_time.replace(tzinfo=None),
        timezone="UTC",
        external_id=external_id,
        organization_id=calendar.organization_id,
    )


def _inbound_edit_event(
    external_id: str,
    title: str = "Edited Title",
    description: str = "Edited description",
    calendar_external_id: str = "forbidden_cal_001",
    start_time: datetime.datetime | None = None,
    end_time: datetime.datetime | None = None,
) -> CalendarEventAdapterOutputData:
    """Build an inbound event representing an external edit."""
    if start_time is None:
        start_time = datetime.datetime(2025, 9, 1, 11, 0, tzinfo=datetime.UTC)
    if end_time is None:
        end_time = datetime.datetime(2025, 9, 1, 12, 0, tzinfo=datetime.UTC)
    return CalendarEventAdapterOutputData(
        calendar_external_id=calendar_external_id,
        external_id=external_id,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        timezone="UTC",
        attendees=[],
        status="confirmed",  # type: ignore[arg-type]
        original_payload={"id": external_id, "summary": title},
    )


def _inbound_cancelled_event(
    external_id: str,
    calendar_external_id: str = "forbidden_cal_001",
) -> CalendarEventAdapterOutputData:
    """Build a cancelled inbound event (provider-side deletion)."""
    return CalendarEventAdapterOutputData(
        calendar_external_id=calendar_external_id,
        external_id=external_id,
        title="",
        description="",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        status="cancelled",  # type: ignore[arg-type]
        original_payload={"id": external_id, "status": "cancelled"},
    )


def _adapter_output_for_create(
    external_id: str,
    calendar_external_id: str = "forbidden_cal_001",
) -> CalendarEventAdapterOutputData:
    """Adapter output returned by create_event (with a new external id)."""
    return CalendarEventAdapterOutputData(
        calendar_external_id=calendar_external_id,
        external_id=external_id,
        title="Original Title",
        description="Original description",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
    )


def _make_calendar_sync(
    calendar: Calendar,
    organization: Organization,
    start_date: datetime.datetime | None = None,
    end_date: datetime.datetime | None = None,
) -> CalendarSync:
    if start_date is None:
        start_date = datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC)
    if end_date is None:
        end_date = datetime.datetime(2025, 9, 1, 23, 59, tzinfo=datetime.UTC)
    return CalendarSync.objects.create(
        calendar=calendar,
        organization=organization,
        start_datetime=start_date,
        end_datetime=end_date,
        should_update_events=True,
        status=CalendarSyncStatus.IN_PROGRESS,
    )


# ---------------------------------------------------------------------------
# Tests: FORBIDDEN + inbound UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_forbidden_update_does_not_mutate_local_event_and_calls_update_event(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under FORBIDDEN, an inbound field edit does NOT mutate the local event;
    outbound ``update_event`` is called with the retained (local) values to
    push them back to the provider; an AUTO_UNDONE row is recorded; external id
    is in matched_event_ids (event is not deleted by full-sync)."""
    existing = _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_upd_001",
        title="Original Title",
        description="Original description",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
    )

    inbound = _inbound_edit_event(
        "evt_forbidden_upd_001",
        title="Inbound Edited Title",
        description="Inbound edited description",
        calendar_external_id="forbidden_cal_001",
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-forbidden-upd",
    }
    # update_event does not return a meaningful value for our path (None ok).
    fake_adapter.update_event.return_value = None

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    service = CalendarSyncService(
        context=context_forbidden,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    # Local event must be UNCHANGED.
    existing.refresh_from_db()
    assert existing.title == "Original Title"
    assert existing.description == "Original description"

    # Outbound update_event must have been called once with the retained values.
    fake_adapter.update_event.assert_called_once()
    call_args = fake_adapter.update_event.call_args
    assert call_args.args[0] == calendar_forbidden.external_id  # calendar external id
    assert call_args.args[1] == "evt_forbidden_upd_001"  # event external id
    adapter_input = call_args.args[2]
    assert adapter_input.title == "Original Title"
    assert adapter_input.description == "Original description"

    # create_event must NOT have been called (this is an update undo, not a delete undo).
    fake_adapter.create_event.assert_not_called()

    # Exactly one AUTO_UNDONE change request must exist.
    requests = ExternalEventChangeRequest.objects.filter(
        organization_id=organization_forbidden.id,
        event=existing,
    )
    assert requests.count() == 1
    cr = requests.get()
    assert cr.status == ExternalEventChangeRequestStatus.AUTO_UNDONE
    assert cr.kind == ExternalEventChangeKind.UPDATE
    assert cr.provider == CalendarProvider.GOOGLE

    # proposed_values carries the inbound edit.
    assert cr.proposed_values["title"] == "Inbound Edited Title"
    assert cr.proposed_values["description"] == "Inbound edited description"

    # retained_values carries the local snapshot.
    assert cr.retained_values["title"] == "Original Title"
    assert cr.retained_values["description"] == "Original description"

    # Resolver fields: SYSTEM auto-undo → resolved_by_user_id is None, resolved_at is set.
    assert cr.resolved_by_user_id is None
    assert cr.resolved_at is not None

    # External id must still be the original (not rebound — this is an UPDATE kind).
    existing.refresh_from_db()
    assert existing.external_id == "evt_forbidden_upd_001"


@pytest.mark.django_db
def test_forbidden_update_external_id_in_matched_event_ids_not_deleted_by_full_sync(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under FORBIDDEN, the auto-undone event's external id is in matched_event_ids
    so the full-sync deletion pass does not treat the event as vanished."""
    intercepted = _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_intercept",
        title="Will Not Be Mutated",
    )
    _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_vanished",
        title="Will Be Deleted",
    )

    inbound = _inbound_edit_event(
        "evt_forbidden_intercept",
        title="Incoming Edit",
        calendar_external_id="forbidden_cal_001",
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": None,  # full sync (no token)
    }
    fake_adapter.update_event.return_value = None

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    service = CalendarSyncService(
        context=context_forbidden,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    # Full sync: pass sync_token=None to trigger _handle_deletions_for_full_sync.
    service._execute_calendar_sync(calendar_sync, sync_token=None)

    # The intercepted event must still exist (matched_event_ids kept it from deletion).
    assert CalendarEvent.objects.filter(
        external_id="evt_forbidden_intercept",
        organization_id=organization_forbidden.id,
    ).exists()
    intercepted.refresh_from_db()
    assert intercepted.title == "Will Not Be Mutated"

    # The truly vanished event must have been deleted by full-sync.
    assert not CalendarEvent.objects.filter(
        external_id="evt_forbidden_vanished",
        organization_id=organization_forbidden.id,
    ).exists()

    # One AUTO_UNDONE change request created for the intercepted event.
    assert (
        ExternalEventChangeRequest.objects.filter(
            organization_id=organization_forbidden.id,
            event=intercepted,
            status=ExternalEventChangeRequestStatus.AUTO_UNDONE,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_forbidden_update_preserves_attendees_and_recurrence_in_adapter_input(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
    user: User,
) -> None:
    """Under FORBIDDEN, the outbound update_event call carries the full attendees
    (internal + external) and RRULE so the adapter's full-replace PUT does not wipe them."""
    recurrence_rule = RecurrenceRule.objects.create(
        organization=organization_forbidden,
        frequency="WEEKLY",
        interval=1,
    )
    existing = CalendarEvent.objects.create(
        calendar=calendar_forbidden,
        title="Original Title",
        description="Original description",
        start_time_tz_unaware=datetime.datetime(2025, 9, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 9, 1, 10, 0),
        timezone="UTC",
        external_id="evt_forbidden_attendees",
        organization_id=calendar_forbidden.organization_id,
        recurrence_rule_fk=recurrence_rule,
    )
    # Internal member attendee — ensure a membership exists for the user first.
    OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization_forbidden,
        defaults={"role": "member"},
    )
    create_event_attendance(event=existing, user=user, status="accepted")
    # External attendee.
    external_attendee = ExternalAttendee.objects.create(
        organization=organization_forbidden,
        email="guest@example.com",
        name="Guest Person",
    )
    EventExternalAttendance.objects.create(
        organization=organization_forbidden,
        event=existing,
        external_attendee=external_attendee,
        status="pending",
    )

    inbound = _inbound_edit_event(
        "evt_forbidden_attendees",
        title="Inbound Title",
        calendar_external_id="forbidden_cal_001",
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-x",
    }
    fake_adapter.update_event.return_value = None

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    sync_service = CalendarSyncService(
        context=context_forbidden,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    sync_service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    fake_adapter.update_event.assert_called_once()
    adapter_input = fake_adapter.update_event.call_args.args[2]

    # Both attendees survive — NOT wiped to [].
    attendee_emails = {a.email for a in adapter_input.attendees}
    assert "guest@example.com" in attendee_emails
    assert len(adapter_input.attendees) == 2

    # Recurrence survives.
    assert adapter_input.recurrence_rule is not None
    assert "FREQ=WEEKLY" in adapter_input.recurrence_rule


# ---------------------------------------------------------------------------
# Tests: FORBIDDEN + inbound DELETE (cancelled event)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_forbidden_delete_does_not_delete_local_event_and_calls_create_event(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under FORBIDDEN, an inbound cancellation does NOT delete the local event;
    outbound ``create_event`` is called; local ``external_id`` is rebound to the new
    provider id; an AUTO_UNDONE row is recorded."""
    existing = _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_del_001",
        title="Will NOT Be Deleted",
        description="Retained description",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
    )
    event_pk = existing.pk
    old_external_id = existing.external_id

    inbound = _inbound_cancelled_event(
        "evt_forbidden_del_001", calendar_external_id="forbidden_cal_001"
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-forbidden-del",
    }
    fake_adapter.create_event.return_value = _adapter_output_for_create(
        "evt_forbidden_NEW_999", "forbidden_cal_001"
    )

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    service = CalendarSyncService(
        context=context_forbidden,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    # Local event must still EXIST — it must NOT have been deleted.
    assert CalendarEvent.objects.filter(
        pk=event_pk,
        organization_id=organization_forbidden.id,
    ).exists()

    # Outbound create_event must have been called once (re-create the deleted event).
    fake_adapter.create_event.assert_called_once()
    fake_adapter.update_event.assert_not_called()

    # The local event's external_id must be rebound to the new provider id.
    existing.refresh_from_db()
    assert existing.external_id == "evt_forbidden_NEW_999"
    assert existing.external_id != old_external_id

    # Exactly one AUTO_UNDONE delete change request must exist.
    requests = ExternalEventChangeRequest.objects.filter(
        organization_id=organization_forbidden.id,
        event=existing,
    )
    assert requests.count() == 1
    cr = requests.get()
    assert cr.status == ExternalEventChangeRequestStatus.AUTO_UNDONE
    assert cr.kind == ExternalEventChangeKind.DELETE
    assert cr.provider == CalendarProvider.GOOGLE
    assert cr.proposed_values == {}
    assert cr.retained_values["title"] == "Will NOT Be Deleted"

    # SYSTEM auto-undo: no human resolver.
    assert cr.resolved_by_user_id is None
    assert cr.resolved_at is not None


# ---------------------------------------------------------------------------
# Tests: fail-loud on misconfiguration (service None / adapter None)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_forbidden_update_service_none_raises_improperly_configured(
    organization_forbidden: Organization,
    calendar_forbidden: Calendar,
    user: User,
    fake_adapter: MagicMock,
) -> None:
    """FORBIDDEN + service None → ImproperlyConfigured; no adapter call."""
    context = CalendarServiceContext(
        organization=organization_forbidden,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    existing = _make_existing_event(calendar_forbidden, external_id="evt_fb_svcnone")
    inbound = _inbound_edit_event("evt_fb_svcnone")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-x",
    }

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    # No external_event_change_request_service injected!
    service = CalendarSyncService(
        context=context,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=None,
    )
    with pytest.raises(ImproperlyConfigured):
        service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    fake_adapter.update_event.assert_not_called()
    fake_adapter.create_event.assert_not_called()

    # Local event must be unchanged.
    existing.refresh_from_db()
    assert existing.title == "Original Title"


@pytest.mark.django_db
def test_forbidden_delete_service_none_raises_improperly_configured(
    organization_forbidden: Organization,
    calendar_forbidden: Calendar,
    user: User,
    fake_adapter: MagicMock,
) -> None:
    """FORBIDDEN + inbound cancellation + service None → ImproperlyConfigured."""
    context = CalendarServiceContext(
        organization=organization_forbidden,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    _make_existing_event(calendar_forbidden, external_id="evt_fb_del_svcnone")
    inbound = _inbound_cancelled_event("evt_fb_del_svcnone")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-x",
    }

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    service = CalendarSyncService(
        context=context,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=None,
    )
    with pytest.raises(ImproperlyConfigured):
        service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    fake_adapter.update_event.assert_not_called()
    fake_adapter.create_event.assert_not_called()


@pytest.mark.django_db
def test_forbidden_update_adapter_none_raises_improperly_configured(
    organization_forbidden: Organization,
    calendar_forbidden: Calendar,
    user: User,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """FORBIDDEN + calendar_adapter None → ImproperlyConfigured before any adapter call."""
    context_no_adapter = CalendarServiceContext(
        organization=organization_forbidden,
        user_or_token=user,
        account=user,
        calendar_adapter=None,  # no write adapter
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    _make_existing_event(calendar_forbidden, external_id="evt_fb_adpnone_upd")

    # Drive via a direct _process_existing_event call to isolate the FORBIDDEN guard
    # without needing to drive a full sync cycle with a broken adapter.
    from calendar_integration.services.dataclasses import EventsSyncChanges

    service = CalendarSyncService(
        context=context_no_adapter,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    inbound = _inbound_edit_event("evt_fb_adpnone_upd")
    existing = CalendarEvent.objects.get(
        external_id="evt_fb_adpnone_upd", organization_id=organization_forbidden.id
    )
    changes = EventsSyncChanges()
    with pytest.raises(ImproperlyConfigured):
        service._process_existing_event(inbound, existing, changes, update_events=True)


@pytest.mark.django_db
def test_forbidden_delete_adapter_none_raises_improperly_configured(
    organization_forbidden: Organization,
    calendar_forbidden: Calendar,
    user: User,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """FORBIDDEN + inbound cancellation + calendar_adapter None → ImproperlyConfigured."""
    context_no_adapter = CalendarServiceContext(
        organization=organization_forbidden,
        user_or_token=user,
        account=user,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    _make_existing_event(calendar_forbidden, external_id="evt_fb_adpnone_del")

    from calendar_integration.services.dataclasses import EventsSyncChanges

    service = CalendarSyncService(
        context=context_no_adapter,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    inbound = _inbound_cancelled_event("evt_fb_adpnone_del")
    existing = CalendarEvent.objects.get(
        external_id="evt_fb_adpnone_del", organization_id=organization_forbidden.id
    )
    changes = EventsSyncChanges()
    with pytest.raises(ImproperlyConfigured):
        service._process_existing_event(inbound, existing, changes, update_events=True)


# ---------------------------------------------------------------------------
# Tests: compensation — provider create succeeds but local commit fails
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_forbidden_delete_partial_failure_compensates(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under FORBIDDEN + inbound deletion, if the provider re-create succeeds but the
    subsequent local commit (external-id rebind + status-flip) fails, a compensating
    ``delete_event`` is called with the new provider id so no provider orphan survives.
    The original exception propagates; the local event keeps its old external id."""
    existing = _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_comp_001",
        title="Event To Keep",
    )
    old_external_id = existing.external_id

    fake_adapter.create_event.return_value = _adapter_output_for_create(
        "evt_forbidden_NEW_COMP", "forbidden_cal_001"
    )

    class _SaveError(RuntimeError):
        pass

    # Force the local save (external-id rebind inside _resolve_with_undo) to fail.
    with patch.object(CalendarEvent, "save", side_effect=_SaveError("save failed")):
        with pytest.raises(_SaveError):
            change_request_service.auto_undo_inbound_change(
                event=existing,
                kind=ExternalEventChangeKind.DELETE,
                proposed_values={},
                retained_values={
                    "title": "Event To Keep",
                    "description": "Original description",
                    "start_time": "2025-09-01T09:00:00+00:00",
                    "end_time": "2025-09-01T10:00:00+00:00",
                },
                payload={"id": "evt_forbidden_comp_001", "status": "cancelled"},
                provider=CalendarProvider.GOOGLE,
                write_adapter=fake_adapter,
            )

    # Provider create was attempted, then COMPENSATED.
    fake_adapter.create_event.assert_called_once()
    fake_adapter.delete_event.assert_called_once_with(
        calendar_forbidden.external_id, "evt_forbidden_NEW_COMP"
    )

    # The local event keeps its old external id (no partial rebind survived).
    existing.refresh_from_db()
    assert existing.external_id == old_external_id


# ---------------------------------------------------------------------------
# Tests: audit record emitted with EXTERNAL_CHANGE_AUTO_UNDONE
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_forbidden_update_records_auto_undone_audit_entry(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service_with_audit: ExternalEventChangeRequestService,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Under FORBIDDEN, auto-undoing an inbound update records an
    EXTERNAL_CHANGE_AUTO_UNDONE audit entry with the SYSTEM actor."""
    _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_audit_upd",
        title="Original Title",
    )
    inbound = _inbound_edit_event(
        "evt_forbidden_audit_upd",
        title="Inbound Title",
        calendar_external_id="forbidden_cal_001",
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-audit",
    }
    fake_adapter.update_event.return_value = None

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    service = CalendarSyncService(
        context=context_forbidden,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service_with_audit,
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]
    auto_undone_payloads = [
        p for p in payloads if p["action"] == AuditAction.EXTERNAL_CHANGE_AUTO_UNDONE
    ]
    assert len(auto_undone_payloads) == 1
    payload = auto_undone_payloads[0]

    assert payload["organization_id"] == organization_forbidden.id
    assert payload["actor"]["actor_type"] == AuditActorType.SYSTEM
    assert payload["subject"]["subject_type"] == "calendar_integration.ExternalEventChangeRequest"
    # Diff: old=proposed (inbound), new=retained (what we restore).
    assert "title" in payload["diff"]
    assert payload["diff"]["title"]["old"] == "Inbound Title"
    assert payload["diff"]["title"]["new"] == "Original Title"


@pytest.mark.django_db
def test_forbidden_delete_records_auto_undone_audit_entry(
    context_forbidden: CalendarServiceContext,
    calendar_forbidden: Calendar,
    organization_forbidden: Organization,
    fake_adapter: MagicMock,
    change_request_service_with_audit: ExternalEventChangeRequestService,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Under FORBIDDEN, auto-undoing an inbound deletion records an
    EXTERNAL_CHANGE_AUTO_UNDONE audit entry with the SYSTEM actor."""
    _make_existing_event(
        calendar_forbidden,
        external_id="evt_forbidden_audit_del",
        title="Event To Keep",
    )
    inbound = _inbound_cancelled_event(
        "evt_forbidden_audit_del", calendar_external_id="forbidden_cal_001"
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-audit-del",
    }
    fake_adapter.create_event.return_value = _adapter_output_for_create(
        "evt_forbidden_NEW_AUDIT", "forbidden_cal_001"
    )

    calendar_sync = _make_calendar_sync(calendar_forbidden, organization_forbidden)
    service = CalendarSyncService(
        context=context_forbidden,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service_with_audit,
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]
    auto_undone_payloads = [
        p for p in payloads if p["action"] == AuditAction.EXTERNAL_CHANGE_AUTO_UNDONE
    ]
    assert len(auto_undone_payloads) == 1
    payload = auto_undone_payloads[0]

    assert payload["organization_id"] == organization_forbidden.id
    assert payload["actor"]["actor_type"] == AuditActorType.SYSTEM
    assert payload["subject"]["subject_type"] == "calendar_integration.ExternalEventChangeRequest"
    # For DELETE: old=None (nothing, event was gone), new=retained values (what we restore).
    assert "title" in payload["diff"]
    assert payload["diff"]["title"]["old"] is None
    assert payload["diff"]["title"]["new"] == "Event To Keep"


# ---------------------------------------------------------------------------
# Tests: ALLOW policy backward-compat (must not be broken by FORBIDDEN wiring)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_allow_policy_inbound_update_applies_directly_no_change_request(
    context_allow: CalendarServiceContext,
    calendar_allow: Calendar,
    organization_allow: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under ALLOW, an inbound edit applies directly to the local event and no change
    request is created. (Backward-compat: FORBIDDEN wiring must not regress ALLOW.)"""
    existing = _make_existing_event(
        calendar_allow,
        external_id="evt_allow_upd_001",
        title="Original Title",
        description="Original description",
    )
    inbound = _inbound_edit_event(
        "evt_allow_upd_001",
        title="Edited Title",
        description="Edited description",
        calendar_external_id="allow_cal_001",
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-allow",
    }

    calendar_sync = _make_calendar_sync(calendar_allow, organization_allow)
    service = CalendarSyncService(
        context=context_allow,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    # ALLOW: local event IS mutated.
    existing.refresh_from_db()
    assert existing.title == "Edited Title"
    assert existing.description == "Edited description"

    # No change request was created.
    assert not ExternalEventChangeRequest.objects.filter(
        organization_id=organization_allow.id,
        event=existing,
    ).exists()

    # No outbound adapter calls.
    fake_adapter.update_event.assert_not_called()
    fake_adapter.create_event.assert_not_called()


@pytest.mark.django_db
def test_allow_policy_inbound_cancellation_deletes_event_no_change_request(
    context_allow: CalendarServiceContext,
    calendar_allow: Calendar,
    organization_allow: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under ALLOW, an inbound cancellation deletes the local event and no change
    request is created. (Backward-compat: FORBIDDEN wiring must not regress ALLOW.)"""
    existing = _make_existing_event(
        calendar_allow,
        external_id="evt_allow_del_001",
        title="Will Be Deleted",
    )
    event_pk = existing.pk

    inbound = _inbound_cancelled_event("evt_allow_del_001", calendar_external_id="allow_cal_001")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-allow-del",
    }

    calendar_sync = _make_calendar_sync(calendar_allow, organization_allow)
    service = CalendarSyncService(
        context=context_allow,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    # ALLOW: the local event is DELETED.
    assert not CalendarEvent.objects.filter(
        pk=event_pk,
        organization_id=organization_allow.id,
    ).exists()

    # No change request was created.
    assert (
        not ExternalEventChangeRequest.objects.filter(
            organization_id=organization_allow.id,
        )
        .filter(event_fk=existing.pk)
        .exists()
    )

    # No outbound adapter calls.
    fake_adapter.update_event.assert_not_called()
    fake_adapter.create_event.assert_not_called()
