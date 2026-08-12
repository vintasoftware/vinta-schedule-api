"""The post-authentication destination reaches every completed authentication,
not just the social OAuth callback.

The social callback (``accounts.views.ProviderCallbackAPIView``) resolves the
destination in the view itself and is covered by
``accounts/tests/test_views.py``. This module covers the other half — the
email/password flows served by allauth's own headless views, where
``accounts.middlewares.PostAuthDestinationMiddleware`` is what puts the field on
the response. The regression it pins: a user who signs up with an email and
verifies it used to get a 200 with tokens and no ``destination``, leaving the
SPA nothing to navigate to, so a branded organization's redirect never happened.

The flows are driven through the real HTTP endpoints (``HEADLESS_ONLY = True``,
so those are the only production entry points) rather than by calling the
middleware directly: what makes this worth testing is precisely that allauth
restores ``request.user`` to anonymous on the way out of its views, which only
shows up end-to-end.
"""

from importlib import import_module

from django.conf import settings as django_settings
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from legal.factories import PolicyDocumentFactory
from legal.models import PolicyDocumentType
from tenancy.models import (
    Organization,
    OrganizationBranding,
    OrganizationMembership,
    OrganizationRole,
)
from users.factories import UserFactory
from users.models import User


pytestmark = pytest.mark.django_db


SIGNUP_PASSWORD = "Sup3r-Secret-Passw0rd!"  # noqa: S105


def _publish_policy_documents() -> None:
    for document_type in PolicyDocumentType.values:
        PolicyDocumentFactory().create(document_type=document_type, version=1)


def _signup(client: APIClient, email: str, organization_name: str = "") -> str:
    """Sign up through the headless endpoint; return the pending session token.

    The response is a 401 (email verification is mandatory), which is the point:
    nothing is authenticated yet, so no destination is resolvable either.
    """
    payload = {
        "email": email,
        "phone": "+123456789",
        "password1": SIGNUP_PASSWORD,
        "password2": SIGNUP_PASSWORD,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "accepted_terms": True,
        "accepted_sms_consent": True,
        "organization_name": organization_name,
    }
    response = client.post(reverse("headless:app:account:signup"), payload, format="json")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.json()
    return response.json()["meta"]["session_token"]


def _verification_code(session_token: str) -> str:
    """The emailed verification code, read from the pending headless session.

    ``ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED`` stashes it there while the
    signup waits for the user; going to the session avoids reaching into the
    notification pipeline just to learn the code.
    """
    session_store = import_module(django_settings.SESSION_ENGINE).SessionStore
    state = session_store(session_key=session_token).get("account_email_verification_code")
    assert state, "no pending email-verification process on the signup session"
    return state["code"]


def _verify_email(client: APIClient, session_token: str):
    return client.post(
        reverse("headless:app:account:verify_email"),
        {"key": _verification_code(session_token)},
        format="json",
        headers={"X-Session-Token": session_token},
    )


def _login(client: APIClient, email: str):
    return client.post(
        reverse("headless:app:account:login"),
        {"email": email, "password": SIGNUP_PASSWORD},
        format="json",
    )


class TestEmailSignupReachesTheOrganizationDestination:
    """The email/password path — the one that was broken."""

    def test_verify_email_returns_the_organizations_configured_destination(self):
        _publish_policy_documents()
        client = APIClient()
        email = "branded-signup@example.com"

        session_token = _signup(client, email)
        # Branding is resolved from the membership the user holds when the
        # verification lands, so the organization is set up between the two
        # steps. (Signing up with an `organization_name` instead would have
        # provisioning create the org during verification, leaving no moment to
        # attach a branding row to it.)
        organization = baker.make(Organization, name="Acme Health")
        baker.make(
            OrganizationBranding,
            organization=organization,
            redirect_url="https://scheduling.acme.example.com/app",
        )
        user = User.objects.get(email=email)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        response = _verify_email(client, session_token)

        assert response.status_code == status.HTTP_200_OK, response.json()
        body = response.json()
        assert body["meta"]["is_authenticated"] is True
        assert body["destination"] == "https://scheduling.acme.example.com/app"

    def test_verify_email_falls_back_to_the_dashboard_without_branding(self):
        _publish_policy_documents()
        client = APIClient()
        email = "unbranded-signup@example.com"

        session_token = _signup(client, email, organization_name="Plain Co")
        response = _verify_email(client, session_token)

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["destination"] == f"{django_settings.FRONTEND_BASE_URL}/dashboard"

    def test_pending_signup_response_carries_no_destination(self):
        """The signup response itself is unauthenticated (verification pending):
        there is no acting organization yet, so no destination is invented."""
        _publish_policy_documents()
        client = APIClient()

        payload_response = client.post(
            reverse("headless:app:account:signup"),
            {
                "email": "pending-signup@example.com",
                "phone": "+123456789",
                "password1": SIGNUP_PASSWORD,
                "password2": SIGNUP_PASSWORD,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "accepted_terms": True,
                "accepted_sms_consent": True,
            },
            format="json",
        )

        assert payload_response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "destination" not in payload_response.json()


class TestLoginReachesTheOrganizationDestination:
    """Every completed authentication answers the same way, not just signup."""

    def _verified_user(self, email: str) -> User:
        from allauth.account.models import EmailAddress

        user = UserFactory().create_user(email=email)
        user.set_password(SIGNUP_PASSWORD)
        user.save()
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
        return user

    def test_login_returns_the_configured_destination(self):
        email = "returning@example.com"
        user = self._verified_user(email)
        organization = baker.make(Organization, name="Acme Health")
        baker.make(
            OrganizationBranding,
            organization=organization,
            redirect_url="https://scheduling.acme.example.com/app",
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        response = _login(APIClient(), email)

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["destination"] == "https://scheduling.acme.example.com/app"

    def test_session_lookup_carries_the_destination_too(self):
        """``GET /auth/{client}/v1/auth/session`` reports a completed
        authentication, so it carries the field like every other 200 -- which is
        what lets ``schema-auth.yml`` mark ``destination`` required on
        ``AuthenticatedResponse``."""
        email = "session-lookup@example.com"
        user = self._verified_user(email)
        organization = baker.make(Organization, name="Acme Health")
        baker.make(
            OrganizationBranding,
            organization=organization,
            redirect_url="https://scheduling.acme.example.com/app",
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        client = APIClient()
        session_token = _login(client, email).json()["meta"]["session_token"]

        response = client.get(
            reverse("headless:app:account:current_session"),
            headers={"X-Session-Token": session_token},
        )

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["destination"] == "https://scheduling.acme.example.com/app"

    def test_membership_less_user_gets_the_dashboard(self):
        """A user still gated on onboarding has no organization to resolve, and
        still gets a usable destination rather than a missing field."""
        email = "gated@example.com"
        self._verified_user(email)

        response = _login(APIClient(), email)

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["destination"] == f"{django_settings.FRONTEND_BASE_URL}/dashboard"

    def test_organization_without_redirect_url_gets_the_dashboard(self):
        """A branded organization that never configured a redirect destination
        is treated exactly like an unbranded one."""
        email = "no-redirect@example.com"
        user = self._verified_user(email)
        organization = baker.make(Organization, name="Acme Health")
        baker.make(
            OrganizationBranding,
            organization=organization,
            app_name="AcmeSchedule",
            redirect_url="",
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        response = _login(APIClient(), email)

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["destination"] == f"{django_settings.FRONTEND_BASE_URL}/dashboard"

    def test_failed_login_carries_no_destination(self):
        self._verified_user("wrongpass@example.com")
        client = APIClient()

        response = client.post(
            reverse("headless:app:account:login"),
            {"email": "wrongpass@example.com", "password": "not-the-password"},
            format="json",
        )

        assert response.status_code != status.HTTP_200_OK
        assert "destination" not in response.json()


class TestMiddlewareLeavesEverythingElseAlone:
    """The middleware keys off allauth's envelope, so no other API surface can
    pick the field up by accident."""

    def test_rest_endpoints_are_untouched(self):
        user = UserFactory().create_user(email="rest-caller@example.com")
        organization = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))

        response = client.get("/organizations/mine/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert "destination" not in (payload if isinstance(payload, dict) else {})
