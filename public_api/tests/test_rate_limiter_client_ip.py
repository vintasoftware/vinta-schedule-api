from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from django.test import RequestFactory, override_settings

from pyrate_limiter import Duration, Rate
from strawberry.types import ExecutionContext

from public_api.extensions import OrganizationRateLimiter, _shared_limiters


def _run_extension(request: object) -> tuple[Mock, Mock]:
    """Run the extension once for a request and return the ``(organization,
    anonymous)`` limiters it used. Both are replaced: they front Redis, and what
    is under test here is which limiter and key a request is charged to."""
    extension = OrganizationRateLimiter(
        rates=[Rate(100, Duration.SECOND)], anonymous_rates=[Rate(10, Duration.SECOND)]
    )
    organization_limiter = Mock(**{"try_acquire.return_value": True})
    anonymous_limiter = Mock(**{"try_acquire.return_value": True})
    extension.limiter = organization_limiter
    extension.anonymous_limiter = anonymous_limiter
    # The extension reads only `context.request`; a full ExecutionContext needs a
    # schema and a parsed query, so a stand-in is cast to the declared type.
    extension.execution_context = cast(
        "ExecutionContext", SimpleNamespace(context=SimpleNamespace(request=request))
    )

    # Strawberry types on_execute as sync-or-async; this extension's is a plain
    # generator.
    list(cast("Iterator[None]", extension.on_execute()))
    return organization_limiter, anonymous_limiter


def _anonymous_rate_limit_key(request: object) -> str:
    """Run the extension once for an anonymous request and return the bucket key
    it charged."""
    organization_limiter, anonymous_limiter = _run_extension(request)

    organization_limiter.try_acquire.assert_not_called()
    anonymous_limiter.try_acquire.assert_called_once()
    return anonymous_limiter.try_acquire.call_args.args[0]


def test_organization_requests_are_charged_to_the_organization_limiter():
    request = RequestFactory().get("/graphql/", REMOTE_ADDR="10.0.0.2")
    request.public_api_organization = SimpleNamespace(id=42)

    organization_limiter, anonymous_limiter = _run_extension(request)

    anonymous_limiter.try_acquire.assert_not_called()
    organization_limiter.try_acquire.assert_called_once_with("42")


@override_settings(
    PUBLIC_API_REQUESTS_PER_SECOND_LIMIT=50,
    PUBLIC_API_REQUESTS_PER_MINUTE_LIMIT=2000,
    PUBLIC_API_REQUESTS_PER_HOUR_LIMIT=60000,
    PUBLIC_API_ANON_REQUESTS_PER_SECOND_LIMIT=5,
    PUBLIC_API_ANON_REQUESTS_PER_MINUTE_LIMIT=0,
    PUBLIC_API_ANON_REQUESTS_PER_HOUR_LIMIT=1000,
)
def test_limiters_read_their_own_settings_and_are_shared_across_requests():
    _shared_limiters.cache_clear()
    try:
        first, second = OrganizationRateLimiter(), OrganizationRateLimiter()
    finally:
        _shared_limiters.cache_clear()

    assert first.limiter is second.limiter
    assert first.anonymous_limiter is second.anonymous_limiter
    # Rate has no __eq__, so compare (limit, interval) pairs.
    assert [(rate.limit, rate.interval) for rate in first.limiter._rates] == [
        (50, Duration.SECOND),
        (2000, Duration.MINUTE),
        (60000, Duration.HOUR),
    ]
    # A zero limit adds no rate for that window.
    assert [(rate.limit, rate.interval) for rate in first.anonymous_limiter._rates] == [
        (5, Duration.SECOND),
        (1000, Duration.HOUR),
    ]


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_a_forged_forwarded_for_does_not_pick_the_bucket_behind_the_alb():
    """Rotating forged X-Forwarded-For prefixes must not give a client a fresh
    bucket per request: only the entry the ALB appended counts."""
    keys = {
        _anonymous_rate_limit_key(
            RequestFactory().get(
                "/graphql/",
                HTTP_X_FORWARDED_FOR=f"6.6.6.{n}, 203.0.113.5",
                REMOTE_ADDR="10.0.0.2",
            )
        )
        for n in range(3)
    }

    assert keys == {"anon:203.0.113.5"}


def test_anonymous_requests_are_keyed_by_remote_addr_without_a_trusted_proxy():
    request = RequestFactory().get(
        "/graphql/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.2"
    )

    assert _anonymous_rate_limit_key(request) == "anon:10.0.0.2"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP", CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_anonymous_requests_are_keyed_by_the_configured_client_ip_header():
    """Behind API Gateway, X-Forwarded-For is whatever the client sent. Keying on
    it would let one client spread its requests across buckets at will."""
    request = RequestFactory().get(
        "/graphql/",
        HTTP_X_CLIENT_IP="198.51.100.7",
        HTTP_X_FORWARDED_FOR="203.0.113.5",
        REMOTE_ADDR="10.0.0.2",
    )

    assert _anonymous_rate_limit_key(request) == "anon:198.51.100.7"
