"""Integration tests for Phase 5b — reject a change request (outbound undo).

Tests exercise ``ExternalEventChangeRequestService.reject`` and the supporting
``_undo_on_provider`` outbound path. The provider write adapter is MOCKED — these
tests never touch a real Google Calendar.

Test matrix:
- Reject an UPDATE request → the outbound ``update_event`` is called with the local
  event's current (retained) values, its external id, and the calendar external id;
  request status becomes REJECTED; resolved_by / resolved_at set; the local event is
  unchanged and keeps its external id.
- Reject a DELETE request → the outbound ``create_event`` is called; the local event
  is rebound to the newly returned external id and still exists; request REJECTED.
- Ineligible membership → ChangeRequestIneligibleError; no outbound call; request stays
  PENDING.
- Non-PENDING request → ChangeRequestNotPendingError; no outbound call.
- Audit record is written with EXTERNAL_CHANGE_REJECTED and the rejecting membership as
  the actor.
- Partial failure: a DELETE re-create succeeds but the subsequent local commit fails →
  the just-created provider event is COMPENSATED (deleted by its new id) so no provider
  orphan/duplicate survives, the surrounding transaction rolls back (request stays
  PENDING, event keeps its old external id), and the original exception propagates.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from audit.constants import AuditAction, AuditActorType
from calendar_integration.constants import (
    CalendarProvider,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.exceptions import (
    ChangeRequestIneligibleError,
    ChangeRequestNotPendingError,
)
from calendar_integration.factories import (
    create_event_attendance,
    create_external_event_change_request,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    EventExternalAttendance,
    ExternalAttendee,
    RecurrenceRule,
)
from calendar_integration.services.dataclasses import CalendarEventAdapterOutputData
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Reject Test Org")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Test Calendar",
        external_id="cal_external_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def attendee_membership(organization: Organization) -> OrganizationMembership:
    """A regular (non-admin) member who attends the event."""
    user = User.objects.create_user(email="attendee@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=user)
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.MEMBER},
    )
    return membership


@pytest.fixture
def admin_membership(organization: Organization) -> OrganizationMembership:
    """An admin member who is NOT an attendee of the event."""
    user = User.objects.create_user(email="admin@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=user)
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.ADMIN},
    )
    return membership


@pytest.fixture
def ineligible_membership(organization: Organization) -> OrganizationMembership:
    """A regular (non-admin) member who is NOT an attendee of the event."""
    user = User.objects.create_user(email="noone@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=user)
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.MEMBER},
    )
    return membership


@pytest.fixture
def event(calendar: Calendar, organization: Organization) -> CalendarEvent:
    """A synced event in its retained (pre-inbound-change) state.

    Phase 3/4 interception never mutates the local event, so its current field values
    *are* the retained values used for the outbound undo.
    """
    return CalendarEvent.objects.create(
        calendar=calendar,
        title="Original Title",
        description="Original description",
        start_time_tz_unaware=datetime.datetime(2025, 9, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 9, 1, 10, 0),
        timezone="UTC",
        external_id="event_external_001",
        organization=organization,
    )


@pytest.fixture
def service() -> ExternalEventChangeRequestService:
    """Service with no audit (audit assertions are done in a separate test)."""
    return ExternalEventChangeRequestService(audit_service=None)


@pytest.fixture
def service_with_audit() -> ExternalEventChangeRequestService:
    """Service with a real AuditService wired via DI container."""
    from di_core.containers import container

    return container.external_event_change_request_service()


def _adapter_output(
    external_id: str, calendar_external_id: str = "cal_external_001"
) -> CalendarEventAdapterOutputData:
    """Build a minimal adapter output for a (re)created/updated provider event."""
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


# ---------------------------------------------------------------------------
# Tests: UPDATE kind — outbound update_event pushes retained values back
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reject_update_pushes_retained_values_to_provider(
    service: ExternalEventChangeRequestService,
    calendar: Calendar,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
) -> None:
    """Rejecting an UPDATE request calls update_event with the retained values, the
    event's external id and the calendar external id; the local event is unchanged."""
    create_event_attendance(event=event, user=attendee_membership.user)

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={
            "title": "Inbound Edited Title",
            "description": "Inbound edited description",
            "start_time": "2025-09-01T11:00:00+00:00",
            "end_time": "2025-09-01T12:00:00+00:00",
        },
        retained_values={
            "title": "Original Title",
            "description": "Original description",
            "start_time": "2025-09-01T09:00:00+00:00",
            "end_time": "2025-09-01T10:00:00+00:00",
        },
    )

    write_adapter = MagicMock()
    write_adapter.update_event.return_value = _adapter_output(
        "event_external_001", calendar.external_id
    )

    result = service.reject(
        change_request, membership=attendee_membership, write_adapter=write_adapter
    )

    # Outbound update was called once with the external ids.
    write_adapter.update_event.assert_called_once()
    write_adapter.create_event.assert_not_called()
    call_args = write_adapter.update_event.call_args
    assert call_args.args[0] == calendar.external_id  # calendar external id (positional)
    assert call_args.args[1] == "event_external_001"  # event external id (positional)

    adapter_input = call_args.args[2]
    # Input carries the retained (current local) values + external ids.
    assert adapter_input.calendar_external_id == calendar.external_id
    assert adapter_input.external_id == "event_external_001"
    assert adapter_input.title == "Original Title"
    assert adapter_input.description == "Original description"
    assert adapter_input.timezone == "UTC"
    assert adapter_input.start_time == datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    assert adapter_input.end_time == datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC)

    # Request is REJECTED with resolver set.
    assert result.status == ExternalEventChangeRequestStatus.REJECTED
    assert result.resolved_by_user_id == attendee_membership.user_id
    assert result.resolved_at is not None

    # The local event is unchanged (it was never mutated) and keeps its external id.
    event.refresh_from_db()
    assert event.title == "Original Title"
    assert event.external_id == "event_external_001"


@pytest.mark.django_db
def test_admin_can_reject_update_request(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """An admin (not an attendee) may reject any event's UPDATE request."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Inbound"},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()
    write_adapter.update_event.return_value = _adapter_output("event_external_001")

    result = service.reject(
        change_request, membership=admin_membership, write_adapter=write_adapter
    )

    write_adapter.update_event.assert_called_once()
    assert result.status == ExternalEventChangeRequestStatus.REJECTED
    assert result.resolved_by_user_id == admin_membership.user_id


@pytest.mark.django_db
def test_reject_update_preserves_attendees_and_recurrence(
    service: ExternalEventChangeRequestService,
    calendar: Calendar,
    organization: Organization,
    attendee_membership: OrganizationMembership,
) -> None:
    """Rejecting an UPDATE for an event that has attendees + a recurrence rule must
    carry the full attendees (internal + external) and the RRULE into the adapter
    input, NOT an empty list / missing recurrence (the Google adapter does a
    full-replace PUT, so omitted fields would be wiped)."""
    recurrence_rule = RecurrenceRule.objects.create(
        organization=organization,
        frequency="WEEKLY",
        interval=1,
    )
    event = CalendarEvent.objects.create(
        calendar=calendar,
        title="Original Title",
        description="Original description",
        start_time_tz_unaware=datetime.datetime(2025, 9, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 9, 1, 10, 0),
        timezone="UTC",
        external_id="event_external_001",
        organization=organization,
        recurrence_rule_fk=recurrence_rule,
    )
    # Internal member attendee.
    create_event_attendance(event=event, user=attendee_membership.user, status="accepted")
    # External (non-member) attendee.
    external_attendee = ExternalAttendee.objects.create(
        organization=organization,
        email="guest@example.com",
        name="Guest Person",
    )
    EventExternalAttendance.objects.create(
        organization=organization,
        event=event,
        external_attendee=external_attendee,
        status="pending",
    )

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Inbound Edited Title"},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()
    write_adapter.update_event.return_value = _adapter_output(
        "event_external_001", calendar.external_id
    )

    service.reject(change_request, membership=attendee_membership, write_adapter=write_adapter)

    adapter_input = write_adapter.update_event.call_args.args[2]

    # Attendees survive (NOT wiped to []): both the internal member and external guest.
    attendee_emails = {a.email for a in adapter_input.attendees}
    assert attendee_emails == {attendee_membership.user.email, "guest@example.com"}
    assert len(adapter_input.attendees) == 2

    # Recurrence survives.
    assert adapter_input.recurrence_rule is not None
    assert "FREQ=WEEKLY" in adapter_input.recurrence_rule


# ---------------------------------------------------------------------------
# Tests: DELETE kind — outbound create_event re-creates + external_id rebind
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reject_delete_recreates_and_rebinds_external_id(
    service: ExternalEventChangeRequestService,
    calendar: Calendar,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
) -> None:
    """Rejecting a DELETE request re-creates the event on the provider and rebinds the
    local event's external id to the newly-returned provider id; the event still
    exists locally; request becomes REJECTED."""
    create_event_attendance(event=event, user=attendee_membership.user)
    event_id = event.pk
    old_external_id = event.external_id

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.DELETE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={},
        retained_values={
            "title": "Original Title",
            "description": "Original description",
            "start_time": "2025-09-01T09:00:00+00:00",
            "end_time": "2025-09-01T10:00:00+00:00",
        },
    )

    write_adapter = MagicMock()
    write_adapter.create_event.return_value = _adapter_output(
        "event_external_NEW_999", calendar.external_id
    )

    result = service.reject(
        change_request, membership=attendee_membership, write_adapter=write_adapter
    )

    # Outbound create was called once with the retained values; no update.
    write_adapter.create_event.assert_called_once()
    write_adapter.update_event.assert_not_called()
    adapter_input = write_adapter.create_event.call_args.args[0]
    assert adapter_input.calendar_external_id == calendar.external_id
    assert adapter_input.title == "Original Title"
    assert adapter_input.description == "Original description"
    assert adapter_input.timezone == "UTC"
    # The old external id is carried in the input (the request's pre-delete id), but the
    # provider returns a NEW one we must rebind to.
    assert adapter_input.external_id == old_external_id

    # The local event still exists and now tracks the NEW external id (churn).
    refreshed = CalendarEvent.objects.get(pk=event_id, organization_id=event.organization_id)
    assert refreshed.external_id == "event_external_NEW_999"
    assert refreshed.external_id != old_external_id

    # Request REJECTED.
    assert result.status == ExternalEventChangeRequestStatus.REJECTED
    assert result.resolved_by_user_id == attendee_membership.user_id
    assert result.resolved_at is not None


# ---------------------------------------------------------------------------
# Tests: eligibility guard — ineligible member, no outbound call, stays PENDING
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ineligible_member_cannot_reject(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    ineligible_membership: OrganizationMembership,
) -> None:
    """A non-attendee, non-admin membership raises ChangeRequestIneligibleError; no
    outbound call is made and the request stays PENDING."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Inbound"},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()

    with pytest.raises(ChangeRequestIneligibleError):
        service.reject(
            change_request, membership=ineligible_membership, write_adapter=write_adapter
        )

    write_adapter.update_event.assert_not_called()
    write_adapter.create_event.assert_not_called()

    change_request.refresh_from_db()
    assert change_request.status == ExternalEventChangeRequestStatus.PENDING
    assert change_request.resolved_by_user_id is None
    assert change_request.resolved_at is None


# ---------------------------------------------------------------------------
# Tests: status guard — non-PENDING request raises, no outbound call
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rejecting_already_rejected_request_raises(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """Rejecting an already-REJECTED request raises ChangeRequestNotPendingError and
    makes no outbound call."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.REJECTED,
        proposed_values={"title": "Inbound"},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()

    with pytest.raises(ChangeRequestNotPendingError):
        service.reject(change_request, membership=admin_membership, write_adapter=write_adapter)

    write_adapter.update_event.assert_not_called()
    write_adapter.create_event.assert_not_called()


@pytest.mark.django_db
def test_rejecting_stale_request_raises(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """Rejecting a STALE request raises ChangeRequestNotPendingError."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.STALE,
        proposed_values={"title": "Old Edit"},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()

    with pytest.raises(ChangeRequestNotPendingError):
        service.reject(change_request, membership=admin_membership, write_adapter=write_adapter)

    write_adapter.update_event.assert_not_called()
    write_adapter.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: audit record emitted with EXTERNAL_CHANGE_REJECTED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reject_records_external_change_rejected_audit_entry(
    service_with_audit: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Rejecting a request records an EXTERNAL_CHANGE_REJECTED audit entry with the
    rejecting membership as the actor."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Inbound Title"},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()
    write_adapter.update_event.return_value = _adapter_output("event_external_001")

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service_with_audit.reject(
                change_request, membership=admin_membership, write_adapter=write_adapter
            )

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]

    rejected_payloads = [p for p in payloads if p["action"] == AuditAction.EXTERNAL_CHANGE_REJECTED]
    assert len(rejected_payloads) == 1
    payload = rejected_payloads[0]

    assert payload["organization_id"] == event.organization_id
    assert payload["actor"]["actor_type"] == AuditActorType.MEMBERSHIP
    assert payload["actor"]["actor_id"] == admin_membership.user_id
    assert payload["subject"]["subject_type"] == "calendar_integration.ExternalEventChangeRequest"
    # Diff re-converges to the retained value (new == retained, old == proposed).
    assert "title" in payload["diff"]
    assert payload["diff"]["title"]["old"] == "Inbound Title"
    assert payload["diff"]["title"]["new"] == "Original Title"


# ---------------------------------------------------------------------------
# Tests: partial failure — re-create succeeds, local save fails → no orphan
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reject_delete_partial_failure_compensates(
    service: ExternalEventChangeRequestService,
    calendar: Calendar,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
) -> None:
    """If the provider re-create succeeds but the subsequent local commit (rebind +
    status flip) raises, the just-created provider event must be COMPENSATED: a
    ``delete_event`` is called with the new provider id so no provider orphan/duplicate
    survives, the surrounding transaction rolls back (request stays PENDING, event keeps
    its old external id), and the original exception propagates."""
    create_event_attendance(event=event, user=attendee_membership.user)
    old_external_id = event.external_id

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.DELETE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={},
        retained_values={"title": "Original Title"},
    )

    write_adapter = MagicMock()
    write_adapter.create_event.return_value = _adapter_output(
        "event_external_NEW_999", calendar.external_id
    )

    # Force the local external-id rebind save (inside the post-create atomic block) to
    # fail AFTER the successful provider create — exercising the compensation path.
    class _SaveError(RuntimeError):
        pass

    with patch.object(CalendarEvent, "save", side_effect=_SaveError("save failed")):
        with pytest.raises(_SaveError):
            service.reject(
                change_request, membership=attendee_membership, write_adapter=write_adapter
            )

    # Provider create was attempted (outside the transaction), then COMPENSATED: the
    # just-created provider event is deleted by its new id so it is never orphaned.
    write_adapter.create_event.assert_called_once()
    write_adapter.delete_event.assert_called_once_with(
        calendar.external_id, "event_external_NEW_999"
    )

    # The local commit rolled back: request stays PENDING, event keeps its old id.
    change_request.refresh_from_db()
    assert change_request.status == ExternalEventChangeRequestStatus.PENDING
    assert change_request.resolved_by_user_id is None

    event.refresh_from_db()
    assert event.external_id == old_external_id


@pytest.mark.django_db
def test_reject_raises_when_event_is_none(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """A PENDING request whose associated event has been deleted (event is None) cannot
    be undone — reject raises ChangeRequestIneligibleError and makes no provider call."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Inbound"},
        retained_values={"title": "Original Title"},
    )
    # Sever the event association to simulate an event deleted out from under the
    # request (SET_NULL cascade) while it is still PENDING.
    change_request.event_fk = None
    change_request.save(update_fields=["event_fk"])
    change_request.refresh_from_db()

    write_adapter = MagicMock()

    with pytest.raises(ChangeRequestIneligibleError):
        service.reject(change_request, membership=admin_membership, write_adapter=write_adapter)

    write_adapter.update_event.assert_not_called()
    write_adapter.create_event.assert_not_called()

    change_request.refresh_from_db()
    assert change_request.status == ExternalEventChangeRequestStatus.PENDING
