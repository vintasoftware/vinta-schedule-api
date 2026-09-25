"""A password reset request sends one working email, and its link opens the frontend.

Three bugs stacked on this path, each hidden by the one before it:

* ``AccountAdapter.send_password_reset_mail`` called ``super()``, so allauth sent its own
  generic reset email first;
* it then built the link by reversing ``account_reset_password_from_key``, a route
  ``HEADLESS_ONLY`` never mounts, so the request ended in a 500; and
* the notification's body template had a stray ``{% endif %}``, so it could never render.

The unit test in ``test_account_adapters.py`` hands the adapter a ready-made context and
mocks the notification, so it could catch none of them. These tests go through the real
headless endpoint and let the notification render and send, into the test mail outbox.
"""

import uuid

from django.core import mail
from django.urls import reverse

import pytest
from allauth.account.models import EmailAddress
from rest_framework import status

from users.factories import UserFactory
from users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    """A user with a verified address no other test in this process has used.

    allauth rate-limits reset requests per address, and that counter lives in the cache,
    which is not rolled back between tests.
    """
    email = f"reset-{uuid.uuid4().hex[:12]}@example.com"
    user = UserFactory().create_user(email=email, first_name="Ada")
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def _request_reset(client, email: str):
    return client.post(
        reverse("headless:browser:account:request_password_reset"),
        {"email": email},
        format="json",
    )


def _full_text(message: mail.EmailMessage) -> str:
    """The plain body plus every alternative, so the check does not depend on which
    part the notification adapter puts the HTML in."""
    parts = [str(message.body)]
    parts += [str(content) for content, _ in getattr(message, "alternatives", [])]
    return "\n".join(parts)


class TestPasswordResetRequest:
    def test_sends_exactly_one_email(self, anonymous_client, user):
        response = _request_reset(anonymous_client, user.email)

        assert response.status_code == status.HTTP_200_OK
        # allauth's own email would be a second message, titled "[example.com] Password
        # Reset Email".
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [user.email]
        assert "Password reset" in mail.outbox[0].subject

    def test_link_opens_the_frontend_reset_page(self, anonymous_client, user, settings):
        _request_reset(anonymous_client, user.email)

        frontend_prefix = settings.HEADLESS_FRONTEND_URLS["account_reset_password_from_key"]
        frontend_prefix = frontend_prefix.removesuffix("{key}")
        body = _full_text(mail.outbox[0])
        assert f'href="{frontend_prefix}' in body
        assert f'href="{frontend_prefix}"' not in body, "the reset key is missing"

    def test_greets_the_user_by_name(self, anonymous_client, user):
        _request_reset(anonymous_client, user.email)

        body = _full_text(mail.outbox[0])
        assert "Dear Ada," in body
        assert "Reset your password" in body
