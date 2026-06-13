"""
Unit tests for DjangoInAppNotificationAdapter.

These tests verify:
  - The adapter advertises notification_type == IN_APP.
  - send() delegates to the template renderer and completes without raising
    when the template renders successfully.
  - send() propagates NotificationBodyTemplateRenderingError when rendering fails.
"""

from unittest.mock import MagicMock

import pytest
from vintasend.constants import NotificationTypes
from vintasend.exceptions import NotificationBodyTemplateRenderingError
from vintasend.services.dataclasses import Notification, NotificationContextDict
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)

from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)


class TestDjangoInAppNotificationAdapterType:
    def test_notification_type_is_in_app(self) -> None:
        assert DjangoInAppNotificationAdapter.notification_type == NotificationTypes.IN_APP


@pytest.mark.django_db
class TestDjangoInAppNotificationAdapterSend:
    @pytest.fixture()
    def backend(self) -> DjangoDbNotificationBackend:
        return DjangoDbNotificationBackend()

    @pytest.fixture()
    def renderer(self) -> DjangoTemplatedInAppRenderer:
        return DjangoTemplatedInAppRenderer()

    @pytest.fixture()
    def adapter(
        self, renderer: DjangoTemplatedInAppRenderer, backend: DjangoDbNotificationBackend
    ) -> DjangoInAppNotificationAdapter:
        return DjangoInAppNotificationAdapter(renderer, backend)

    @pytest.fixture()
    def notification(self) -> Notification:
        return Notification(
            id=1,
            user_id=1,
            notification_type=NotificationTypes.IN_APP.value,
            title="Hello",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "Test message"},
            send_after=None,
            subject_template="",
            preheader_template="",
            status="PENDING_SEND",
        )

    def test_send_renders_body_template_without_error(
        self,
        adapter: DjangoInAppNotificationAdapter,
        notification: Notification,
    ) -> None:
        context = NotificationContextDict({"message": "Test message"})

        assert adapter.send(notification, context) is None

    def test_send_raises_on_render_failure(
        self,
        adapter: DjangoInAppNotificationAdapter,
        notification: Notification,
    ) -> None:
        bad_notification = Notification(
            id=2,
            user_id=1,
            notification_type=NotificationTypes.IN_APP.value,
            title="Bad",
            body_template="notifications/in_app/nonexistent_template.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "hello"},
            send_after=None,
            subject_template="",
            preheader_template="",
            status="PENDING_SEND",
        )
        context = NotificationContextDict({"message": "hello"})

        with pytest.raises(NotificationBodyTemplateRenderingError):
            adapter.send(bad_notification, context)

    def test_send_calls_template_renderer(
        self,
        backend: DjangoDbNotificationBackend,
    ) -> None:
        mock_renderer = MagicMock(spec=DjangoTemplatedInAppRenderer)
        adapter = DjangoInAppNotificationAdapter(mock_renderer, backend)
        notification = Notification(
            id=3,
            user_id=1,
            notification_type=NotificationTypes.IN_APP.value,
            title="Check render call",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs={"message": "check"},
            send_after=None,
            subject_template="",
            preheader_template="",
            status="PENDING_SEND",
        )
        context = NotificationContextDict({"message": "check"})

        adapter.send(notification, context)

        mock_renderer.render.assert_called_once_with(notification, context)
