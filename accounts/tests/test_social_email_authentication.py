"""
Social login into an existing account that has no social account linked yet.

A user who signed up with email and password and later chooses "Sign in with Google"
must end up signed in to that same account, with the Google account linked to it.

Before ``SocialAccountAdapter.can_authenticate_by_email`` existed, allauth treated
the matching email as a signup conflict. With mandatory verification and
enumeration prevention on, it showed the "verify your email" step. But it sent only
an "account already exists" email and never a code, so the user was stuck.

These tests go through allauth's own ``pre_social_login`` (the lookup) and
``SocialLogin._accept_login`` (the auto-connect), the same way allauth's test suite
does for this feature. They do not call ``complete_social_login``, because the
response it builds depends on views that ``HEADLESS_ONLY`` does not mount.
"""

from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

import pytest
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.internal.flows.login import pre_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin, SocialToken
from kombu.exceptions import OperationalError
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from users.factories import UserFactory
from users.models import User


pytestmark = pytest.mark.django_db

PROVIDERS = {
    "google": {"APPS": [{"client_id": "google-client", "secret": "google-secret", "key": ""}]},
    "apple": {
        "APPS": [
            {
                "client_id": "apple-client",
                "secret": "apple-key-id",
                "key": "apple-member",
                "settings": {"certificate_key": "unused"},
            }
        ]
    },
}


@pytest.fixture(autouse=True)
def _social_providers(settings) -> None:
    settings.SOCIALACCOUNT_PROVIDERS = PROVIDERS


def _no_response(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


def _request() -> HttpRequest:
    request = RequestFactory().get("/")
    SessionMiddleware(_no_response).process_request(request)
    MessageMiddleware(_no_response).process_request(request)
    request.user = AnonymousUser()
    return request


def _sociallogin(
    email: str, *, provider: str = "google", email_verified: bool = True
) -> SocialLogin:
    provider_instance = get_adapter().get_provider(request=None, provider=provider)
    sociallogin = SocialLogin(
        provider=provider_instance,
        user=User(email=email),
        account=SocialAccount(provider=provider, uid="google-uid-123"),
        email_addresses=[EmailAddress(email=email, verified=email_verified, primary=True)],
    )
    # Set the token after construction, like allauth's OAuth2 flow does.
    # ``SocialLogin``'s constructor reads ``token.account``, and that raises on an
    # unsaved token.
    sociallogin.token = SocialToken(token="access-token", token_secret="refresh-token")
    sociallogin.state = {"process": "login"}
    return sociallogin


def _existing_user(email: str, *, email_verified: bool = True) -> User:
    user = UserFactory().create_user(email=email)
    EmailAddress.objects.create(user=user, email=email, verified=email_verified, primary=True)
    return user


def _lookup_and_accept(sociallogin: SocialLogin) -> None:
    request = _request()
    with context.request_context(request):
        pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            sociallogin._accept_login(request)


class TestSocialEmailAuthentication:
    def test_google_login_signs_in_and_links_existing_account(self):
        user = _existing_user("existing@example.com")
        sociallogin = _sociallogin("existing@example.com")

        _lookup_and_accept(sociallogin)

        assert sociallogin.is_existing
        assert sociallogin.user.pk == user.pk
        assert User.objects.filter(email="existing@example.com").count() == 1
        account = SocialAccount.objects.get(user=user, provider="google")
        # The calendar integration reads the stored token, so the link must carry it.
        assert SocialToken.objects.filter(account=account, token_secret="refresh-token").exists()
        user.refresh_from_db()
        assert user.has_usable_password(), "a verified local address keeps its password"

    def test_existing_account_with_unverified_email_loses_its_password(self):
        # Whoever registered the address first never proved they own it. Signing in
        # through Google must not leave that person able to use the old password.
        user = _existing_user("unverified@example.com", email_verified=False)

        _lookup_and_accept(_sociallogin("unverified@example.com"))

        user.refresh_from_db()
        assert SocialAccount.objects.filter(user=user, provider="google").exists()
        assert not user.has_usable_password()

    def test_email_the_provider_did_not_verify_is_not_matched(self):
        user = _existing_user("existing@example.com")
        sociallogin = _sociallogin("existing@example.com", email_verified=False)

        _lookup_and_accept(sociallogin)

        assert not sociallogin.is_existing
        assert not SocialAccount.objects.filter(user=user).exists()

    def test_provider_outside_the_allowlist_is_not_matched(self):
        user = _existing_user("existing@example.com")
        sociallogin = _sociallogin("existing@example.com", provider="apple")

        _lookup_and_accept(sociallogin)

        assert not sociallogin.is_existing
        assert not SocialAccount.objects.filter(user=user).exists()

    @pytest.mark.parametrize("widen", ["global_setting", "provider_setting"])
    def test_allauth_settings_cannot_add_a_provider(self, settings, widen):
        # allauth reads both of these in its own can_authenticate_by_email. The adapter
        # replaces that lookup, so neither one may trust a provider left off the list.
        if widen == "global_setting":
            settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
        else:
            settings.SOCIALACCOUNT_PROVIDERS = {
                **PROVIDERS,
                "apple": {**PROVIDERS["apple"], "EMAIL_AUTHENTICATION": True},
            }
        user = _existing_user("existing@example.com")
        sociallogin = _sociallogin("existing@example.com", provider="apple")

        _lookup_and_accept(sociallogin)

        assert not sociallogin.is_existing
        assert not SocialAccount.objects.filter(user=user).exists()


class TestLinkedAccountCalendarImport:
    """Linking goes through ``SocialLogin.connect``, never ``save_user``, so the
    calendar import has to come from the ``social_account_added`` receiver."""

    def test_linking_google_to_an_existing_member_imports_its_calendars(
        self, django_capture_on_commit_callbacks
    ):
        user = _existing_user("existing@example.com")
        organization = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=True)
        sociallogin = _sociallogin("existing@example.com")

        with (
            patch("accounts.calendar_import.import_account_calendars_task.delay") as delay,
            django_capture_on_commit_callbacks(execute=True),
        ):
            _lookup_and_accept(sociallogin)

        account = SocialAccount.objects.get(user=user, provider="google")
        delay.assert_called_once_with(
            account_type="social_account",
            account_id=account.pk,
            organization_id=organization.pk,
        )

    def test_linking_without_a_membership_imports_nothing(self, django_capture_on_commit_callbacks):
        user = _existing_user("existing@example.com")

        with (
            patch("accounts.calendar_import.import_account_calendars_task.delay") as delay,
            django_capture_on_commit_callbacks(execute=True),
        ):
            _lookup_and_accept(_sociallogin("existing@example.com"))

        assert SocialAccount.objects.filter(user=user, provider="google").exists()
        delay.assert_not_called()

    def test_a_failed_import_does_not_fail_the_login(self, django_capture_on_commit_callbacks):
        # The import is optional. A broker error must not turn the login into a 500
        # once the social account has been linked.
        user = _existing_user("existing@example.com")
        organization = baker.make(Organization)
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=True)
        sociallogin = _sociallogin("existing@example.com")

        with (
            patch(
                "accounts.calendar_import.import_account_calendars_task.delay",
                side_effect=OperationalError("broker unavailable"),
            ) as delay,
            django_capture_on_commit_callbacks(execute=True),
        ):
            _lookup_and_accept(sociallogin)

        delay.assert_called_once()
        assert sociallogin.is_existing
        assert SocialAccount.objects.filter(user=user, provider="google").exists()
