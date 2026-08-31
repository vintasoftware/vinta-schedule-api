"""Sign-up is not allowed to succeed without the email that makes it usable.

``ACCOUNT_EMAIL_VERIFICATION`` is ``"mandatory"``: the code in the confirmation email is
the only way an account can ever be used. ``NotificationService.send`` already raises
``NotificationSendError`` when a send fails, so the failure was never silent -- but
nothing acted on it. It escaped ``allauth.headless.account.views.SignupView`` uncaught,
which is an HTML 500 to a caller that parses JSON, and by then the user row was
committed. The visitor was left with an address they could neither verify (no code
arrived) nor register again (enumeration prevention answers the retry by resuming the
existing account's verification, sending nothing new). The address was spent on an
account nobody could reach.

Three things had to change together, and this module pins all three:

* ``AccountAdapter.send_confirmation_mail`` re-raises as
  ``VerificationEmailUndeliverableError``;
* ``accounts.views.AtomicSignupView`` runs the whole POST in one transaction, so the
  account is rolled back; and
* it answers in allauth's own envelope, so a client has a sentence to show.

The two settings fixes that ship alongside are pinned here too, since both are about the
same thing -- a person who cannot get past the verification screen: the resend quota
(``ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_RESEND``) and the message a correct password gets
when the ``confirm_email`` cooldown is still warm.
"""

import uuid
from unittest import mock

from django.core.cache import cache
from django.urls import resolve, reverse

import pytest
from allauth.account.models import EmailAddress
from rest_framework import status
from vintasend.exceptions import NotificationSendError

from accounts.views import AtomicSignupView
from legal.factories import PolicyDocumentFactory
from legal.models import PolicyDocumentType
from users.models import User


pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "Sup3r-Secret-Passw0rd!"


@pytest.fixture
def email():
    """An address no other test in this process has used.

    Not cosmetic. allauth rate-limits confirmation sends at ``1/10s`` **per address**,
    that counter lives in the cache rather than the database, and the cache is not rolled
    back between tests -- so a second test reusing one address is answered by the
    limiter: no send is attempted, nothing raises, and the signup succeeds. Which looks
    exactly like the guard under test having failed.
    """
    return f"ada-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def signup_url():
    """The path this project registers ahead of allauth's own.

    Reversed by our name rather than allauth's, so the day the override is removed this
    fixture fails loudly instead of the tests below quietly exercising allauth's
    unmodified view.
    """
    return reverse("atomic_signup")


@pytest.fixture
def policy_documents():
    """Signup refuses without a published version of every consent document."""
    for document_type in PolicyDocumentType.values:
        PolicyDocumentFactory().create(document_type=document_type, version=1)


@pytest.fixture
def undeliverable_mail():
    """Make the confirmation send fail.

    Patched at the notification service rather than at the SMTP layer: what is being
    pinned is the *contract* between the adapter and the view -- that a
    ``NotificationSendError`` out of the notification service becomes a rolled-back
    signup -- and any failure below that point (a dead SMTP host, an unroutable channel,
    an unrenderable template) arrives here as the same exception.
    """
    from di_core.containers import container

    service = container.notification_service()
    with mock.patch.object(
        service,
        "create_notification",
        side_effect=NotificationSendError("SMTP said 451"),
    ) as patched:
        yield patched


def _post_signup(client, signup_url, email, **overrides):
    payload = {
        "email": email,
        "phone": "+12345678901",
        "password": PASSWORD,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "accepted_terms": True,
        "accepted_sms_consent": True,
    }
    payload.update(overrides)
    return client.post(signup_url, payload, format="json")


class TestSignupWhenTheVerificationEmailCannotBeSent:
    """The send fails. Nothing is left behind, and the caller is told."""

    def test_the_response_says_what_went_wrong(
        self, anonymous_client, signup_url, email, policy_documents, undeliverable_mail
    ):
        response = _post_signup(anonymous_client, signup_url, email)

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        payload = response.json()
        # allauth's own envelope, so a client's single error mapper renders it with no
        # special case. A bare Django 500 -- which is what an unhandled exception out of
        # a headless view produces -- is an HTML page to a caller that parses JSON.
        assert payload["errors"][0]["code"] == "verification_email_failed"
        assert "verification email" in payload["errors"][0]["message"].lower()

    def test_no_account_is_left_behind(
        self, anonymous_client, signup_url, email, policy_documents, undeliverable_mail
    ):
        _post_signup(anonymous_client, signup_url, email)

        assert not User.objects.filter(email=email).exists()
        assert not EmailAddress.objects.filter(email=email).exists()

    def test_the_address_can_be_used_again_once_mail_works(
        self, anonymous_client, signup_url, email, policy_documents, undeliverable_mail
    ):
        """The whole point of the rollback, stated as the thing a person hits.

        Without it the retry is answered by enumeration prevention, which resumes the
        committed account's verification and sends no new code -- the address is
        permanently spent.
        """
        assert (
            _post_signup(anonymous_client, signup_url, email).status_code
            == status.HTTP_503_SERVICE_UNAVAILABLE
        )

        undeliverable_mail.side_effect = None
        # allauth rate-limits confirmation sends at `1/10s` per address, in the cache --
        # which a rolled-back transaction does not touch. Two requests milliseconds apart
        # therefore hit the cooldown, and the retry answers 401 with *no* pending flow
        # because no verification was started. Real people are never that fast; clearing
        # it keeps this test about the rollback rather than about the limiter.
        cache.clear()
        response = _post_signup(anonymous_client, signup_url, email)

        # 401 with a pending flow is what a *successful* headless signup looks like: the
        # account exists and is waiting on its verification.
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        pending = [flow for flow in response.json()["data"]["flows"] if flow.get("is_pending")]
        assert [flow["id"] for flow in pending] == ["verify_email"]
        assert User.objects.filter(email=email).exists()


class TestSignupWhenTheVerificationEmailIsSent:
    """The unchanged path, pinned so the guard above cannot swallow it."""

    def test_the_account_is_created_and_awaits_verification(
        self, anonymous_client, signup_url, email, policy_documents
    ):
        response = _post_signup(anonymous_client, signup_url, email)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert User.objects.filter(email=email).exists()

    @pytest.mark.parametrize(
        "allauth_route",
        ["headless:app:account:signup", "headless:browser:account:signup"],
    )
    def test_the_override_is_wired_to_allauths_own_paths(self, allauth_route):
        """The subclass only helps on the paths clients actually post to.

        ``accounts.urls`` is included at the same ``auth/`` prefix as, and before,
        ``allauth.headless.urls``, so allauth's own route name still reverses to the
        shared path and Django resolves it to the subclass. Asserting on the resolved
        view rather than on the string is what makes this a test of the shadow: get the
        include order wrong and the path is identical but the view is allauth's.

        Both clients in ``HEADLESS_CLIENTS``, because the flaw is in the signup flow
        rather than in either client's transport.
        """
        view = resolve(reverse(allauth_route)).func

        assert view.view_class is AtomicSignupView


class TestSomebodyWhoNeedsAnotherCode:
    """Both ways the verification screen used to become a dead end."""

    def test_a_second_code_can_be_requested(
        self, anonymous_client, signup_url, email, policy_documents
    ):
        """`ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_RESEND` gates this endpoint entirely.

        Unset it defaults to False, which makes `can_resend` permanently False and every
        resend a bare 409 with no body -- so with verification mandatory, a first code
        that went missing left no way forward at all.
        """
        signup = _post_signup(anonymous_client, signup_url, email)
        session_token = signup.json()["meta"]["session_token"]

        # The `confirm_email` cooldown (`1/10s` per address) was just spent by signup's
        # own send, and it lives in the cache. Left warm, `process.resend()` raises
        # `RateLimited` and the endpoint answers 429 -- which would pass the "not 409"
        # assertion below for the wrong reason. Cleared, this test is about the quota.
        cache.clear()

        response = anonymous_client.post(
            reverse("headless:app:account:resend_email_verification_code"),
            {},
            format="json",
            headers={"X-Session-Token": session_token},
        )

        assert response.status_code == status.HTTP_200_OK, response.content

    def test_a_correct_password_is_not_called_a_failed_attempt(
        self, anonymous_client, signup_url, email, policy_documents
    ):
        """Signing in seconds after signing up, with the right password, first try.

        `is_login_rate_limited` consults the `confirm_email` cooldown -- completing this
        login would mean sending a new code -- so allauth answers
        `too_many_login_attempts`. Its stock wording claims *failed* attempts, which is
        false in every word that matters and sends somebody off to reset a password that
        was never wrong. `AccountAdapter.error_messages` replaces it.
        """
        _post_signup(anonymous_client, signup_url, email)

        response = anonymous_client.post(
            reverse("headless:app:account:login"),
            {"email": email, "password": PASSWORD},
            format="json",
        )

        error = response.json()["errors"][0]
        assert error["code"] == "too_many_login_attempts"
        assert "failed" not in error["message"].lower()
        assert "wait a minute" in error["message"].lower()
