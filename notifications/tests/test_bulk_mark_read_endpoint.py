"""
Integration tests for POST /notifications/mark-read-bulk/ (bulk mark-as-read)

Covers:
- Two own SENT notifications → 200, both become READ, gone from /notifications/unread/.
- Mix of own SENT + own already-READ ids → 200 idempotent, all returned READ.
- Another user's id in the list → that row stays SENT, absent from results (IDOR guard).
- Empty / missing ids → 400.
- Non-existent ids → silently skipped, 200.
- Unauthenticated → 401.
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


BULK_MARK_READ_URL = "/notifications/mark-read-bulk/"
UNREAD_URL = "/notifications/unread/"


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
    return Notification.objects.filter(user_id=user_id).latest("created")


@pytest.mark.django_db
class TestBulkMarkReadAuth:
    def test_unauthenticated_returns_401(self) -> None:
        """Unauthenticated requests receive HTTP 401."""
        client = APIClient()
        response = client.post(BULK_MARK_READ_URL, {"ids": [1]}, format="json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_authenticated_returns_200(self, user) -> None:
        """Authenticated users receive HTTP 200."""
        service = _build_notification_service()
        notif = _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [notif.id]}, format="json")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBulkMarkReadTwoSent:
    def test_two_own_sent_notifications_marked_read(self, user) -> None:
        """POSTing two own SENT ids → 200, both become READ, both returned in results."""
        service = _build_notification_service()
        notif1 = _send_in_app(service, user.id, "first")
        notif2 = _send_in_app(service, user.id, "second")

        assert notif1.status == NotificationStatus.SENT.value
        assert notif2.status == NotificationStatus.SENT.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [notif1.id, notif2.id]}, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "results" in data
        result_ids = {int(item["id"]) for item in data["results"]}
        assert notif1.id in result_ids
        assert notif2.id in result_ids

        # Verify both are now READ in the DB
        notif1.refresh_from_db()
        notif2.refresh_from_db()
        assert notif1.status == NotificationStatus.READ.value
        assert notif2.status == NotificationStatus.READ.value

    def test_both_marked_notifications_gone_from_unread(self, user) -> None:
        """After bulk mark-read, both notifications disappear from /notifications/unread/."""
        service = _build_notification_service()
        notif1 = _send_in_app(service, user.id, "first")
        notif2 = _send_in_app(service, user.id, "second")

        client = APIClient()
        client.force_authenticate(user=user)

        # Verify both are initially unread
        unread_before = client.get(UNREAD_URL)
        assert len(unread_before.json()["results"]) == 2

        # Bulk mark both as read
        response = client.post(BULK_MARK_READ_URL, {"ids": [notif1.id, notif2.id]}, format="json")
        assert response.status_code == status.HTTP_200_OK

        # Both should be gone from unread
        unread_after = client.get(UNREAD_URL)
        assert unread_after.json()["results"] == []


@pytest.mark.django_db
class TestBulkMarkReadIdempotency:
    def test_mix_of_sent_and_already_read_is_idempotent(self, user) -> None:
        """Mix of own SENT + own already-READ ids → 200 idempotent, all returned READ."""
        service = _build_notification_service()
        notif_sent = _send_in_app(service, user.id, "still sent")
        notif_read = _send_in_app(service, user.id, "already read")

        # Mark notif_read as READ before the bulk call
        service.mark_read(notif_read.id)
        notif_read.refresh_from_db()
        assert notif_read.status == NotificationStatus.READ.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            BULK_MARK_READ_URL,
            {"ids": [notif_sent.id, notif_read.id]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        result_ids = {int(item["id"]) for item in data["results"]}
        assert notif_sent.id in result_ids
        assert notif_read.id in result_ids

        # Verify all returned items have READ status
        for item in data["results"]:
            assert item["status"] == NotificationStatus.READ.value

        # Verify DB state
        notif_sent.refresh_from_db()
        assert notif_sent.status == NotificationStatus.READ.value

    def test_calling_bulk_twice_is_idempotent(self, user) -> None:
        """Calling bulk mark-read twice on the same ids returns 200 both times."""
        service = _build_notification_service()
        notif = _send_in_app(service, user.id, "message")

        client = APIClient()
        client.force_authenticate(user=user)

        # First call
        resp1 = client.post(BULK_MARK_READ_URL, {"ids": [notif.id]}, format="json")
        assert resp1.status_code == status.HTTP_200_OK
        assert resp1.json()["results"][0]["status"] == NotificationStatus.READ.value

        # Second call — should also succeed idempotently
        resp2 = client.post(BULK_MARK_READ_URL, {"ids": [notif.id]}, format="json")
        assert resp2.status_code == status.HTTP_200_OK
        assert resp2.json()["results"][0]["status"] == NotificationStatus.READ.value


@pytest.mark.django_db
class TestBulkMarkReadOwnershipScope:
    def test_another_users_id_stays_sent_and_absent_from_results(self, user) -> None:
        """Another user's id in the list → that row stays SENT, absent from results (IDOR guard)."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        other_notif = _send_in_app(service, other_user.id, "other user message")
        assert other_notif.status == NotificationStatus.SENT.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [other_notif.id]}, format="json")

        # Should return 200 (not 403 or 404) — ownership scoping silently skips foreign ids
        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # other_notif should NOT appear in results
        result_ids = {int(item["id"]) for item in data["results"]}
        assert other_notif.id not in result_ids

        # other_notif should still be SENT in the DB (not marked READ)
        other_notif.refresh_from_db()
        assert other_notif.status == NotificationStatus.SENT.value

    def test_mix_of_own_and_foreign_ids(self, user) -> None:
        """Mix of own + foreign ids → own marked READ, foreign skipped, 200."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        own_notif = _send_in_app(service, user.id, "mine")
        other_notif = _send_in_app(service, other_user.id, "theirs")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            BULK_MARK_READ_URL, {"ids": [own_notif.id, other_notif.id]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Only own_notif should appear in results
        result_ids = {int(item["id"]) for item in data["results"]}
        assert own_notif.id in result_ids
        assert other_notif.id not in result_ids

        # Verify DB state
        own_notif.refresh_from_db()
        other_notif.refresh_from_db()
        assert own_notif.status == NotificationStatus.READ.value
        assert other_notif.status == NotificationStatus.SENT.value


@pytest.mark.django_db
class TestBulkMarkReadValidation:
    def test_empty_ids_list_returns_400(self, user) -> None:
        """Empty ids list returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": []}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_missing_ids_field_returns_400(self, user) -> None:
        """Missing ids field returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_ids_as_non_list_returns_400(self, user) -> None:
        """ids field as a non-list (string) returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": "not-a-list"}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_ids_with_non_integer_values_returns_400(self, user) -> None:
        """ids list containing non-integer values returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": ["abc", "def"]}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_ids_list_exceeding_max_length_returns_400(self, user) -> None:
        """ids list with 101 entries exceeds max_length=100 and returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": list(range(1, 102))}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestBulkMarkReadNonExistentIds:
    def test_nonexistent_ids_silently_skipped_returns_200(self, user) -> None:
        """Non-existent notification ids are silently skipped; endpoint returns 200."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [999999, 888888]}, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["results"] == []

    def test_mix_of_valid_and_nonexistent_ids(self, user) -> None:
        """Mix of own valid ids + non-existent ids → valid marked READ, non-existent skipped."""
        service = _build_notification_service()
        notif = _send_in_app(service, user.id, "valid notification")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [notif.id, 999999]}, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Only the valid notification appears in results
        result_ids = {int(item["id"]) for item in data["results"]}
        assert notif.id in result_ids

        # Verify the valid notification is now READ
        notif.refresh_from_db()
        assert notif.status == NotificationStatus.READ.value


@pytest.mark.django_db
class TestBulkMarkReadResponseShape:
    def test_response_envelope_has_results_key(self, user) -> None:
        """The response envelope contains a 'results' key."""
        service = _build_notification_service()
        notif = _send_in_app(service, user.id, "message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [notif.id]}, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_result_item_has_notification_fields(self, user) -> None:
        """Each item in results has the expected notification serializer fields."""
        service = _build_notification_service()
        notif = _send_in_app(service, user.id, "message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(BULK_MARK_READ_URL, {"ids": [notif.id]}, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 1
        item = data["results"][0]
        assert set(item.keys()) == {
            "id",
            "title",
            "notification_type",
            "status",
            "body",
            "created",
            "modified",
        }
        assert item["status"] == NotificationStatus.READ.value
        assert item["created"] is not None
        assert item["modified"] is not None
