"""Request middleware shared by every app."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from common.utils.request_utils import proxied_client_ip


class TrustedProxyClientIPMiddleware:
    """Replace ``REMOTE_ADDR`` with the client IP the trusted proxies reported.

    Behind the ALB or API Gateway, ``REMOTE_ADDR`` holds the proxy's own address.
    Anything that keys on it then puts every user in one bucket: allauth's
    login, signup and password-reset rate limits, and django-defender's
    admin-login lockout. One client failing to log in three times would lock the
    admin login for everyone. Code that records it, such as
    ``legal.services``' consent records, would store the proxy instead of the
    client.

    Neither library can be pointed at the right entry from settings alone.
    django-defender reads the *first* entry of a forwarded header, which the
    client controls. allauth could count proxies on ``X-Forwarded-For``, but
    API Gateway does not send that header. So this middleware derives the
    address once, with ``proxied_client_ip``, and writes it where every library
    already looks. This is the approach Werkzeug's ``ProxyFix`` takes.

    It is first in ``MIDDLEWARE`` so that nothing reads ``REMOTE_ADDR`` before it
    runs. With ``CLIENT_IP_TRUSTED_PROXY_COUNT = 0``, the default, it does
    nothing.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        client_ip = proxied_client_ip(request.META)
        if client_ip:
            request.META["REMOTE_ADDR"] = client_ip
        return self.get_response(request)
