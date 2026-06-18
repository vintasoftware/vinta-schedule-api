from typing import TYPE_CHECKING, Any

from common.utils.authentication_utils import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from public_api.models import SystemUser


if TYPE_CHECKING:
    from organizations.models import Organization
    from users.models import User


class PublicAPIAuthService:
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
        system_user = SystemUser.objects.get(id=system_user_id_int)

        if not system_user.is_active or system_user.deleted_at is not None:
            return system_user, False

        return system_user, verify_long_lived_token(token, system_user.long_lived_token_hash)

    def create_system_user(
        self,
        integration_name: str,
        organization: "Organization",
        scoped_to_user: "User | None" = None,
    ) -> tuple[SystemUser, str]:
        """
        Create a new system user with a long-lived token.

        :param integration_name: Unique name identifying the integration.
        :param organization: The organization this system user belongs to.
        :param scoped_to_user: Optional user to scope this token to. When set, the token
            may only read/write data belonging to calendars owned by this user.
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
        if scoped_to_user is not None:
            create_kwargs["scoped_to_user"] = scoped_to_user
        system_user = SystemUser.objects.create(**create_kwargs)
        return system_user, token
