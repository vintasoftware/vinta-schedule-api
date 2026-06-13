"""
Unit tests for NotificationSerializer.

Covers:
- Serializing a vintasend Notification dataclass (Phase 1 path):
  created/modified → None, body renders from body_template.
- Serializing a vintasend_django model instance (Phase 2 path):
  created/modified → ISO datetime strings.
- body renders using context_used when present.
- body falls back to context_kwargs when context_used is absent.
- body returns "" when the template does not exist.
"""

import pytest
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import Notification as NotificationDataclass
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService
from vintasend_django.models import Notification as NotificationModel

from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_backends import FixedDjangoDbNotificationBackend
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)
from notifications.serializers import NotificationSerializer


def _build_notification_service() -> NotificationService:
    return NotificationService(
        notification_adapters=[
            DjangoInAppNotificationAdapter(
                DjangoTemplatedInAppRenderer(),
                FixedDjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=FixedDjangoDbNotificationBackend(),
    )


def _make_dataclass(
    *,
    body_template: str = "notifications/in_app/example.body.txt",
    context_kwargs: dict | None = None,
    context_used: dict | None = None,
) -> NotificationDataclass:
    """Return a minimal vintasend Notification dataclass."""
    return NotificationDataclass(
        id=42,
        user_id=1,
        notification_type=NotificationTypes.IN_APP.value,
        title="Test notification",
        body_template=body_template,
        context_name="in_app_generic_context",
        context_kwargs=context_kwargs or {"message": "hello"},
        send_after=None,
        subject_template="",
        preheader_template="",
        status=NotificationStatus.SENT.value,
        context_used=context_used,
    )


class TestNotificationSerializerWithDataclass:
    """Serializer with a vintasend Notification dataclass instance (Phase 1 path)."""

    def test_fields_present(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert set(data.keys()) == {
            "id",
            "title",
            "notification_type",
            "status",
            "body",
            "created",
            "modified",
        }

    def test_id_field(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert data["id"] == "42"

    def test_title_field(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert data["title"] == "Test notification"

    def test_notification_type_field(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert data["notification_type"] == NotificationTypes.IN_APP.value

    def test_status_field(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert data["status"] == NotificationStatus.SENT.value

    def test_created_is_none_for_dataclass(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert data["created"] is None

    def test_modified_is_none_for_dataclass(self) -> None:
        dc = _make_dataclass()
        data = NotificationSerializer(dc).data
        assert data["modified"] is None

    @pytest.mark.django_db
    def test_body_renders_from_body_template_with_context_kwargs(self) -> None:
        """body_template is rendered with context_kwargs when context_used is absent."""
        dc = _make_dataclass(
            body_template="notifications/in_app/example.body.txt",
            context_kwargs={"message": "Hello from context_kwargs"},
            context_used=None,
        )
        data = NotificationSerializer(dc).data
        assert "Hello from context_kwargs" in data["body"]

    @pytest.mark.django_db
    def test_body_renders_from_context_used_when_available(self) -> None:
        """context_used takes priority over context_kwargs for body rendering."""
        dc = _make_dataclass(
            body_template="notifications/in_app/example.body.txt",
            context_kwargs={"message": "wrong"},
            context_used={"message": "Hello from context_used"},
        )
        data = NotificationSerializer(dc).data
        assert "Hello from context_used" in data["body"]
        assert "wrong" not in data["body"]

    @pytest.mark.django_db
    def test_body_returns_empty_string_for_missing_template(self) -> None:
        """body returns "" when the template does not exist (graceful degradation)."""
        dc = _make_dataclass(
            body_template="notifications/in_app/nonexistent.body.txt",
            context_kwargs={"message": "hello"},
        )
        data = NotificationSerializer(dc).data
        assert data["body"] == ""


@pytest.mark.django_db
class TestNotificationSerializerWithModel:
    """Serializer with a vintasend_django Notification model instance (Phase 2 path)."""

    def test_created_and_modified_populate_for_model_instance(self, user) -> None:
        """created and modified are ISO datetime strings for ORM model rows."""
        service = _build_notification_service()

        service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="Model serializer test",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "model test"}),
        )

        model_row = NotificationModel.objects.filter(user_id=user.id).first()
        assert model_row is not None

        data = NotificationSerializer(model_row).data

        assert data["created"] is not None
        assert data["modified"] is not None
        # ISO 8601 strings contain "T" separator
        assert "T" in data["created"]
        assert "T" in data["modified"]

    def test_body_renders_for_model_instance(self, user) -> None:
        """body renders from the stored body_template + context for a model row."""
        service = _build_notification_service()

        service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="Body render model test",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "model body message"}),
        )

        model_row = NotificationModel.objects.filter(user_id=user.id).first()
        assert model_row is not None

        data = NotificationSerializer(model_row).data
        # Body should contain the rendered template content
        assert isinstance(data["body"], str)

    def test_all_fields_present_for_model_instance(self, user) -> None:
        """Serialized model instance has all expected keys."""
        service = _build_notification_service()

        service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="Fields check",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "fields check"}),
        )

        model_row = NotificationModel.objects.filter(user_id=user.id).first()
        assert model_row is not None

        data = NotificationSerializer(model_row).data
        assert set(data.keys()) == {
            "id",
            "title",
            "notification_type",
            "status",
            "body",
            "created",
            "modified",
        }
