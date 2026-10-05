"""Unit tests for ``RoomSyncNotifier``.

Three groups:

- **Recipients.** The three room-level notices go to every active membership
  holding ``MANAGE_MEMBERS`` and to nobody else: a plain member and an inactive
  admin get nothing. The organizer notice goes to the one user the caller named.
  The ``NotificationService`` is a ``MagicMock`` here, so the assertions are on
  the full set of ``create_notification`` calls.
- **Transaction boundary.** Every send is queued with ``transaction.on_commit``:
  nothing is sent before the commit, and nothing is sent when the enclosing
  atomic block rolls back.
- **Rendering.** Each of the four contexts renders its subject, pre-header and
  body through the real email adapter into ``django.core.mail.outbox``.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock

from django.core import mail
from django.db import transaction

import pytest
from vintasend.exceptions import NotificationContextGenerationError
from vintasend.services.notification_service import NotificationService
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, CalendarEvent
from calendar_integration.notification_contexts import (
    booking_room_changed_context,
    room_edit_discarded_context,
    room_sync_failed_context,
)
from calendar_integration.services.room_sync_notifier import (
    BookingRoomChange,
    RoomSyncNotifier,
    RoomSyncOperation,
)
from common.organization_context import organization_context
from notifications.notification_adapters.django_email import (
    ReplyToDjangoEmailNotificationAdapter,
)
from organizations.models import Organization, OrganizationMembership
from organizations.tests.helpers import make_admin_membership, make_membership
from users.factories import UserFactory
from users.models import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Sync Notifier Org")


@pytest.fixture
def other_organization(db: Any) -> Organization:
    return Organization.objects.create(name="Some Other Org")


@pytest.fixture
def room(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Boardroom 4",
        external_id="room-boardroom-4",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
        organization=organization,
    )


@pytest.fixture
def event(room: Calendar, organization: Organization) -> CalendarEvent:
    return CalendarEvent.objects.create(
        calendar=room,
        title="Quarterly planning",
        description="private notes",
        start_time_tz_unaware=datetime.datetime(2026, 11, 1, 12, 0),
        end_time_tz_unaware=datetime.datetime(2026, 11, 1, 13, 0),
        timezone="America/Sao_Paulo",
        organization=organization,
    )


def _user(email: str) -> User:
    return UserFactory().create_user(email=email)


@pytest.fixture
def admin_a(organization: Organization) -> OrganizationMembership:
    return make_admin_membership(
        user=_user("admin-a@example.com"), organization=organization, is_active=True
    )


@pytest.fixture
def admin_b(organization: Organization) -> OrganizationMembership:
    return make_admin_membership(
        user=_user("admin-b@example.com"), organization=organization, is_active=True
    )


@pytest.fixture
def plain_member(organization: Organization) -> OrganizationMembership:
    return make_membership(
        user=_user("member@example.com"), organization=organization, is_active=True
    )


@pytest.fixture
def inactive_admin(organization: Organization) -> OrganizationMembership:
    return make_admin_membership(
        user=_user("inactive-admin@example.com"), organization=organization, is_active=False
    )


@pytest.fixture
def other_org_admin(other_organization: Organization) -> OrganizationMembership:
    return make_admin_membership(
        user=_user("other-org-admin@example.com"),
        organization=other_organization,
        is_active=True,
    )


@pytest.fixture
def organizer(db: Any) -> User:
    return _user("organizer@example.com")


@pytest.fixture
def mock_notification_service() -> MagicMock:
    return MagicMock(spec=NotificationService)


@pytest.fixture
def notifier(mock_notification_service: MagicMock) -> RoomSyncNotifier:
    return RoomSyncNotifier(notification_service=mock_notification_service)


@pytest.fixture
def bound(organization: Organization) -> Iterator[None]:
    """Bind the organization the way every sync task does before reading a room."""
    with organization_context(organization):
        yield


def _calls_by_user(mock_notification_service: MagicMock) -> dict[int, dict[str, Any]]:
    return {
        call.kwargs["user_id"]: call.kwargs
        for call in mock_notification_service.create_notification.call_args_list
    }


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "plain_member", "inactive_admin", "other_org_admin")
def test_sync_failed_emails_every_active_admin_and_nobody_else(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    organization: Organization,
    admin_a: OrganizationMembership,
    admin_b: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        notifier.notify_sync_failed(
            room.id, RoomSyncOperation.CREATE, "The provider rejected the request."
        )

    calls = _calls_by_user(mock_notification_service)
    assert set(calls) == {admin_a.user_id, admin_b.user_id}
    for kwargs in calls.values():
        assert kwargs["notification_type"] == "EMAIL"
        assert kwargs["title"] == "Room sync failed"
        assert kwargs["body_template"] == "calendar_integration/emails/room_sync_failed.body.html"
        assert (
            kwargs["subject_template"] == "calendar_integration/emails/room_sync_failed.subject.txt"
        )
        assert (
            kwargs["preheader_template"]
            == "calendar_integration/emails/room_sync_failed.pre_header.txt"
        )
        assert kwargs["context_name"] == "room_sync_failed_context"
        assert dict(kwargs["context_kwargs"]) == {
            "room_id": room.id,
            "room_name": "Boardroom 4",
            "operation": "create",
            "reason": "The provider rejected the request.",
            "organization_id": organization.id,
        }


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "plain_member", "inactive_admin")
def test_edit_discarded_emails_admins_with_the_dropped_fields(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    organization: Organization,
    admin_a: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        notifier.notify_edit_discarded(room.id, ("name", "capacity"))

    calls = _calls_by_user(mock_notification_service)
    assert set(calls) == {admin_a.user_id}
    kwargs = calls[admin_a.user_id]
    assert kwargs["notification_type"] == "EMAIL"
    assert kwargs["title"] == "Room edit discarded"
    assert kwargs["body_template"] == "calendar_integration/emails/room_edit_discarded.body.html"
    assert kwargs["context_name"] == "room_edit_discarded_context"
    assert dict(kwargs["context_kwargs"]) == {
        "room_id": room.id,
        "room_name": "Boardroom 4",
        "fields": [{"name": "name"}, {"name": "capacity"}],
        "organization_id": organization.id,
    }


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "plain_member", "inactive_admin")
def test_bookings_flagged_emails_admins_with_the_count(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    organization: Organization,
    admin_a: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        notifier.notify_bookings_flagged(room.id, 3)

    calls = _calls_by_user(mock_notification_service)
    assert set(calls) == {admin_a.user_id}
    kwargs = calls[admin_a.user_id]
    assert kwargs["notification_type"] == "EMAIL"
    assert kwargs["title"] == "Room bookings need attention"
    assert kwargs["body_template"] == "calendar_integration/emails/room_bookings_flagged.body.html"
    assert kwargs["context_name"] == "room_bookings_flagged_context"
    assert dict(kwargs["context_kwargs"]) == {
        "room_id": room.id,
        "room_name": "Boardroom 4",
        "count": 3,
        "organization_id": organization.id,
    }


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "admin_a", "plain_member")
def test_booking_room_changed_emails_only_the_organizer(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    event: CalendarEvent,
    organization: Organization,
    organizer: User,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        notifier.notify_booking_room_changed(event.id, organizer.id, BookingRoomChange.MOVED)

    calls = _calls_by_user(mock_notification_service)
    assert set(calls) == {organizer.id}
    kwargs = calls[organizer.id]
    assert kwargs["notification_type"] == "EMAIL"
    assert kwargs["title"] == "Your room booking changed"
    assert kwargs["body_template"] == "calendar_integration/emails/booking_room_changed.body.html"
    assert kwargs["context_name"] == "booking_room_changed_context"
    # The naive stored value is the wall-clock time in the event's own timezone,
    # so it renders unchanged with that timezone named.
    assert dict(kwargs["context_kwargs"]) == {
        "event_id": event.id,
        "event_title": "Quarterly planning",
        "event_start": "2026-11-01 12:00 (America/Sao_Paulo)",
        "change": "moved",
        "organization_id": organization.id,
    }


@pytest.mark.django_db
@pytest.mark.usefixtures("bound")
def test_no_admins_means_no_emails(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    plain_member: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        notifier.notify_bookings_flagged(room.id, 1)

    mock_notification_service.create_notification.assert_not_called()


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "admin_a")
def test_empty_discarded_fields_are_rejected_before_anything_is_queued(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(ValueError, match="at least one discarded field"):
            notifier.notify_edit_discarded(room.id, [])

    assert callbacks == []
    mock_notification_service.create_notification.assert_not_called()


# ---------------------------------------------------------------------------
# Transaction boundary
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "admin_a")
def test_sends_wait_for_the_commit(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        notifier.notify_sync_failed(room.id, RoomSyncOperation.UPDATE, "Timed out.")
        # Queued, not sent: the captured callbacks run when the block exits.
        mock_notification_service.create_notification.assert_not_called()

    assert len(callbacks) == 1
    mock_notification_service.create_notification.assert_called_once()


@pytest.mark.django_db
@pytest.mark.usefixtures("bound", "admin_a")
def test_nothing_is_sent_when_the_transaction_rolls_back(
    notifier: RoomSyncNotifier,
    mock_notification_service: MagicMock,
    room: Calendar,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with transaction.atomic():
            notifier.notify_sync_failed(room.id, RoomSyncOperation.DELETE, "Timed out.")
            transaction.set_rollback(True)

    assert callbacks == []
    mock_notification_service.create_notification.assert_not_called()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _email_notification_service() -> NotificationService:
    """The email half of the DI wiring, so sends land in ``django.core.mail.outbox``."""
    return NotificationService(
        notification_adapters=[
            ReplyToDjangoEmailNotificationAdapter(
                DjangoTemplatedEmailRenderer(),
                DjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=DjangoDbNotificationBackend(),
    )


@pytest.fixture
def real_notifier() -> RoomSyncNotifier:
    return RoomSyncNotifier(notification_service=_email_notification_service())


@pytest.mark.django_db
@pytest.mark.usefixtures("bound")
def test_room_sync_failed_renders_subject_and_body(
    real_notifier: RoomSyncNotifier,
    room: Calendar,
    admin_a: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        real_notifier.notify_sync_failed(
            room.id, RoomSyncOperation.UPDATE, "The provider rejected the request."
        )

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [admin_a.user.email]
    assert sent.subject == 'Action needed: room "Boardroom 4" could not be synced'
    assert "A room could not be synced" in sent.body
    assert f'"Boardroom 4" (id {room.id})' in sent.body
    assert '"Update" change' in sent.body
    assert "Reason: The provider rejected the request." in sent.body


@pytest.mark.django_db
@pytest.mark.usefixtures("bound")
def test_room_edit_discarded_renders_subject_and_body(
    real_notifier: RoomSyncNotifier,
    room: Calendar,
    admin_a: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        real_notifier.notify_edit_discarded(room.id, ["name", "capacity"])

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [admin_a.user.email]
    assert sent.subject == 'Some edits to room "Boardroom 4" were not applied'
    assert "Some room edits were not applied" in sent.body
    assert "<li>name</li>" in sent.body
    assert "<li>capacity</li>" in sent.body


@pytest.mark.django_db
@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize(
    ("count", "expected_line"),
    [
        (1, "1 future booking still references this room and has been flagged."),
        (3, "3 future bookings still reference this room and have been flagged."),
    ],
)
def test_room_bookings_flagged_renders_subject_and_body(
    real_notifier: RoomSyncNotifier,
    room: Calendar,
    admin_a: OrganizationMembership,
    django_capture_on_commit_callbacks: Callable[..., Any],
    count: int,
    expected_line: str,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        real_notifier.notify_bookings_flagged(room.id, count)

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [admin_a.user.email]
    assert sent.subject == 'Action needed: bookings for room "Boardroom 4" need attention'
    assert "A room was deleted and its bookings need attention" in sent.body
    assert expected_line in sent.body


@pytest.mark.django_db
@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize(
    ("change", "expected_subject", "expected_heading"),
    [
        (
            BookingRoomChange.MOVED,
            'Your booking "Quarterly planning" was moved to another room',
            "Your booking was moved to another room",
        ),
        (
            BookingRoomChange.ROOM_REMOVED,
            'Your booking "Quarterly planning" no longer has a room',
            "Your booking no longer has a room",
        ),
        (
            BookingRoomChange.EVENT_CANCELLED,
            'Your booking "Quarterly planning" was cancelled',
            "Your booking was cancelled",
        ),
    ],
)
def test_booking_room_changed_renders_subject_and_body(
    real_notifier: RoomSyncNotifier,
    event: CalendarEvent,
    organizer: User,
    django_capture_on_commit_callbacks: Callable[..., Any],
    change: BookingRoomChange,
    expected_subject: str,
    expected_heading: str,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        real_notifier.notify_booking_room_changed(event.id, organizer.id, change)

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [organizer.email]
    assert sent.subject == expected_subject
    assert expected_heading in sent.body
    assert (
        'The room you booked for "Quarterly planning" on 2026-11-01 12:00 (America/Sao_Paulo)'
        in sent.body
    )
    # The organizer's own event is named; nothing else about it leaks, and no
    # actor is named: the change may come from an admin, a partner or Vinta ops.
    assert "private notes" not in sent.body
    assert "admin" not in sent.body


# ---------------------------------------------------------------------------
# Context validation
# ---------------------------------------------------------------------------


def test_room_sync_failed_context_rejects_unknown_operation() -> None:
    with pytest.raises(NotificationContextGenerationError):
        room_sync_failed_context(
            room_id=1, room_name="x", operation="rename", reason="r", organization_id=1
        )


def test_room_edit_discarded_context_flattens_field_names_for_the_template() -> None:
    context = room_edit_discarded_context(
        room_id=1,
        room_name="x",
        fields=[{"name": "name"}, {"name": "capacity"}],
        organization_id=1,
    )

    assert context == {
        "room_id": 1,
        "room_name": "x",
        "fields": ["name", "capacity"],
        "organization_id": 1,
    }


def test_booking_room_changed_context_rejects_unknown_change() -> None:
    with pytest.raises(NotificationContextGenerationError):
        booking_room_changed_context(
            event_id=1, event_title="x", event_start="s", change="teleported", organization_id=1
        )
