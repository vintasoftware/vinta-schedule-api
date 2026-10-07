import ipaddress
import logging
from collections.abc import Mapping
from typing import Any

from django.conf import settings


logger = logging.getLogger(__name__)


def proxied_client_ip(meta: Mapping[str, Any]) -> str:
    """Return the client IP that the trusted proxies in front of the app reported,
    or ``""`` when there is none to trust.

    Reads the ``request.META`` key named by ``settings.CLIENT_IP_HEADER``:
    ``X-Forwarded-For`` behind the ALB, and ``X-Client-IP`` behind API Gateway,
    which cannot send ``X-Forwarded-For``.

    Each proxy *appends* the address it received the request from. So with
    ``settings.CLIENT_IP_TRUSTED_PROXY_COUNT`` proxies in front of the app, the
    entry that many places from the right is the address the outermost trusted
    proxy saw. Every entry to its left was sent by the client and can be forged.
    This is the same rule as allauth's ``TRUSTED_PROXY_COUNT`` and Werkzeug's
    ``ProxyFix``. A count of 0, the default (local dev and tests), trusts no
    proxy: the header is ignored, and callers fall back to ``REMOTE_ADDR``.

    ``common.middlewares.TrustedProxyClientIPMiddleware`` writes the result into
    ``REMOTE_ADDR``, which is how allauth and django-defender see it. Code that
    runs inside a request can read ``REMOTE_ADDR`` and get the same answer.
    """
    trusted_proxy_count = settings.CLIENT_IP_TRUSTED_PROXY_COUNT
    header = str(meta.get(settings.CLIENT_IP_HEADER, ""))
    if trusted_proxy_count <= 0 or not header:
        return ""

    entries = [entry.strip() for entry in header.split(",")]
    if len(entries) < trusted_proxy_count:
        # Every trusted proxy appends one entry, so this means the count is wrong
        # or the request did not come through the proxies. The value is not
        # logged: it is an IP address the client may have supplied.
        logger.warning(
            "%s has fewer entries than CLIENT_IP_TRUSTED_PROXY_COUNT (%d); ignoring it.",
            settings.CLIENT_IP_HEADER,
            trusted_proxy_count,
        )
        return ""

    candidate = entries[-trusted_proxy_count]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        logger.warning(
            "The trusted entry of %s is not an IP address; ignoring it.",
            settings.CLIENT_IP_HEADER,
        )
        return ""
    return candidate


def client_ip_from_request(request: object) -> str | None:
    """Extract the client IP address from a Django/DRF request for audit logging.

    Prefers the address the proxy reported (see ``proxied_client_ip``); falls
    back to ``REMOTE_ADDR``. Robust to a missing/``None`` request (e.g.
    allauth's ``signup()`` hook can be invoked with ``request=None`` in some
    tests) -- returns ``None`` rather than raising, since fields such as
    ``UserConsent.ip_address`` are nullable.
    """
    meta = getattr(request, "META", {})
    return proxied_client_ip(meta) or meta.get("REMOTE_ADDR") or None


def user_agent_from_request(request: object) -> str:
    """Extract the client User-Agent header. Robust to a missing/``None`` request."""
    return getattr(request, "META", {}).get("HTTP_USER_AGENT", "")
