from django.db import IntegrityError, transaction

import pytest

from legal.factories import PolicyDocumentFactory
from legal.models import PolicyDocument, PolicyDocumentType


pytestmark = pytest.mark.django_db


class TestPolicyDocumentManagerLatestFor:
    def test_returns_highest_version_for_the_type(self):
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)
        latest_privacy = factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=2)
        factory.create(document_type=PolicyDocumentType.TERMS_OF_USE, version=5)

        result = PolicyDocument.objects.latest_for(PolicyDocumentType.PRIVACY_POLICY)

        assert result == latest_privacy

    def test_returns_none_when_no_versions_exist(self):
        result = PolicyDocument.objects.latest_for(PolicyDocumentType.SMS_CONSENT)

        assert result is None


class TestPolicyDocumentManagerLatestPerType:
    def test_returns_one_row_per_type_at_highest_version(self):
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)
        latest_privacy = factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=2)
        latest_terms = factory.create(document_type=PolicyDocumentType.TERMS_OF_USE, version=1)
        factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        latest_sms_consent = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=2)

        result = list(PolicyDocument.objects.latest_per_type())

        assert set(result) == {latest_privacy, latest_terms, latest_sms_consent}
        assert len(result) == 3

    def test_returns_empty_when_no_documents_exist(self):
        result = list(PolicyDocument.objects.latest_per_type())

        assert result == []


class TestPolicyDocumentUniqueConstraint:
    def test_duplicate_type_and_version_is_rejected(self):
        factory = PolicyDocumentFactory()
        factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)

        with pytest.raises(IntegrityError), transaction.atomic():
            factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)

    def test_same_version_allowed_across_different_types(self):
        factory = PolicyDocumentFactory()
        privacy = factory.create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)
        terms = factory.create(document_type=PolicyDocumentType.TERMS_OF_USE, version=1)

        assert privacy.version == terms.version == 1
