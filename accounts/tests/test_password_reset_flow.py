"""A password reset request sends one email, and its link opens the frontend.

``AccountAdapter.send_password_reset_mail`` used to call ``super()``, so allauth sent its
own reset email next to the vintasend notification and the user got two. It also built
the link by reversing ``account_reset_password_from_key``, a route ``HEADLESS_ONLY`` never
mounts.

The unit test in ``test_account_adapters.py`` hands the adapter a ready-made context, so
it could not catch either bug. These tests go through the real headless endpoint, so the
context comes from allauth itself.
"""

import uuid
from unittest import mock

from django.core import mail
from django.urls import reverse

import pytest
from allauth.account.models import EmailAddress
from rest_framework import status

from users.factories import UserFactory


pytestmark = pytest.mark.django_db


@pytest.fixture
def email():
    """An address no other test in this process has used.

    allauth rate-limits reset requests per address, and that counter lives in the cache,
    which is not rolled back between tests.
    """
    return f"reset-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def create_notification():
    """Capture the reset notification instead of rendering and sending it."""
    from di_core.containers import container

    with mock.patch.object(container.notification_service(), "create_notification") as patched:
        yield patched


def _request_reset(client, email: str):
    return client.post(
        reverse("headless:browser:account:request_password_reset"),
        {"email": email},
        format="json",
    )


class TestPasswordResetRequest:
    def test_sends_one_notification_linking_to_the_frontend(
        self, anonymous_client, email, create_notification, settings
    ):
        user = UserFactory().create_user(email=email)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)

        response = _request_reset(anonymous_client, email)

        assert response.status_code == status.HTTP_200_OK
        create_notification.assert_called_once()
        context_kwargs = create_notification.call_args.kwargs["context_kwargs"]
        assert context_kwargs["user_id"] == user.id
        frontend_prefix = settings.HEADLESS_FRONTEND_URLS["account_reset_password_from_key"]
        frontend_prefix = frontend_prefix.removesuffix("{key}")
        assert context_kwargs["password_reset_url"].startswith(frontend_prefix)
        assert context_kwargs["password_reset_url"] != frontend_prefix, "the key is missing"

    def test_allauth_sends_no_email_of_its_own(self, anonymous_client, email, create_notification):
        user = UserFactory().create_user(email=email)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)

        _request_reset(anonymous_client, email)

        create_notification.assert_called_once()
        # The notification is mocked, so any email in the outbox came from allauth.
        assert mail.outbox == []
