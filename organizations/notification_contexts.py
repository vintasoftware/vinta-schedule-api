from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from vintasend.exceptions import NotificationContextGenerationError
from vintasend.services.notification_service import register_context

from organizations.branding_logo import build_logo_delivery_url
from organizations.models import OrganizationInvitation, resolve_branding_for_display


# Vinta Schedule default branding values (used when no reseller branding is configured)
VINTA_DEFAULT_APP_NAME = "Vinta Schedule"
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
        invited_by_name = invitation.organization.name
    else:
        try:
            first_name = invitation.invited_by.profile.first_name
            last_name = invitation.invited_by.profile.last_name
        except ObjectDoesNotExist as e:  # noqa: BLE001
            raise NotificationContextGenerationError("Failed to retrieve inviter's profile") from e
        # Compute invited_by_name without strip() to preserve the trailing space when
        # last_name is empty (byte-for-byte compatible with the earlier behavior).
        invited_by_name = f"{first_name} {last_name}"

    # Resolve branding: walks to the nearest reseller ancestor and uses its branding row.
    # If no reseller ancestor, no branding row, or no `white_label_branding` entitlement,
    # returns None → vinta defaults apply. This is a presentation caller (the invitation
    # email's app name / logo / colors / support address), so it uses the gated variant.
    branding_row = resolve_branding_for_display(invitation.organization)

    # Build the branding context dict with resolved values or vinta defaults.
    # `logo_url` is always the logo delivery route's absolute URL, never a signed S3
    # URL and never a bare key -- so an email opened days later still renders it. No
    # `request` is available in a notification-context generator, so
    # `build_logo_delivery_url` falls back to `settings.API_DOMAIN`. When there is no
    # branding row (or no entitlement), `organization=None` resolves through the
    # route's reserved "default" sentinel slug to our bundled default logo -- the
    # same miss-path an unbranded organization's own logo request would take.
    support_email = branding_row.support_email if branding_row else VINTA_DEFAULT_SUPPORT_EMAIL

    branding_context = {
        "app_name": (branding_row.app_name if branding_row else VINTA_DEFAULT_APP_NAME),
        "logo_url": build_logo_delivery_url(branding_row.organization if branding_row else None),
        "primary_color": (
            branding_row.primary_color if branding_row else VINTA_DEFAULT_PRIMARY_COLOR
        ),
        "secondary_color": (
            branding_row.secondary_color if branding_row else VINTA_DEFAULT_SECONDARY_COLOR
        ),
        "support_email": support_email,
    }

    return {
        "invitation": {
            "id": invitation.id,
            "email": invitation.email,
            "first_name": invitation.first_name,
            "last_name": invitation.last_name,
            "organization_name": invitation.organization.name,
            "invited_by_name": invited_by_name,
            "expires_at": invitation.expires_at,
        },
        "organization_join_url": invitation_url,
        "branding": branding_context,
        # Read by ReplyToDjangoEmailNotificationAdapter (notifications/notification_adapters/
        # django_email.py) to set the outbound message's reply-to. Empty string when there is
        # no branding row or no entitlement -- the adapter treats a falsy reply_to as "no
        # override" and falls back to our own From address, matching today's behavior exactly.
        # The From address itself is never touched here: no custom sender, no
        # sending-domain verification (Organization Auth-Area Branding plan, Non-goals).
        "reply_to": support_email,
    }
