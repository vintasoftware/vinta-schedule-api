"""App-only Microsoft Graph tokens for a customer's tenant.

Vinta's multi-tenant Entra app is identified by ``MS_CLIENT_ID`` / ``MS_CLIENT_SECRET``.
Once a tenant admin has granted it admin consent, the OAuth client-credentials flow
returns a token for that tenant with no user behind it. That is what room sync uses,
since partner tokens and background tasks have no Microsoft user to act as.

Tokens are cached in Redis until five minutes before they expire. Redis is optional
here, as everywhere else: when it is missing or its circuit is open, every call asks
Microsoft for a fresh token.

The same Entra app also redeems the authorization code of the admin-consent sign-in
(``tenant_from_sign_in``). That is how Vinta learns which tenant consented: from the
``id_token`` Microsoft returns to this server, never from the browser.

Neither the client secret, an authorization code, nor any token is ever logged.
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
    MicrosoftSignInError,
)
from common.redis import CircuitBreakerOpenError, get_redis_connection, redis_breaker


logger = logging.getLogger(__name__)

TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"  # noqa: S105 - a URL
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
# The admin signs in through the multi-tenant endpoint; the code is redeemed there too.
SIGN_IN_TOKEN_URL = "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"  # noqa: S105 - a URL
# `openid` makes Microsoft return an id_token, whose `tid` claim names the tenant.
SIGN_IN_SCOPE = f"openid {GRAPH_DEFAULT_SCOPE}"
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


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """The claims of a JWT that Microsoft's token endpoint returned to this server.

    The signature is not checked. Only call this on a token received directly from
    Microsoft over TLS in the response to our own request (OpenID Connect Core 3.1.3.7
    allows that for an id_token from the token endpoint), never on one a browser or
    caller handed in. Anything that is not a JWT gives ``{}``.
    """
    parts = token.split(".")
    if len(parts) < 2:  # noqa: PLR2004
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def decode_roles(access_token: str) -> frozenset[str]:
    """The ``roles`` claim of an access token: the application permissions granted.

    The roles only tell an admin which permission is missing. Graph still enforces the
    real permissions on every call.
    """
    roles = decode_jwt_claims(access_token).get("roles")
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

    def get_token(self, tenant_id: str, force_refresh: bool = False) -> MicrosoftAppOnlyToken:
        """Return a token for ``tenant_id`` that is valid for at least five more minutes.

        With ``force_refresh``, the cached token is skipped and the new one replaces it.
        Use it when the answer must reflect what the tenant grants right now, since a
        cached token keeps the ``roles`` it was minted with until it expires.

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

        cached = None if force_refresh else self._read_cache(tenant_id)
        if cached is not None:
            return cached
        token = self._request_token(tenant_id)
        self._write_cache(tenant_id, token)
        return token

    def tenant_from_sign_in(self, code: str, redirect_uri: str, nonce: str) -> str:
        """Redeem the admin-consent sign-in code and return the tenant the admin signed in to.

        The tenant is the ``tid`` claim of the ``id_token`` in Microsoft's answer. That
        token is accepted only when it was issued to Vinta's app (``aud``) for this
        consent attempt (``nonce``). The delegated access token in the same answer is
        dropped unused.

        Raises:
            MicrosoftConnectionNotConfiguredError: ``MS_CLIENT_ID`` or ``MS_CLIENT_SECRET``
                is empty.
            MicrosoftSignInError: Microsoft refused or did not answer, or the id_token
                does not match this app and attempt, or names no tenant.
        """
        if not self.is_configured:
            raise MicrosoftConnectionNotConfiguredError()
        try:
            response = requests.post(
                SIGN_IN_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "scope": SIGN_IN_SCOPE,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("Microsoft sign-in code redemption failed: %s", type(exc).__name__)
            raise MicrosoftSignInError() from None

        body = _json_object(response)
        if response.status_code != 200:  # noqa: PLR2004
            error_code = body.get("error") if isinstance(body.get("error"), str) else None
            logger.warning(
                "Microsoft sign-in code redemption returned HTTP %s (%s)",
                response.status_code,
                error_code,
            )
            raise MicrosoftSignInError()

        id_token = body.get("id_token")
        claims = decode_jwt_claims(id_token) if isinstance(id_token, str) else {}
        tenant_id = claims.get("tid")
        if (
            claims.get("aud") != self._client_id
            or claims.get("nonce") != nonce
            or not isinstance(tenant_id, str)
            or not is_tenant_id(tenant_id)
        ):
            logger.warning("Microsoft sign-in returned an id_token not issued for this attempt")
            raise MicrosoftSignInError()
        return tenant_id.lower()

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
