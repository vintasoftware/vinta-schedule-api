from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from django.test import RequestFactory, override_settings

from pyrate_limiter import Duration, Rate
from strawberry.types import ExecutionContext

from public_api.extensions import OrganizationRateLimiter


def _anonymous_rate_limit_key(request: object) -> str:
    """Run the extension once for an anonymous request and return the bucket key
    it charged. The limiter itself is replaced: it fronts Redis, and what is under
    test here is only which key the request is charged to."""
    extension = OrganizationRateLimiter(rates=[Rate(100, Duration.SECOND)])
    extension.limiter = Mock(**{"try_acquire.return_value": True})
    # The extension reads only `context.request`; a full ExecutionContext needs a
    # schema and a parsed query, so a stand-in is cast to the declared type.
    extension.execution_context = cast(
        "ExecutionContext", SimpleNamespace(context=SimpleNamespace(request=request))
    )

    # Strawberry types on_execute as sync-or-async; this extension's is a plain
    # generator.
    list(cast("Iterator[None]", extension.on_execute()))

    extension.limiter.try_acquire.assert_called_once()
    return extension.limiter.try_acquire.call_args.args[0]


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
