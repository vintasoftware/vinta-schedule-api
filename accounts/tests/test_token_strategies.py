import datetime
import hashlib
from unittest.mock import MagicMock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest

import jwt
import pytest

from accounts.models import RefreshToken
from accounts.token_strategies import AccessAndRefreshTokenStrategy


User = get_user_model()


DEVICE_HEADERS = {
    "user-agent": "pytest-agent",
    "x-device-name": "pytest-device",
    "x-device-id": "pytest-id",
    "x-operating-system": "pytest-os",
    "x-device-location-latitude": "1.23",
    "x-device-location-longitude": "4.56",
}


@pytest.fixture
def strategy():
    return AccessAndRefreshTokenStrategy()


def _authenticated_request(user, headers=None):
    request = HttpRequest()
    request.user = user
    request.headers = headers or {}
    return request


@pytest.mark.django_db
def test_generate_access_token_str(strategy, user):
    token = strategy._generate_access_token_str(user.id)
    assert isinstance(token, str)
    # Should be a valid JWT (SimpleJWT AccessToken)
    payload = jwt.decode(
        token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
    )
    # SimpleJWT serializes the user id claim as a string.
    assert payload["user_id"] == str(user.id)


@pytest.mark.django_db
def test_create_access_token_returns_jwt_string(strategy, user):
    """Native contract: create_access_token returns the access token *string*."""
    request = _authenticated_request(user, DEVICE_HEADERS)
    token = strategy.create_access_token(request)
    assert isinstance(token, str)
    payload = jwt.decode(
        token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert payload["user_id"] == str(user.id)
    # create_access_token alone must NOT mint a refresh token.
    assert not RefreshToken.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_create_access_token_unauthenticated(strategy):
    request = HttpRequest()
    request.user = MagicMock(is_authenticated=False)
    assert strategy.create_access_token(request) is None


@pytest.mark.django_db
def test_create_access_token_payload_attaches_refresh(strategy, user):
    """Payload exposes a flat access_token + refresh_token and records device metadata."""
    request = _authenticated_request(user, DEVICE_HEADERS)
    payload = strategy.create_access_token_payload(request)

    assert set(payload) == {"access_token", "refresh_token"}
    assert isinstance(payload["access_token"], str)

    refresh_claims = jwt.decode(payload["refresh_token"], settings.SECRET_KEY, algorithms=["HS256"])
    assert refresh_claims["user_id"] == user.id

    token_hash = hashlib.sha256(refresh_claims["refresh_token"].encode()).hexdigest()
    instance = RefreshToken.objects.get(user=user, token_hash=token_hash)
    assert instance.user_agent == "pytest-agent"
    assert instance.device_id == "pytest-id"
    assert instance.operational_system == "pytest-os"


@pytest.mark.django_db
def test_create_access_token_payload_unauthenticated_returns_none(strategy):
    request = HttpRequest()
    request.user = MagicMock(is_authenticated=False)
    request.headers = {}
    assert strategy.create_access_token_payload(request) is None


@pytest.mark.django_db
def test_refresh_token_rotates_and_preserves_device(strategy, user):
    device = {
        "user_agent": "pytest-agent",
        "device_name": "pytest-device",
        "device_id": "pytest-id",
        "operational_system": "pytest-os",
        "latitude": 1.23,
        "longitude": 4.56,
    }
    original = strategy._issue_refresh_token(user, device)
    original_hash = RefreshToken.objects.get(user=user).token_hash

    result = strategy.refresh_token(original)
    assert result is not None
    access_token, next_refresh_token = result

    # New access token is valid for the user.
    access_claims = jwt.decode(
        access_token, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert access_claims["user_id"] == str(user.id)

    # Old token is invalidated (single-use rotation), exactly one row remains.
    rows = RefreshToken.objects.filter(user=user)
    assert rows.count() == 1
    rotated = rows.get()
    assert rotated.token_hash != original_hash

    # Device metadata carried over to the rotated token.
    assert rotated.user_agent == "pytest-agent"
    assert rotated.device_id == "pytest-id"
    assert rotated.latitude == 1.23

    # The next refresh token is the one now stored.
    next_claims = jwt.decode(next_refresh_token, settings.SECRET_KEY, algorithms=["HS256"])
    next_hash = hashlib.sha256(next_claims["refresh_token"].encode()).hexdigest()
    assert next_hash == rotated.token_hash


@pytest.mark.django_db
def test_refresh_token_invalid_returns_none(strategy):
    assert strategy.refresh_token("invalid.token.value") is None


@pytest.mark.django_db
def test_refresh_token_unknown_secret_returns_none(strategy, user):
    # Validly signed envelope, but no matching RefreshToken row.
    token = jwt.encode(
        {
            "user_id": user.id,
            "refresh_token": "does-not-exist",
            "exp": datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=1),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    assert strategy.refresh_token(token) is None


@pytest.mark.django_db
def test_refresh_token_expired_returns_none_and_deletes(strategy, user):
    secret = "expiredtokenstring"
    RefreshToken.objects.create(
        user=user,
        token_hash=hashlib.sha256(secret.encode()).hexdigest(),
        expires_at=datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1),
    )
    token = jwt.encode(
        {
            "user_id": user.id,
            "refresh_token": secret,
            "exp": datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=30),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    assert strategy.refresh_token(token) is None
    assert not RefreshToken.objects.filter(user=user).exists()


def test_socialaccount_store_tokens_enabled():
    """allauth must persist OAuth tokens (SocialToken) for the calendar integration.

    Regression: allauth defaults SOCIALACCOUNT_STORE_TOKENS to False since v65,
    which silently drops access/refresh tokens after login. Without stored
    tokens the Google/Microsoft calendar import has nothing to authenticate with.
    """
    from allauth.socialaccount import app_settings

    assert settings.SOCIALACCOUNT_STORE_TOKENS is True
    assert app_settings.STORE_TOKENS is True
