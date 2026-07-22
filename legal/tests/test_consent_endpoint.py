"""
Integration tests for the authenticated consent-record endpoint.

`POST /consents/` is the OAuth post-signup step: OAuth signups collect no
phone/consent at signup, so the frontend calls this endpoint (authenticated,
after social login completes) to record acceptance of a policy document
type with `source=ConsentSource.OAUTH_STEP`, capturing IP + User-Agent.

Covers:
- Authenticated POST records a UserConsent with source=OAUTH_STEP + IP/UA.
- Unauthenticated POST -> 401.
- Unknown document_type -> 400.
- No published document of a known type -> 400 (NoPolicyDocumentError).
"""

from django.urls import reverse

import pytest
from rest_framework import status

from legal.factories import PolicyDocumentFactory
from legal.models import ConsentSource, PolicyDocumentType, UserConsent


pytestmark = pytest.mark.django_db

CONSENTS_URL = "api:Consents-list"


def _consents_url() -> str:
    return reverse(CONSENTS_URL)


class TestRecordConsentEndpoint:
    def test_authenticated_post_records_consent_with_oauth_step_source(self, auth_client, user):
        latest = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )

        response = auth_client.post(
            _consents_url(),
            {"document_type": PolicyDocumentType.SMS_CONSENT},
            HTTP_USER_AGENT="oauth-frontend/1.0",
            REMOTE_ADDR="198.51.100.7",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        consent = UserConsent.objects.get(user=user, policy_document=latest)
        assert consent.source == ConsentSource.OAUTH_STEP
        assert consent.ip_address == "198.51.100.7"
        assert consent.user_agent == "oauth-frontend/1.0"

        data = response.json()
        assert data["document_type"] == PolicyDocumentType.SMS_CONSENT
        assert data["source"] == ConsentSource.OAUTH_STEP

    def test_optional_phone_number_is_persisted(self, auth_client, user):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)

        response = auth_client.post(
            _consents_url(),
            {
                "document_type": PolicyDocumentType.SMS_CONSENT,
                "phone_number": "+15555550100",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        consent = UserConsent.objects.get(user=user)
        assert consent.phone_number == "+15555550100"
        assert response.json()["phone_number"] == "+15555550100"

    def test_phone_number_omitted_defaults_to_blank(self, auth_client, user):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)

        response = auth_client.post(
            _consents_url(), {"document_type": PolicyDocumentType.SMS_CONSENT}
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        consent = UserConsent.objects.get(user=user)
        assert consent.phone_number == ""

    def test_unauthenticated_returns_401(self, anonymous_client):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)

        response = anonymous_client.post(
            _consents_url(), {"document_type": PolicyDocumentType.SMS_CONSENT}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert not UserConsent.objects.exists()

    def test_unknown_document_type_returns_400(self, auth_client):
        response = auth_client.post(_consents_url(), {"document_type": "not_a_real_type"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not UserConsent.objects.exists()

    def test_unpublished_document_type_returns_400(self, auth_client):
        """A known enum value with zero published PolicyDocument rows -> 400."""
        response = auth_client.post(
            _consents_url(), {"document_type": PolicyDocumentType.TERMS_OF_USE}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not UserConsent.objects.exists()
