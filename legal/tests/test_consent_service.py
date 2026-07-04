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


class TestUserConsentManagerDirectly:
    """Sanity check the manager predicate independent of the service layer."""

    def test_has_sms_consent_matches_manager(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document)

        assert UserConsent.objects.has_sms_consent(user) is True
