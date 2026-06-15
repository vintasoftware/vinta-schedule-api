"""
Integration tests for POST /notifications/{id}/mark-read/

Covers:
- Owner marks own SENT notification → 200, status becomes READ.
- Marking another user's notification → 404 (IDOR guard).
- Marking an already-READ notification → 200 idempotent.
- Marking a non-existent id → 404.
- Unauthenticated requests → 401.
- After marking as read, notification disappears from /notifications/unread/.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService
from vintasend_django.models import Notification
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)

from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)
from users.factories import UserFactory


def _build_notification_service() -> NotificationService:
    """Build a NotificationService with only the IN_APP adapter (mirrors DI wiring)."""
    return NotificationService(
        notification_adapters=[
            DjangoInAppNotificationAdapter(
                DjangoTemplatedInAppRenderer(),
                DjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=DjangoDbNotificationBackend(),
    )


def _send_in_app(service: NotificationService, user_id: int, message: str = "msg") -> Notification:
    """Create and send an IN_APP notification; returns the ORM model instance."""
    service.create_notification(
        user_id=user_id,
        notification_type=NotificationTypes.IN_APP.value,
        title="Test notification",
        body_template="notifications/in_app/example.body.txt",
        context_name="in_app_generic_context",
        context_kwargs=NotificationContextDict({"message": message}),
    )
    # Fetch the persisted model instance from the database
    return Notification.objects.filter(user_id=user_id).latest("created")


@pytest.mark.django_db
class TestMarkReadAuth:
    def test_unauthenticated_returns_401(self, user) -> None:
        """Unauthenticated requests receive HTTP 401."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id)

        client = APIClient()
        response = client.post(f"/notifications/{notification.id}/mark-read/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_authenticated_returns_200(self, user) -> None:
        """Authenticated users can mark their own notifications."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id)
        assert notification.status == NotificationStatus.SENT.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{notification.id}/mark-read/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestMarkReadOwnership:
    def test_owner_marks_own_sent_notification(self, user) -> None:
        """Owner can mark their own SENT notification as READ."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id, "my message")
        assert notification.status == NotificationStatus.SENT.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{notification.id}/mark-read/")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == NotificationStatus.READ.value

        # Verify the notification was actually updated in the DB
        notification.refresh_from_db()
        assert notification.status == NotificationStatus.READ.value

    def test_non_owner_cannot_mark_others_notification(self, user) -> None:
        """Non-owners get 404 when attempting to mark another user's notification."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()
        other_notification = _send_in_app(service, other_user.id, "other user message")
        assert other_notification.status == NotificationStatus.SENT.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{other_notification.id}/mark-read/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Verify the other user's notification was NOT modified
        other_notification.refresh_from_db()
        assert other_notification.status == NotificationStatus.SENT.value

    def test_marking_another_user_notification_returns_404_idor_guard(self, user) -> None:
        """Explicit IDOR test: accessing another user's notification returns 404."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        # Create SENT notification for other_user
        other_notification = _send_in_app(service, other_user.id, "other message")
        assert other_notification.status == NotificationStatus.SENT.value

        # Authenticate as user and try to mark other_notification as read
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{other_notification.id}/mark-read/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Verify other_notification is still SENT
        other_notification.refresh_from_db()
        assert other_notification.status == NotificationStatus.SENT.value


@pytest.mark.django_db
class TestMarkReadIdempotency:
    def test_marking_already_read_notification_returns_200_idempotent(self, user) -> None:
        """Marking an already-READ notification returns 200 idempotently."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id, "message")

        # Mark as read once
        service.mark_read(notification.id)
        notification.refresh_from_db()
        assert notification.status == NotificationStatus.READ.value

        # Mark again via the endpoint — should return 200 idempotently
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{notification.id}/mark-read/")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == NotificationStatus.READ.value

        # Verify status is still READ (unchanged)
        notification.refresh_from_db()
        assert notification.status == NotificationStatus.READ.value

    def test_marking_already_read_twice_via_endpoint_is_idempotent(self, user) -> None:
        """Calling mark-read twice on an already-read notification both return 200."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)

        # First call
        response1 = client.post(f"/notifications/{notification.id}/mark-read/")
        assert response1.status_code == status.HTTP_200_OK
        assert response1.json()["status"] == NotificationStatus.READ.value

        # Second call — should also return 200 (idempotent)
        response2 = client.post(f"/notifications/{notification.id}/mark-read/")
        assert response2.status_code == status.HTTP_200_OK
        assert response2.json()["status"] == NotificationStatus.READ.value


@pytest.mark.django_db
class TestMarkReadNotFound:
    def test_non_existent_id_returns_404(self, user) -> None:
        """Marking a non-existent notification returns 404."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post("/notifications/99999/mark-read/")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_pending_send_notification_returns_404(self, user) -> None:
        """PENDING_SEND is never marked READ by mark_read_bulk → empty result → 404."""
        # Create a PENDING_SEND notification directly via ORM
        pending_notif = Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Pending notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "pending"},
            status=NotificationStatus.PENDING_SEND.value,
        )
        assert pending_notif.status == NotificationStatus.PENDING_SEND.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{pending_notif.id}/mark-read/")

        # mark_read_bulk only marks SENT rows READ; PENDING_SEND stays out → 404
        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Verify the notification was NOT modified
        pending_notif.refresh_from_db()
        assert pending_notif.status == NotificationStatus.PENDING_SEND.value

    def test_failed_notification_returns_404(self, user) -> None:
        """FAILED is never marked READ by mark_read_bulk → empty result → 404."""
        failed_notif = Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Failed notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "failed"},
            status=NotificationStatus.FAILED.value,
        )
        assert failed_notif.status == NotificationStatus.FAILED.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{failed_notif.id}/mark-read/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Verify the notification was NOT modified
        failed_notif.refresh_from_db()
        assert failed_notif.status == NotificationStatus.FAILED.value

    def test_cancelled_notification_returns_404(self, user) -> None:
        """CANCELLED is never marked READ by mark_read_bulk → empty result → 404."""
        cancelled_notif = Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Cancelled notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "cancelled"},
            status=NotificationStatus.CANCELLED.value,
        )
        assert cancelled_notif.status == NotificationStatus.CANCELLED.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{cancelled_notif.id}/mark-read/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Verify the notification was NOT modified
        cancelled_notif.refresh_from_db()
        assert cancelled_notif.status == NotificationStatus.CANCELLED.value


@pytest.mark.django_db
class TestMarkReadResponseShape:
    def test_response_includes_all_notification_fields(self, user) -> None:
        """The 200 response includes all expected notification fields."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{notification.id}/mark-read/")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert set(data.keys()) == {
            "id",
            "title",
            "notification_type",
            "status",
            "body",
            "created",
            "modified",
        }

    def test_response_has_rendered_body(self, user) -> None:
        """The body field in the response is rendered."""
        service = _build_notification_service()
        known_message = "test message for mark-read"
        notification = _send_in_app(service, user.id, message=known_message)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{notification.id}/mark-read/")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert known_message in data["body"]


@pytest.mark.django_db
class TestMarkReadRemovesFromUnreadList:
    def test_marked_notification_disappears_from_unread_endpoint(self, user) -> None:
        """After marking a notification as read, it no longer appears in /notifications/unread/."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id, "unread message")

        client = APIClient()
        client.force_authenticate(user=user)

        # Initially, the notification should be in /notifications/unread/
        unread_response = client.get("/notifications/unread/")
        assert unread_response.status_code == status.HTTP_200_OK
        unread_data = unread_response.json()
        assert len(unread_data["results"]) == 1
        assert int(unread_data["results"][0]["id"]) == notification.id

        # Mark the notification as read
        mark_response = client.post(f"/notifications/{notification.id}/mark-read/")
        assert mark_response.status_code == status.HTTP_200_OK
        assert mark_response.json()["status"] == NotificationStatus.READ.value

        # Now it should NOT appear in /notifications/unread/
        unread_response_after = client.get("/notifications/unread/")
        assert unread_response_after.status_code == status.HTTP_200_OK
        unread_data_after = unread_response_after.json()
        assert len(unread_data_after["results"]) == 0

    def test_marked_notification_still_in_list_endpoint(self, user) -> None:
        """After marking a notification as read, it still appears in /notifications/ (list all)."""
        service = _build_notification_service()
        notification = _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)

        # Mark as read
        mark_response = client.post(f"/notifications/{notification.id}/mark-read/")
        assert mark_response.status_code == status.HTTP_200_OK

        # It should still be in the list endpoint (list-all returns both SENT and READ)
        list_response = client.get("/notifications/")
        assert list_response.status_code == status.HTTP_200_OK
        list_data = list_response.json()
        assert list_data["count"] == 1
        assert int(list_data["results"][0]["id"]) == notification.id
        assert list_data["results"][0]["status"] == NotificationStatus.READ.value


@pytest.mark.django_db
class TestMarkReadConcurrentRace:
    def test_already_read_out_of_band_returns_200(self, user) -> None:
        """Native idempotency under a concurrent winner: a row flipped to READ
        out-of-band (e.g. by a parallel request) still returns an idempotent 200.

        The endpoint delegates to mark_read_bulk([pk], user_id=...), which is
        idempotent by construction — it marks only SENT rows and returns the
        requested ids that are READ after the op. An already-READ owned id is
        returned, so there is no 0-row-update error and no 500 to guard against.
        """
        service = _build_notification_service()
        notification = _send_in_app(service, user.id, "concurrent message")
        assert notification.status == NotificationStatus.SENT.value

        # Simulate a concurrent winner having already transitioned the row to READ.
        Notification.objects.filter(pk=notification.pk).update(status=NotificationStatus.READ.value)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(f"/notifications/{notification.id}/mark-read/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == NotificationStatus.READ.value
