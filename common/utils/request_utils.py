from collections.abc import Mapping
from typing import Any

from django.conf import settings


def proxied_client_ip(meta: Mapping[str, Any]) -> str:
    """Return the client IP the proxy in front of the app reported, or ``""``.

    Reads the first entry of the ``request.META`` key named by
    ``settings.CLIENT_IP_HEADER``: ``X-Forwarded-For`` behind the ALB,
    ``X-Client-IP`` behind API Gateway, which cannot send ``X-Forwarded-For``.
    Every helper that derives a client IP goes through this, so a change of
    proxy is one setting rather than a hunt for header names.

    Behind API Gateway the value is trustworthy: API Gateway overwrites the
    header. Behind the ALB it is not, because the ALB appends to whatever
    ``X-Forwarded-For`` the client sent, so the first entry is client-supplied.
    That is a known, accepted limitation; see ``public_api/extensions.py``.
    """
    return str(meta.get(settings.CLIENT_IP_HEADER, "")).split(",")[0].strip()


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
