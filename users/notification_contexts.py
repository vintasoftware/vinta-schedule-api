from typing import Any

from vintasend.services.notification_service import register_context

from users.models import User


@register_context("user_context")
def user_context(user_id: str) -> dict[str, Any]:
    """
    Provides a context for user-related notifications.
    """
    user = User.objects.select_related("profile").get(id=user_id)

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "first_name": user.profile.first_name if user.profile else "",
            "last_name": user.profile.last_name if user.profile else "",
            "phone_number": user.phone_number,
        }
    }
