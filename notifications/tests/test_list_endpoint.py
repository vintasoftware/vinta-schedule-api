"""
Integration tests for GET /notifications/

Covers:
- Returns the authenticated user's IN_APP notifications with status in (SENT, READ).
- Excludes notifications with status PENDING_SEND, FAILED, CANCELLED.
- Excludes another user's notifications.
- Respects limit / offset query params.
- Returns 401 for unauthenticated requests.
- Response envelope: standard LimitOffsetPagination {count, next, previous, results}.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService
from vintasend_django.models import Notification

from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_backends import FixedDjangoDbNotificationBackend
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
                FixedDjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=FixedDjangoDbNotificationBackend(),
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
        """Response has the LimitOffsetPagination envelope: count, next, previous, results."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "count" in data
        assert "next" in data
        assert "previous" in data
        assert "results" in data
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
    def test_default_limit_and_offset(self, user) -> None:
        """Default limit=10, offset=0 when params are absent."""
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL)

        data = response.json()
        assert data["count"] == 0
        assert data["next"] is None
        assert data["previous"] is None

    def test_custom_limit_applied(self, user) -> None:
        """Custom limit parameter restricts the results per page."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")
        _send_in_app(service, user.id, "msg3")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"limit": 1})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 3
        assert len(data["results"]) == 1
        assert data["next"] is not None
        assert data["previous"] is None

    def test_custom_offset_applied(self, user) -> None:
        """Custom offset parameter skips the first N results."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        notif2 = _send_in_app(service, user.id, "msg2")
        notif3 = _send_in_app(service, user.id, "msg3")

        client = APIClient()
        client.force_authenticate(user=user)

        # Get first page (newest first = notif3, notif2, notif1)
        response = client.get(LIST_URL, {"limit": 1, "offset": 0})
        data = response.json()
        assert len(data["results"]) == 1
        assert int(data["results"][0]["id"]) == notif3.id

        # Get second page (skip 1, get next 1)
        response = client.get(LIST_URL, {"limit": 1, "offset": 1})
        data = response.json()
        assert len(data["results"]) == 1
        assert int(data["results"][0]["id"]) == notif2.id

    def test_offset_beyond_count_returns_empty(self, user) -> None:
        """Offset beyond the total count returns an empty results list."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"offset": 100})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 1
        assert data["results"] == []
        assert data["next"] is None

    def test_next_link_present_when_more_results(self, user) -> None:
        """The 'next' link is present when there are more results to fetch."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"limit": 1})

        data = response.json()
        assert data["next"] is not None
        assert "limit=1" in data["next"]
        assert "offset=1" in data["next"]

    def test_previous_link_present_when_offset(self, user) -> None:
        """The 'previous' link is present when offset > 0."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"limit": 1, "offset": 1})

        data = response.json()
        assert data["previous"] is not None
        # The previous link should point back to offset=0 or no offset param (defaults to 0)
        assert "limit=1" in data["previous"]


@pytest.mark.django_db
class TestListEndpointSchemaCompliance:
    def test_count_reflects_total_matching_rows(self, user) -> None:
        """The count field reflects the total number of matching rows (across all pages)."""
        service = _build_notification_service()
        _send_in_app(service, user.id, "msg1")
        _send_in_app(service, user.id, "msg2")
        _send_in_app(service, user.id, "msg3")

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(LIST_URL, {"limit": 1})

        data = response.json()
        assert data["count"] == 3

    def test_differentiate_from_unread_endpoint(self, user) -> None:
        """The list endpoint differs from unread in pagination style and content.

        - List uses limit/offset (LimitOffsetPagination)
        - Unread uses page/page_size (passthrough pagination)
        - List includes READ + SENT; unread is SENT only
        """
        service = _build_notification_service()

        # Create one SENT and one READ
        _send_in_app(service, user.id, "sent")
        read = _send_in_app(service, user.id, "read")
        service.mark_read(read.id)

        client = APIClient()
        client.force_authenticate(user=user)

        # List should return both — LimitOffsetPagination envelope
        list_response = client.get(LIST_URL)
        list_data = list_response.json()
        assert list_data["count"] == 2
        assert "count" in list_data
        assert "next" in list_data
        assert "page" not in list_data
        assert "page_size" not in list_data

        # Unread should return only SENT
        unread_response = client.get("/notifications/unread/")
        unread_data = unread_response.json()
        assert len(unread_data["results"]) == 1
        assert unread_data["results"][0]["status"] == NotificationStatus.SENT.value


@pytest.mark.django_db
class TestListEndpointMixedStatus:
    def test_excludes_pending_while_passing_sent_and_read(self, user) -> None:
        """PENDING_SEND rows are filtered out while SENT and READ rows pass through in the same request."""
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
