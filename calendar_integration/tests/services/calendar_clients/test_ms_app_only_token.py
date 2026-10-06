"""Unit tests for the app-only Microsoft token provider: request shape, cache, safe logs."""

import base64
import json
import logging
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests
from redis.exceptions import RedisError

from calendar_integration.exceptions import (
    MicrosoftAppOnlyTokenError,
    MicrosoftConnectionNotConfiguredError,
    MicrosoftSignInError,
    MicrosoftSignInNotAdminError,
)
from calendar_integration.services.calendar_clients import ms_app_only_token
from calendar_integration.services.calendar_clients.ms_app_only_token import (
    MicrosoftAppOnlyToken,
    MicrosoftAppOnlyTokenProvider,
    decode_roles,
)


TENANT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "vinta-client-id"
CLIENT_SECRET = "super-secret-value-never-logged"  # noqa: S105 - test value


def make_jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature-part"


ACCESS_TOKEN = make_jwt({"roles": ["Place.ReadWrite.All", "Calendars.Read"], "tid": TENANT_ID})


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str, ex: int) -> bool:
        self.values[name] = value
        self.ttls[name] = ex
        return True


class BrokenCache:
    def get(self, name: str) -> Any:
        raise RedisError("down")

    def set(self, name: str, value: str, ex: int) -> Any:
        raise RedisError("down")


def token_response(access_token: str = ACCESS_TOKEN, expires_in: int = 3600) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {
        "token_type": "Bearer",
        "expires_in": expires_in,
        "access_token": access_token,
    }
    return response


def error_response(status_code: int, error: str) -> Mock:
    response = Mock(status_code=status_code)
    response.json.return_value = {
        "error": error,
        "error_description": f"AADSTS7000215: Invalid client secret {CLIENT_SECRET}",
        "error_codes": [7000215],
    }
    return response


def make_provider(
    cache: Any = None, clock: FakeClock | None = None
) -> MicrosoftAppOnlyTokenProvider:
    return MicrosoftAppOnlyTokenProvider(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        cache_getter=lambda: cache,
        clock=clock or FakeClock(),
    )


@pytest.fixture
def post():
    with patch.object(ms_app_only_token.requests, "post") as mock_post:
        yield mock_post


class TestTokenRequest:
    def test_posts_client_credentials_to_the_tenant_token_endpoint(self, post):
        post.return_value = token_response()

        make_provider().get_token(TENANT_ID)

        post.assert_called_once_with(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=ms_app_only_token.REQUEST_TIMEOUT_SECONDS,
        )

    def test_returns_the_token_its_roles_and_expiry(self, post):
        post.return_value = token_response(expires_in=3599)
        clock = FakeClock(now=5_000.0)

        token = make_provider(clock=clock).get_token(TENANT_ID)

        assert token == MicrosoftAppOnlyToken(
            access_token=ACCESS_TOKEN,
            roles=frozenset({"Place.ReadWrite.All", "Calendars.Read"}),
            expires_at=8_599.0,
        )

    def test_token_is_hidden_from_repr(self, post):
        post.return_value = token_response()

        token = make_provider().get_token(TENANT_ID)

        assert ACCESS_TOKEN not in repr(token)

    def test_error_response_raises_with_status_and_oauth_error_code(self, post):
        post.return_value = error_response(401, "invalid_client")

        with pytest.raises(MicrosoftAppOnlyTokenError) as exc_info:
            make_provider().get_token(TENANT_ID)

        assert (exc_info.value.status_code, exc_info.value.error_code) == (401, "invalid_client")
        assert CLIENT_SECRET not in str(exc_info.value)

    def test_network_error_raises_token_error(self, post):
        post.side_effect = requests.ConnectionError(f"boom {CLIENT_SECRET}")

        with pytest.raises(MicrosoftAppOnlyTokenError) as exc_info:
            make_provider().get_token(TENANT_ID)

        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True

    def test_missing_access_token_raises(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"expires_in": 3600}
        post.return_value = response

        with pytest.raises(MicrosoftAppOnlyTokenError):
            make_provider().get_token(TENANT_ID)

    @pytest.mark.parametrize(
        "tenant_id",
        ["", "contoso.onmicrosoft.com", "../common", f"{TENANT_ID}/../x", "organizations"],
    )
    def test_rejects_anything_but_a_tenant_guid_without_a_request(self, post, tenant_id):
        with pytest.raises(MicrosoftAppOnlyTokenError):
            make_provider().get_token(tenant_id)

        post.assert_not_called()

    @pytest.mark.parametrize(("client_id", "client_secret"), [("", "secret"), ("id", "")])
    def test_unconfigured_app_raises_without_a_request(self, post, client_id, client_secret):
        provider = MicrosoftAppOnlyTokenProvider(
            client_id=client_id, client_secret=client_secret, cache_getter=lambda: None
        )

        with pytest.raises(MicrosoftConnectionNotConfiguredError):
            provider.get_token(TENANT_ID)

        post.assert_not_called()

    def test_reads_credentials_from_settings_by_default(self, settings):
        settings.MS_CLIENT_ID = "from-settings-id"
        settings.MS_CLIENT_SECRET = "from-settings-secret"  # noqa: S105 - test value

        provider = MicrosoftAppOnlyTokenProvider(cache_getter=lambda: None)

        assert (provider.client_id, provider.is_configured) == ("from-settings-id", True)


class TestTokenCache:
    def test_second_call_is_served_from_the_cache(self, post):
        post.return_value = token_response()
        cache = FakeCache()
        provider = make_provider(cache=cache)

        first = provider.get_token(TENANT_ID)
        second = provider.get_token(TENANT_ID)

        assert first == second
        assert post.call_count == 1

    def test_cache_entry_lives_until_five_minutes_before_expiry(self, post):
        post.return_value = token_response(expires_in=3600)
        cache = FakeCache()

        make_provider(cache=cache).get_token(TENANT_ID)

        key = f"ms_app_only_token:{CLIENT_ID}:{TENANT_ID}"
        assert cache.ttls == {key: 3300}
        assert json.loads(cache.values[key]) == {
            "access_token": ACCESS_TOKEN,
            "roles": ["Calendars.Read", "Place.ReadWrite.All"],
            "expires_at": 1_003_600.0,
        }

    def test_token_inside_the_expiry_margin_is_refreshed(self, post):
        post.return_value = token_response(expires_in=3600)
        cache = FakeCache()
        clock = FakeClock()
        provider = make_provider(cache=cache, clock=clock)
        provider.get_token(TENANT_ID)

        clock.now += 3600 - 300
        provider.get_token(TENANT_ID)

        assert post.call_count == 2

    def test_token_just_outside_the_margin_is_still_cached(self, post):
        post.return_value = token_response(expires_in=3600)
        clock = FakeClock()
        provider = make_provider(cache=FakeCache(), clock=clock)
        provider.get_token(TENANT_ID)

        clock.now += 3600 - 301
        provider.get_token(TENANT_ID)

        assert post.call_count == 1

    def test_forced_refresh_skips_the_cache_and_replaces_its_entry(self, post):
        old_token = make_jwt({"roles": ["Calendars.Read"]})
        cache = FakeCache()
        provider = make_provider(cache=cache)
        post.return_value = token_response(access_token=old_token)
        provider.get_token(TENANT_ID)

        post.return_value = token_response()
        refreshed = provider.get_token(TENANT_ID, force_refresh=True)
        served_next = provider.get_token(TENANT_ID)

        assert post.call_count == 2
        assert refreshed.access_token == ACCESS_TOKEN
        assert served_next == refreshed

    def test_short_lived_token_is_not_cached(self, post):
        post.return_value = token_response(expires_in=200)
        cache = FakeCache()

        make_provider(cache=cache).get_token(TENANT_ID)

        assert cache.values == {}

    def test_tenants_do_not_share_a_cache_entry(self, post):
        post.return_value = token_response()
        cache = FakeCache()
        provider = make_provider(cache=cache)

        provider.get_token(TENANT_ID)
        provider.get_token("99999999-2222-3333-4444-555555555555")

        assert post.call_count == 2

    def test_without_redis_every_call_asks_microsoft(self, post):
        post.return_value = token_response()
        provider = make_provider(cache=None)

        provider.get_token(TENANT_ID)
        provider.get_token(TENANT_ID)

        assert post.call_count == 2

    def test_redis_errors_fall_back_to_a_fresh_token(self, post):
        post.return_value = token_response()

        with patch.object(ms_app_only_token.redis_breaker, "allows_request", return_value=True):
            token = make_provider(cache=BrokenCache()).get_token(TENANT_ID)

        assert token.access_token == ACCESS_TOKEN


class TestSecretsStayOutOfLogs:
    def test_success_error_and_network_paths_never_log_secret_or_token(self, post, caplog):
        caplog.set_level(logging.DEBUG)
        provider = make_provider(cache=FakeCache())

        post.return_value = token_response()
        provider.get_token(TENANT_ID)
        post.return_value = error_response(401, "invalid_client")
        with pytest.raises(MicrosoftAppOnlyTokenError):
            make_provider().get_token(TENANT_ID)
        post.side_effect = requests.ConnectionError(f"boom {CLIENT_SECRET} {ACCESS_TOKEN}")
        with pytest.raises(MicrosoftAppOnlyTokenError):
            make_provider().get_token(TENANT_ID)

        assert caplog.records, "the error paths are expected to log something"
        assert CLIENT_SECRET not in caplog.text
        assert ACCESS_TOKEN not in caplog.text
        assert "AADSTS" not in caplog.text


class TestDecodeRoles:
    def test_reads_the_roles_claim(self):
        assert decode_roles(make_jwt({"roles": ["A", "B", 3]})) == frozenset({"A", "B"})

    @pytest.mark.parametrize(
        "token",
        ["", "opaque", "a.!!!.c", make_jwt({"scp": "User.Read"}), make_jwt({"roles": "A"})],
    )
    def test_anything_else_has_no_roles(self, token):
        assert decode_roles(token) == frozenset()


GLOBAL_ADMINISTRATOR = "62e90394-69f5-4237-9190-012177145e10"
PRIVILEGED_ROLE_ADMINISTRATOR = "e8611ab8-c189-46e8-94e1-60213ab1f814"
APPLICATION_ADMINISTRATOR = "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3"


def sign_in_response(**claims: Any) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {
        "id_token": make_jwt(
            {
                "aud": CLIENT_ID,
                "nonce": "nonce-1",
                "tid": TENANT_ID,
                "wids": [GLOBAL_ADMINISTRATOR],
                **claims,
            }
        ),
        "access_token": ACCESS_TOKEN,
    }
    return response


class TestTenantFromSignIn:
    REDIRECT_URI = "https://api.example.com/calendar/microsoft-connection/callback/"

    def test_redeems_the_code_with_the_client_secret(self, post):
        post.return_value = sign_in_response()

        make_provider().tenant_from_sign_in(
            "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
        )

        post.assert_called_once_with(
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": "auth-code",
                "redirect_uri": self.REDIRECT_URI,
                "scope": "openid https://graph.microsoft.com/.default",
            },
            timeout=ms_app_only_token.REQUEST_TIMEOUT_SECONDS,
        )

    def test_returns_the_tid_of_the_id_token(self, post):
        post.return_value = sign_in_response(tid=TENANT_ID.upper())

        tenant = make_provider().tenant_from_sign_in(
            "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
        )

        assert tenant == TENANT_ID

    @pytest.mark.parametrize(
        "claims",
        [
            {"aud": "another-app"},
            {"nonce": "another-attempt"},
            {"tid": None},
            {"tid": "contoso.onmicrosoft.com"},
        ],
    )
    def test_id_token_not_issued_for_this_attempt_is_refused(self, post, claims):
        post.return_value = sign_in_response(**claims)

        with pytest.raises(MicrosoftSignInError):
            make_provider().tenant_from_sign_in(
                "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
            )

    def test_privileged_role_administrator_may_sign_in(self, post):
        post.return_value = sign_in_response(
            wids=[APPLICATION_ADMINISTRATOR, PRIVILEGED_ROLE_ADMINISTRATOR]
        )

        tenant = make_provider().tenant_from_sign_in(
            "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
        )

        assert tenant == TENANT_ID

    @pytest.mark.parametrize(
        "claims",
        [
            {"wids": []},
            {"wids": [APPLICATION_ADMINISTRATOR]},
            {"wids": GLOBAL_ADMINISTRATOR},
            {"wids": None},
        ],
    )
    def test_sign_in_by_a_non_administrator_is_refused(self, post, claims):
        post.return_value = sign_in_response(**claims)

        with pytest.raises(MicrosoftSignInNotAdminError):
            make_provider().tenant_from_sign_in(
                "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
            )

    def test_answer_without_an_id_token_is_refused(self, post):
        post.return_value = token_response()

        with pytest.raises(MicrosoftSignInError):
            make_provider().tenant_from_sign_in(
                "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
            )

    def test_refused_code_raises(self, post):
        post.return_value = error_response(400, "invalid_grant")

        with pytest.raises(MicrosoftSignInError):
            make_provider().tenant_from_sign_in(
                "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
            )

    def test_network_error_raises(self, post):
        post.side_effect = requests.ConnectionError("down")

        with pytest.raises(MicrosoftSignInError):
            make_provider().tenant_from_sign_in(
                "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
            )

    def test_unconfigured_app_raises_without_a_request(self, post):
        provider = MicrosoftAppOnlyTokenProvider(
            client_id="", client_secret="", cache_getter=lambda: None
        )

        with pytest.raises(MicrosoftConnectionNotConfiguredError):
            provider.tenant_from_sign_in(
                "auth-code", redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
            )

        post.assert_not_called()

    def test_secret_code_and_tokens_never_reach_the_logs(self, post, caplog):
        caplog.set_level(logging.DEBUG)
        code = "secret-authorization-code"
        for answer in (
            error_response(400, "invalid_grant"),
            sign_in_response(aud="another-app"),
        ):
            post.return_value = answer
            with pytest.raises(MicrosoftSignInError):
                make_provider().tenant_from_sign_in(
                    code, redirect_uri=self.REDIRECT_URI, nonce="nonce-1"
                )

        assert caplog.records, "the error paths are expected to log something"
        for value in (CLIENT_SECRET, code, ACCESS_TOKEN, "AADSTS"):
            assert value not in caplog.text
