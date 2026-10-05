"""Integration tests for MicrosoftConnectionService: the consent round trip and verify."""

import base64
import contextlib
import datetime
import json
import urllib.parse
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock, patch

from django.core import signing
from django.utils import timezone

import pytest
import requests
from freezegun import freeze_time
from model_bakery import baker

from calendar_integration.exceptions import (
    MicrosoftAppOnlyTokenError,
    MicrosoftConnectionNotConfiguredError,
    MicrosoftConsentDeniedError,
    MicrosoftConsentStateError,
    MicrosoftSignInError,
    MicrosoftSignInNotAdminError,
)
from calendar_integration.factories import create_microsoft_organization_connection
from calendar_integration.models import MicrosoftOrganizationConnection
from calendar_integration.services import microsoft_connection_service as service_module
from calendar_integration.services.calendar_clients import ms_app_only_token as token_module
from calendar_integration.services.calendar_clients.ms_app_only_token import (
    MicrosoftAppOnlyToken,
    MicrosoftAppOnlyTokenProvider,
)
from calendar_integration.services.microsoft_connection_service import (
    CONSENT_STATE_SALT,
    EXCHANGE_RBAC_REMINDER,
    NOT_CONSENTED_MESSAGE,
    MicrosoftConnectionService,
    MicrosoftConnectionVerification,
)
from common.organization_context import organization_context
from organizations.models import Organization


TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REDIRECT_URI = "https://api.example.com/calendar/microsoft-connection/callback/"
BOTH_ROLES = frozenset({"Place.ReadWrite.All", "Calendars.Read"})


def make_service() -> MicrosoftConnectionService:
    provider = MicrosoftAppOnlyTokenProvider(
        client_id="vinta-client-id",
        client_secret="vinta-client-secret",  # noqa: S106 - test value
        cache_getter=lambda: None,
    )
    return MicrosoftConnectionService(token_provider=provider)


def connection_of(organization: Organization) -> MicrosoftOrganizationConnection:
    return MicrosoftOrganizationConnection.objects.filter_by_organization(organization).get()


def state_from(consent_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(consent_url).query)["state"][0]


def tamper(state: str) -> str:
    """Flip one character of the signed payload, keeping the signature as it was."""
    index = len(state) // 3
    replacement = "A" if state[index] != "A" else "B"
    return state[:index] + replacement + state[index + 1 :]


def app_token(roles: frozenset[str] = BOTH_ROLES) -> MicrosoftAppOnlyToken:
    return MicrosoftAppOnlyToken(access_token="app-token", roles=roles, expires_at=0.0)


def graph_response(status_code: int) -> Mock:
    return Mock(status_code=status_code)


def id_token(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


class InMemoryCache:
    """Stands in for Redis behind a real token provider."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str, ex: int) -> bool:
        self.values[name] = value
        return True


def token_endpoint_response(roles: list[str]) -> Mock:
    """Microsoft's client-credentials answer: an hour-long token carrying ``roles``."""
    response = Mock(status_code=200)
    response.json.return_value = {
        "access_token": id_token({"roles": roles}),
        "expires_in": 3600,
    }
    return response


GLOBAL_ADMINISTRATOR = "62e90394-69f5-4237-9190-012177145e10"


@contextlib.contextmanager
def microsoft_sign_in(state: str, tid: str | None, **overrides: Any) -> Iterator[Mock]:
    """Answer the code redemption the way Microsoft would for an admin of ``tid``."""
    claims = {
        "aud": "vinta-client-id",
        "nonce": signing.loads(state, salt=CONSENT_STATE_SALT)["nonce"],
        "tid": tid,
        "wids": [GLOBAL_ADMINISTRATOR],
        **overrides,
    }
    response = Mock(status_code=200)
    response.json.return_value = {"id_token": id_token(claims), "access_token": "delegated"}
    with patch.object(token_module.requests, "post", return_value=response) as post:
        yield post


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization)


@pytest.fixture
def service() -> MicrosoftConnectionService:
    return make_service()


@pytest.mark.django_db
class TestBuildConsentUrl:
    def test_url_asks_an_admin_to_sign_in_and_consent_with_a_signed_state(
        self, service, organization
    ):
        consent_url = service.build_consent_url(organization, redirect_uri=REDIRECT_URI)

        parts = urllib.parse.urlsplit(consent_url)
        query = urllib.parse.parse_qs(parts.query)
        nonce = connection_of(organization).consent_state
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
        )
        assert {key: values for key, values in query.items() if key != "state"} == {
            "client_id": ["vinta-client-id"],
            "response_type": ["code"],
            "response_mode": ["query"],
            "redirect_uri": [REDIRECT_URI],
            "scope": ["openid https://graph.microsoft.com/.default"],
            "prompt": ["admin_consent"],
            "nonce": [nonce],
        }
        payload = signing.loads(query["state"][0], salt=CONSENT_STATE_SALT)
        assert payload == {"org": organization.pk, "nonce": nonce}

    def test_creates_the_connection_with_only_a_nonce(self, service, organization):
        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)

        connection = connection_of(organization)
        assert len(connection.consent_state) >= 32
        assert (connection.tenant_id, connection.consented_at, connection.write_enabled) == (
            "",
            None,
            False,
        )

    def test_a_new_link_replaces_the_nonce_of_the_previous_one(self, service, organization):
        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)
        first_nonce = connection_of(organization).consent_state

        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)

        assert connection_of(organization).consent_state != first_nonce
        assert (
            MicrosoftOrganizationConnection.objects.filter_by_organization(organization).count()
            == 1
        )

    def test_unconfigured_app_raises(self, organization):
        service = MicrosoftConnectionService(
            token_provider=MicrosoftAppOnlyTokenProvider(
                client_id="", client_secret="", cache_getter=lambda: None
            )
        )

        with pytest.raises(MicrosoftConnectionNotConfiguredError):
            service.build_consent_url(organization, redirect_uri=REDIRECT_URI)


@pytest.mark.django_db
class TestCompleteConsent:
    def test_stores_the_tenant_from_the_id_token_and_uses_up_the_nonce(self, service, organization):
        with freeze_time("2026-10-05 12:00:00"):
            state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))
            with microsoft_sign_in(state, tid=TENANT_ID.upper()) as post:
                service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        connection = connection_of(organization)
        assert (
            connection.tenant_id,
            connection.consented_at,
            connection.consent_state,
            connection.write_enabled,
        ) == (TENANT_ID, datetime.datetime(2026, 10, 5, 12, tzinfo=datetime.UTC), "", False)
        post.assert_called_once_with(
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "vinta-client-id",
                "client_secret": "vinta-client-secret",
                "code": "auth-code",
                "redirect_uri": REDIRECT_URI,
                "scope": "openid https://graph.microsoft.com/.default",
            },
            timeout=token_module.REQUEST_TIMEOUT_SECONDS,
        )

    def test_new_consent_resets_a_previous_verification(self, service, organization):
        with organization_context(organization):
            create_microsoft_organization_connection(
                organization=organization,
                tenant_id="00000000-0000-0000-0000-000000000001",
                write_enabled=True,
                verified_at=timezone.now(),
                last_verification_error="old",
            )
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))

        with microsoft_sign_in(state, tid=TENANT_ID):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        connection = connection_of(organization)
        assert (
            connection.tenant_id,
            connection.write_enabled,
            connection.verified_at,
            connection.last_verification_error,
        ) == (TENANT_ID, False, None, "")

    @pytest.mark.parametrize(
        "id_token_claims",
        [
            {"aud": "another-app"},
            {"nonce": "a-nonce-from-another-attempt"},
            {"tid": None},
            {"tid": "contoso.onmicrosoft.com"},
        ],
    )
    def test_id_token_not_issued_for_this_attempt_stores_nothing(
        self, service, organization, id_token_claims
    ):
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))

        with (
            microsoft_sign_in(state, **{"tid": TENANT_ID, **id_token_claims}),
            pytest.raises(MicrosoftSignInError),
        ):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        connection = connection_of(organization)
        assert (connection.tenant_id, connection.consent_state) == ("", "")

    def test_sign_in_by_a_non_administrator_stores_nothing(self, service, organization):
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))

        with (
            microsoft_sign_in(state, tid=TENANT_ID, wids=[]),
            pytest.raises(MicrosoftSignInNotAdminError),
        ):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        connection = connection_of(organization)
        assert (connection.tenant_id, connection.consented_at, connection.consent_state) == (
            "",
            None,
            "",
        )

    def test_refused_code_stores_nothing(self, service, organization):
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))
        refused = Mock(status_code=400)
        refused.json.return_value = {"error": "invalid_grant"}

        with (
            patch.object(token_module.requests, "post", return_value=refused),
            pytest.raises(MicrosoftSignInError),
        ):
            service.complete_consent(state, code="stolen-or-stale", redirect_uri=REDIRECT_URI)

        connection = connection_of(organization)
        assert (connection.tenant_id, connection.consent_state) == ("", "")

    def test_declined_consent_stores_nothing_but_uses_up_the_nonce(self, service, organization):
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))

        with (
            patch.object(token_module.requests, "post") as post,
            pytest.raises(MicrosoftConsentDeniedError),
        ):
            service.complete_consent(state, code="", redirect_uri=REDIRECT_URI)

        post.assert_not_called()
        connection = connection_of(organization)
        assert (connection.tenant_id, connection.consented_at, connection.consent_state) == (
            "",
            None,
            "",
        )

    def test_tampered_state_is_rejected(self, service, organization):
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))

        with (
            microsoft_sign_in(state, tid=TENANT_ID) as post,
            pytest.raises(MicrosoftConsentStateError),
        ):
            service.complete_consent(tamper(state), code="auth-code", redirect_uri=REDIRECT_URI)

        post.assert_not_called()
        assert connection_of(organization).tenant_id == ""

    def test_state_signed_with_another_salt_is_rejected(self, service, organization):
        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)
        nonce = connection_of(organization).consent_state
        forged = signing.dumps({"org": organization.pk, "nonce": nonce}, salt="another.purpose")

        with pytest.raises(MicrosoftConsentStateError):
            service.complete_consent(forged, code="auth-code", redirect_uri=REDIRECT_URI)

        assert connection_of(organization).tenant_id == ""

    def test_replayed_state_is_rejected(self, service, organization):
        state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))
        with microsoft_sign_in(state, tid=TENANT_ID):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        with (
            microsoft_sign_in(state, tid="99999999-9999-9999-9999-999999999999") as post,
            pytest.raises(MicrosoftConsentStateError),
        ):
            service.complete_consent(state, code="second-code", redirect_uri=REDIRECT_URI)

        post.assert_not_called()
        assert connection_of(organization).tenant_id == TENANT_ID

    def test_superseded_link_is_rejected(self, service, organization):
        old_state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))
        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)

        with pytest.raises(MicrosoftConsentStateError):
            service.complete_consent(old_state, code="auth-code", redirect_uri=REDIRECT_URI)

        assert connection_of(organization).tenant_id == ""

    def test_state_naming_another_organization_is_rejected(self, service, organization):
        other_organization = baker.make(Organization)
        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)
        service.build_consent_url(other_organization, redirect_uri=REDIRECT_URI)
        # A validly signed state that pairs this organization with the other one's nonce.
        cross_state = signing.dumps(
            {"org": organization.pk, "nonce": connection_of(other_organization).consent_state},
            salt=CONSENT_STATE_SALT,
        )

        with pytest.raises(MicrosoftConsentStateError):
            service.complete_consent(cross_state, code="auth-code", redirect_uri=REDIRECT_URI)

        assert connection_of(organization).tenant_id == ""
        assert connection_of(other_organization).tenant_id == ""
        assert connection_of(other_organization).consent_state != ""

    def test_state_for_an_organization_with_no_connection_is_rejected(self, service):
        never_asked = baker.make(Organization)
        state = signing.dumps({"org": never_asked.pk, "nonce": "guess"}, salt=CONSENT_STATE_SALT)

        with pytest.raises(MicrosoftConsentStateError):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        assert not MicrosoftOrganizationConnection.objects.filter_by_organization(
            never_asked
        ).exists()

    @pytest.mark.parametrize(
        "payload",
        [
            {"org": 1, "nonce": ""},
            {"org": True, "nonce": "x"},
            {"org": "1", "nonce": "x"},
            {"nonce": "x"},
            ["not", "a", "dict"],
        ],
    )
    def test_malformed_payload_is_rejected(self, service, payload):
        state = signing.dumps(payload, salt=CONSENT_STATE_SALT)

        with pytest.raises(MicrosoftConsentStateError):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

    def test_expired_state_is_rejected(self, service, organization):
        with freeze_time("2026-10-05 12:00:00"):
            state = state_from(service.build_consent_url(organization, redirect_uri=REDIRECT_URI))

        with (
            freeze_time("2026-10-05 13:00:01"),
            pytest.raises(MicrosoftConsentStateError),
        ):
            service.complete_consent(state, code="auth-code", redirect_uri=REDIRECT_URI)

        assert connection_of(organization).tenant_id == ""


@pytest.mark.django_db
class TestVerify:
    @pytest.fixture
    def connection(self, organization) -> MicrosoftOrganizationConnection:
        with organization_context(organization):
            return create_microsoft_organization_connection(
                organization=organization, tenant_id=TENANT_ID
            )

    def test_both_roles_and_a_places_read_enable_writes(self, service, organization, connection):
        with (
            patch.object(service.token_provider, "get_token", return_value=app_token()) as get,
            patch.object(
                service_module.requests, "get", return_value=graph_response(200)
            ) as graph_get,
            freeze_time("2026-10-05 12:00:00"),
        ):
            result = service.verify(organization)

        verified_at = datetime.datetime(2026, 10, 5, 12, tzinfo=datetime.UTC)
        assert result == MicrosoftConnectionVerification(
            write_enabled=True, verified_at=verified_at, error=""
        )
        get.assert_called_once_with(TENANT_ID, force_refresh=True)
        graph_get.assert_called_once_with(
            "https://graph.microsoft.com/v1.0/places/microsoft.graph.building",
            params={"$top": "1"},
            headers={"Authorization": "Bearer app-token"},
            timeout=service_module.GRAPH_REQUEST_TIMEOUT_SECONDS,
        )
        connection.refresh_from_db()
        assert (
            connection.write_enabled,
            connection.verified_at,
            connection.last_verification_error,
        ) == (True, verified_at, "")

    def test_missing_role_gives_a_remediation_message(self, service, organization, connection):
        with (
            patch.object(
                service.token_provider,
                "get_token",
                return_value=app_token(frozenset({"Calendars.Read"})),
            ),
            patch.object(service_module.requests, "get") as graph_get,
        ):
            result = service.verify(organization)

        expected_error = (
            "The Vinta Schedule app is missing these Microsoft Graph application "
            "permissions: Place.ReadWrite.All. Ask a Global Administrator to open the "
            "admin consent link again and accept every permission. "
            f"{EXCHANGE_RBAC_REMINDER}"
        )
        assert result == MicrosoftConnectionVerification(
            write_enabled=False, verified_at=None, error=expected_error
        )
        graph_get.assert_not_called()
        connection.refresh_from_db()
        assert (connection.write_enabled, connection.last_verification_error) == (
            False,
            expected_error,
        )
        assert "TenantPlacesManagement" in expected_error

    def test_reverify_sees_a_permission_granted_after_the_last_check(
        self, organization, connection
    ):
        cache = InMemoryCache()
        service = MicrosoftConnectionService(
            token_provider=MicrosoftAppOnlyTokenProvider(
                client_id="vinta-client-id",
                client_secret="vinta-client-secret",  # noqa: S106 - test value
                cache_getter=lambda: cache,
            )
        )

        with (
            patch.object(
                token_module.requests,
                "post",
                return_value=token_endpoint_response(["Calendars.Read"]),
            ),
            patch.object(service_module.requests, "get", return_value=graph_response(200)),
        ):
            first = service.verify(organization)
        # The admin consents again, and Microsoft now grants both roles.
        with (
            patch.object(
                token_module.requests,
                "post",
                return_value=token_endpoint_response(sorted(BOTH_ROLES)),
            ) as token_post,
            patch.object(service_module.requests, "get", return_value=graph_response(200)),
        ):
            second = service.verify(organization)

        assert (first.write_enabled, second.write_enabled, second.error) == (False, True, "")
        token_post.assert_called_once()

    def test_reverify_sees_a_permission_revoked_after_the_last_check(
        self, organization, connection
    ):
        cache = InMemoryCache()
        service = MicrosoftConnectionService(
            token_provider=MicrosoftAppOnlyTokenProvider(
                client_id="vinta-client-id",
                client_secret="vinta-client-secret",  # noqa: S106 - test value
                cache_getter=lambda: cache,
            )
        )

        with (
            patch.object(
                token_module.requests,
                "post",
                return_value=token_endpoint_response(sorted(BOTH_ROLES)),
            ),
            patch.object(service_module.requests, "get", return_value=graph_response(200)),
        ):
            first = service.verify(organization)
        with (
            patch.object(
                token_module.requests,
                "post",
                return_value=token_endpoint_response(["Calendars.Read"]),
            ),
            patch.object(service_module.requests, "get", return_value=graph_response(200)),
        ):
            second = service.verify(organization)

        assert (first.write_enabled, second.write_enabled) == (True, False)
        connection.refresh_from_db()
        assert connection.write_enabled is False

    def test_failed_check_turns_off_a_previously_enabled_connection(
        self, service, organization, connection
    ):
        earlier = timezone.now() - datetime.timedelta(days=1)
        connection.write_enabled = True
        connection.verified_at = earlier
        connection.save(update_fields=["write_enabled", "verified_at"])

        with patch.object(
            service.token_provider,
            "get_token",
            side_effect=MicrosoftAppOnlyTokenError(status_code=400, error_code="invalid_grant"),
        ):
            result = service.verify(organization)

        assert (result.write_enabled, result.verified_at) == (False, earlier)
        assert result.error.startswith("Vinta Schedule could not get an access token")
        assert result.error.endswith(EXCHANGE_RBAC_REMINDER)

    @pytest.mark.parametrize("status_code", [401, 403, 500])
    def test_places_read_failure_gives_a_remediation_message(
        self, service, organization, connection, status_code
    ):
        with (
            patch.object(service.token_provider, "get_token", return_value=app_token()),
            patch.object(service_module.requests, "get", return_value=graph_response(status_code)),
        ):
            result = service.verify(organization)

        assert result.write_enabled is False
        assert result.error == (
            f"Microsoft Graph refused to list your buildings (HTTP {status_code}). "
            "Check that the Vinta Schedule app still has Place.ReadWrite.All. "
            f"{EXCHANGE_RBAC_REMINDER}"
        )

    def test_unreachable_graph_gives_a_retry_message(self, service, organization, connection):
        with (
            patch.object(service.token_provider, "get_token", return_value=app_token()),
            patch.object(
                service_module.requests, "get", side_effect=requests.ConnectionError("down")
            ),
        ):
            result = service.verify(organization)

        assert result.write_enabled is False
        assert result.error.startswith("Vinta Schedule could not reach Microsoft Graph.")

    def test_no_connection_says_consent_comes_first(self, service, organization):
        with patch.object(service.token_provider, "get_token") as get:
            result = service.verify(organization)

        assert result == MicrosoftConnectionVerification(
            write_enabled=False, verified_at=None, error=NOT_CONSENTED_MESSAGE
        )
        get.assert_not_called()
        assert not MicrosoftOrganizationConnection.objects.filter_by_organization(
            organization
        ).exists()

    def test_connection_without_tenant_records_that_consent_comes_first(
        self, service, organization
    ):
        service.build_consent_url(organization, redirect_uri=REDIRECT_URI)

        with patch.object(service.token_provider, "get_token") as get:
            result = service.verify(organization)

        assert result.error == NOT_CONSENTED_MESSAGE
        get.assert_not_called()
        assert connection_of(organization).last_verification_error == NOT_CONSENTED_MESSAGE

    def test_reads_only_its_own_organizations_connection(self, service, organization):
        other_organization = baker.make(Organization)
        with organization_context(other_organization):
            create_microsoft_organization_connection(
                organization=other_organization, tenant_id=TENANT_ID
            )

        with patch.object(service.token_provider, "get_token") as get:
            result = service.verify(organization)

        assert result.error == NOT_CONSENTED_MESSAGE
        get.assert_not_called()
