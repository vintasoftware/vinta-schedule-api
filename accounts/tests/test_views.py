import datetime
import hashlib
import json
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse

import jwt
import pytest
from allauth.socialaccount.providers.base import ProviderException

from accounts.models import RefreshToken
from accounts.token_strategies import AccessAndRefreshTokenStrategy


class TestProviderCallbackAPIView:
    @staticmethod
    def get_url():
        return reverse("provider_callback_json")

    @pytest.mark.django_db
    def test_missing_provider_id(self, client):
        data = {"code": "dummy_code"}
        response = client.post(
            self.get_url(), data=json.dumps(data), content_type="application/json"
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Provider ID is required."

    @pytest.mark.django_db
    def test_error_in_data(self, client):
        data = {"provider_id": "test", "error": "some_error"}
        with patch("accounts.views.get_socialaccount_adapter") as mock_adapter:
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.login_cancelled_error = "cancelled"
            response = client.post(
                self.get_url(), data=json.dumps(data), content_type="application/json"
            )
        assert response.status_code == 400
        assert "error" in response.json()
        assert "message" in response.json()

    @pytest.mark.django_db
    def test_success(self, client):
        data = {"provider_id": "test", "code": "dummy_code", "state": "dummy_state"}
        with (
            patch("accounts.views.get_socialaccount_adapter") as mock_adapter,
            patch("accounts.views.statekit.unstash_state", return_value={"next": "/callback/"}),
            patch("accounts.views.complete_social_login", return_value=JsonResponse({"ok": True})),
        ):
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.supports_state = True
            mock_oauth2_adapter.parse_token.return_value = MagicMock()
            mock_oauth2_adapter.complete_login.return_value = MagicMock()
            mock_client.get_access_token.return_value = {"access_token": "token"}
            mock_client.callback_url = "/callback/"
            mock_provider.app = mock_app
            response = client.post(
                self.get_url(), data=json.dumps(data), content_type="application/json"
            )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    @pytest.mark.django_db
    def test_authentication_failed(self, client):
        data = {"provider_id": "test", "code": "dummy_code", "state": "dummy_state"}
        with (
            patch("accounts.views.get_socialaccount_adapter") as mock_adapter,
            patch("accounts.views.statekit.unstash_state", return_value={"next": "/callback/"}),
        ):
            mock_app = MagicMock()
            mock_provider = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_app.get_provider.return_value = mock_provider
            mock_adapter.return_value.get_app.return_value = mock_app
            mock_oauth2_adapter.supports_state = True
            mock_oauth2_adapter.parse_token.side_effect = ProviderException("fail")
            mock_client.get_access_token.return_value = {"access_token": "token"}
            mock_client.callback_url = "/callback/"
            mock_provider.app = mock_app
            response = client.post(
                self.get_url(), data=json.dumps(data), content_type="application/json"
            )
        assert response.status_code == 400
        assert "error" in response.json()
        assert "message" in response.json()


class TestRefreshTokenView:
    @staticmethod
    def get_url():
        return reverse("refresh_token")

    @pytest.mark.django_db
    def test_success(self, client, user):
        # Generate a refresh token for the user
        strategy = AccessAndRefreshTokenStrategy()

        class DummyRequest:
            def __init__(self, user):
                self.user = user
                self.headers = {}

        request = DummyRequest(user)
        tokens = strategy.create_access_token(request)
        refresh_token = tokens["refresh_token"]

        response = client.post(self.get_url(), {"refresh_token": refresh_token}, format="json")
        assert response.status_code == 200
        assert "access_token" in response.data

    @pytest.mark.django_db
    def test_invalid(self, client):
        # Use an invalid refresh token
        response = client.post(
            self.get_url(), {"refresh_token": "invalid.token.value"}, format="json"
        )
        assert response.status_code == 400
        assert "refresh_token" in response.data

    @pytest.mark.django_db
    def test_expired(self, client, user):
        # Generate a refresh token with an expired date
        refresh_token_str = "expiredtokenstring"
        hashed_refresh_token = hashlib.sha256(refresh_token_str.encode()).hexdigest()
        expired_at = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1)
        RefreshToken.objects.create(
            user=user,
            token_hash=hashed_refresh_token,
            expires_at=expired_at,
        )
        payload = {
            "user_id": user.id,
            "refresh_token": refresh_token_str,
            "exp": int(
                (datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=30)).timestamp()
            ),
        }
        refresh_token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
        response = client.post(self.get_url(), {"refresh_token": refresh_token}, format="json")
        assert response.status_code == 400
        assert "refresh_token" in response.data


class TestProviderRedirectAPIView:
    @staticmethod
    def get_url():
        from django.urls import reverse

        return reverse("provider_redirect_json")

    @pytest.mark.django_db
    def test_invalid_form(self, client):
        # Missing required fields
        data = {}
        response = client.post(
            self.get_url(), data=json.dumps(data), content_type="application/json"
        )
        assert response.status_code == 400
        assert isinstance(response.json(), dict)
        assert any(k in response.json() for k in ["provider", "callback_url", "process"])

    @pytest.mark.django_db
    def test_success(self, client):
        # Patch all external/provider logic
        with (
            patch("accounts.views.RedirectToProviderForm") as mock_form,
            patch("accounts.views.get_socialaccount_adapter"),
        ):
            mock_form_instance = mock_form.return_value
            mock_form_instance.is_valid.return_value = True
            mock_provider = MagicMock()
            mock_app = MagicMock()
            mock_oauth2_adapter = MagicMock()
            mock_client = MagicMock()
            # Setup cleaned_data
            mock_form_instance.cleaned_data = {
                "provider": mock_provider,
                "callback_url": "https://callback/",
                "process": "login",
            }
            mock_provider.app = mock_app
            mock_provider.get_oauth2_adapter.return_value = mock_oauth2_adapter
            mock_oauth2_adapter.get_client.return_value = mock_client
            mock_provider.get_auth_params.return_value = {"foo": "bar"}
            mock_provider.get_pkce_params.return_value = {"code_verifier": "verifier"}
            mock_provider.get_scope.return_value = ["email"]
            mock_provider.stash_redirect_state.return_value = "stateid"
            mock_client.get_redirect_url.return_value = "https://provider/redirect"
            mock_oauth2_adapter.authorize_url = "https://provider/auth"

            # Simulate session
            class DummySession(dict):
                def save(self):
                    self["session_key"] = "sessiontoken"

                @property
                def session_key(self):
                    return self.get("session_key", "sessiontoken")

            # Patch request.session
            from django.test.client import RequestFactory

            rf = RequestFactory()
            request = rf.post(self.get_url(), data=json.dumps({}), content_type="application/json")
            request.session = DummySession()
            # Actually call the view
            from accounts.views import ProviderRedirectAPIView

            view = ProviderRedirectAPIView.as_view()
            response = view(request)
            assert response.status_code == 200
            data = json.loads(response.content)
            assert data["redirect_url"] == "https://provider/redirect"
            assert data["session_token"] == "sessiontoken"
