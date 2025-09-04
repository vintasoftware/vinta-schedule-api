from public_api.authentication import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from public_api.models import SystemUser


class PublicAPIAuthService:
    def check_system_user_token(self, system_user_id: str, token: str) -> tuple[SystemUser, bool]:
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
        except (TypeError, ValueError):
            raise SystemUser.DoesNotExist(f"Invalid system_user_id: {system_user_id!r}")
        system_user = SystemUser.objects.get(id=system_user_id_int)

        if not system_user.is_active:
            return system_user, False

        return system_user, verify_long_lived_token(token, system_user.long_lived_token_hash)

    def create_system_user(self, integration_name: str, organization) -> tuple[SystemUser, str]:
        token = generate_long_lived_token()
        system_user = SystemUser.objects.create(
            organization=organization,
            integration_name=integration_name,
            long_lived_token_hash=hash_long_lived_token(token),
            is_active=True,
        )
        return system_user, token
