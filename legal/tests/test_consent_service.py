"""Tests for ConsentService.

Audit-emission tests mirror organizations/tests/test_audit.py: patch
``audit.services.persist_audit_record`` and execute on_commit callbacks so the
enqueue happens, then inspect the serialized payloads.
"""

from unittest.mock import patch

import pytest
from model_bakery import baker

from audit.constants import AuditAction
from legal.exceptions import NoPolicyDocumentError
from legal.factories import PolicyDocumentFactory, UserConsentFactory
from legal.models import ConsentSource, PolicyDocumentType, UserConsent
from legal.services import ConsentService
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import User


pytestmark = pytest.mark.django_db


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


class TestRecordConsent:
    def test_pins_the_latest_published_version(self) -> None:
        user: User = baker.make(User)
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        latest = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=2)
        service = ConsentService()

        consent = service.record_consent(
            user,
            PolicyDocumentType.SMS_CONSENT,
            source=ConsentSource.SIGNUP_FORM,
        )

        assert consent.policy_document == latest
        assert consent.policy_document.version == 2

    def test_stores_ip_user_agent_and_source(self) -> None:
        user: User = baker.make(User)
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        service = ConsentService()

        consent = service.record_consent(
            user,
            PolicyDocumentType.SMS_CONSENT,
            source=ConsentSource.OAUTH_STEP,
            ip="203.0.113.5",
            user_agent="Mozilla/5.0 test-agent",
        )

        consent.refresh_from_db()
        assert consent.user_id == user.id
        assert consent.ip_address == "203.0.113.5"
        assert consent.user_agent == "Mozilla/5.0 test-agent"
        assert consent.source == ConsentSource.OAUTH_STEP

    def test_stores_phone_number_when_provided(self) -> None:
        user: User = baker.make(User)
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        service = ConsentService()

        consent = service.record_consent(
            user,
            PolicyDocumentType.SMS_CONSENT,
            source=ConsentSource.SIGNUP_FORM,
            phone_number="+15555550100",
        )

        consent.refresh_from_db()
        assert consent.phone_number == "+15555550100"

    def test_phone_number_defaults_to_blank(self) -> None:
        user: User = baker.make(User)
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        service = ConsentService()

        consent = service.record_consent(
            user,
            PolicyDocumentType.SMS_CONSENT,
            source=ConsentSource.SIGNUP_FORM,
        )

        consent.refresh_from_db()
        assert consent.phone_number == ""

    def test_raises_when_no_policy_document_exists_for_type(self) -> None:
        user: User = baker.make(User)
        service = ConsentService()

        with pytest.raises(NoPolicyDocumentError):
            service.record_consent(
                user,
                PolicyDocumentType.SMS_CONSENT,
                source=ConsentSource.SIGNUP_FORM,
            )

    def test_emits_audit_create_entry_when_user_has_active_membership(
        self, django_capture_on_commit_callbacks
    ) -> None:
        user: User = baker.make(User)
        org = baker.make(Organization)
        OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.MEMBER
        )
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        service = ConsentService()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                consent = service.record_consent(
                    user,
                    PolicyDocumentType.SMS_CONSENT,
                    source=ConsentSource.SIGNUP_FORM,
                )

        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        assert payloads[0]["organization_id"] == org.id
        assert payloads[0]["action"] == AuditAction.CREATE
        assert payloads[0]["subject"]["subject_type"] == "legal.UserConsent"
        assert payloads[0]["subject"]["subject_id"] == str(consent.id)
        assert payloads[0]["actor"]["actor_type"] == "membership"
        assert payloads[0]["actor"]["actor_id"] == user.id

    def test_skips_audit_when_user_has_no_organization(
        self, django_capture_on_commit_callbacks
    ) -> None:
        user: User = baker.make(User)
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        service = ConsentService()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record_consent(
                    user,
                    PolicyDocumentType.SMS_CONSENT,
                    source=ConsentSource.SIGNUP_FORM,
                )

        assert mock_task.delay.call_count == 0


class TestHasSmsConsent:
    def test_true_when_sms_consent_row_exists_any_version(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=3
        )
        UserConsentFactory().create(user, policy_document=document)
        service = ConsentService()

        assert service.has_sms_consent(user) is True

    def test_false_when_no_consent_rows_exist(self) -> None:
        user: User = baker.make(User)
        service = ConsentService()

        assert service.has_sms_consent(user) is False

    def test_false_when_only_other_document_type_consented(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY, version=1
        )
        UserConsentFactory().create(user, policy_document=document)
        service = ConsentService()

        assert service.has_sms_consent(user) is False

    def test_false_for_a_different_users_sms_consent(self) -> None:
        user: User = baker.make(User)
        other_user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(other_user, policy_document=document)
        service = ConsentService()

        assert service.has_sms_consent(user) is False


class TestHasSmsConsentForPhone:
    """Phone-keyed consent gate, tied to phone ownership.

    A consent row only satisfies the gate when its own `user.phone_number`
    also equals the checked phone (ownership join) — this stops an attacker
    fabricating a consent row for a phone they don't own (their own user, a
    victim's phone) from unlocking SMS sends to that phone.
    """

    def test_true_when_phone_has_sms_consent_row_and_user_owns_the_phone(self) -> None:
        user: User = baker.make(User, phone_number="+15555550100")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550100")
        service = ConsentService()

        assert service.has_sms_consent_for_phone("+15555550100") is True

    def test_false_for_a_different_phone(self) -> None:
        user: User = baker.make(User, phone_number="+15555550100")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550100")
        service = ConsentService()

        assert service.has_sms_consent_for_phone("+15555550999") is False

    def test_false_for_blank_phone_even_with_matching_blank_rows(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="")
        service = ConsentService()

        assert service.has_sms_consent_for_phone("") is False

    def test_false_when_phone_only_consented_to_other_document_type(self) -> None:
        user: User = baker.make(User, phone_number="+15555550100")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550100")
        service = ConsentService()

        assert service.has_sms_consent_for_phone("+15555550100") is False

    def test_true_regardless_of_which_user_recorded_it_as_long_as_they_own_the_phone(
        self,
    ) -> None:
        """Phone-keyed, not tied to a *specific* user — but ownership still applies."""
        user: User = baker.make(User)
        other_user: User = baker.make(User, phone_number="+15555550100")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(
            other_user, policy_document=document, phone_number="+15555550100"
        )
        service = ConsentService()

        assert service.has_sms_consent_for_phone("+15555550100") is True
        assert user  # sanity: user exists but is irrelevant to the phone-keyed check

    def test_false_when_consent_recorded_by_a_user_who_does_not_own_the_phone(self) -> None:
        """An attacker fabricating a row for a victim's phone must not satisfy the gate.

        `user_a` owns phone X but records a consent row claiming phone Y (the
        victim's phone) — `has_sms_consent_for_phone(Y)` must stay False,
        because `user_a.phone_number` (X) never equals Y.
        """
        user_a: User = baker.make(User, phone_number="+15555550100")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user_a, policy_document=document, phone_number="+15555550999")
        service = ConsentService()

        assert service.has_sms_consent_for_phone("+15555550999") is False

    def test_true_when_the_consenting_users_own_phone_matches(self) -> None:
        """A row whose recording user's own phone equals the checked phone satisfies the gate."""
        user: User = baker.make(User, phone_number="+15555550999")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550999")
        service = ConsentService()

        assert service.has_sms_consent_for_phone("+15555550999") is True


class TestHasSmsConsentForPhoneAndUser:
    """User-tied phone-keyed gate for ``send_verification_code_sms``.

    Unlike ``has_sms_consent_for_phone`` (which requires `user.phone_number ==
    phone`), this variant requires the consent row to belong to the specific
    `user` passed in — see ``UserConsentManager.has_sms_consent_for_phone_and_user``
    for why (allauth's `ChangePhoneForm` ordering makes `user.phone_number ==
    phone` unreachable for a first-time add/change).
    """

    def test_true_when_the_same_user_recorded_a_row_for_that_phone(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550100")
        service = ConsentService()

        assert service.has_sms_consent_for_phone_and_user("+15555550100", user) is True

    def test_false_when_a_different_user_recorded_the_row_for_that_phone(self) -> None:
        """User A's fabricated row for phone Y must not satisfy user B's check."""
        user_a: User = baker.make(User)
        user_b: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user_a, policy_document=document, phone_number="+15555550100")
        service = ConsentService()

        assert service.has_sms_consent_for_phone_and_user("+15555550100", user_b) is False

    def test_false_for_a_different_phone(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550100")
        service = ConsentService()

        assert service.has_sms_consent_for_phone_and_user("+15555550999", user) is False

    def test_false_for_blank_phone(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="")
        service = ConsentService()

        assert service.has_sms_consent_for_phone_and_user("", user) is False


class TestUserConsentManagerDirectly:
    """Sanity check the manager predicate independent of the service layer."""

    def test_has_sms_consent_matches_manager(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document)

        assert UserConsent.objects.has_sms_consent(user) is True

    def test_has_sms_consent_for_phone_matches_manager(self) -> None:
        user: User = baker.make(User, phone_number="+15555550100")
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document, phone_number="+15555550100")

        assert UserConsent.objects.has_sms_consent_for_phone("+15555550100") is True
