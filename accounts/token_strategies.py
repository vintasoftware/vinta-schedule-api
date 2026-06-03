import datetime
import hashlib
import hmac
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest

import jwt
from allauth.headless.tokens.strategies.sessions import SessionTokenStrategy
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import RefreshToken


User = get_user_model()

# Device-tracking fields persisted per refresh token. Captured from request
# headers on login and carried over on rotation (refresh has no request).
DEVICE_FIELDS = (
    "user_agent",
    "device_name",
    "device_id",
    "operational_system",
    "latitude",
    "longitude",
)


class AccessAndRefreshTokenStrategy(SessionTokenStrategy):
    """
    Headless token strategy following allauth's native access/refresh contract:

    - ``create_access_token`` returns the JWT access token *string*.
    - ``create_access_token_payload`` adds a rotating ``refresh_token``.
    - ``refresh_token`` validates and rotates the stored refresh token, wired to
      allauth's native ``/_allauth/app/v1/tokens/refresh`` endpoint.

    Refresh tokens are persisted in :class:`accounts.models.RefreshToken` so we
    keep per-device tracking (user agent, device id/name, OS, geo) on top of the
    native flow.
    """

    def _generate_access_token_str(self, user_id: int) -> str:
        """Generate a JWT access token for the user."""
        return str(AccessToken.for_user(User.objects.get(id=user_id)))

    def _refresh_token_expiry(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
            days=getattr(settings, "REFRESH_TOKEN_EXPIRY_DAYS", 30)
        )

    def _encode_refresh_token(
        self, user_id: int, secret: str, expires_at: datetime.datetime
    ) -> str:
        return jwt.encode(
            {"user_id": user_id, "refresh_token": secret, "exp": expires_at},
            settings.SECRET_KEY,
            algorithm="HS256",
        )

    def _issue_refresh_token(self, user: "User", device: dict) -> str:
        """Create a RefreshToken row (with device metadata) and return the encoded token."""
        secret = secrets.token_urlsafe(32)
        expires_at = self._refresh_token_expiry()
        RefreshToken.objects.create(
            user=user,
            token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            expires_at=expires_at,
            **device,
        )
        return self._encode_refresh_token(user.id, secret, expires_at)

    @staticmethod
    def _device_from_request(request: HttpRequest) -> dict:
        headers = request.headers
        return {
            "user_agent": headers.get("user-agent", ""),
            "device_name": headers.get("x-device-name", ""),
            "device_id": headers.get("x-device-id", ""),
            "operational_system": headers.get("x-operating-system", ""),
            "latitude": headers.get("x-device-location-latitude", None),
            "longitude": headers.get("x-device-location-longitude", None),
        }

    @staticmethod
    def _device_from_instance(instance: RefreshToken) -> dict:
        return {field: getattr(instance, field) for field in DEVICE_FIELDS}

    def _lookup_refresh_token(self, refresh_token: str) -> RefreshToken | None:
        try:
            claims = jwt.decode(refresh_token, settings.SECRET_KEY, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None

        user_id = claims.get("user_id")
        secret = claims.get("refresh_token")
        if not user_id or not secret:
            return None

        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        for instance in RefreshToken.objects.filter(user_id=user_id):
            if hmac.compare_digest(instance.token_hash, secret_hash):
                return instance
        return None

    def create_access_token(self, request: HttpRequest) -> str | None:
        """Return a JWT access token string for the authenticated user (native contract)."""
        if not request.user.is_authenticated:
            return None
        return self._generate_access_token_str(request.user.id)

    def create_access_token_payload(self, request: HttpRequest) -> dict | None:
        """Build the access token payload, attaching a device-tracked refresh token."""
        payload = super().create_access_token_payload(request)
        if payload is None:
            return None
        payload["refresh_token"] = self._issue_refresh_token(
            request.user, self._device_from_request(request)
        )
        return payload

    def refresh_token(self, refresh_token: str) -> tuple[str, str] | None:
        """
        Validate and rotate the given refresh token.

        Returns ``(access_token, next_refresh_token)`` on success, ``None`` otherwise.
        The presented token is single-use: it is invalidated and replaced by a fresh
        token that inherits the original device metadata.
        """
        instance = self._lookup_refresh_token(refresh_token)
        if instance is None:
            return None

        if instance.expires_at < datetime.datetime.now(tz=datetime.UTC):
            instance.delete()
            return None

        user = instance.user
        device = self._device_from_instance(instance)
        # Rotate: invalidate the presented token, then issue a fresh pair.
        instance.delete()
        access_token = self._generate_access_token_str(user.id)
        next_refresh_token = self._issue_refresh_token(user, device)
        return access_token, next_refresh_token
