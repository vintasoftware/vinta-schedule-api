"""
Integration tests for GET /notifications/

Covers:
- Returns the authenticated user's IN_APP notifications with status in (SENT, READ).
- Excludes notifications with status PENDING_SEND, FAILED, CANCELLED.
- Excludes another user's notifications.
- Respects page / page_size query params.
- Returns 401 for unauthenticated requests.
- Response envelope: {results: [...], page: int, page_size: int, count: int}.
- count reflects the user's total SENT+READ notifications (not just the current page).
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


LIST_URL = "/notifications/"


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
class TestListEndpointAuth:
    def test_unauthenticated_returns_401(self) -> None:
        """Unauthenticated requests receive HTTP 401."""
        client = APIClient()
        response = client.get(LIST_URL)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_authenticated_returns_200(self, user) -> None:
        """Authenticated users receive HTTP 200."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestListEndpointContent:
    def test_returns_sent_and_read_notifications_for_user(self, user) -> None:
        """SENT and READ IN_APP notifications for the requesting user are returned."""
        service = _build_notification_service()

        # Create one SENT notification
        sent_notif = _send_in_app(service, user.id, "sent message")
        assert sent_notif.status == NotificationStatus.SENT.value

        # Create another and mark it READ
        read_notif = _send_in_app(service, user.id, "read message")
        service.mark_read(read_notif.id)
        read_notif.refresh_from_db()
        assert read_notif.status == NotificationStatus.READ.value

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 2
        assert len(data["results"]) == 2

        # Verify both SENT and READ are present
        statuses = {item["status"] for item in data["results"]}
        assert NotificationStatus.SENT.value in statuses
        assert NotificationStatus.READ.value in statuses

    def test_excludes_pending_send_notifications(self, user) -> None:
        """PENDING_SEND notifications are excluded from the list."""
        # Create a PENDING_SEND notification directly via ORM (bypassing the service)
        Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Pending notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "pending"},
            status=NotificationStatus.PENDING_SEND.value,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_excludes_failed_notifications(self, user) -> None:
        """FAILED notifications are excluded from the list."""
        Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Failed notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "failed"},
            status=NotificationStatus.FAILED.value,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_excludes_cancelled_notifications(self, user) -> None:
        """CANCELLED notifications are excluded from the list."""
        Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Cancelled notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "cancelled"},
            status=NotificationStatus.CANCELLED.value,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_excludes_other_users_notifications(self, user) -> None:
        """Notifications belonging to another user are never returned."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        # Send to other_user only
        _send_in_app(service, other_user.id, "other user message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_only_returns_requesting_users_notifications_when_both_have(self, user) -> None:
        """Mixed scenario: only the requesting user's SENT+READ items appear."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        _send_in_app(service, user.id, "my message")
        _send_in_app(service, other_user.id, "other message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 1
        assert len(data["results"]) == 1

    def test_returns_newest_first(self, user) -> None:
        """Notifications are ordered by creation date, newest first."""
        service = _build_notification_service()

        # Create three notifications in sequence
        notif1 = _send_in_app(service, user.id, "first")
        notif2 = _send_in_app(service, user.id, "second")
        notif3 = _send_in_app(service, user.id, "third")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 3

        # Verify newest-first order
        ids = [item["id"] for item in data["results"]]
        assert ids == [str(notif3.id), str(notif2.id), str(notif1.id)]

    def test_response_envelope_shape(self, user) -> None:
        """Response has the passthrough pagination envelope: results, page, page_size, count."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "count" in data
        assert "page" in data
        assert "page_size" in data
        assert "results" in data
        # No LimitOffsetPagination-style next/previous links
        assert "next" not in data
        assert "previous" not in data
        assert isinstance(data["results"], list)

    def test_result_item_shape(self, user) -> None:
        """Each result item has the expected fields."""
        service = _build_notification_service()
        _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

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

    def test_created_is_iso_string_for_model(self, user) -> None:
        """The created field is an ISO 8601 string (model-only field, present in list endpoint)."""
        service = _build_notification_service()
        _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        data = response.json()
        assert len(data["results"]) == 1
        item = data["results"][0]
        assert item["created"] is not None
        assert isinstance(item["created"], str)
        # Basic ISO format check (YYYY-MM-DDTHH:MM:SS...)
        assert "T" in item["created"]

    def test_body_renders_template_against_context(self, user) -> None:
        """The body field contains the rendered body_template output."""
        known_message = "hello from list template"
        service = _build_notification_service()
        _send_in_app(service, user.id, message=known_message)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 1
        assert known_message in data["results"][0]["body"]


@pytest.mark.django_db
class TestListEndpointPagination:
    def test_page_and_page_size_defaults(self, user) -> None:
        """Default page=1, page_size=10 when params are absent."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        data = response.json()
        assert data["count"] == 0
        assert data["page"] == 1
        assert data["page_size"] == 10

    def test_custom_page_size_limits_results(self, user) -> None:
        """page_size=1 returns at most 1 result even when multiple exist."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")
        _send_in_app(service, user.id, "msg3")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page": 1, "page_size": 1})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 3
        assert len(data["results"]) == 1
        assert data["page"] == 1
        assert data["page_size"] == 1

    def test_second_page_returns_next_items(self, user) -> None:
        """Page 2 with page_size=1 returns a different item than page 1."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")

        client = APIClient()
        client.force_authenticate(user=user)

        response_p1 = client.get(LIST_URL, {"page": 1, "page_size": 1})
        response_p2 = client.get(LIST_URL, {"page": 2, "page_size": 1})

        assert response_p1.status_code == status.HTTP_200_OK
        assert response_p2.status_code == status.HTTP_200_OK

        ids_p1 = [item["id"] for item in response_p1.json()["results"]]
        ids_p2 = [item["id"] for item in response_p2.json()["results"]]

        # Pages should not overlap
        assert set(ids_p1).isdisjoint(set(ids_p2))

    def test_page_beyond_results_returns_empty(self, user) -> None:
        """Requesting a page beyond available results returns an empty list (not 404)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page": 999, "page_size": 10})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["results"] == []

    def test_page_size_clamped_to_max(self, user) -> None:
        """page_size above the maximum is silently clamped to 100 (not a 400)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page_size": 100000})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["page_size"] == 100

    def test_invalid_page_returns_400(self, user) -> None:
        """Non-integer page param returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page": "abc"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_page_size_returns_400(self, user) -> None:
        """Non-integer page_size param returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page_size": "abc"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_zero_page_returns_400(self, user) -> None:
        """page=0 returns HTTP 400 (must be >= 1)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page": 0})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_zero_page_size_returns_400(self, user) -> None:
        """page_size=0 returns HTTP 400 (must be >= 1)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"page_size": 0})
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestListEndpointCount:
    def test_count_reflects_total_sent_and_read_for_user(self, user) -> None:
        """count reflects the user's total SENT+READ notifications across all pages."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")
        _send_in_app(service, user.id, "msg3")

        client = APIClient()
        client.force_authenticate(user=user)
        # Request page_size=1 — results is 1 item but count should be 3
        response = client.get(LIST_URL, {"page": 1, "page_size": 1})

        data = response.json()
        assert data["count"] == 3
        assert len(data["results"]) == 1

    def test_count_excludes_pending_send(self, user) -> None:
        """count does not include PENDING_SEND notifications."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "sent")

        Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Pending",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "pending"},
            status=NotificationStatus.PENDING_SEND.value,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        data = response.json()
        assert data["count"] == 1

    def test_count_is_per_user(self, user) -> None:
        """count only reflects the authenticated user's own notifications."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        _send_in_app(service, user.id, "mine")
        _send_in_app(service, other_user.id, "theirs1")
        _send_in_app(service, other_user.id, "theirs2")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        data = response.json()
        assert data["count"] == 1


@pytest.mark.django_db
class TestListEndpointMixedStatus:
    def test_excludes_pending_while_passing_sent_and_read(self, user) -> None:
        """PENDING_SEND rows are filtered out while SENT and READ rows pass through."""
        service = _build_notification_service()

        # SENT notification (via service — status becomes SENT after send)
        sent_notif = _send_in_app(service, user.id, "sent message")
        assert sent_notif.status == NotificationStatus.SENT.value

        # READ notification (sent then marked read)
        read_notif = _send_in_app(service, user.id, "read message")
        service.mark_read(read_notif.id)
        read_notif.refresh_from_db()
        assert read_notif.status == NotificationStatus.READ.value

        # PENDING_SEND notification (created directly via ORM to force status)
        pending_notif = Notification.objects.create(
            user=user,
            notification_type=NotificationTypes.IN_APP.value,
            title="Pending notification",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "pending"},
            status=NotificationStatus.PENDING_SEND.value,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Only SENT and READ should appear; PENDING_SEND must be excluded
        assert data["count"] == 2
        result_ids = [item["id"] for item in data["results"]]
        assert str(sent_notif.id) in result_ids
        assert str(read_notif.id) in result_ids
        assert str(pending_notif.id) not in result_ids

    def test_list_includes_read_while_unread_excludes_them(self, user) -> None:
        """List endpoint includes READ notifications; unread endpoint excludes them."""
        service = _build_notification_service()

        # Create one SENT and one READ
        _send_in_app(service, user.id, "sent")
        read = _send_in_app(service, user.id, "read")
        service.mark_read(read.id)

        client = APIClient()
        client.force_authenticate(user=user)

        # List should return both — page/page_size envelope
        list_response = client.get(LIST_URL)
        list_data = list_response.json()
        assert list_data["count"] == 2
        assert "page" in list_data
        assert "page_size" in list_data
        assert "next" not in list_data

        # Unread should return only SENT
        unread_response = client.get("/notifications/unread/")
        unread_data = unread_response.json()
        assert len(unread_data["results"]) == 1
        assert unread_data["results"][0]["status"] == NotificationStatus.SENT.value
