from typing import Any

from vintasend.services.notification_service import register_context


@register_context("in_app_generic_context")
def in_app_generic_context(message: str, **kwargs: Any) -> dict[str, Any]:
    """
    A generic in-app notification context.

    Provides a simple context for in-app notifications with a single
    ``message`` variable available in the body template. Pass additional
    keyword arguments to make them available as template variables too.

    Usage example::

        notification_service.create_notification(
            user_id=user.id,
            notification_type=NotificationTypes.IN_APP.value,
            title="Hello",
            body_template="notifications/in_app/example.body.txt",
            context_name="in_app_generic_context",
            context_kwargs=NotificationContextDict({"message": "You have a new notification."}),
        )
    """
    return {"message": message, **kwargs}
