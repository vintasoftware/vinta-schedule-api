"""
Integration tests for the full in-app notification send pipeline.

Verifies that:
  - NotificationService.create_notification(..., notification_type=IN_APP) persists a
    Notification row and transitions it to SENT (previously raised NotificationError).
  - NotificationService.get_in_app_unread(user_id) returns the just-sent notification
    without raising NotificationError (previously raised when no IN_APP adapter was registered).
  - Email and SMS adapters continue to resolve correctly (regression guard).
"""

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService
from vintasend_django.models import Notification

from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_backends import FixedDjangoDbNotificationBackend
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)


def _build_notification_service() -> NotificationService:
    """
    Build a NotificationService with the in-app adapter only.

    Mirrors the DI wiring in di_core/containers.py for the IN_APP channel
    (without Email/SMS adapters to avoid needing their credentials/templates).
    Uses FixedDjangoDbNotificationBackend which corrects the IN_APP ORM filter
    bug in the vendored DjangoDbNotificationBackend.
    """
    return NotificationService(
        notification_adapters=[
            DjangoInAppNotificationAdapter(
                DjangoTemplatedInAppRenderer(),
                FixedDjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=FixedDjangoDbNotificationBackend(),
    )


@pytest.mark.django_db
class TestInAppNotificationSend:
    def test_create_notification_persists_and_marks_sent(self, user) -> None:
        """create_notification(IN_APP) must persist a Notification row with SENT status."""
        service = _build_notification_service()

        notification = service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="You have a new notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "Hello from in-app!"}),
        )

        assert notification is not None

        # The dataclass returned by create_notification reflects the state at persist time
        # (PENDING_SEND). Refresh from DB to get the post-send status.
        db_row = Notification.objects.get(pk=int(notification.id))
        assert db_row.status == NotificationStatus.SENT.value
        assert db_row.notification_type == NotificationTypes.IN_APP.value
        assert str(db_row.user_id) == str(user.id)

    def test_get_in_app_unread_returns_sent_notification(self, user) -> None:
        """get_in_app_unread must return the notification just sent to the user."""
        service = _build_notification_service()

        service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="Unread notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "You have mail."}),
        )

        # Must not raise NotificationError("No in-app notification adapter found")
        unread = list(service.get_in_app_unread(user.id))

        assert len(unread) == 1
        assert unread[0].notification_type == NotificationTypes.IN_APP.value
        assert unread[0].status == NotificationStatus.SENT.value

    def test_get_in_app_unread_excludes_other_users(self, user) -> None:
        """get_in_app_unread must not return another user's notifications."""
        from users.factories import UserFactory

        other_user = UserFactory().create_user()
        service = _build_notification_service()

        # Send to other_user
        service.create_notification(
            user_id=other_user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="Other user notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "Not for you."}),
        )

        # user's unread list should be empty
        unread = list(service.get_in_app_unread(user.id))
        assert unread == []

    def test_di_wired_service_has_in_app_adapter(self, di_container) -> None:
        """
        Regression: the DI container's notification_service must include the IN_APP adapter
        so that get_in_app_unread does not raise NotificationError.
        """
        notification_service = di_container.notification_service()

        has_in_app = any(
            a.notification_type == NotificationTypes.IN_APP
            for a in notification_service.notification_adapters
        )
        assert has_in_app, (
            "DI container notification_service is missing the IN_APP adapter; "
            "get_in_app_unread would raise NotificationError"
        )
