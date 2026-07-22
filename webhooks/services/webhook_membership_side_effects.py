from typing import TYPE_CHECKING, Annotated

from django.db import transaction

from dependency_injector.wiring import Provide, inject

from organizations.models import OrganizationMembership
from webhooks.constants import WebhookEventType
from webhooks.services.payloads import OrganizationMemberCreatedWebhookPayload


if TYPE_CHECKING:
    from webhooks.services import WebhookService


class WebhookMembershipSideEffectsService:
    """Emits webhook side-effects for OrganizationMembership lifecycle events."""

    @inject
    def __init__(self, webhook_service: Annotated["WebhookService", Provide["webhook_service"]]):
        self.webhook_service = webhook_service

    def _serialize_membership(
        self, membership: OrganizationMembership
    ) -> OrganizationMemberCreatedWebhookPayload:
        return {
            "user_id": membership.user_id,
            "email": membership.user.email,
            "organization_id": membership.organization_id,
            "organization_name": membership.organization.name,
            "membership_role": membership.role,
        }

    def on_member_created(self, membership: OrganizationMembership) -> None:
        """Emit the organization_member_created event for an active membership.

        Returns early without emitting anything when the membership is not active.

        Args:
            membership: The newly created OrganizationMembership. Must have
                ``user``, ``organization``, and ``role`` accessible (i.e. already
                saved to the DB and related objects pre-loaded or accessible via
                FK lookup). The membership identity in the payload is the
                ``(user_id, organization_id)`` pair — no scalar membership id.
        """
        if not membership.is_active:
            return

        payload = dict(self._serialize_membership(membership))
        transaction.on_commit(
            lambda: self.webhook_service.send_event(
                organization=membership.organization,
                event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
                payload=payload,
            )
        )
