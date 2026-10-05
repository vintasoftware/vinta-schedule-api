"""Tests for the Microsoft connection endpoints: consent URL, consent callback and verify."""

import base64
import contextlib
import datetime
import json
import urllib.parse
from collections.abc import Iterator
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client
from django.urls import resolve, reverse

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.microsoft_connection_views import (
    MicrosoftConnectionVerifyView,
    MicrosoftConsentCallbackView,
    MicrosoftConsentUrlView,
)
from calendar_integration.models import MicrosoftOrganizationConnection
from calendar_integration.services.calendar_clients import ms_app_only_token as token_module
from calendar_integration.services.microsoft_connection_service import (
    CONSENT_STATE_SALT,
    MicrosoftConnectionVerification,
)
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import make_membership


User = get_user_model()

TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SIGNED_IN_TENANT_ID = "11111111-2222-3333-4444-555555555555"
CONSENT_URL_PATH = "/calendar/microsoft-connection/consent-url/"
VERIFY_PATH = "/calendar/microsoft-connection/verify/"
CALLBACK_PATH = "/calendar/microsoft-connection/callback/"


def set_flag(organization: Organization, enabled: bool) -> None:
    with organization_context(organization):
        OrganizationFeatureFlag.objects.update_or_create(
            organization=organization,
            key=RESOURCE_CALENDAR_PROVIDER_SYNC,
            defaults={"enabled": enabled},
        )


def client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def state_from(consent_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(consent_url).query)["state"][0]


def connection_of(organization: Organization) -> MicrosoftOrganizationConnection:
    return MicrosoftOrganizationConnection.objects.filter_by_organization(organization).get()


@contextlib.contextmanager
def microsoft_sign_in(state: str, tid: str) -> Iterator[Mock]:
    """Answer the code redemption the way Microsoft would for an admin of tid."""
    claims = {
        "aud": "vinta-client-id",
        "nonce": signing.loads(state, salt=CONSENT_STATE_SALT)["nonce"],
        "tid": tid,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    response = Mock(status_code=200)
    response.json.return_value = {"id_token": f"eyJhbGciOiJub25lIn0.{payload}.sig"}
    with patch.object(token_module.requests, "post", return_value=response) as post:
        yield post


@pytest.fixture(autouse=True)
def ms_app_credentials(settings):
    settings.MS_CLIENT_ID = "vinta-client-id"
    settings.MS_CLIENT_SECRET = "vinta-client-secret"  # noqa: S105 - test value


@pytest.fixture
def organization() -> Organization:
    organization = baker.make(Organization)
    set_flag(organization, enabled=True)
    return organization


@pytest.fixture
def admin_client(organization) -> APIClient:
    admin = baker.make(User)
    make_membership(user=admin, organization=organization, groups=[GROUP_ORGANIZATION_ADMIN])
    return client_for(admin)


@pytest.fixture
def member_client(organization) -> APIClient:
    member = baker.make(User)
    make_membership(user=member, organization=organization)
    return client_for(member)


def consent_url_for(admin_client: APIClient) -> str:
    response = admin_client.post(CONSENT_URL_PATH)
    assert response.status_code == 200, response.content
    return response.data["consent_url"]


def expected_redirect(query: dict[str, str], settings) -> str:
    return (
        f"{settings.FRONTEND_BASE_URL}/settings/integrations/microsoft?"
        f"{urllib.parse.urlencode(query)}"
    )


def test_paths_resolve_to_these_views_and_not_the_calendar_router():
    assert (
        reverse("microsoft-connection-consent-url"),
        reverse("microsoft-connection-verify"),
        reverse("microsoft-connection-callback"),
    ) == (CONSENT_URL_PATH, VERIFY_PATH, CALLBACK_PATH)
    assert [
        resolve(CONSENT_URL_PATH).func.view_class,  # type: ignore[attr-defined]
        resolve(VERIFY_PATH).func.view_class,  # type: ignore[attr-defined]
        resolve(CALLBACK_PATH).func.view_class,  # type: ignore[attr-defined]
    ] == [MicrosoftConsentUrlView, MicrosoftConnectionVerifyView, MicrosoftConsentCallbackView]


@pytest.mark.django_db
class TestConsentUrl:
    def test_admin_gets_a_consent_url_pointing_back_at_the_callback(
        self, admin_client, organization
    ):
        consent_url = consent_url_for(admin_client)

        query = urllib.parse.parse_qs(urllib.parse.urlsplit(consent_url).query)
        assert consent_url.startswith(
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?"
        )
        assert query["client_id"] == ["vinta-client-id"]
        assert query["redirect_uri"] == [f"http://testserver{CALLBACK_PATH}"]
        assert connection_of(organization).consent_state != ""

    def test_non_admin_member_is_forbidden(self, member_client, organization):
        response = member_client.post(CONSENT_URL_PATH)

        assert response.status_code == 403
        assert not MicrosoftOrganizationConnection.objects.filter_by_organization(
            organization
        ).exists()

    def test_anonymous_caller_is_unauthorized(self):
        response = APIClient().post(CONSENT_URL_PATH)

        assert response.status_code == 401

    def test_flag_off_returns_404(self, admin_client, organization):
        set_flag(organization, enabled=False)

        response = admin_client.post(CONSENT_URL_PATH)

        assert response.status_code == 404
        assert response.data == {
            "detail": "Resource calendar provider sync is not enabled for this organization."
        }
        assert not MicrosoftOrganizationConnection.objects.filter_by_organization(
            organization
        ).exists()

    def test_unconfigured_app_returns_503(self, admin_client, settings):
        settings.MS_CLIENT_SECRET = ""

        response = admin_client.post(CONSENT_URL_PATH)

        assert response.status_code == 503
        assert response.data == {
            "detail": "Microsoft room sync is not configured on this environment."
        }


@pytest.mark.django_db
class TestVerify:
    def test_admin_gets_the_verification_result(self, admin_client, organization):
        from di_core.containers import container

        verified_at = datetime.datetime(2026, 10, 5, 12, tzinfo=datetime.UTC)
        service = Mock()
        service.verify.return_value = MicrosoftConnectionVerification(
            write_enabled=True, verified_at=verified_at, error=""
        )

        with container.microsoft_connection_service.override(service):
            response = admin_client.post(VERIFY_PATH)

        assert response.status_code == 200
        assert response.json() == {
            "write_enabled": True,
            "verified_at": "2026-10-05T12:00:00Z",
            "error": "",
        }
        service.verify.assert_called_once_with(organization)

    def test_failed_verification_is_reported_in_the_body(self, admin_client):
        response = admin_client.post(VERIFY_PATH)

        assert response.status_code == 200
        assert response.json()["write_enabled"] is False
        assert response.json()["verified_at"] is None
        assert response.json()["error"].startswith("No Microsoft 365 tenant is connected yet.")

    def test_non_admin_member_is_forbidden(self, member_client):
        assert member_client.post(VERIFY_PATH).status_code == 403

    def test_flag_off_returns_404(self, admin_client, organization):
        set_flag(organization, enabled=False)

        assert admin_client.post(VERIFY_PATH).status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("path", [CONSENT_URL_PATH, VERIFY_PATH])
def test_flag_off_hides_the_endpoint_from_non_admin_members_too(member_client, organization, path):
    set_flag(organization, enabled=False)

    response = member_client.post(path)

    assert response.status_code == 404
    assert response.data == {
        "detail": "Resource calendar provider sync is not enabled for this organization."
    }


@pytest.mark.django_db
class TestCallback:
    def test_valid_callback_stores_the_signed_in_tenant_and_redirects_to_the_frontend(
        self, admin_client, organization, settings
    ):
        state = state_from(consent_url_for(admin_client))

        with microsoft_sign_in(state, tid=TENANT_ID) as post:
            response = Client().get(CALLBACK_PATH, {"code": "auth-code", "state": state})

        assert response.status_code == 302
        assert response["Location"] == expected_redirect({"status": "connected"}, settings)
        assert connection_of(organization).tenant_id == TENANT_ID
        assert post.call_args.kwargs["data"]["code"] == "auth-code"
        assert post.call_args.kwargs["data"]["redirect_uri"] == f"http://testserver{CALLBACK_PATH}"

    def test_tenant_in_the_query_string_is_ignored(self, admin_client, organization, settings):
        state = state_from(consent_url_for(admin_client))

        with microsoft_sign_in(state, tid=SIGNED_IN_TENANT_ID):
            response = Client().get(
                CALLBACK_PATH,
                {"code": "auth-code", "state": state, "tenant": TENANT_ID, "tid": TENANT_ID},
            )

        assert response["Location"] == expected_redirect({"status": "connected"}, settings)
        assert connection_of(organization).tenant_id == SIGNED_IN_TENANT_ID

    def test_naming_a_tenant_without_signing_in_stores_nothing(
        self, admin_client, organization, settings
    ):
        """The org admin holds a valid state and hand-writes the old admin-consent answer."""
        state = state_from(consent_url_for(admin_client))

        with patch.object(token_module.requests, "post") as post:
            response = Client().get(
                CALLBACK_PATH, {"admin_consent": "True", "tenant": TENANT_ID, "state": state}
            )

        assert response["Location"] == expected_redirect(
            {"status": "error", "reason": "consent_denied"}, settings
        )
        post.assert_not_called()
        connection = connection_of(organization)
        assert (connection.tenant_id, connection.consent_state) == ("", "")

    def test_code_microsoft_refuses_stores_nothing(self, admin_client, organization, settings):
        state = state_from(consent_url_for(admin_client))
        refused = Mock(status_code=400)
        refused.json.return_value = {"error": "invalid_grant"}

        with patch.object(token_module.requests, "post", return_value=refused):
            response = Client().get(CALLBACK_PATH, {"code": "made-up", "state": state})

        assert response["Location"] == expected_redirect(
            {"status": "error", "reason": "sign_in_failed"}, settings
        )
        connection = connection_of(organization)
        assert (connection.tenant_id, connection.consent_state) == ("", "")

    def test_replayed_callback_is_rejected(self, admin_client, organization, settings):
        state = state_from(consent_url_for(admin_client))
        with microsoft_sign_in(state, tid=TENANT_ID):
            Client().get(CALLBACK_PATH, {"code": "auth-code", "state": state})

        with microsoft_sign_in(state, tid=SIGNED_IN_TENANT_ID) as post:
            response = Client().get(CALLBACK_PATH, {"code": "second-code", "state": state})

        assert response["Location"] == expected_redirect(
            {"status": "error", "reason": "invalid_state"}, settings
        )
        post.assert_not_called()
        assert connection_of(organization).tenant_id == TENANT_ID

    def test_tampered_state_is_rejected(self, admin_client, organization, settings):
        state = state_from(consent_url_for(admin_client))
        index = len(state) // 3
        tampered = state[:index] + ("A" if state[index] != "A" else "B") + state[index + 1 :]

        with microsoft_sign_in(state, tid=TENANT_ID) as post:
            response = Client().get(CALLBACK_PATH, {"code": "auth-code", "state": tampered})

        assert response["Location"] == expected_redirect(
            {"status": "error", "reason": "invalid_state"}, settings
        )
        post.assert_not_called()
        assert connection_of(organization).tenant_id == ""

    def test_missing_state_is_rejected(self, settings):
        response = Client().get(CALLBACK_PATH, {"code": "auth-code"})

        assert response["Location"] == expected_redirect(
            {"status": "error", "reason": "invalid_state"}, settings
        )

    def test_denied_consent_redirects_with_the_reason(self, admin_client, organization, settings):
        state = state_from(consent_url_for(admin_client))

        response = Client().get(
            CALLBACK_PATH,
            {
                "error": "access_denied",
                "error_description": "The admin canceled the request",
                "state": state,
            },
        )

        assert response["Location"] == expected_redirect(
            {"status": "error", "reason": "consent_denied"}, settings
        )
        assert connection_of(organization).tenant_id == ""

    def test_redirect_target_never_comes_from_the_request(self, admin_client, settings):
        state = state_from(consent_url_for(admin_client))

        with microsoft_sign_in(state, tid=TENANT_ID):
            response = Client().get(
                CALLBACK_PATH,
                {
                    "code": "auth-code",
                    "state": state,
                    "redirect_uri": "https://evil.example/steal",
                    "next": "https://evil.example/steal",
                },
            )

        assert response.status_code == 302
        assert response["Location"] == expected_redirect({"status": "connected"}, settings)

    def test_flag_off_returns_404_and_stores_nothing(self, admin_client, organization):
        state = state_from(consent_url_for(admin_client))
        set_flag(organization, enabled=False)

        with microsoft_sign_in(state, tid=TENANT_ID) as post:
            response = Client().get(CALLBACK_PATH, {"code": "auth-code", "state": state})

        assert response.status_code == 404
        post.assert_not_called()
        assert connection_of(organization).tenant_id == ""
