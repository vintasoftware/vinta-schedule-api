"""
Tests for consent capture in the email/password signup form (Phase 4 / Phase 8).

Covers:
- Completing the signup form with `accepted_policies=True` records a
  version-pinned SMS_CONSENT UserConsent with source=SIGNUP_FORM, capturing
  the request's client IP + User-Agent.
- Consent is recorded for all three PolicyDocumentType values when published.
- Missing / false `accepted_policies` -> form invalid, signup never runs.
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
        "accepted_policies": True,
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


class TestAcceptedPoliciesRequired:
    """`accepted_policies` is a required, must-be-True acknowledgement field."""

    def test_missing_acceptance_makes_form_invalid(self):
        data = _signup_form_data()
        del data["accepted_policies"]

        form = BaseVintaScheduleSignupForm(data=data)

        assert not form.is_valid()
        assert "accepted_policies" in form.errors

    def test_false_acceptance_makes_form_invalid(self):
        form = BaseVintaScheduleSignupForm(data=_signup_form_data(accepted_policies=False))

        assert not form.is_valid()
        assert "accepted_policies" in form.errors
