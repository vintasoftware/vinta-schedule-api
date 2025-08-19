import datetime
import hashlib
import secrets
from typing import TypedDict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest

import jwt
from allauth.headless.tokens.sessions import SessionTokenStrategy
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import RefreshToken


User = get_user_model()


class AccessTokenResponse(TypedDict):
    access_token: str


class AccessAndRefreshTokenResponse(AccessTokenResponse):
    refresh_token: str


class AccessAndRefreshTokenStrategy(SessionTokenStrategy):
    """
    A token strategy that creates both access and refresh tokens.
    """

    def _generate_access_token_str(self, user_id: int) -> str:
        """
        Generate a JWT access token for the user.
        """
        return str(
            AccessToken.for_user(
                User.objects.get(id=user_id),
            )
        )

    def generate_access_token_only(self, request: HttpRequest) -> AccessTokenResponse:
        """
        Generate only an access token without a refresh token.
        """
        if not request.user.is_authenticated:
            raise ValueError("User must be authenticated to generate an access token.")

        access_token = self._generate_access_token_str(request.user.id)

        return {"access_token": access_token}

    def create_access_token(self, request: HttpRequest) -> AccessAndRefreshTokenResponse:
        """
        Create an access token for the authenticated user.
        """
        if not request.user.is_authenticated:
            raise ValueError("User must be authenticated to create an access token.")

        access_token = self._generate_access_token_str(request.user.id)

        refresh_token_str = secrets.token_urlsafe(32)
        refresh_token_hash = hashlib.sha256(refresh_token_str.encode()).hexdigest()

        RefreshToken.objects.create(
            user=request.user,
            token_hash=refresh_token_hash,
            expires_at=(
                datetime.datetime.now(tz=datetime.UTC)
                + datetime.timedelta(days=getattr(settings, "REFRESH_TOKEN_EXPIRY_DAYS", 30))
            ),
            user_agent=request.headers.get("user-agent", ""),
            device_name=request.headers.get("x-device-name", ""),
            device_id=request.headers.get("x-device-id", ""),
            operational_system=request.headers.get("x-operating-system", ""),
            latitude=request.headers.get("x-device-location-latitude", None),
            longitude=request.headers.get("x-device-location-longitude", None),
        )

        refresh_token_encoded = jwt.encode(
            {
                "user_id": request.user.id,
                "refresh_token": refresh_token_str,
                "exp": datetime.datetime.now(tz=datetime.UTC)
                + datetime.timedelta(days=getattr(settings, "REFRESH_TOKEN_EXPIRY_DAYS", 30)),
            },
            settings.SECRET_KEY,
            algorithm="HS256",
        )

        return {"access_token": access_token, "refresh_token": refresh_token_encoded}

    def create_access_token_payload(
        self, request: HttpRequest
    ) -> AccessAndRefreshTokenResponse | None:
        return super().create_access_token_payload(request)
