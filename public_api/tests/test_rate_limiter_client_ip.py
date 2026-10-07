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


def test_anonymous_requests_are_keyed_by_forwarded_for_by_default():
    request = RequestFactory().get(
        "/graphql/", HTTP_X_FORWARDED_FOR="203.0.113.5, 10.0.0.1", REMOTE_ADDR="10.0.0.2"
    )

    assert _anonymous_rate_limit_key(request) == "anon:203.0.113.5"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP")
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
