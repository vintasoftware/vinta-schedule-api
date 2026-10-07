from django.test import Client, RequestFactory, override_settings

from common.utils.request_utils import client_ip_from_request, proxied_client_ip


def test_proxied_client_ip_reads_first_forwarded_for_entry_by_default():
    """Behind the ALB, the client is the first X-Forwarded-For entry."""
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="203.0.113.5, 10.0.0.1", REMOTE_ADDR="10.0.0.2"
    )

    assert proxied_client_ip(request.META) == "203.0.113.5"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP")
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


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP")
def test_proxied_client_ip_is_empty_when_the_configured_header_is_absent():
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.5")

    assert proxied_client_ip(request.META) == ""


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP")
def test_client_ip_from_request_falls_back_to_remote_addr_without_the_configured_header():
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.2")

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
