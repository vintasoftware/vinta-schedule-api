"""Phase 5 — SMS consent enforcement gate.

Covers both layers of the gate:

- Unit: ``AccountAdapter.send_verification_code_sms`` refuses to dispatch a
  verification SMS (zero calls to ``notification_service.create_notification``)
  when ``ConsentService.has_sms_consent`` is False, and signals the refusal via
  ``ConsentRequiredError``.
- Integration: the real headless phone-verification entry point
  (``POST /auth/browser/v1/account/phone``) returns a deterministic, well-formed
  4xx (not a 500) for a consent-less user and dispatches zero notifications; a
  consented user still receives the OTP. ``ACCOUNT_PHONE_VERIFICATION_ENABLED``
  is off in production settings until Phase 6 — it is overridden here, in-test
  only, to exercise the gate through the real view/flow.
"""

import json
from unittest.mock import MagicMock, patch

from django.test import override_settings

import pytest

from accounts.account_adapters import AccountAdapter
from accounts.exceptions import ConsentRequiredError
from legal.factories import UserConsentFactory
from legal.models import PolicyDocumentType


@pytest.mark.django_db
class TestSendVerificationCodeSmsConsentGate:
    """Unit-level: adapter dependencies fully controlled (consent_service mocked)."""

    @pytest.fixture
    def notification_service(self):
        return MagicMock()

    @pytest.fixture
    def consent_service(self):
        return MagicMock()

    @pytest.fixture
    def adapter(self, notification_service, consent_service):
        return AccountAdapter(
            notification_service=notification_service,
            consent_service=consent_service,
        )

    def test_sends_when_consent_recorded(self, adapter, user):
        adapter.consent_service.has_sms_consent.return_value = True

        adapter.send_verification_code_sms(user, "+15555550100", "1234")

        adapter.consent_service.has_sms_consent.assert_called_once_with(user)
        adapter.notification_service.create_notification.assert_called_once()

    def test_refuses_when_consent_missing(self, adapter, user):
        adapter.consent_service.has_sms_consent.return_value = False

        with pytest.raises(ConsentRequiredError):
            adapter.send_verification_code_sms(user, "+15555550100", "1234")

        adapter.consent_service.has_sms_consent.assert_called_once_with(user)
        adapter.notification_service.create_notification.assert_not_called()

    def test_refusal_error_carries_a_well_formed_client_response(self, adapter, user):
        adapter.consent_service.has_sms_consent.return_value = False

        with pytest.raises(ConsentRequiredError) as exc_info:
            adapter.send_verification_code_sms(user, "+15555550100", "1234")

        error = exc_info.value
        assert error.response.status_code == 403
        body = json.loads(error.response.content)
        assert body["errors"][0]["code"] == "consent_required"

    def test_logs_a_warning_on_refusal(self, adapter, user):
        adapter.consent_service.has_sms_consent.return_value = False

        with patch("accounts.account_adapters.logger.warning") as log_warn:
            with pytest.raises(ConsentRequiredError):
                adapter.send_verification_code_sms(user, "+15555550100", "1234")

        log_warn.assert_called_once()


@pytest.mark.django_db
class TestPhoneVerifyConsentGateIntegration:
    """Drives the real headless phone-verification entry point end-to-end."""

    url = "/auth/browser/v1/account/phone"

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_consent_less_user_is_refused_with_zero_notifications(self, auth_client):
        with patch(
            "vintasend.services.notification_service.NotificationService.create_notification"
        ) as mock_create:
            response = auth_client.post(self.url, {"phone": "+15555550100"}, format="json")

        assert response.status_code == 403
        assert response.json()["errors"][0]["code"] == "consent_required"
        mock_create.assert_not_called()

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_consented_user_receives_otp(self, auth_client, user):
        UserConsentFactory().create(user=user, document_type=PolicyDocumentType.SMS_CONSENT)

        with patch(
            "vintasend.services.notification_service.NotificationService.create_notification"
        ) as mock_create:
            response = auth_client.post(self.url, {"phone": "+15555550101"}, format="json")

        assert response.status_code == 202
        mock_create.assert_called_once()
