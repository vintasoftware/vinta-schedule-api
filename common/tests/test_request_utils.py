from django.test import Client, RequestFactory, override_settings

from common.utils.request_utils import client_ip_from_request, proxied_client_ip


def test_proxied_client_ip_trusts_no_proxy_by_default():
    """With no proxy in front (local dev, tests), X-Forwarded-For is whatever the
    client sent, so it is ignored."""
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.5")

    assert proxied_client_ip(request.META) == ""


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_proxied_client_ip_takes_the_entry_the_alb_appended():
    """The ALB appends the address it saw to whatever the client sent. The last
    entry is the client, and the forged ones before it are ignored."""
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="6.6.6.6, 7.7.7.7, 203.0.113.5", REMOTE_ADDR="10.0.0.2"
    )

    assert proxied_client_ip(request.META) == "203.0.113.5"


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=2)
def test_proxied_client_ip_counts_trusted_proxies_from_the_right():
    """Two trusted hops (say, a CDN in front of the ALB): the CDN appended the
    client, and the ALB appended the CDN."""
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5, 192.0.2.10", REMOTE_ADDR="10.0.0.2"
    )

    assert proxied_client_ip(request.META) == "203.0.113.5"


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=2)
def test_proxied_client_ip_ignores_a_header_shorter_than_the_proxy_chain():
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.5")

    assert proxied_client_ip(request.META) == ""


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_proxied_client_ip_ignores_an_entry_that_is_not_an_ip_address():
    """Audit fields that store the result are GenericIPAddressFields, which would
    fail to save anything else."""
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.5, not-an-ip")

    assert proxied_client_ip(request.META) == ""


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_proxied_client_ip_accepts_ipv6():
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="2001:db8::1")

    assert proxied_client_ip(request.META) == "2001:db8::1"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP", CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_proxied_client_ip_reads_the_configured_header_and_ignores_forwarded_for():
    """Behind API Gateway the client IP arrives in X-Client-IP, and X-Forwarded-For
    carries whatever the client chose to send -- it must not win."""
    request = RequestFactory().get(
        "/",
        HTTP_X_CLIENT_IP="198.51.100.7",
        HTTP_X_FORWARDED_FOR="203.0.113.5",
        REMOTE_ADDR="10.0.0.2",
    )

    assert proxied_client_ip(request.META) == "198.51.100.7"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP", CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_proxied_client_ip_is_empty_when_the_configured_header_is_absent():
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.5")

    assert proxied_client_ip(request.META) == ""


@override_settings(CLIENT_IP_TRUSTED_PROXY_COUNT=1)
def test_client_ip_from_request_prefers_the_proxied_address():
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5", REMOTE_ADDR="10.0.0.2"
    )

    assert client_ip_from_request(request) == "203.0.113.5"


def test_client_ip_from_request_falls_back_to_remote_addr_without_a_trusted_proxy():
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.2")

    assert client_ip_from_request(request) == "10.0.0.2"


def test_client_ip_from_request_returns_none_for_a_missing_request():
    assert client_ip_from_request(None) is None


@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_CLIENT_PROTO", "https"),
)
def test_configured_proxy_ssl_header_marks_the_request_secure():
    """API Gateway sends X-Client-Proto instead of X-Forwarded-Proto. With
    PROXY_SSL_HEADER pointing at it, a TLS request is not redirected to itself."""
    response = Client().get("/super/", HTTP_X_CLIENT_PROTO="https")

    # The admin's own redirect to its login page, not SecurityMiddleware's to https.
    assert response.status_code == 302
    assert response["Location"] == "/super/login/?next=/super/"


@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_CLIENT_PROTO", "https"),
)
def test_forwarded_proto_is_ignored_once_another_proxy_ssl_header_is_configured():
    response = Client().get("/super/", HTTP_X_FORWARDED_PROTO="https")

    assert response.status_code == 301
