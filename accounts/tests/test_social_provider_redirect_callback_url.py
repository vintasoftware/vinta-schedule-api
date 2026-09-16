"""HTTP-level tests: the SPA's ``callback_url`` survives under ECS-shaped host
settings, on both endpoints that start a social login.

``test_account_adapters.py`` tests ``AccountAdapter.is_safe_url`` on its own. These
tests drive the whole path instead: the view, the ``RedirectToProviderForm`` field
that calls ``is_safe_url``, and the provider URL that comes back.

The web SPA calls ``ProviderRedirectAPIView`` (``/auth/app/v1/auth/provider/redirect-json/``),
so that one comes first. allauth's own ``/auth/browser/v1/auth/provider/redirect`` is
covered too, because both read the same setting and either can be the entry point.

Each endpoint carries a negative control that reproduces the staging failure, with
CORS_ALLOWED_ORIGINS still listing the SPA origin -- which is what made the failure
confusing, since allauth reads ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS and never
CORS_ALLOWED_ORIGINS.
"""

import json

from django.test import Client
from django.urls import reverse

import pytest

from vinta_schedule_api.settings.base import build_csrf_trusted_origins


pytestmark = pytest.mark.django_db

API_HOST = "api.schedule-staging.vintasoftware.com"
FRONTEND_ORIGIN = "https://schedule-staging.vintasoftware.com"
CALLBACK_URL = f"{FRONTEND_ORIGIN}/auth/social/google/callback"
PAYLOAD = {"provider": "google", "callback_url": CALLBACK_URL, "process": "login"}
STOCK_ENDPOINT = "/auth/browser/v1/auth/provider/redirect"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


@pytest.fixture
def ecs_style_settings(settings):
    """Staging's shape: ALLOWED_HOSTS is the API hostname, the SPA lives elsewhere."""
    settings.ALLOWED_HOSTS = [API_HOST]
    settings.FRONTEND_BASE_URL = FRONTEND_ORIGIN
    settings.CORS_ALLOWED_ORIGINS = [FRONTEND_ORIGIN]
    settings.CSRF_TRUSTED_ORIGINS = build_csrf_trusted_origins(FRONTEND_ORIGIN)
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {"APPS": [{"client_id": "test-client-id", "secret": "test-secret", "key": ""}]}
    }
    return settings


def _post_json(payload: dict):
    """What the web SPA sends: a JSON body to the custom app-client endpoint."""
    return Client().post(
        reverse("provider_redirect_json"),
        json.dumps(payload),
        content_type="application/json",
        HTTP_HOST=API_HOST,
    )


def _post_form(payload: dict):
    """What allauth's stock endpoint expects: a form-encoded body."""
    return Client().post(STOCK_ENDPOINT, payload, HTTP_HOST=API_HOST)


class TestCustomJSONRedirectEndpoint:
    """``ProviderRedirectAPIView`` -- the endpoint the web SPA calls."""

    def test_spa_callback_url_reaches_the_provider(self, ecs_style_settings):
        response = _post_json(PAYLOAD)

        assert response.status_code == 200, response.content
        assert response.json()["redirect_url"].startswith(GOOGLE_AUTH_URL)

    def test_without_the_frontend_origin_the_same_request_is_refused(self, ecs_style_settings):
        """The staging failure: 400 with ``Invalid URL.`` on the callback_url field."""
        ecs_style_settings.CSRF_TRUSTED_ORIGINS = []

        response = _post_json(PAYLOAD)

        assert response.status_code == 400
        assert response.json() == {"callback_url": ["Invalid URL."]}

    def test_foreign_callback_url_is_still_refused(self, ecs_style_settings):
        """The open redirect Render's ``ALLOWED_HOSTS=*`` used to allow."""
        response = _post_json({**PAYLOAD, "callback_url": "https://evil.example.com/callback"})

        assert response.status_code == 400
        assert response.json() == {"callback_url": ["Invalid URL."]}


class TestStockAllauthRedirectEndpoint:
    """allauth's own ``RedirectToProviderView``, which answers with a 302."""

    def test_spa_callback_url_reaches_the_provider(self, ecs_style_settings):
        response = _post_form(PAYLOAD)

        assert response.status_code == 302, response.content
        assert response["Location"].startswith(GOOGLE_AUTH_URL), response["Location"]

    def test_without_the_frontend_origin_the_same_request_is_refused(self, ecs_style_settings):
        """Here a rejected callback_url sends the user to the social login error page
        rather than answering with an error, so the browser never reaches Google."""
        ecs_style_settings.CSRF_TRUSTED_ORIGINS = []

        response = _post_form(PAYLOAD)

        assert response.status_code == 302
        assert "error=unknown" in response["Location"], response["Location"]
        assert "accounts.google.com" not in response["Location"]

    def test_foreign_callback_url_is_still_refused(self, ecs_style_settings):
        response = _post_form({**PAYLOAD, "callback_url": "https://evil.example.com/callback"})

        assert response.status_code == 302
        assert "error=unknown" in response["Location"], response["Location"]
        assert "evil.example.com" not in response["Location"]
        assert "accounts.google.com" not in response["Location"]
