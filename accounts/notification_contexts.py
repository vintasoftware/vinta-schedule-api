from typing import Any

from vintasend.services.notification_service import register_context

from users.notification_contexts import user_context


@register_context("phone_verification_context")
def phone_verification_context(
    user_id: str, phone_verification_code: str, phone_number: str
) -> dict[str, Any]:
    """
    Provides a context for phone verification-related notifications.

    Uses vinta defaults for phone verification — phone verification is typically
    a secondary verification path and not tied to org invitations, so no clear
    org context to brand from.
    """
    return {
        "user": user_context(user_id)["user"],
        "phone_verification_code": phone_verification_code,
        "phone_number": phone_number,
    }


@register_context("password_reset_context")
def password_reset_context(user_id: str, password_reset_url: str) -> dict[str, Any]:
    """
    Provides a context for password reset-related notifications.

    Uses vinta defaults for password reset emails. Password resets are not part of
    the invitation flow and typically happen after the user is already a member, so
    there is no clear org context to brand from.
    """
    return {
        "user": user_context(user_id)["user"],
        "password_reset_url": password_reset_url,
    }


@register_context("email_confirmation_context")
def email_confirmation_context(user_id: str, **kwargs) -> dict[str, Any]:
    """
    Provides a context for email confirmation notifications.

    Uses vinta defaults. Confirmation templates are not tied to the invitation
    flow, so they stay unchanged.
    """
    code = kwargs.get("code")
    key = kwargs.get("key")
    activate_url = kwargs.get("activate_url")

    if code:
        return {
            "user": user_context(user_id)["user"],
            "code": code,
        }

    return {
        "user": user_context(user_id)["user"],
        "key": key,
        "activate_url": activate_url,
    }
