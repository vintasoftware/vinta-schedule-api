from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from allauth.core.internal.httpkit import get_client_ip as allauth_client_ip

from common.middlewares import TrustedProxyClientIPMiddleware


def _remote_addr_seen_downstream(request: HttpRequest) -> str:
    """Run the middleware and return the REMOTE_ADDR the next layer saw."""
    seen: dict[str, str] = {}

    def get_response(downstream_request: HttpRequest) -> HttpResponse:
        seen["remote_addr"] = downstream_request.META["REMOTE_ADDR"]
        return HttpResponse()

    TrustedProxyClientIPMiddleware(get_response)(request)
    return seen["remote_addr"]


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_remote_addr_becomes_the_client_behind_the_alb():
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5", REMOTE_ADDR="10.20.0.15"
    )

    assert _remote_addr_seen_downstream(request) == "203.0.113.5"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP", CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_remote_addr_becomes_the_client_behind_api_gateway():
    request = RequestFactory().get(
        "/",
        HTTP_X_CLIENT_IP="198.51.100.7",
        HTTP_X_FORWARDED_FOR="6.6.6.6",
        REMOTE_ADDR="10.20.100.4",
    )

    assert _remote_addr_seen_downstream(request) == "198.51.100.7"


def test_remote_addr_is_left_alone_without_a_trusted_proxy():
    """Local dev: a client-sent X-Forwarded-For must not change REMOTE_ADDR."""
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="127.0.0.1")

    assert _remote_addr_seen_downstream(request) == "127.0.0.1"


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_remote_addr_is_left_alone_when_the_proxy_header_is_missing():
    """The ECS container health check calls gunicorn directly, with no proxy."""
    request = RequestFactory().get("/healthz/", REMOTE_ADDR="127.0.0.1")

    assert _remote_addr_seen_downstream(request) == "127.0.0.1"


def test_middleware_runs_before_everything_else():
    """allauth, django-defender and the rest read REMOTE_ADDR. Anything placed
    ahead of this middleware would see the proxy's address instead."""
    assert settings.MIDDLEWARE[0] == "common.middlewares.TrustedProxyClientIPMiddleware"


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_allauth_keys_its_rate_limits_on_the_client_downstream():
    """The reason the middleware exists: allauth reads REMOTE_ADDR, and behind the
    proxy it would otherwise rate-limit every user as one."""
    seen: dict[str, str | None] = {}

    def get_response(downstream_request: HttpRequest) -> HttpResponse:
        seen["allauth"] = allauth_client_ip(downstream_request)
        return HttpResponse()

    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5", REMOTE_ADDR="10.20.0.15"
    )
    TrustedProxyClientIPMiddleware(get_response)(request)

    assert seen == {"allauth": "203.0.113.5"}


def test_libraries_are_left_reading_remote_addr():
    """Each of these settings would make its library re-derive the client from a
    header instead of reading the REMOTE_ADDR the middleware wrote. django-defender
    would take the header's first entry, which the client controls. (defender
    cannot be exercised here: it is only installed when Redis is configured.)"""
    assert getattr(settings, "ALLAUTH_TRUSTED_PROXY_COUNT", 0) == 0
    assert getattr(settings, "ALLAUTH_TRUSTED_CLIENT_IP_HEADER", None) is None
    assert getattr(settings, "DEFENDER_BEHIND_REVERSE_PROXY", False) is False
