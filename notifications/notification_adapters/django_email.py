from typing import TYPE_CHECKING

from django.core.mail import EmailMessage

from vintasend.app_settings import NotificationSettings
from vintasend.services.notification_backends.base import BaseNotificationBackend
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    BaseTemplatedEmailRenderer,
)
from vintasend_django.services.notification_adapters.django_email import (
    DjangoEmailNotificationAdapter,
)


if TYPE_CHECKING:
    from vintasend.services.dataclasses import Notification, OneOffNotification
    from vintasend.services.notification_service import NotificationContextDict


class ReplyToDjangoEmailNotificationAdapter[
    B: BaseNotificationBackend,
    T: BaseTemplatedEmailRenderer,
](DjangoEmailNotificationAdapter[B, T]):
    """
    Email adapter that honors a per-notification reply-to address.

    Extends ``vintasend_django``'s stock email adapter, which has no reply-to
    concept at all -- it always sends with no ``Reply-To`` header, so replies fall
    back to whatever mail client behavior applies to the From address. This adapter
    reads an optional ``reply_to`` string out of the rendered notification context
    (set by the registered context function, e.g.
    ``organizations.notification_contexts.organization_invitation_context``) and
    sets it as the outbound message's reply-to.

    The From address is untouched in every case, branded or not: it is always
    ``NotificationSettings().NOTIFICATION_DEFAULT_FROM_EMAIL``, exactly like the
    base adapter. Per the Organization Auth-Area Branding plan's Non-goals, there is
    no custom sender and no sending-domain verification -- only the reply-to is
    per-organization.

    When the context has no ``reply_to`` key (or its value is falsy, e.g. an
    unbranded/unentitled organization's blank support email), the reply-to falls
    back to that same From address, so every outbound email always carries an
    explicit Reply-To that lands back on us by default -- identical, in effect, to
    the base adapter's un-set Reply-To.
    """

    def send(
        self,
        notification: "Notification | OneOffNotification",
        context: "NotificationContextDict",
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Send the notification to the user through email, with reply-to support.

        :param notification: The notification to send (regular or one-off).
        :param context: The context to render the notification templates. An
            optional ``reply_to`` string key, when present and truthy, becomes the
            outbound message's reply-to address.
        :param headers: Extra raw email headers, forwarded unchanged to
            ``EmailMessage`` (matches the base adapter's signature).
        """
        notification_settings = NotificationSettings()

        recipient_info = self._get_recipient_info(notification)

        to = [recipient_info["email"]]
        bcc = [email for email in notification_settings.NOTIFICATION_DEFAULT_BCC_EMAILS] or []

        context_with_base_url: NotificationContextDict = context.copy()
        context_with_base_url["base_url"] = (
            f"{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_PROTOCOL}://"
            f"{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_DOMAIN}"
        )

        template = self.template_renderer.render(notification, context_with_base_url)

        from_email = notification_settings.NOTIFICATION_DEFAULT_FROM_EMAIL
        reply_to_address = context.get("reply_to") or from_email

        email = EmailMessage(
            subject=template.subject.strip(),
            body=template.body,
            from_email=from_email,
            to=to,
            bcc=bcc,
            headers=headers,
            reply_to=[reply_to_address],
        )
        email.content_subtype = "html"

        # Attach files if any
        self._attach_files(email, notification)

        email.send()
