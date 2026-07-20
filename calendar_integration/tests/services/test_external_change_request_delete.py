"""Integration tests for Phase 4 — inbound DELETION interception under CHANGE_REQUEST policy.

Tests exercise the full sync diff engine path via ``CalendarSyncService._execute_calendar_sync``.
A real ``ExternalEventChangeRequestService`` is wired in (with its ``audit_service`` set to a
real ``AuditService`` for audit-assertion tests and ``None`` for non-audit tests). The adapter
is a MagicMock. DB writes are exercised against the real test database.

Test matrix:
- Under ``CHANGE_REQUEST``: an inbound cancellation leaves the local CalendarEvent present,
  creates exactly one ``PENDING`` delete change request with correct retained_values, does NOT
  append the event to ``events_to_delete``, and adds the external id to ``matched_event_ids``.
- Full-sync deletion immunity: the intercepted event's external id is in ``matched_event_ids``
  so the full-sync deletion pass does not delete it.
- Re-cancel (supersede): a second inbound cancellation marks the first request ``STALE`` and
  creates a new ``PENDING`` one — two rows total, history preserved.
- Under ``ALLOW``: the same cancellation deletes the local event (existing behavior); no change
  request is created. Backward-compat load-bearing test.
- Fail-loud: ``CHANGE_REQUEST`` + service None → raises ``ImproperlyConfigured``.
- Audit: a ``PENDING`` delete request creation records the expected audit entry.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured

import pytest
from allauth.socialaccount.models import SocialAccount

from audit.constants import AuditAction
from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarSync,
    ExternalEventChangeRequest,
)
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from organizations.models import ExternalEventUpdatePolicy, Organization
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fake sync host (mirrors FakeHost from test_external_change_request_update.py)
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
        import_workflow_state: Any = None,
        bypass_limits: bool = False,
    ) -> list:
        self.execute_org_import_calls.append((start_time, end_time))
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization_change_request(db: Any) -> Organization:
    """Organization with CHANGE_REQUEST policy."""
    return Organization.objects.create(
        name="CR Delete Policy Org",
        external_event_update_policy=ExternalEventUpdatePolicy.CHANGE_REQUEST,
    )


@pytest.fixture
def organization_allow(db: Any) -> Organization:
    """Organization with ALLOW policy (direct-delete, legacy behavior)."""
    return Organization.objects.create(
        name="Allow Delete Policy Org",
        external_event_update_policy=ExternalEventUpdatePolicy.ALLOW,
    )


@pytest.fixture
def user(db: Any) -> User:
    u = User.objects.create_user(email="sync_del_cr@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=u)
    return u


@pytest.fixture
def social_account(db: Any, user: User) -> SocialAccount:
    return SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid="del_cr_test_999"
    )


@pytest.fixture
def calendar_cr(db: Any, organization_change_request: Organization) -> Calendar:
    return Calendar.objects.create(
        name="CR Delete Calendar",
        external_id="del_cr_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization_change_request,
    )


@pytest.fixture
def calendar_allow(db: Any, organization_allow: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Allow Delete Calendar",
        external_id="del_allow_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization_allow,
    )


@pytest.fixture
def fake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.provider = CalendarProvider.GOOGLE
    return adapter


@pytest.fixture
def context_cr(
    organization_change_request: Organization, user: User, fake_adapter: MagicMock
) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization_change_request,
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
    """Service with audit_service=None (no audit assertions needed)."""
    return ExternalEventChangeRequestService(audit_service=None)


@pytest.fixture
def change_request_service_with_audit() -> ExternalEventChangeRequestService:
    """Service with a real AuditService for audit-assertion tests.

    Uses the DI container to construct the service so the repository is wired
    correctly (AuditService.__init__ requires a repository via @inject).
    """
    from di_core.containers import container

    assert container is not None, "DI container must be initialized"
    return container.external_event_change_request_service()


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


def _inbound_cancelled_event(
    external_id: str,
    calendar_external_id: str = "del_cr_cal_001",
) -> CalendarEventAdapterOutputData:
    """Build a cancelled CalendarEventAdapterOutputData as if received from the provider.

    Real providers send cancellation notices with minimal payload data; the
    start_time/end_time may be absent or stale. We pass sentinel values since
    the cancelled-event processing path reads ``event.status``, not the times.
    """
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
# Tests: CHANGE_REQUEST policy — deletion interception
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_change_request_policy_creates_pending_delete_request_and_keeps_local_event(
    context_cr: CalendarServiceContext,
    calendar_cr: Calendar,
    organization_change_request: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under CHANGE_REQUEST, an inbound cancellation creates a PENDING delete request and
    does NOT delete the local CalendarEvent."""
    existing = _make_existing_event(
        calendar_cr,
        external_id="evt_del_cr_001",
        title="Event To Possibly Delete",
        description="Important description",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
    )

    inbound = _inbound_cancelled_event("evt_del_cr_001", calendar_external_id="del_cr_cal_001")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-del-cr",
    }

    calendar_sync = _make_calendar_sync(calendar_cr, organization_change_request)
    service = CalendarSyncService(
        context=context_cr,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    # Local event must still EXIST — it must NOT have been deleted.
    assert CalendarEvent.objects.filter(
        external_id="evt_del_cr_001",
        organization_id=organization_change_request.id,
    ).exists()

    # Exactly one PENDING delete change request must exist.
    requests = ExternalEventChangeRequest.objects.filter(
        organization_id=organization_change_request.id,
        event=existing,
    )
    assert requests.count() == 1
    cr = requests.get()
    assert cr.status == ExternalEventChangeRequestStatus.PENDING
    assert cr.kind == ExternalEventChangeKind.DELETE
    assert cr.provider == CalendarProvider.GOOGLE

    # retained_values captures the pre-deletion local snapshot.
    assert cr.retained_values["title"] == "Event To Possibly Delete"
    assert cr.retained_values["description"] == "Important description"
    assert "start_time" in cr.retained_values
    assert "end_time" in cr.retained_values

    # proposed_values is empty for a deletion.
    assert cr.proposed_values == {}

    # proposed_payload is the raw inbound payload.
    assert cr.proposed_payload == {"id": "evt_del_cr_001", "status": "cancelled"}


@pytest.mark.django_db
def test_change_request_policy_deletion_external_id_in_matched_event_ids(
    context_cr: CalendarServiceContext,
    calendar_cr: Calendar,
    organization_change_request: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under CHANGE_REQUEST, the intercepted deletion's external id is in matched_event_ids
    so the full-sync deletion pass does NOT delete the local event even though it was not
    added to events_to_delete."""
    # Pre-create two events; only one will be returned as cancelled by the provider.
    intercepted = _make_existing_event(
        calendar_cr,
        external_id="evt_del_intercept",
        title="Will Not Be Deleted",
    )
    _make_existing_event(
        calendar_cr,
        external_id="evt_del_vanished",
        title="Will Be Deleted By Full Sync",
    )

    inbound = _inbound_cancelled_event("evt_del_intercept", calendar_external_id="del_cr_cal_001")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": None,  # full sync (no token)
    }

    calendar_sync = _make_calendar_sync(calendar_cr, organization_change_request)
    service = CalendarSyncService(
        context=context_cr,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    # Full sync: pass sync_token=None to trigger _handle_deletions_for_full_sync.
    service._execute_calendar_sync(calendar_sync, sync_token=None)

    # The intercepted event must still exist (matched_event_ids kept it from deletion).
    assert CalendarEvent.objects.filter(
        external_id="evt_del_intercept",
        organization_id=organization_change_request.id,
    ).exists()
    intercepted.refresh_from_db()
    assert intercepted.title == "Will Not Be Deleted"

    # The truly vanished event must have been deleted by full-sync.
    assert not CalendarEvent.objects.filter(
        external_id="evt_del_vanished",
        organization_id=organization_change_request.id,
    ).exists()

    # One PENDING delete request created for the intercepted event.
    assert (
        ExternalEventChangeRequest.objects.filter(
            organization_id=organization_change_request.id,
            event=intercepted,
            status=ExternalEventChangeRequestStatus.PENDING,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_change_request_policy_re_cancel_marks_prior_stale_and_creates_new_pending(
    context_cr: CalendarServiceContext,
    calendar_cr: Calendar,
    organization_change_request: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under CHANGE_REQUEST, a second inbound cancellation supersedes the first: the prior
    PENDING request is marked STALE and a fresh PENDING is created. Two rows total,
    history preserved. Local event remains present throughout."""
    existing = _make_existing_event(
        calendar_cr,
        external_id="evt_del_recancel",
        title="Event That Gets Re-cancelled",
    )

    # --- First inbound cancellation ---
    first_inbound = _inbound_cancelled_event(
        "evt_del_recancel", calendar_external_id="del_cr_cal_001"
    )
    fake_adapter.get_events.return_value = {
        "events": [first_inbound],
        "next_sync_token": "tok-del-1",
    }
    calendar_sync_1 = _make_calendar_sync(calendar_cr, organization_change_request)
    service = CalendarSyncService(
        context=context_cr,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync_1, sync_token="tok-prev-1")

    # Confirm first PENDING request exists.
    first_request = ExternalEventChangeRequest.objects.get(
        organization_id=organization_change_request.id,
        event=existing,
        status=ExternalEventChangeRequestStatus.PENDING,
    )
    assert first_request.kind == ExternalEventChangeKind.DELETE

    # --- Second inbound cancellation ---
    second_inbound = _inbound_cancelled_event(
        "evt_del_recancel", calendar_external_id="del_cr_cal_001"
    )
    fake_adapter.get_events.return_value = {
        "events": [second_inbound],
        "next_sync_token": "tok-del-2",
    }
    calendar_sync_2 = _make_calendar_sync(calendar_cr, organization_change_request)
    service2 = CalendarSyncService(
        context=context_cr,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service2._execute_calendar_sync(calendar_sync_2, sync_token="tok-prev-2")

    # Total of 2 rows: one STALE, one PENDING.
    all_requests = list(
        ExternalEventChangeRequest.objects.filter(
            organization_id=organization_change_request.id,
            event=existing,
        ).order_by("id")
    )
    assert len(all_requests) == 2

    statuses = {r.status for r in all_requests}
    assert statuses == {
        ExternalEventChangeRequestStatus.PENDING,
        ExternalEventChangeRequestStatus.STALE,
    }

    # Both are delete-kind.
    assert all(r.kind == ExternalEventChangeKind.DELETE for r in all_requests)

    # Local event still present — never deleted during interception.
    assert CalendarEvent.objects.filter(
        external_id="evt_del_recancel",
        organization_id=organization_change_request.id,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: ALLOW policy — backward-compat (direct-delete)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_allow_policy_deletes_event_directly_and_creates_no_change_request(
    context_allow: CalendarServiceContext,
    calendar_allow: Calendar,
    organization_allow: Organization,
    fake_adapter: MagicMock,
    change_request_service: ExternalEventChangeRequestService,
) -> None:
    """Under ALLOW, an inbound cancellation deletes the local CalendarEvent directly
    (existing behavior) and no ExternalEventChangeRequest is created."""
    _make_existing_event(
        calendar_allow,
        external_id="evt_del_allow_001",
        title="Event To Delete Directly",
    )

    inbound = _inbound_cancelled_event(
        "evt_del_allow_001", calendar_external_id="del_allow_cal_001"
    )
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-del-allow",
    }

    calendar_sync = _make_calendar_sync(calendar_allow, organization_allow)
    service = CalendarSyncService(
        context=context_allow,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service,
    )
    service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    # Local event must be DELETED.
    assert not CalendarEvent.objects.filter(
        external_id="evt_del_allow_001",
        organization_id=organization_allow.id,
    ).exists()

    # No ExternalEventChangeRequest created.
    assert not ExternalEventChangeRequest.objects.filter(
        organization_id=organization_allow.id,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: Misconfiguration guard — CHANGE_REQUEST without service raises loud
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_change_request_policy_without_service_raises_improperly_configured(
    context_cr: CalendarServiceContext,
    calendar_cr: Calendar,
    organization_change_request: Organization,
    fake_adapter: MagicMock,
) -> None:
    """Under CHANGE_REQUEST policy, if ExternalEventChangeRequestService is not
    injected (None), the sync must raise ImproperlyConfigured rather than silently
    falling through to direct-delete (which would defeat the safety guarantee)."""
    _make_existing_event(
        calendar_cr,
        external_id="evt_del_misconfig",
        title="Event That Must Not Be Silently Deleted",
    )

    inbound = _inbound_cancelled_event("evt_del_misconfig", calendar_external_id="del_cr_cal_001")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-del-misconfig",
    }

    calendar_sync = _make_calendar_sync(calendar_cr, organization_change_request)
    # Deliberately omit external_event_change_request_service (defaults to None).
    service = CalendarSyncService(
        context=context_cr,
        calendar_cache={},
        host=FakeHost(),
    )

    with pytest.raises(ImproperlyConfigured, match="ExternalEventChangeRequestService"):
        service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")


# ---------------------------------------------------------------------------
# Tests: Audit record emitted on delete change request creation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_change_request_deletion_creation_records_audit_entry(
    context_cr: CalendarServiceContext,
    calendar_cr: Calendar,
    organization_change_request: Organization,
    fake_adapter: MagicMock,
    change_request_service_with_audit: ExternalEventChangeRequestService,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Creating a PENDING delete change request emits a SYSTEM-actor audit entry with the
    expected action and diff (retained values as 'old', None as 'new')."""
    existing = _make_existing_event(
        calendar_cr,
        external_id="evt_del_audit_cr",
        title="Audited Event Title",
        description="Audited description",
    )

    inbound = _inbound_cancelled_event("evt_del_audit_cr", calendar_external_id="del_cr_cal_001")
    fake_adapter.get_events.return_value = {
        "events": [inbound],
        "next_sync_token": "tok-del-audit",
    }

    calendar_sync = _make_calendar_sync(calendar_cr, organization_change_request)
    service = CalendarSyncService(
        context=context_cr,
        calendar_cache={},
        host=FakeHost(),
        external_event_change_request_service=change_request_service_with_audit,
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service._execute_calendar_sync(calendar_sync, sync_token="tok-prev")

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]

    # Find the change-request audit entry.
    cr_payloads = [p for p in payloads if p["action"] == AuditAction.EXTERNAL_CHANGE_REQUESTED]
    assert len(cr_payloads) == 1
    payload = cr_payloads[0]

    assert payload["organization_id"] == organization_change_request.id
    assert payload["actor"]["actor_type"] == "system"
    assert payload["actor"]["actor_id"] is None

    # The subject is the ExternalEventChangeRequest.
    assert payload["subject"]["subject_type"] == "calendar_integration.ExternalEventChangeRequest"

    # The diff must show the retained values as "old" with None as "new" (deletion intent).
    assert "title" in payload["diff"]
    assert payload["diff"]["title"]["old"] == "Audited Event Title"
    assert payload["diff"]["title"]["new"] is None

    # Description must be in the audit diff with the correct old value.
    assert "description" in payload["diff"]
    assert payload["diff"]["description"]["old"] == "Audited description"
    assert payload["diff"]["description"]["new"] is None

    # start_time and end_time must be in the audit diff with correct isoformat values.
    assert "start_time" in payload["diff"]
    assert payload["diff"]["start_time"]["old"] == "2025-09-01T09:00:00+00:00"
    assert payload["diff"]["start_time"]["new"] is None

    assert "end_time" in payload["diff"]
    assert payload["diff"]["end_time"]["old"] == "2025-09-01T10:00:00+00:00"
    assert payload["diff"]["end_time"]["new"] is None

    # The local event is still present (not deleted).
    assert CalendarEvent.objects.filter(
        external_id="evt_del_audit_cr",
        organization_id=organization_change_request.id,
    ).exists()
    existing.refresh_from_db()
    assert existing.title == "Audited Event Title"
