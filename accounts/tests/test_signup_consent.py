"""
Tests for consent capture in the email/password signup form (Phase 4 / Phase 8 /
Phase 9).

Covers:
- Completing the signup form with `accepted_terms=True` and
  `accepted_sms_consent=True` records a version-pinned SMS_CONSENT UserConsent
  with source=SIGNUP_FORM, capturing the request's client IP + User-Agent.
- Consent is recorded for all three PolicyDocumentType values when published.
- Missing / false `accepted_terms` -> form invalid, signup never runs.
- Missing / false `accepted_sms_consent` -> form invalid, signup never runs.
- SMS_CONSENT recording is driven specifically by `accepted_sms_consent`,
  independently of `accepted_terms` (Phase 9 — separate SMS consent
  checkbox, Twilio/TCPA compliance).
- A document type with no published version yet is guarded (logged, not
  raised) -- signup still succeeds.
- Phase 8: every recorded consent row carries `phone_number=user.phone_number`
  (phone-keyed consent) -- blank for the email path (no phone collected at
  signup), populated when the user already has one on the model.
"""

import pytest

from accounts.base_forms import BaseVintaScheduleSignupForm
from legal.factories import PolicyDocumentFactory
from legal.models import ConsentSource, PolicyDocumentType, UserConsent
from users.factories import UserFactory


pytestmark = pytest.mark.django_db


def _signup_form_data(**overrides):
    data = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "organization_name": "ACME Corp",
        "accepted_terms": True,
        "accepted_sms_consent": True,
    }
    data.update(overrides)
    return data


class TestEmailSignupRecordsConsent:
    """Integration: BaseVintaScheduleSignupForm.signup() records consent."""

    def test_records_version_pinned_sms_consent_with_ip_and_user_agent(self, rf):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        latest_sms = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=2
        )
        user = UserFactory().create_user(email="consent-sms@example.com")
        # The email/password path collects no phone at signup -- model_bakery's
        # UserFactory otherwise fills phone_number with a random value that
        # doesn't represent that reality, so blank it explicitly here.
        user.phone_number = ""
        user.save()
        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors
        request = rf.post("/", REMOTE_ADDR="203.0.113.9", HTTP_USER_AGENT="pytest-agent/1.0")

        form.signup(request=request, user=user)

        consent = UserConsent.objects.get(
            user=user, policy_document__document_type=PolicyDocumentType.SMS_CONSENT
        )
        assert consent.policy_document == latest_sms
        assert consent.policy_document.version == 2
        assert consent.source == ConsentSource.SIGNUP_FORM
        assert consent.ip_address == "203.0.113.9"
        assert consent.user_agent == "pytest-agent/1.0"
        assert consent.phone_number == user.phone_number == ""

    def test_records_the_users_phone_number_when_already_set(self, rf):
        """The email/password path collects no phone at signup, but if the User
        already carries one (e.g. set by an earlier step), it must land on the
        consent row -- phone-keyed consent (Phase 8) is keyed off whatever
        `user.phone_number` holds at record time."""
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        user = UserFactory().create_user(email="consent-phone@example.com")
        user.phone_number = "+15555550100"
        user.save()
        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors
        request = rf.post("/", REMOTE_ADDR="203.0.113.9", HTTP_USER_AGENT="pytest-agent/1.0")

        form.signup(request=request, user=user)

        consent = UserConsent.objects.get(
            user=user, policy_document__document_type=PolicyDocumentType.SMS_CONSENT
        )
        assert consent.phone_number == "+15555550100"

    def test_records_consent_for_all_three_document_types_when_published(self):
        for document_type in PolicyDocumentType.values:
            PolicyDocumentFactory().create(document_type=document_type, version=1)
        user = UserFactory().create_user(email="consent-all@example.com")
        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors

        form.signup(request=None, user=user)

        recorded_types = set(
            UserConsent.objects.filter(user=user).values_list(
                "policy_document__document_type", flat=True
            )
        )
        assert recorded_types == set(PolicyDocumentType.values)
        assert all(
            consent.source == ConsentSource.SIGNUP_FORM
            for consent in UserConsent.objects.filter(user=user)
        )

    def test_signup_succeeds_when_non_sms_document_type_unpublished(self):
        """Only SMS_CONSENT is published; PRIVACY_POLICY / TERMS_OF_USE are not.

        Signup must still succeed -- the missing-document guard swallows
        NoPolicyDocumentError for the unpublished types, logging instead of
        raising.
        """
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        user = UserFactory().create_user(email="consent-partial@example.com")
        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors

        returned_user = form.signup(request=None, user=user)

        assert returned_user == user
        assert UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.SMS_CONSENT
        ).exists()
        assert not UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.PRIVACY_POLICY
        ).exists()
        assert not UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.TERMS_OF_USE
        ).exists()

    def test_signup_succeeds_when_no_policy_documents_published_at_all(self):
        """No PolicyDocument exists anywhere -- every record_consent call is guarded."""
        user = UserFactory().create_user(email="consent-none@example.com")
        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors

        returned_user = form.signup(request=None, user=user)

        assert returned_user == user
        assert not UserConsent.objects.filter(user=user).exists()


class TestAcceptedTermsRequired:
    """`accepted_terms` is a required, must-be-True acknowledgement field."""

    def test_missing_acceptance_makes_form_invalid(self):
        data = _signup_form_data()
        del data["accepted_terms"]

        form = BaseVintaScheduleSignupForm(data=data)

        assert not form.is_valid()
        assert "accepted_terms" in form.errors

    def test_false_acceptance_makes_form_invalid(self):
        form = BaseVintaScheduleSignupForm(data=_signup_form_data(accepted_terms=False))

        assert not form.is_valid()
        assert "accepted_terms" in form.errors


class TestAcceptedSmsConsentRequired:
    """`accepted_sms_consent` is its own required, must-be-True acknowledgement
    field -- Twilio / TCPA compliance requires SMS consent to be a distinct,
    explicit opt-in, not bundled with Terms/Privacy acceptance (Phase 9)."""

    def test_missing_acceptance_makes_form_invalid(self):
        data = _signup_form_data()
        del data["accepted_sms_consent"]

        form = BaseVintaScheduleSignupForm(data=data)

        assert not form.is_valid()
        assert "accepted_sms_consent" in form.errors

    def test_false_acceptance_makes_form_invalid(self):
        form = BaseVintaScheduleSignupForm(data=_signup_form_data(accepted_sms_consent=False))

        assert not form.is_valid()
        assert "accepted_sms_consent" in form.errors

    def test_accepted_terms_alone_does_not_satisfy_sms_consent(self):
        """Checking only the terms checkbox must not also satisfy the SMS
        checkbox -- the two are independent required fields."""
        form = BaseVintaScheduleSignupForm(
            data=_signup_form_data(accepted_terms=True, accepted_sms_consent=False)
        )

        assert not form.is_valid()
        assert "accepted_sms_consent" in form.errors
        assert "accepted_terms" not in form.errors

    def test_sms_consent_recording_driven_by_sms_field_not_terms_field(self, di_container):
        """SMS_CONSENT recording is driven specifically by `accepted_sms_consent`,
        independently of `accepted_terms`.

        Both fields are required for a *valid* form submission, so this
        exercises the recording helper directly with `accepted_terms=True` /
        `accepted_sms_consent=False` in `cleaned_data` -- a state a valid
        submission can never reach, but the one an implementation bug (e.g.
        recording all three document types whenever *any* consent field is
        truthy) would mishandle. This test would fail if SMS_CONSENT recording
        were driven by `accepted_terms` instead of `accepted_sms_consent`.
        """
        for document_type in PolicyDocumentType.values:
            PolicyDocumentFactory().create(document_type=document_type, version=1)

        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors
        # Simulate "terms accepted, SMS consent not accepted" -- impossible via
        # a real submission (both required), but proves the mapping's wiring.
        form.cleaned_data["accepted_terms"] = True
        form.cleaned_data["accepted_sms_consent"] = False

        user = UserFactory().create_user(email="consent-terms-only@example.com")
        consent_service = di_container.consent_service()

        form._record_signup_consents(request=None, user=user, consent_service=consent_service)

        assert not UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.SMS_CONSENT
        ).exists()
        assert UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.PRIVACY_POLICY
        ).exists()
        assert UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.TERMS_OF_USE
        ).exists()

    def test_sms_consent_recorded_when_sms_field_checked_even_if_terms_not(self, di_container):
        """Mirror case: `accepted_sms_consent=True` records SMS_CONSENT even
        with `accepted_terms=False` -- proving the SMS row is gated solely by
        its own field, not by terms acceptance."""
        for document_type in PolicyDocumentType.values:
            PolicyDocumentFactory().create(document_type=document_type, version=1)

        form = BaseVintaScheduleSignupForm(data=_signup_form_data())
        assert form.is_valid(), form.errors
        form.cleaned_data["accepted_terms"] = False
        form.cleaned_data["accepted_sms_consent"] = True

        user = UserFactory().create_user(email="consent-sms-only@example.com")
        consent_service = di_container.consent_service()

        form._record_signup_consents(request=None, user=user, consent_service=consent_service)

        assert UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.SMS_CONSENT
        ).exists()
        assert not UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.PRIVACY_POLICY
        ).exists()
        assert not UserConsent.objects.filter(
            user=user, policy_document__document_type=PolicyDocumentType.TERMS_OF_USE
        ).exists()
