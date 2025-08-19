import hashlib
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest

import jwt
import pytest

from accounts.models import RefreshToken
from accounts.token_strategies import AccessAndRefreshTokenStrategy


User = get_user_model()


@pytest.fixture
def strategy():
    return AccessAndRefreshTokenStrategy()


@pytest.mark.django_db
def test_generate_access_token_str(strategy, user):
    token = strategy._generate_access_token_str(user.id)
    assert isinstance(token, str)
    # Should be a valid JWT (SimpleJWT AccessToken)
    payload = jwt.decode(
        token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert payload["user_id"] == user.id


@pytest.mark.django_db
def test_generate_access_token_only_authenticated(strategy, user):
    request = HttpRequest()
    request.user = user  # user fixture is authenticated
    result = strategy.generate_access_token_only(request)
    assert "access_token" in result
    payload = jwt.decode(
        result["access_token"],
        settings.SECRET_KEY,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert payload["user_id"] == user.id


@pytest.mark.django_db
def test_generate_access_token_only_unauthenticated(strategy):
    request = HttpRequest()
    request.user = MagicMock(is_authenticated=False)
    with pytest.raises(ValueError):
        strategy.generate_access_token_only(request)


@pytest.mark.django_db
def test_create_access_token_authenticated(strategy, user, settings):
    request = HttpRequest()
    request.user = user  # user fixture is authenticated
    request.headers = {
        "user-agent": "pytest-agent",
        "x-device-name": "pytest-device",
        "x-device-id": "pytest-id",
        "x-operating-system": "pytest-os",
        "x-device-location-latitude": "1.23",
        "x-device-location-longitude": "4.56",
    }
    result = strategy.create_access_token(request)
    assert "access_token" in result
    assert "refresh_token" in result
    # Check refresh token is a valid JWT
    payload = jwt.decode(result["refresh_token"], settings.SECRET_KEY, algorithms=["HS256"])
    assert payload["user_id"] == user.id
    assert "refresh_token" in payload
    # Check RefreshToken model instance was created
    token_hash = hashlib.sha256(payload["refresh_token"].encode()).hexdigest()
    assert RefreshToken.objects.filter(user=user, token_hash=token_hash).exists()


@pytest.mark.django_db
def test_create_access_token_unauthenticated(strategy):
    request = HttpRequest()
    request.user = MagicMock(is_authenticated=False)
    request.headers = {}
    with pytest.raises(ValueError):
        strategy.create_access_token(request)


@pytest.mark.django_db
def test_create_access_token_payload_delegates(strategy):
    # Should call super().create_access_token_payload
    request = MagicMock()
    with patch(
        "allauth.headless.tokens.sessions.SessionTokenStrategy.create_access_token_payload",
        return_value=None,
    ) as super_method:
        result = strategy.create_access_token_payload(request)
        super_method.assert_called_once_with(request)
        assert result is None
