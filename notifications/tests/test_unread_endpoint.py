"""
Integration tests for GET /notifications/unread/

Covers:
- Returns only the authenticated user's unread (SENT) IN_APP notifications.
- Excludes notifications marked as READ.
- Excludes another user's notifications.
- Respects page / page_size query params.
- Returns 401 for unauthenticated requests.
- Response envelope: {results: [...], page: int, page_size: int}.
- Returns 400 for invalid page / page_size values.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import Notification as NotificationDataclass
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService

from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_backends import FixedDjangoDbNotificationBackend
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)
from users.factories import UserFactory


UNREAD_URL = "/notifications/unread/"


def _build_notification_service() -> NotificationService:
    """Build a NotificationService with only the IN_APP adapter (mirrors DI wiring)."""
    return NotificationService(
        notification_adapters=[
            DjangoInAppNotificationAdapter(
                DjangoTemplatedInAppRenderer(),
                FixedDjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=FixedDjangoDbNotificationBackend(),
    )


def _send_in_app(
    service: NotificationService, user_id: int, message: str = "msg"
) -> NotificationDataclass:
    """Create and send an IN_APP notification; returns the vintasend dataclass."""
    return service.create_notification(
        user_id=user_id,
        notification_type=NotificationTypes.IN_APP.value,
        title="Test notification",
        body_template="notifications/in_app/example.body.txt",
        context_name="in_app_generic_context",
        context_kwargs=NotificationContextDict({"message": message}),
    )


@pytest.mark.django_db
class TestUnreadEndpointAuth:
    def test_unauthenticated_returns_401(self) -> None:
        """Unauthenticated requests receive HTTP 401."""
        client = APIClient()
        response = client.get(UNREAD_URL)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_authenticated_returns_200(self, user) -> None:
        """Authenticated users receive HTTP 200."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestUnreadEndpointContent:
    def test_returns_only_sent_notifications_for_user(self, user) -> None:
        """Only SENT (unread) IN_APP notifications for the requesting user are returned."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "unread message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["status"] == NotificationStatus.SENT.value

    def test_excludes_read_notifications(self, user) -> None:
        """READ notifications are excluded from the unread list."""
        service = _build_notification_service()

        # Create one notification and mark it read
        notification_dc = _send_in_app(service, user.id, "read message")
        notification_id = notification_dc.id
        service.mark_read(notification_id)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["results"] == []

    def test_excludes_other_users_notifications(self, user) -> None:
        """Notifications belonging to another user are never returned."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        # Send to other_user only
        _send_in_app(service, other_user.id, "other user message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["results"] == []

    def test_only_returns_requesting_users_notifications_when_both_have_unread(self, user) -> None:
        """Mixed scenario: only the requesting user's unread items appear."""
        other_user = UserFactory().create_user()
        service = _build_notification_service()

        _send_in_app(service, user.id, "my message")
        _send_in_app(service, other_user.id, "other message")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["title"] == "Test notification"

    def test_response_envelope_shape(self, user) -> None:
        """Response has the passthrough pagination envelope: results, page, page_size."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "results" in data
        assert "page" in data
        assert "page_size" in data
        assert isinstance(data["results"], list)

    def test_result_item_shape(self, user) -> None:
        """Each result item has the expected fields."""
        service = _build_notification_service()
        _send_in_app(service, user.id)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

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

    def test_body_renders_template_against_context(self, user) -> None:
        """The body field contains the rendered body_template output with the stored context.

        Sends a notification with message='hello from template' and asserts that exact
        string appears in results[0]['body'], proving end-to-end template rendering via
        notifications/in_app/example.body.txt which renders {{ message }}.
        """
        known_message = "hello from template"
        service = _build_notification_service()
        _send_in_app(service, user.id, message=known_message)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 1
        assert known_message in data["results"][0]["body"]


@pytest.mark.django_db
class TestUnreadEndpointPagination:
    def test_page_and_page_size_defaults(self, user) -> None:
        """Default page=1, page_size=10 when params are absent."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL)

        data = response.json()
        assert data["page"] == 1
        assert data["page_size"] == 10

    def test_custom_page_and_page_size_reflected_in_envelope(self, user) -> None:
        """Provided page / page_size are echoed back in the envelope."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page": 2, "page_size": 5})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["page"] == 2
        assert data["page_size"] == 5

    def test_page_size_limits_results(self, user) -> None:
        """page_size=1 returns at most 1 result even when multiple exist."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")
        _send_in_app(service, user.id, "msg3")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page": 1, "page_size": 1})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["results"]) == 1

    def test_second_page_returns_next_items(self, user) -> None:
        """Page 2 with page_size=1 returns a different item than page 1."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")

        client = APIClient()
        client.force_authenticate(user=user)

        response_p1 = client.get(UNREAD_URL, {"page": 1, "page_size": 1})
        response_p2 = client.get(UNREAD_URL, {"page": 2, "page_size": 1})

        assert response_p1.status_code == status.HTTP_200_OK
        assert response_p2.status_code == status.HTTP_200_OK

        ids_p1 = [item["id"] for item in response_p1.json()["results"]]
        ids_p2 = [item["id"] for item in response_p2.json()["results"]]

        # Pages should not overlap
        assert set(ids_p1).isdisjoint(set(ids_p2))

    def test_empty_page_beyond_results(self, user) -> None:
        """Requesting a page beyond available results returns an empty list (not 404)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page": 999, "page_size": 10})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["results"] == []


@pytest.mark.django_db
class TestUnreadEndpointValidation:
    def test_invalid_page_returns_400(self, user) -> None:
        """Non-integer page param returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page": "abc"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_page_size_returns_400(self, user) -> None:
        """Non-integer page_size param returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page_size": "abc"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_zero_page_returns_400(self, user) -> None:
        """page=0 returns HTTP 400 (must be >= 1)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page": 0})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_zero_page_size_returns_400(self, user) -> None:
        """page_size=0 returns HTTP 400 (must be >= 1)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page_size": 0})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_negative_page_returns_400(self, user) -> None:
        """Negative page returns HTTP 400."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page": -1})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_page_size_clamped_to_max(self, user) -> None:
        """page_size above the maximum is silently clamped to 100 (not a 400)."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(UNREAD_URL, {"page_size": 100000})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["page_size"] == 100
