from django.test import RequestFactory, override_settings

from calendar_integration.mutations import _client_ip_from_request


def test_client_ip_prefers_forwarded_for_by_default():
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.5", REMOTE_ADDR="10.0.0.2")

    assert _client_ip_from_request(request) == "203.0.113.5"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP")
def test_client_ip_reads_the_configured_header_and_ignores_forwarded_for():
    request = RequestFactory().get(
        "/",
        HTTP_X_CLIENT_IP="198.51.100.7",
        HTTP_X_FORWARDED_FOR="203.0.113.5",
        REMOTE_ADDR="10.0.0.2",
    )

    assert _client_ip_from_request(request) == "198.51.100.7"


@override_settings(CLIENT_IP_HEADER="HTTP_X_CLIENT_IP")
def test_client_ip_falls_back_to_remote_addr_without_the_configured_header():
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.2")

    assert _client_ip_from_request(request) == "10.0.0.2"


def test_client_ip_is_empty_for_an_object_without_meta():
    assert _client_ip_from_request(object()) == ""
