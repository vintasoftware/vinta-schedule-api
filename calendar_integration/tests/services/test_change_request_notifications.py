"""Integration tests for in-app notifications on PENDING request creation.

Tests exercise ``ExternalEventChangeRequestService._notify_eligible_approvers`` via
the two public entry-points: ``create_or_supersede_update_request`` and
``create_or_supersede_delete_request``.

The ``NotificationService`` is MOCKED in all tests here so no real notification rows
are persisted — only call-count and call-arg assertions are made. DB writes
(request rows, attendance, memberships) are exercised against the real test database.

Test matrix:
- UPDATE request creation notifies each eligible approver exactly once:
  - member-attendees of the event
  - organization admins (even if not attendees)
- Deduplication: a member who is BOTH an attendee AND an admin receives exactly ONE
  notification (not two).
- Non-attendee, non-admin member receives NO notification.
- DELETE request creation similarly notifies exactly the eligible approvers.
- ``auto_undo_inbound_change`` (FORBIDDEN mode) does NOT dispatch any notifications.
- Notifications are dispatched on_commit (asserted via the mock pattern that patches
  ``transaction.on_commit`` to fire synchronously so we can inspect the mock calls).
- When the service is constructed without a ``notification_service`` (tests that
  bypass DI), no notifications are attempted (safe no-op).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from calendar_integration.constants import (
    CalendarProvider,
    ExternalEventChangeKind,
)
from calendar_integration.factories import create_event_attendance
from calendar_integration.models import Calendar, CalendarEvent
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_MODULE = "calendar_integration.services.external_event_change_request_service"


def _patch_on_commit():
    """Context manager that patches transaction.on_commit so callbacks fire immediately.

    This is the canonical pattern in this project for testing on_commit-wrapped
    side effects without wrapping everything in ``django_capture_on_commit_callbacks``.
    The patch target is the ``transaction`` imported inside the service module so that
    only that module's on_commit calls are intercepted.
    """
    return patch(f"{_MODULE}.transaction.on_commit", side_effect=lambda fn: fn())


def _make_user(email: str) -> User:
    user = User.objects.create_user(email=email, password="pass")  # noqa: S106
    Profile.objects.create(user=user)
    return user


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Notification Test Org")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Test Calendar",
        external_id="ext_cal_notif_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def event(calendar: Calendar, organization: Organization) -> CalendarEvent:
    return CalendarEvent.objects.create(
        calendar=calendar,
        title="Notification Test Event",
        description="desc",
        start_time_tz_unaware=datetime.datetime(2025, 11, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 11, 1, 10, 0),
        timezone="UTC",
        organization=organization,
    )


@pytest.fixture
def attendee_membership(organization: Organization) -> OrganizationMembership:
    """A regular member who ATTENDS the event (not an admin)."""
    user = _make_user("attendee_notif@example.com")
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.MEMBER},
    )
    return membership


@pytest.fixture
def admin_membership(organization: Organization) -> OrganizationMembership:
    """An admin member who does NOT attend the event."""
    user = _make_user("admin_notif@example.com")
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.ADMIN},
    )
    return membership


@pytest.fixture
def ineligible_membership(organization: Organization) -> OrganizationMembership:
    """A regular member who is neither an attendee nor an admin."""
    user = _make_user("nobody_notif@example.com")
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.MEMBER},
    )
    return membership


@pytest.fixture
def mock_notification_service() -> MagicMock:
    return MagicMock()


@pytest.fixture
def service(mock_notification_service: MagicMock) -> ExternalEventChangeRequestService:
    """Service with a mocked notification_service (no audit)."""
    return ExternalEventChangeRequestService(
        audit_service=None,
        notification_service=mock_notification_service,
    )


@pytest.fixture
def service_no_notifications() -> ExternalEventChangeRequestService:
    """Service with notification_service=None (safe no-op path)."""
    return ExternalEventChangeRequestService(audit_service=None, notification_service=None)


# ---------------------------------------------------------------------------
# Shared request args
# ---------------------------------------------------------------------------

_UPDATE_PROPOSED: dict[str, Any] = {
    "title": "Edited Title",
    "description": "Edited desc",
    "start_time": "2025-11-01T09:30:00+00:00",
    "end_time": "2025-11-01T10:30:00+00:00",
}
_UPDATE_RETAINED: dict[str, Any] = {
    "title": "Notification Test Event",
    "description": "desc",
    "start_time": "2025-11-01T09:00:00+00:00",
    "end_time": "2025-11-01T10:00:00+00:00",
}
_DELETE_RETAINED: dict[str, Any] = _UPDATE_RETAINED


# ---------------------------------------------------------------------------
# Tests: UPDATE request notifies eligible approvers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_request_notifies_attendee(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """Creating an UPDATE request dispatches an in-app notification to the member-attendee."""
    create_event_attendance(event=event, user=attendee_membership.user)

    with _patch_on_commit():
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    mock_notification_service.create_notification.assert_called_once()
    call_kwargs = mock_notification_service.create_notification.call_args[1]
    assert call_kwargs["user_id"] == attendee_membership.user_id
    assert "IN_APP" in call_kwargs["notification_type"].upper()
    assert call_kwargs["context_name"] == "external_event_change_request_approver_context"
    assert call_kwargs["context_kwargs"]["event_title"] == event.title
    assert call_kwargs["context_kwargs"]["change_kind"] == ExternalEventChangeKind.UPDATE


@pytest.mark.django_db
def test_update_request_notifies_admin(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """Creating an UPDATE request dispatches an in-app notification to the org admin."""
    with _patch_on_commit():
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    mock_notification_service.create_notification.assert_called_once()
    call_kwargs = mock_notification_service.create_notification.call_args[1]
    assert call_kwargs["user_id"] == admin_membership.user_id


@pytest.mark.django_db
def test_inactive_admin_is_not_notified(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """An inactive admin membership receives NO notification."""
    # Mark the admin as inactive.
    admin_membership.is_active = False
    admin_membership.save()

    with _patch_on_commit():
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    # No notification should be dispatched.
    mock_notification_service.create_notification.assert_not_called()


@pytest.mark.django_db
def test_update_request_notifies_both_attendee_and_admin(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
    admin_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """Creating an UPDATE request notifies ALL eligible approvers (attendee + admin)."""
    create_event_attendance(event=event, user=attendee_membership.user)

    with _patch_on_commit():
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    assert mock_notification_service.create_notification.call_count == 2
    notified_user_ids = {
        c[1]["user_id"] for c in mock_notification_service.create_notification.call_args_list
    }
    assert notified_user_ids == {attendee_membership.user_id, admin_membership.user_id}


@pytest.mark.django_db
def test_update_request_deduplicates_attendee_who_is_also_admin(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    organization: Organization,
    mock_notification_service: MagicMock,
) -> None:
    """An admin who is also an attendee of the event receives exactly ONE notification."""
    # Create a membership that is BOTH admin and an attendee.
    user = _make_user("adminattendee_notif@example.com")
    admin_attendee, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.ADMIN},
    )
    create_event_attendance(event=event, user=user)

    with _patch_on_commit():
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    # Should be exactly ONE notification — deduplication must prevent the double-notify.
    assert mock_notification_service.create_notification.call_count == 1
    call_kwargs = mock_notification_service.create_notification.call_args[1]
    assert call_kwargs["user_id"] == admin_attendee.user_id


@pytest.mark.django_db
def test_update_request_does_not_notify_ineligible_member(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    ineligible_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """A non-attendee, non-admin member receives NO notification."""
    with _patch_on_commit():
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    mock_notification_service.create_notification.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: DELETE request notifies eligible approvers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_request_notifies_attendee(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """Creating a DELETE request dispatches an in-app notification to the member-attendee."""
    create_event_attendance(event=event, user=attendee_membership.user)

    with _patch_on_commit():
        service.create_or_supersede_delete_request(
            event=event,
            retained_values=_DELETE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    mock_notification_service.create_notification.assert_called_once()
    call_kwargs = mock_notification_service.create_notification.call_args[1]
    assert call_kwargs["user_id"] == attendee_membership.user_id
    assert call_kwargs["context_kwargs"]["change_kind"] == ExternalEventChangeKind.DELETE


@pytest.mark.django_db
def test_delete_request_notifies_admin(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """Creating a DELETE request dispatches an in-app notification to the org admin."""
    with _patch_on_commit():
        service.create_or_supersede_delete_request(
            event=event,
            retained_values=_DELETE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    mock_notification_service.create_notification.assert_called_once()
    call_kwargs = mock_notification_service.create_notification.call_args[1]
    assert call_kwargs["user_id"] == admin_membership.user_id


@pytest.mark.django_db
def test_delete_request_deduplicates_attendee_who_is_also_admin(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    organization: Organization,
    mock_notification_service: MagicMock,
) -> None:
    """A member who is both an attendee and an admin gets exactly ONE delete notification."""
    user = _make_user("adminattendee_del_notif@example.com")
    admin_attendee, _ = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": OrganizationRole.ADMIN},
    )
    create_event_attendance(event=event, user=user)

    with _patch_on_commit():
        service.create_or_supersede_delete_request(
            event=event,
            retained_values=_DELETE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    assert mock_notification_service.create_notification.call_count == 1
    call_kwargs = mock_notification_service.create_notification.call_args[1]
    assert call_kwargs["user_id"] == admin_attendee.user_id


# ---------------------------------------------------------------------------
# Test: FORBIDDEN (auto_undo_inbound_change) does NOT notify
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_auto_undo_does_not_notify_approvers(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    attendee_membership: OrganizationMembership,
    admin_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """FORBIDDEN auto-undo does NOT dispatch any in-app notifications.

    Only the CHANGE_REQUEST path (create_or_supersede_*) notifies approvers.
    AUTO_UNDONE requests are self-resolved immediately — no human approval is
    needed and no notification is sent.
    """
    create_event_attendance(event=event, user=attendee_membership.user)

    mock_write_adapter = MagicMock()
    mock_write_adapter.update_event.return_value = None

    with _patch_on_commit():
        service.auto_undo_inbound_change(
            event=event,
            kind=ExternalEventChangeKind.UPDATE,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
            write_adapter=mock_write_adapter,
        )

    mock_notification_service.create_notification.assert_not_called()


# ---------------------------------------------------------------------------
# Test: no notification_service → safe no-op
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_notification_service_is_safe_noop(
    service_no_notifications: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
) -> None:
    """When notification_service is None, create_or_supersede_* completes without error."""
    # Should complete without raising; no notifications attempted.
    request = service_no_notifications.create_or_supersede_update_request(
        event=event,
        proposed_values=_UPDATE_PROPOSED,
        retained_values=_UPDATE_RETAINED,
        payload={},
        provider=CalendarProvider.GOOGLE,
    )
    assert request.pk is not None


# ---------------------------------------------------------------------------
# Test: notifications dispatched on_commit (not before)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_request_notification_is_wrapped_in_on_commit(
    service: ExternalEventChangeRequestService,
    event: CalendarEvent,
    admin_membership: OrganizationMembership,
    mock_notification_service: MagicMock,
) -> None:
    """The notification dispatch is wrapped in transaction.on_commit.

    We verify this by intercepting the raw ``on_commit`` call (before patching it
    to fire immediately) and confirming the callback is enqueued, not called directly.
    """
    on_commit_calls: list[Any] = []

    def _capture(fn: Any) -> None:
        on_commit_calls.append(fn)
        # Do NOT invoke fn — verify it is registered but not yet fired.

    with patch(f"{_MODULE}.transaction.on_commit", side_effect=_capture):
        service.create_or_supersede_update_request(
            event=event,
            proposed_values=_UPDATE_PROPOSED,
            retained_values=_UPDATE_RETAINED,
            payload={},
            provider=CalendarProvider.GOOGLE,
        )

    # Notification has NOT been called yet (still pending on_commit).
    mock_notification_service.create_notification.assert_not_called()

    # Exactly one on_commit callback was registered for the admin.
    assert len(on_commit_calls) == 1

    # Fire the callbacks manually now and verify the notification is called.
    for cb in on_commit_calls:
        cb()
    mock_notification_service.create_notification.assert_called_once()
