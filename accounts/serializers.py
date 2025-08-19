import datetime
import hashlib
import hmac

import jwt
from rest_framework import serializers

from accounts.models import RefreshToken
from accounts.token_strategies import AccessAndRefreshTokenStrategy


class RefreshTokenSerializer(serializers.Serializer):
    refresh_token = serializers.CharField(write_only=True)

    def validate_refresh_token(self, refresh_token: str) -> RefreshToken:
        refresh_token_encoded = refresh_token
        try:
            refresh_token_payload = jwt.decode(
                refresh_token_encoded, options={"verify_signature": False}
            )
        except (jwt.DecodeError, jwt.exceptions.DecodeError, Exception) as e:
            raise serializers.ValidationError("Invalid refresh token.") from e

        uid = refresh_token_payload.get("user_id")
        refresh_token_str = refresh_token_payload.get("refresh_token")
        if not uid or not refresh_token_str:
            raise serializers.ValidationError("Invalid refresh token payload.")

        hashed_refresh_token = hashlib.sha256(refresh_token_str.encode()).hexdigest()

        refresh_token_instances = RefreshToken.objects.filter(user_id=uid)

        refresh_token_instance = None
        for check_refresh_token_instance in refresh_token_instances:
            if hmac.compare_digest(check_refresh_token_instance.token_hash, hashed_refresh_token):
                refresh_token_instance = check_refresh_token_instance

        if refresh_token_instance is None:
            raise serializers.ValidationError("Invalid refresh token.")

        if refresh_token_instance.expires_at < datetime.datetime.now(tz=datetime.UTC):
            raise serializers.ValidationError("Refresh token has expired.")

        return refresh_token_instance

    def save(self, **kwargs):
        """
        Override save method to handle refresh token logic.
        """
        refresh_token_instance: RefreshToken = self.validated_data.get("refresh_token")

        self.context["request"].user = refresh_token_instance.user

        return AccessAndRefreshTokenStrategy().generate_access_token_only(
            request=self.context["request"]
        )
