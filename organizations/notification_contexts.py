from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from vintasend.exceptions import NotificationContextGenerationError
from vintasend.services.notification_service import register_context

from organizations.models import OrganizationInvitation, resolve_branding


# Vinta Schedule default branding values (used when no reseller branding is configured)
VINTA_DEFAULT_APP_NAME = "Vinta Schedule"
VINTA_DEFAULT_LOGO_URL = ""
VINTA_DEFAULT_PRIMARY_COLOR = ""
VINTA_DEFAULT_SECONDARY_COLOR = ""
VINTA_DEFAULT_SUPPORT_EMAIL = ""


@register_context("organization_invitation_context")
def organization_invitation_context(
    organization_invitation_id: int, invitation_url: str
) -> dict[str, Any]:
    """
    Provides a context for organization invitation-related notifications.

    Injects resolved branding (app_name, logo_url, primary_color, secondary_color,
    support_email) for the invitation's organization. When the organization has a
    reseller ancestor with a branding row, that branding is used. Otherwise, vinta
    defaults are applied (byte-for-byte backwards-compat guarantee).
    """
    try:
        invitation = OrganizationInvitation.objects.get(id=organization_invitation_id)
    except OrganizationInvitation.DoesNotExist as e:
        raise NotificationContextGenerationError("Invalid organization invitation ID") from e

    if invitation.invited_by is None:
        # Public-API invites (e.g. reseller createInvitation) are created with
        # invited_by=None because the caller is a system actor, not a Django User.
        # Use the organization name as a natural-reading fallback so the sentence
        # "invited by <org name>" renders correctly in the template.
        first_name = invitation.organization.name
        last_name = ""
    else:
        try:
            first_name = invitation.invited_by.profile.first_name
            last_name = invitation.invited_by.profile.last_name
        except ObjectDoesNotExist as e:  # noqa: BLE001
            raise NotificationContextGenerationError("Failed to retrieve inviter's profile") from e

    # Resolve branding: walks to the nearest reseller ancestor and uses its branding row.
    # If no reseller ancestor or no branding row, returns None → vinta defaults apply.
    branding_row = resolve_branding(invitation.organization)

    # Build the branding context dict with resolved values or vinta defaults.
    branding_context = {
        "app_name": (branding_row.app_name if branding_row else VINTA_DEFAULT_APP_NAME),
        "logo_url": (branding_row.logo_url if branding_row else VINTA_DEFAULT_LOGO_URL),
        "primary_color": (
            branding_row.primary_color if branding_row else VINTA_DEFAULT_PRIMARY_COLOR
        ),
        "secondary_color": (
            branding_row.secondary_color if branding_row else VINTA_DEFAULT_SECONDARY_COLOR
        ),
        "support_email": (
            branding_row.support_email if branding_row else VINTA_DEFAULT_SUPPORT_EMAIL
        ),
    }

    return {
        "invitation": {
            "id": invitation.id,
            "email": invitation.email,
            "first_name": invitation.first_name,
            "last_name": invitation.last_name,
            "organization_name": invitation.organization.name,
            "invited_by_name": f"{first_name} {last_name}".strip(),
            "expires_at": invitation.expires_at,
        },
        "organization_join_url": invitation_url,
        "branding": branding_context,
    }
