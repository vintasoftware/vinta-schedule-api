def client_ip_from_request(request: object) -> str | None:
    """Extract the client IP address from a Django/DRF request for audit logging.

    Prefers the first entry of ``X-Forwarded-For`` (set by load balancers /
    proxies); falls back to ``REMOTE_ADDR``. Robust to a missing/``None``
    request (e.g. allauth's ``signup()`` hook can be invoked with
    ``request=None`` in some tests) -- returns ``None`` rather than raising,
    since fields such as ``UserConsent.ip_address`` are nullable.
    """
    meta = getattr(request, "META", {})
    forwarded_for = meta.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return meta.get("REMOTE_ADDR") or None


def user_agent_from_request(request: object) -> str:
    """Extract the client User-Agent header. Robust to a missing/``None`` request."""
    return getattr(request, "META", {}).get("HTTP_USER_AGENT", "")
