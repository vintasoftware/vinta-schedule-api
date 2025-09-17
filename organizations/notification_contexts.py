from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from vintasend.exceptions import NotificationContextGenerationError
from vintasend.services.notification_service import register_context

from organizations.models import OrganizationInvitation


@register_context("organization_invitation_context")
def organization_invitation_context(
    organization_invitation_id: int, invitation_url: str
) -> dict[str, Any]:
    """
    Provides a context for organization invitation-related notifications.
    """
    try:
        invitation = OrganizationInvitation.objects.get(id=organization_invitation_id)
    except OrganizationInvitation.DoesNotExist as e:
        raise NotificationContextGenerationError("Invalid organization invitation ID") from e

    try:
        first_name = invitation.invited_by.profile.first_name
        last_name = invitation.invited_by.profile.last_name
    except ObjectDoesNotExist as e:  # noqa: BLE001
        raise NotificationContextGenerationError("Failed to retrieve inviter's profile") from e

    return {
        "invitation": {
            "id": invitation.id,
            "email": invitation.email,
            "first_name": invitation.first_name,
            "last_name": invitation.last_name,
            "organization_name": invitation.organization.name,
            "invited_by_name": f"{first_name} {last_name}",
            "expires_at": invitation.expires_at,
        },
        "organization_join_url": invitation_url,
    }
