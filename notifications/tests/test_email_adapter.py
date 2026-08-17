"""
Unit tests for ReplyToDjangoEmailNotificationAdapter.

Covers the invitation reply-to plumbing. The
vintasend/vintasend_django email adapter has no reply-to concept at all -- it always
sends with no ``Reply-To`` header. ``ReplyToDjangoEmailNotificationAdapter`` extends it
to honor an optional ``reply_to`` string in the rendered notification context, while
leaving the From address exactly as the base adapter sets it in every case.
"""

from django.core import mail

import pytest
from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict, OneOffNotification
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)

from notifications.notification_adapters.django_email import (
    ReplyToDjangoEmailNotificationAdapter,
)


# Shipped with vintasend_django precisely for adapter-level tests like this one --
# minimal, generic templates with no organization/branding-specific context requirements.
_TEST_BODY_TEMPLATE = "vintasend_django/emails/test/test_templated_email_body.html"
_TEST_SUBJECT_TEMPLATE = "vintasend_django/emails/test/test_templated_email_subject.txt"
_TEST_PREHEADER_TEMPLATE = "vintasend_django/emails/test/test_templated_email_preheader.html"


def _one_off_notification() -> OneOffNotification:
    return OneOffNotification(
        id=1,
        email_or_phone="invitee@example.com",
        first_name="Jane",
        last_name="Doe",
        notification_type=NotificationTypes.EMAIL.value,
        title="Test notification",
        body_template=_TEST_BODY_TEMPLATE,
        context_name="unused",
        context_kwargs={},
        send_after=None,
        subject_template=_TEST_SUBJECT_TEMPLATE,
        preheader_template=_TEST_PREHEADER_TEMPLATE,
        status=NotificationStatus.PENDING_SEND.value,
    )


@pytest.mark.django_db
class TestReplyToDjangoEmailNotificationAdapter:
    @pytest.fixture()
    def adapter(self) -> ReplyToDjangoEmailNotificationAdapter:
        return ReplyToDjangoEmailNotificationAdapter(
            DjangoTemplatedEmailRenderer(),
            DjangoDbNotificationBackend(),
        )

    def test_notification_type_is_email(
        self, adapter: ReplyToDjangoEmailNotificationAdapter
    ) -> None:
        assert adapter.notification_type == NotificationTypes.EMAIL

    def test_send_honors_reply_to_from_context(
        self, adapter: ReplyToDjangoEmailNotificationAdapter
    ) -> None:
        """A context carrying a truthy ``reply_to`` sets it as the message's reply-to,
        while the From address stays the configured default -- unchanged."""
        notification = _one_off_notification()
        context = NotificationContextDict(
            {
                "test_subject": "hello",
                "test_body": "world",
                "reply_to": "support@brandco.example",
            }
        )

        adapter.send(notification, context)

        assert len(mail.outbox) == 1
        sent = mail.outbox[0]
        assert sent.reply_to == ["support@brandco.example"]
        assert sent.from_email == NotificationSettings().NOTIFICATION_DEFAULT_FROM_EMAIL

    def test_send_sets_no_reply_to_when_context_has_no_reply_to(
        self, adapter: ReplyToDjangoEmailNotificationAdapter
    ) -> None:
        """No ``reply_to`` key in the context -- e.g. an unbranded or unentitled
        organization, or any non-invitation notification that never sets one --
        sends with no Reply-To header at all, exactly like the base adapter."""
        notification = _one_off_notification()
        context = NotificationContextDict({"test_subject": "hello", "test_body": "world"})

        adapter.send(notification, context)

        sent = mail.outbox[0]
        assert sent.reply_to == []
        assert sent.from_email == NotificationSettings().NOTIFICATION_DEFAULT_FROM_EMAIL

    def test_send_sets_no_reply_to_when_reply_to_is_blank(
        self, adapter: ReplyToDjangoEmailNotificationAdapter
    ) -> None:
        """A present-but-blank ``reply_to`` (an entitled organization with no support
        email set) is falsy -- also sends with no Reply-To header."""
        notification = _one_off_notification()
        context = NotificationContextDict(
            {"test_subject": "hello", "test_body": "world", "reply_to": ""}
        )

        adapter.send(notification, context)

        sent = mail.outbox[0]
        assert sent.reply_to == []
