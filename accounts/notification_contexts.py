import datetime
from typing import Any

from vintasend.services.notification_service import register_context

from organizations.models import OrganizationInvitation, resolve_branding
from users.notification_contexts import user_context


# Vinta Schedule default branding values (used when no reseller branding is configured)
VINTA_DEFAULT_APP_NAME = "Vinta Schedule"
VINTA_DEFAULT_LOGO_URL = ""
VINTA_DEFAULT_PRIMARY_COLOR = ""
VINTA_DEFAULT_SECONDARY_COLOR = ""
VINTA_DEFAULT_SUPPORT_EMAIL = ""


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
        "branding": {
            "app_name": VINTA_DEFAULT_APP_NAME,
            "logo_url": VINTA_DEFAULT_LOGO_URL,
            "primary_color": VINTA_DEFAULT_PRIMARY_COLOR,
            "secondary_color": VINTA_DEFAULT_SECONDARY_COLOR,
            "support_email": VINTA_DEFAULT_SUPPORT_EMAIL,
        },
    }


@register_context("password_reset_context")
def password_reset_context(user_id: str, password_reset_url: str) -> dict[str, Any]:
    """
    Provides a context for password reset-related notifications.

    Uses vinta defaults for password reset emails — password resets are not part of
    the invitation flow and typically occur after the user is already a member, so
    there's no clear org context to brand from. Being conservative per Phase 8 guidance.
    """
    return {
        "user": user_context(user_id)["user"],
        "password_reset_url": password_reset_url,
        "branding": {
            "app_name": VINTA_DEFAULT_APP_NAME,
            "logo_url": VINTA_DEFAULT_LOGO_URL,
            "primary_color": VINTA_DEFAULT_PRIMARY_COLOR,
            "secondary_color": VINTA_DEFAULT_SECONDARY_COLOR,
            "support_email": VINTA_DEFAULT_SUPPORT_EMAIL,
        },
    }


def _get_branding_for_user(user_email: str) -> dict[str, Any]:
    """
    Resolve branding for a user based on pending invitations.

    If the user has a pending, non-expired invitation to a reseller-branded org,
    return the reseller's branding. Otherwise, return vinta defaults.

    This is used in email confirmation contexts where the user might be joining
    an inviting organization. Being conservative: only brand when there's a clear
    org context via a pending invitation.
    """
    # Check if the user has a pending invitation to a reseller-branded org
    now = datetime.datetime.now(tz=datetime.UTC)
    pending_invitations = OrganizationInvitation.objects.filter(
        email__iexact=user_email,
        expires_at__gt=now,
        accepted_at__isnull=True,
        membership__isnull=True,
    ).select_related("organization")

    for invitation in pending_invitations:
        branding_row = resolve_branding(invitation.organization)
        if branding_row is not None:
            # Found a branded invitation — use that branding
            return {
                "app_name": branding_row.app_name,
                "logo_url": branding_row.logo_url,
                "primary_color": branding_row.primary_color,
                "secondary_color": branding_row.secondary_color,
                "support_email": branding_row.support_email,
            }

    # No pending branded invitation — use vinta defaults
    return {
        "app_name": VINTA_DEFAULT_APP_NAME,
        "logo_url": VINTA_DEFAULT_LOGO_URL,
        "primary_color": VINTA_DEFAULT_PRIMARY_COLOR,
        "secondary_color": VINTA_DEFAULT_SECONDARY_COLOR,
        "support_email": VINTA_DEFAULT_SUPPORT_EMAIL,
    }


@register_context("email_confirmation_context")
def email_confirmation_context(user_id: str, **kwargs) -> dict[str, Any]:
    """
    Provides a context for email confirmation notifications.

    Injects branding when the user has a pending invitation to a reseller-branded
    organization. Otherwise, uses vinta defaults (conservative approach — no guessing).
    """
    from users.models import User

    user = User.objects.get(id=user_id)
    code = kwargs.get("code")
    key = kwargs.get("key")
    activate_url = kwargs.get("activate_url")

    # Resolve branding based on pending invitations
    branding = _get_branding_for_user(user.email)

    if code:
        return {
            "user": user_context(user_id)["user"],
            "code": code,
            "branding": branding,
        }

    return {
        "user": user_context(user_id)["user"],
        "key": key,
        "activate_url": activate_url,
        "branding": branding,
    }
