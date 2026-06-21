from typing import TYPE_CHECKING, Annotated, Any

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from audit.services import AuditService
from common.utils.authentication_utils import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from public_api.models import SystemUser


if TYPE_CHECKING:
    from organizations.models import Organization, OrganizationMembership


class PublicAPIAuthService:
    @inject
    def __init__(
        self,
        audit_service: Annotated[AuditService, Provide["audit_service"]],
    ) -> None:
        self.audit_service = audit_service

    def check_system_user_token(self, system_user_id: int, token: str) -> tuple[SystemUser, bool]:
        """
        Check if the provided token matches the system user's long-lived token.
        :param system_user_id: ID of the system user.
        :param token: The long-lived token to verify.
        :return: Tuple containing the system user and a boolean indicating if the token is valid.
        :raises SystemUser.DoesNotExist: If the system user does not exist.
        :raises ValueError: If the token is invalid or does not match the user's token.
        """
        try:
            system_user_id_int = int(system_user_id)
        except (TypeError, ValueError) as e:
            raise SystemUser.DoesNotExist(f"Invalid system_user_id: {system_user_id!r}") from e
        system_user = SystemUser.original_manager.get(id=system_user_id_int)

        if not system_user.is_active or system_user.deleted_at is not None:
            return system_user, False

        return system_user, verify_long_lived_token(token, system_user.long_lived_token_hash)

    def create_system_user(
        self,
        integration_name: str,
        organization: "Organization",
        scoped_to_membership: "OrganizationMembership | None" = None,
    ) -> tuple[SystemUser, str]:
        """
        Create a new system user with a long-lived token.

        :param integration_name: Unique name identifying the integration.
        :param organization: The organization this system user belongs to.
        :param scoped_to_membership: Optional OrganizationMembership to scope this token to.
            When set, the token may only read/write data belonging to calendars owned by the
            membership's user within that organization.
            When None (default), the token has org-wide access (legacy default).
        :return: Tuple of (system_user, plaintext_token). The plaintext token is exposed
            once and never persisted; only the hash is stored.
        """
        token = generate_long_lived_token()
        create_kwargs: dict[str, Any] = {
            "organization": organization,
            "integration_name": integration_name,
            "long_lived_token_hash": hash_long_lived_token(token),
            "is_active": True,
        }
        if scoped_to_membership is not None:
            create_kwargs["scoped_to_membership_user_id"] = scoped_to_membership.user_id
        system_user = SystemUser.objects.create(**create_kwargs)

        # Audit: a system-user (API integration credential) is provisioned for the org.
        # No acting Django User is threaded here, so the actor is the system; when the
        # token is membership-scoped, that membership is the affected party.
        self.audit_service.record(
            organization_id=organization.id,
            action=AuditAction.CREATE,
            actor=self.audit_service.system_actor(),
            subject=self.audit_service.subject_from_instance(system_user),
            affected_membership_ids=(
                [scoped_to_membership.user_id] if scoped_to_membership is not None else []
            ),
        )
        return system_user, token
