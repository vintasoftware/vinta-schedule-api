"""Phase 5 / Phase 8 — SMS consent enforcement gate.

Covers all three SMS-sending entry points on ``AccountAdapter``, all gated on
phone-keyed consent tied to phone ownership (BLOCKER-1 security fix — see
``legal.managers.UserConsentManager``):

- ``send_verification_code_sms``: refuses to dispatch (zero calls to
  ``notification_service.create_notification``) when
  ``ConsentService.has_sms_consent_for_phone_and_user`` is False for the
  submitted phone + the requesting ``user``, signaling the refusal via
  ``ConsentRequiredError`` (a clean 403). Also fails closed (zero dispatch)
  when the consent check itself raises. This gate is tied to *the requesting
  user's own* consent row rather than `user.phone_number == phone`: allauth's
  `ChangePhoneForm` rejects a submitted phone equal to the user's current
  phone before the adapter is ever called, and `set_phone` only runs *after*
  the code is verified, so `user.phone_number` can never already equal the
  phone being verified for a first-time add/change (verified empirically).
- ``send_unknown_account_sms`` / ``send_account_already_exists_sms``: gated on
  ``ConsentService.has_sms_consent_for_phone`` (no `user` at their call site,
  so ownership is enforced via `user__phone_number == phone` on the consent
  row itself). When the phone has no (owned) consent, this is a **silent
  no-op** — no SMS dispatched, no error raised — preserving allauth's uniform
  anti-enumeration response. When the phone has owned consent, dispatch
  proceeds as before.
- Integration: the real headless phone-verification entry point
  (``POST /auth/browser/v1/account/phone``) returns a deterministic, well-formed
  4xx (not a 500) for a consent-less phone and dispatches zero notifications; a
  consented phone still receives the OTP. ``ACCOUNT_PHONE_VERIFICATION_ENABLED``
  is off in production settings until Phase 6 — it is overridden here, in-test
  only, to exercise the gate through the real view/flow.
"""

import json
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.urls import reverse

import pytest
from model_bakery import baker

from accounts.account_adapters import AccountAdapter
from accounts.exceptions import ConsentRequiredError
from legal.factories import PolicyDocumentFactory, UserConsentFactory
from legal.models import PolicyDocumentType
from legal.services import ConsentService
from users.models import User


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
        adapter.consent_service.has_sms_consent_for_phone_and_user.return_value = True

        adapter.send_verification_code_sms(user, "+15555550100", "1234")

        adapter.consent_service.has_sms_consent_for_phone_and_user.assert_called_once_with(
            "+15555550100", user
        )
        adapter.notification_service.create_notification.assert_called_once()

    def test_refuses_when_consent_missing(self, adapter, user):
        adapter.consent_service.has_sms_consent_for_phone_and_user.return_value = False

        with pytest.raises(ConsentRequiredError):
            adapter.send_verification_code_sms(user, "+15555550100", "1234")

        adapter.consent_service.has_sms_consent_for_phone_and_user.assert_called_once_with(
            "+15555550100", user
        )
        adapter.notification_service.create_notification.assert_not_called()

    def test_refusal_error_carries_a_well_formed_client_response(self, adapter, user):
        adapter.consent_service.has_sms_consent_for_phone_and_user.return_value = False

        with pytest.raises(ConsentRequiredError) as exc_info:
            adapter.send_verification_code_sms(user, "+15555550100", "1234")

        error = exc_info.value
        assert error.response.status_code == 403
        body = json.loads(error.response.content)
        assert body["errors"][0]["code"] == "consent_required"

    def test_logs_a_warning_on_refusal(self, adapter, user):
        adapter.consent_service.has_sms_consent_for_phone_and_user.return_value = False

        with patch("accounts.account_adapters.logger.warning") as log_warn:
            with pytest.raises(ConsentRequiredError):
                adapter.send_verification_code_sms(user, "+15555550100", "1234")

        log_warn.assert_called_once()

    def test_fails_closed_when_consent_check_raises(self, adapter, user):
        adapter.consent_service.has_sms_consent_for_phone_and_user.side_effect = RuntimeError(
            "db unavailable"
        )

        with pytest.raises(RuntimeError):
            adapter.send_verification_code_sms(user, "+15555550100", "1234")

        adapter.notification_service.create_notification.assert_not_called()


@pytest.mark.django_db
class TestSendUnknownAccountSmsConsentGate:
    """Anti-enumeration send for a phone with no matching account.

    Phase 8: gated on phone-keyed consent. No consent -> silent no-op (no SMS,
    no error) so the caller can't distinguish "no consent" from "SMS sent",
    preserving allauth's enumeration-prevention guarantee.
    """

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

    def test_dispatches_nothing_and_raises_nothing_when_no_consent(self, adapter):
        adapter.consent_service.has_sms_consent_for_phone.return_value = False

        result = adapter.send_unknown_account_sms("+15555550100")

        assert result is None
        adapter.consent_service.has_sms_consent_for_phone.assert_called_once_with("+15555550100")
        adapter.notification_service.create_one_off_notification.assert_not_called()

    def test_dispatches_when_phone_has_consent(self, adapter):
        adapter.consent_service.has_sms_consent_for_phone.return_value = True

        adapter.send_unknown_account_sms("+15555550100")

        adapter.notification_service.create_one_off_notification.assert_called_once()

    def test_logs_a_warning_on_silent_refusal(self, adapter):
        adapter.consent_service.has_sms_consent_for_phone.return_value = False

        with patch("accounts.account_adapters.logger.warning") as log_warn:
            adapter.send_unknown_account_sms("+15555550100")

        log_warn.assert_called_once()

    def test_no_phone_is_a_no_op(self, adapter):
        adapter.send_unknown_account_sms(None)

        adapter.consent_service.has_sms_consent_for_phone.assert_not_called()
        adapter.notification_service.create_one_off_notification.assert_not_called()


@pytest.mark.django_db
class TestSendAccountAlreadyExistsSmsConsentGate:
    """Anti-enumeration send for a phone that already has an account.

    Phase 8: gated on phone-keyed consent. No consent -> silent no-op (no SMS,
    no error).
    """

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

    def test_dispatches_nothing_and_raises_nothing_when_no_consent(self, adapter):
        adapter.consent_service.has_sms_consent_for_phone.return_value = False

        result = adapter.send_account_already_exists_sms("+15555550100")

        assert result is None
        adapter.consent_service.has_sms_consent_for_phone.assert_called_once_with("+15555550100")
        adapter.notification_service.create_one_off_notification.assert_not_called()

    def test_dispatches_when_phone_has_consent(self, adapter):
        adapter.consent_service.has_sms_consent_for_phone.return_value = True

        adapter.send_account_already_exists_sms("+15555550100")

        adapter.notification_service.create_one_off_notification.assert_called_once()

    def test_logs_a_warning_on_silent_refusal(self, adapter):
        adapter.consent_service.has_sms_consent_for_phone.return_value = False

        with patch("accounts.account_adapters.logger.warning") as log_warn:
            adapter.send_account_already_exists_sms("+15555550100")

        log_warn.assert_called_once()

    def test_no_phone_is_a_no_op(self, adapter):
        adapter.send_account_already_exists_sms(None)

        adapter.consent_service.has_sms_consent_for_phone.assert_not_called()
        adapter.notification_service.create_one_off_notification.assert_not_called()


@pytest.mark.django_db
class TestPhoneVerifyConsentGateIntegration:
    """Drives the real headless phone-verification entry point end-to-end."""

    url = "/auth/browser/v1/account/phone"

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_consent_less_phone_is_refused_with_zero_notifications(self, auth_client):
        with patch(
            "vintasend.services.notification_service.NotificationService.create_notification"
        ) as mock_create:
            response = auth_client.post(self.url, {"phone": "+15555550100"}, format="json")

        assert response.status_code == 403
        assert response.json()["errors"][0]["code"] == "consent_required"
        mock_create.assert_not_called()

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_consented_phone_receives_otp(self, auth_client, user):
        UserConsentFactory().create(
            user=user,
            document_type=PolicyDocumentType.SMS_CONSENT,
            phone_number="+15555550101",
        )

        with patch(
            "vintasend.services.notification_service.NotificationService.create_notification"
        ) as mock_create:
            response = auth_client.post(self.url, {"phone": "+15555550101"}, format="json")

        assert response.status_code == 202
        mock_create.assert_called_once()


@pytest.mark.django_db
class TestAntiEnumerationSmsRealFlowIntegration:
    """Security-review SHOULD-FIX: exercise the anti-enumeration gate through a
    real ``ConsentService`` + DB (not a mocked ``consent_service``), proving the
    silent no-op / dispatch behavior end-to-end.

    ``send_unknown_account_sms`` fires only when no user anywhere has
    `phone_number == phone` (that is what makes the account "unknown"), so
    under the ownership-joined gate (BLOCKER 1) it can never dispatch — a
    consent row's ownership check (`user__phone_number=phone`) is
    structurally unsatisfiable there. Only the unconsented (no-op) case is
    exercised for it; the "dispatches when owned" case is covered for
    ``send_account_already_exists_sms``, where a real, matching account can
    exist.
    """

    @pytest.fixture
    def notification_service(self):
        return MagicMock()

    @pytest.fixture
    def adapter(self, notification_service):
        return AccountAdapter(
            notification_service=notification_service,
            consent_service=ConsentService(),
        )

    def test_send_unknown_account_sms_is_a_silent_no_op_for_an_unconsented_phone(self, adapter):
        result = adapter.send_unknown_account_sms("+15555550200")

        assert result is None
        adapter.notification_service.create_one_off_notification.assert_not_called()

    def test_send_account_already_exists_sms_is_a_silent_no_op_for_an_unconsented_phone(
        self, adapter
    ):
        result = adapter.send_account_already_exists_sms("+15555550201")

        assert result is None
        adapter.notification_service.create_one_off_notification.assert_not_called()

    def test_send_account_already_exists_sms_dispatches_for_a_consented_owned_phone(self, adapter):
        owner: User = baker.make(User, phone_number="+15555550202")
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        UserConsentFactory().create(
            owner, document_type=PolicyDocumentType.SMS_CONSENT, phone_number="+15555550202"
        )

        adapter.send_account_already_exists_sms("+15555550202")

        adapter.notification_service.create_one_off_notification.assert_called_once()


@pytest.mark.django_db
class TestConsentPhoneNormalizationRegression:
    """Security-review BLOCKER 2 regression: a human-formatted phone posted to
    ``/consents/`` must still satisfy the SMS gate for the E.164-normalized
    phone allauth passes to the adapter.
    """

    def test_format_mismatched_consent_phone_still_satisfies_the_gate(self, auth_client, user):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)

        response = auth_client.post(
            reverse("api:Consents-list"),
            {
                "document_type": PolicyDocumentType.SMS_CONSENT,
                "phone_number": "+1 555-555-0102",
            },
        )

        assert response.status_code == 201, response.data
        assert response.json()["phone_number"] == "+15555550102"

        adapter = AccountAdapter(
            notification_service=MagicMock(),
            consent_service=ConsentService(),
        )

        adapter.send_verification_code_sms(user, "+15555550102", "1234")

        adapter.notification_service.create_notification.assert_called_once()
