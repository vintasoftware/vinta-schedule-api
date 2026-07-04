"""
Phase 4 — HTTP-level test: consent capture through the real headless signup
endpoint (`POST /auth/{client}/v1/auth/signup`).

``HEADLESS_ONLY = True`` (see ``vinta_schedule_api/settings/base.py``), so the
headless endpoint -- not a direct call to
``BaseVintaScheduleSignupForm.signup()`` -- is the only production
email/password signup surface. The form-level tests in
``test_signup_consent.py`` cover the consent-capture behavior in detail; this
file proves the same behavior fires end-to-end through the real HTTP request
allauth-headless routes to ``form.signup(request, user)``.
"""

from django.urls import reverse

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from legal.factories import PolicyDocumentFactory
from legal.models import ConsentSource, PolicyDocumentType, UserConsent
from users.models import User


pytestmark = pytest.mark.django_db


def _signup_payload(**overrides):
    data = {
        "email": "headless-signup@example.com",
        "phone": "+123456789",
        "password1": "Sup3r-Secret-Passw0rd!",
        "password2": "Sup3r-Secret-Passw0rd!",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "accepted_policies": True,
    }
    data.update(overrides)
    return data


class TestHeadlessSignupRecordsConsent:
    """POST /auth/app/v1/auth/signup with accepted_policies=True records consent."""

    def test_accepted_policies_true_creates_user_and_sms_consent(self):
        for document_type in PolicyDocumentType.values:
            PolicyDocumentFactory().create(document_type=document_type, version=1)

        client = APIClient()
        url = reverse("headless:app:account:signup")

        response = client.post(url, _signup_payload(), format="json")

        # Email verification is mandatory, so a fresh signup responds 401
        # (pending email verification) rather than 200 -- the account itself
        # is created regardless.
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.json()

        user = User.objects.get(email="headless-signup@example.com")
        consent = UserConsent.objects.get(
            user=user, policy_document__document_type=PolicyDocumentType.SMS_CONSENT
        )
        assert consent.source == ConsentSource.SIGNUP_FORM
        # Phase 8 -- phone-keyed consent: allauth's adapter.save_user() sets
        # user.phone_number from the submitted "phone" field before our custom
        # form's signup() runs, so the phone lands on the recorded consent.
        assert user.phone_number == "+123456789"
        assert consent.phone_number == "+123456789"

    def test_missing_accepted_policies_rejects_signup(self):
        for document_type in PolicyDocumentType.values:
            PolicyDocumentFactory().create(document_type=document_type, version=1)

        client = APIClient()
        url = reverse("headless:app:account:signup")

        response = client.post(
            url,
            _signup_payload(email="headless-no-consent@example.com", accepted_policies=False),
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert not User.objects.filter(email="headless-no-consent@example.com").exists()
        assert not UserConsent.objects.filter(
            user__email="headless-no-consent@example.com"
        ).exists()
