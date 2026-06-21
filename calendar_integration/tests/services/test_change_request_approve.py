"""Integration tests for Phase 5a — approve a change request.

Tests exercise ``ExternalEventChangeRequestService.approve`` and the
supporting ``can_resolve`` eligibility helper directly against the real test
database.

Test matrix:
- Member-attendee approves their event's UPDATE request → local CalendarEvent
  reflects proposed values; request status is APPROVED, resolved_by and
  resolved_at are set.
- Admin (not an attendee) approves any event's UPDATE request.
- DELETE-kind approval → the local CalendarEvent is deleted; the request row
  survives with status APPROVED and event NULL.
- Ineligible membership (not attendee, not admin) → raises
  ChangeRequestIneligibleError; event unchanged; request stays PENDING.
- Approving a non-PENDING request (already APPROVED or STALE) → raises
  ChangeRequestNotPendingError.
- Audit record is written with EXTERNAL_CHANGE_APPROVED and the approving
  membership as the actor.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

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
from calendar_integration.models import Calendar, CalendarEvent, ExternalEventChangeRequest
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
    return Organization.objects.create(name="Approve Test Org")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Test Calendar",
        external_id="test_cal_001",
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
    return CalendarEvent.objects.create(
        calendar=calendar,
        title="Original Title",
        description="Original description",
        start_time_tz_unaware=datetime.datetime(2025, 9, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 9, 1, 10, 0),
        timezone="UTC",
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


# ---------------------------------------------------------------------------
# Tests: UPDATE kind — member-attendee approves
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_attendee_approves_update_request_applies_proposed_values(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
) -> None:
    """Member-attendee approves an UPDATE request → local event reflects proposed values."""
    # Make the membership an attendee of the event.
    create_event_attendance(event=event, user=attendee_membership.user)

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={
            "title": "Edited Title",
            "description": "Edited description",
            "start_time": "2025-09-01T09:30:00+00:00",
            "end_time": "2025-09-01T10:30:00+00:00",
        },
        retained_values={
            "title": "Original Title",
            "description": "Original description",
            "start_time": "2025-09-01T09:00:00+00:00",
            "end_time": "2025-09-01T10:00:00+00:00",
        },
    )

    result = service.approve(change_request, membership=attendee_membership)

    # Request must be APPROVED with resolver set.
    assert result.status == ExternalEventChangeRequestStatus.APPROVED
    assert result.resolved_by_user_id == attendee_membership.user_id
    assert result.resolved_at is not None

    # Local event must reflect proposed values.
    event.refresh_from_db()
    assert event.title == "Edited Title"
    assert event.description == "Edited description"
    # Django's USE_TZ=True attaches UTC when reading DateTimeField values back from the DB;
    # compare to aware datetimes to avoid naive/aware mismatches.
    assert event.start_time_tz_unaware == datetime.datetime(2025, 9, 1, 9, 30, tzinfo=datetime.UTC)
    assert event.end_time_tz_unaware == datetime.datetime(2025, 9, 1, 10, 30, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Tests: UPDATE kind — admin approves (not an attendee)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_approves_update_request_for_any_event(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """Admin (not an attendee) can approve an UPDATE request for any event in the org."""
    assert admin_membership.is_admin  # sanity check

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={
            "title": "Admin Approved Title",
            "description": "Admin approved description",
        },
        retained_values={
            "title": "Original Title",
            "description": "Original description",
        },
    )

    result = service.approve(change_request, membership=admin_membership)

    assert result.status == ExternalEventChangeRequestStatus.APPROVED
    assert result.resolved_by_user_id == admin_membership.user_id

    event.refresh_from_db()
    assert event.title == "Admin Approved Title"
    assert event.description == "Admin approved description"


# ---------------------------------------------------------------------------
# Tests: DELETE kind — approval deletes the event; request survives with event=NULL
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_attendee_approves_delete_request_removes_event_and_keeps_request(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
) -> None:
    """DELETE-kind approval deletes the local CalendarEvent. The request row survives
    with status APPROVED and event_fk NULL (SET_NULL cascade)."""
    create_event_attendance(event=event, user=attendee_membership.user)

    event_id = event.pk  # capture before deletion

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.DELETE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={},
        retained_values={
            "title": "Original Title",
            "description": "Original description",
        },
    )
    request_id = change_request.pk

    result = service.approve(change_request, membership=attendee_membership)

    # The local event must no longer exist.
    assert not CalendarEvent.objects.filter(
        pk=event_id, organization_id=event.organization_id
    ).exists()

    # The request row must survive with status APPROVED and event NULL.
    # Multi-tenancy manager requires organization_id in the filter.
    saved = ExternalEventChangeRequest.objects.get(
        pk=request_id, organization_id=event.organization_id
    )
    assert saved.status == ExternalEventChangeRequestStatus.APPROVED
    assert saved.event_fk_id is None
    assert saved.resolved_by_user_id == attendee_membership.user_id
    assert saved.resolved_at is not None

    # The returned object must also reflect the nulled event.
    assert result.event_fk_id is None
    assert result.status == ExternalEventChangeRequestStatus.APPROVED


# ---------------------------------------------------------------------------
# Tests: admin approves a DELETE request
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_approves_delete_request(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """Admin can approve a DELETE request for any event."""
    event_id = event.pk

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.DELETE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={},
        retained_values={"title": "Original Title"},
    )

    result = service.approve(change_request, membership=admin_membership)

    assert not CalendarEvent.objects.filter(
        pk=event_id, organization_id=event.organization_id
    ).exists()
    assert result.status == ExternalEventChangeRequestStatus.APPROVED
    assert result.event_fk_id is None


# ---------------------------------------------------------------------------
# Tests: eligibility — ineligible member (not attendee, not admin)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ineligible_member_cannot_approve(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    ineligible_membership: OrganizationMembership,
) -> None:
    """A non-attendee, non-admin membership raises ChangeRequestIneligibleError.
    The event must remain unchanged and the request stays PENDING."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Hacked Title"},
        retained_values={"title": "Original Title"},
    )

    with pytest.raises(ChangeRequestIneligibleError):
        service.approve(change_request, membership=ineligible_membership)

    # Event unchanged.
    event.refresh_from_db()
    assert event.title == "Original Title"

    # Request still PENDING.
    change_request.refresh_from_db()
    assert change_request.status == ExternalEventChangeRequestStatus.PENDING
    assert change_request.resolved_by_user_id is None
    assert change_request.resolved_at is None


# ---------------------------------------------------------------------------
# Tests: status guard — non-PENDING request raises ChangeRequestNotPendingError
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_approving_already_approved_request_raises(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """Approving an already-APPROVED request raises ChangeRequestNotPendingError."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.APPROVED,
        proposed_values={"title": "Already Done"},
        retained_values={"title": "Original Title"},
    )

    with pytest.raises(ChangeRequestNotPendingError):
        service.approve(change_request, membership=admin_membership)


@pytest.mark.django_db
def test_approving_stale_request_raises(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """Approving a STALE request raises ChangeRequestNotPendingError."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.STALE,
        proposed_values={"title": "Old Edit"},
        retained_values={"title": "Original Title"},
    )

    with pytest.raises(ChangeRequestNotPendingError):
        service.approve(change_request, membership=admin_membership)


# ---------------------------------------------------------------------------
# Tests: audit record emitted with EXTERNAL_CHANGE_APPROVED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_approve_records_external_change_approved_audit_entry(
    service_with_audit: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Approving a request records an EXTERNAL_CHANGE_APPROVED audit entry
    with the approving membership as the actor."""
    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "New Title", "description": "New description"},
        retained_values={"title": "Original Title", "description": "Original description"},
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            service_with_audit.approve(change_request, membership=admin_membership)

    payloads = [call.args[0] for call in mock_task.delay.call_args_list]

    # Find the EXTERNAL_CHANGE_APPROVED audit entry.
    approved_payloads = [p for p in payloads if p["action"] == AuditAction.EXTERNAL_CHANGE_APPROVED]
    assert len(approved_payloads) == 1
    payload = approved_payloads[0]

    # Organization matches.
    assert payload["organization_id"] == event.organization_id

    # Actor is the approving membership.
    assert payload["actor"]["actor_type"] == AuditActorType.MEMBERSHIP
    assert payload["actor"]["actor_id"] == admin_membership.user_id

    # Subject is the ExternalEventChangeRequest.
    assert payload["subject"]["subject_type"] == "calendar_integration.ExternalEventChangeRequest"

    # Diff includes the changed fields.
    assert "title" in payload["diff"]
    assert payload["diff"]["title"]["old"] == "Original Title"
    assert payload["diff"]["title"]["new"] == "New Title"


# ---------------------------------------------------------------------------
# Tests: can_resolve helper — cross-org membership is ineligible
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_can_resolve_returns_false_for_different_org(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
) -> None:
    """A membership from a different organization cannot resolve the request."""
    other_org = Organization.objects.create(name="Other Org")
    user = User.objects.create_user(email="otherorg@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=user)
    other_membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=other_org,
        defaults={"role": OrganizationRole.ADMIN},  # even admin in another org
    )

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Cross-org title"},
        retained_values={"title": "Original Title"},
    )

    assert not service.can_resolve(change_request, other_membership)


# ---------------------------------------------------------------------------
# Tests: UPDATE only modifies fields present in proposed_values
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_approve_update_only_updates_present_fields(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """When proposed_values only contains 'title', only title is updated on the event;
    description, start_time, end_time remain unchanged."""
    original_description = event.description
    original_start = event.start_time_tz_unaware
    original_end = event.end_time_tz_unaware

    change_request = create_external_event_change_request(
        event=event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={"title": "Only Title Changed"},
        retained_values={"title": "Original Title"},
    )

    service.approve(change_request, membership=admin_membership)

    event.refresh_from_db()
    assert event.title == "Only Title Changed"
    assert event.description == original_description
    # After refresh_from_db() Django attaches UTC tzinfo. Compare as UTC-aware.
    # original_start/end were stored as naive; the DB returns them tz-aware.
    assert event.start_time_tz_unaware == original_start.replace(tzinfo=datetime.UTC)
    assert event.end_time_tz_unaware == original_end.replace(tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Tests: cross-timezone datetime round-trip
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_approve_update_cross_timezone_stores_local_wall_clock(
    service: ExternalEventChangeRequestService,
    calendar: Calendar,
    organization: Organization,
    attendee_membership: OrganizationMembership,
) -> None:
    """Prove the datetime round-trip is correct for a NON-UTC event timezone.

    The event lives in America/Sao_Paulo (UTC-3). The proposed_values carry
    tz-aware ISO strings expressed in the event's OWN local timezone. After
    approval the stored ``start_time_tz_unaware`` must equal the LOCAL
    wall-clock digits (stripping tzinfo recovers them directly), matching
    what the canonical ``models.py`` creation path stores via
    ``parsed.astimezone(event_tz).replace(tzinfo=None)``.
    """
    # Create an event in the America/Sao_Paulo timezone.
    # Local wall-clock start: 10:00 (stored as naive 10:00 in _tz_unaware).
    sao_paulo_event = CalendarEvent.objects.create(
        calendar=calendar,
        title="Sao Paulo Event",
        description="Cross-tz test",
        start_time_tz_unaware=datetime.datetime(2025, 9, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2025, 9, 1, 11, 0),
        timezone="America/Sao_Paulo",
        organization=organization,
    )

    # Make the attendee a member of this event.
    create_event_attendance(event=sao_paulo_event, user=attendee_membership.user)

    # proposed_values: NEW local time 11:00 in America/Sao_Paulo (-03:00).
    # The tz-aware ISO string carries the offset explicitly so the round-trip
    # is unambiguous.
    change_request = create_external_event_change_request(
        event=sao_paulo_event,
        kind=ExternalEventChangeKind.UPDATE,
        status=ExternalEventChangeRequestStatus.PENDING,
        proposed_values={
            "start_time": "2025-09-01T11:00:00-03:00",
            "end_time": "2025-09-01T12:00:00-03:00",
        },
        retained_values={
            "start_time": "2025-09-01T10:00:00-03:00",
            "end_time": "2025-09-01T11:00:00-03:00",
        },
    )

    service.approve(change_request, membership=attendee_membership)

    sao_paulo_event.refresh_from_db()

    # The _tz_unaware columns must hold the LOCAL wall-clock digits (11:00 / 12:00).
    # Django USE_TZ=True attaches UTC when reading DateTimeField back from the DB;
    # compare as UTC-aware — the numeric digits are what matter, not the offset.
    assert sao_paulo_event.start_time_tz_unaware == datetime.datetime(
        2025, 9, 1, 11, 0, tzinfo=datetime.UTC
    ), (
        f"Expected local wall-clock 11:00 stored in start_time_tz_unaware, "
        f"got {sao_paulo_event.start_time_tz_unaware!r}"
    )
    assert sao_paulo_event.end_time_tz_unaware == datetime.datetime(
        2025, 9, 1, 12, 0, tzinfo=datetime.UTC
    ), (
        f"Expected local wall-clock 12:00 stored in end_time_tz_unaware, "
        f"got {sao_paulo_event.end_time_tz_unaware!r}"
    )
