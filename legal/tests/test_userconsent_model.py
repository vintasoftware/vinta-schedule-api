from django.db.models import ProtectedError

import pytest
from model_bakery import baker

from legal.factories import PolicyDocumentFactory, UserConsentFactory
from legal.models import PolicyDocument, PolicyDocumentType, UserConsent
from users.models import User


pytestmark = pytest.mark.django_db


class TestUserConsentProtectsPolicyDocument:
    def test_deleting_a_consented_policy_document_is_blocked(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        UserConsentFactory().create(user, policy_document=document)

        with pytest.raises(ProtectedError):
            document.delete()

    def test_deleting_an_unconsented_policy_document_succeeds(self) -> None:
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )

        document.delete()

        assert not PolicyDocument.objects.filter(pk=document.pk).exists()


class TestUserConsentDeletingUserCascades:
    def test_deleting_the_user_cascades_to_their_consents(self) -> None:
        user: User = baker.make(User)
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1
        )
        consent = UserConsentFactory().create(user, policy_document=document)

        user.delete()

        assert not UserConsent.objects.filter(pk=consent.pk).exists()


class TestUserConsentIndexes:
    def test_user_policy_document_index_is_present(self) -> None:
        index_names = {index.name for index in UserConsent._meta.indexes}
        assert any(
            set(index.fields) == {"user", "policy_document"} for index in UserConsent._meta.indexes
        )
        assert index_names  # sanity: at least one named index configured

    def test_multiple_versions_can_be_consented_by_the_same_user(self) -> None:
        user: User = baker.make(User)
        factory = PolicyDocumentFactory()
        v1 = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=1)
        v2 = factory.create(document_type=PolicyDocumentType.SMS_CONSENT, version=2)
        UserConsentFactory().create(user, policy_document=v1)
        UserConsentFactory().create(user, policy_document=v2)

        assert UserConsent.objects.filter(user=user).count() == 2
