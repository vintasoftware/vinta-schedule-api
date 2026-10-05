"""App-only Microsoft Graph tokens for a customer's tenant.

Vinta's multi-tenant Entra app is identified by ``MS_CLIENT_ID`` / ``MS_CLIENT_SECRET``.
Once a tenant admin has granted it admin consent, the OAuth client-credentials flow
returns a token for that tenant with no user behind it. That is what room sync uses,
since partner tokens and background tasks have no Microsoft user to act as.

Tokens are cached in Redis until five minutes before they expire. Redis is optional
here, as everywhere else: when it is missing or its circuit is open, every call asks
Microsoft for a fresh token.

Neither the client secret nor any token is ever logged.
"""

import base64
import binascii
import dataclasses
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from django.conf import settings

import requests
from redis.exceptions import RedisError

from calendar_integration.exceptions import (
    MicrosoftAppOnlyTokenError,
    MicrosoftConnectionNotConfiguredError,
)
from common.redis import CircuitBreakerOpenError, get_redis_connection, redis_breaker


logger = logging.getLogger(__name__)

TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"  # noqa: S105 - a URL
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
CACHE_EXPIRY_MARGIN_SECONDS = 5 * 60
REQUEST_TIMEOUT_SECONDS = 15
CACHE_KEY_PREFIX = "ms_app_only_token"

# Microsoft sends the directory (tenant) id as a GUID. Nothing else is accepted, so a
# tenant value can never change the path of the token URL.
_TENANT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_tenant_id(value: str) -> bool:
    """Whether ``value`` has the shape of a Microsoft directory (tenant) id."""
    return bool(_TENANT_ID_RE.fullmatch(value))


class TokenCache(Protocol):
    """The two Redis commands the cache uses."""

    def get(self, name: str) -> Any:
        """Return the stored value, or ``None``."""
        ...

    def set(self, name: str, value: str, ex: int) -> Any:
        """Store ``value`` for ``ex`` seconds."""
        ...


@dataclasses.dataclass(frozen=True)
class MicrosoftAppOnlyToken:
    """An app-only access token and the application permissions it carries."""

    access_token: str = dataclasses.field(repr=False)
    roles: frozenset[str]
    expires_at: float


def decode_roles(access_token: str) -> frozenset[str]:
    """The ``roles`` claim of an access token: the application permissions granted.

    The signature is not checked. The token came straight from Microsoft's token
    endpoint over TLS, and the roles only tell an admin which permission is missing.
    Graph still enforces the real permissions on every call.
    """
    parts = access_token.split(".")
    if len(parts) < 2:
        return frozenset()
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return frozenset()
    roles = claims.get("roles") if isinstance(claims, dict) else None
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(role for role in roles if isinstance(role, str))


class MicrosoftAppOnlyTokenProvider:
    """Mints and caches app-only Graph tokens for a tenant that granted admin consent."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        cache_getter: Callable[[], TokenCache | None] = get_redis_connection,
        clock: Callable[[], float] = time.time,
    ):
        self._client_id = settings.MS_CLIENT_ID if client_id is None else client_id
        self._client_secret = settings.MS_CLIENT_SECRET if client_secret is None else client_secret
        self._cache_getter = cache_getter
        self._clock = clock

    @property
    def client_id(self) -> str:
        """The application (client) id of Vinta's Entra app. Public: it is in every consent URL."""
        return self._client_id

    @property
    def is_configured(self) -> bool:
        """Whether Vinta's Entra app credentials are set."""
        return bool(self._client_id and self._client_secret)

    def get_token(self, tenant_id: str) -> MicrosoftAppOnlyToken:
        """Return a token for ``tenant_id`` that is valid for at least five more minutes.

        Raises:
            MicrosoftConnectionNotConfiguredError: ``MS_CLIENT_ID`` or ``MS_CLIENT_SECRET``
                is empty.
            MicrosoftAppOnlyTokenError: the tenant id is malformed, or Microsoft refused
                or did not answer the token request.
        """
        if not self.is_configured:
            raise MicrosoftConnectionNotConfiguredError()
        if not is_tenant_id(tenant_id):
            raise MicrosoftAppOnlyTokenError("The Microsoft tenant id is not a valid GUID.")

        cached = self._read_cache(tenant_id)
        if cached is not None:
            return cached
        token = self._request_token(tenant_id)
        self._write_cache(tenant_id, token)
        return token

    def _cache_key(self, tenant_id: str) -> str:
        # Keyed by client id too, so rotating to another Entra app never serves a
        # token the old app minted.
        return f"{CACHE_KEY_PREFIX}:{self._client_id}:{tenant_id.lower()}"

    def _request_token(self, tenant_id: str) -> MicrosoftAppOnlyToken:
        requested_at = self._clock()
        try:
            response = requests.post(
                TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": GRAPH_DEFAULT_SCOPE,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # The exception type only: a requests error message can carry the URL and,
            # for some adapters, the prepared request.
            logger.warning(
                "Microsoft app-only token request failed for tenant %s: %s",
                tenant_id,
                type(exc).__name__,
            )
            raise MicrosoftAppOnlyTokenError() from None

        body = _json_object(response)
        if response.status_code != 200:
            error_code = body.get("error") if isinstance(body.get("error"), str) else None
            logger.warning(
                "Microsoft app-only token request for tenant %s returned HTTP %s (%s)",
                tenant_id,
                response.status_code,
                error_code,
            )
            raise MicrosoftAppOnlyTokenError(
                status_code=response.status_code, error_code=error_code
            )

        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise MicrosoftAppOnlyTokenError("Microsoft returned no access token.")
        try:
            lifetime = int(expires_in) if expires_in is not None else 0
        except (TypeError, ValueError):
            lifetime = 0
        return MicrosoftAppOnlyToken(
            access_token=access_token,
            roles=decode_roles(access_token),
            expires_at=requested_at + lifetime,
        )

    def _read_cache(self, tenant_id: str) -> MicrosoftAppOnlyToken | None:
        cache = self._cache_getter()
        if cache is None or not redis_breaker.allows_request():
            return None
        try:
            raw = redis_breaker.call(cache.get, self._cache_key(tenant_id))
        except (RedisError, CircuitBreakerOpenError):
            logger.warning("Redis unavailable; requesting a new Microsoft app-only token")
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            token = MicrosoftAppOnlyToken(
                access_token=str(data["access_token"]),
                roles=frozenset(str(role) for role in data["roles"]),
                expires_at=float(data["expires_at"]),
            )
        except (ValueError, TypeError, KeyError):
            return None
        if token.expires_at - CACHE_EXPIRY_MARGIN_SECONDS <= self._clock():
            return None
        return token

    def _write_cache(self, tenant_id: str, token: MicrosoftAppOnlyToken) -> None:
        ttl = int(token.expires_at - CACHE_EXPIRY_MARGIN_SECONDS - self._clock())
        if ttl <= 0:
            return
        cache = self._cache_getter()
        if cache is None or not redis_breaker.allows_request():
            return
        payload = json.dumps(
            {
                "access_token": token.access_token,
                "roles": sorted(token.roles),
                "expires_at": token.expires_at,
            }
        )
        try:
            redis_breaker.call(cache.set, self._cache_key(tenant_id), payload, ex=ttl)
        except (RedisError, CircuitBreakerOpenError):
            logger.warning("Redis unavailable; Microsoft app-only token not cached")


def _json_object(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
