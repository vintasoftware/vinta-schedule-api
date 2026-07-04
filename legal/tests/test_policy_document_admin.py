from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

import pytest

from legal.factories import PolicyDocumentFactory
from legal.models import PolicyDocument, PolicyDocumentType


User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser():
    return User.objects.create_superuser(email="admin@example.com", password="adminpassword")  # noqa: S106


@pytest.fixture
def admin_client(superuser):
    client = Client()
    client.force_login(superuser)
    return client


class TestPolicyDocumentAdminCreate:
    def test_admin_can_create_a_new_version(self, admin_client):
        add_url = reverse("admin:legal_policydocument_add")

        response = admin_client.post(
            add_url,
            data={
                "document_type": PolicyDocumentType.SMS_CONSENT,
                "version": "1",
                "title": "SMS Messaging Consent",
                "body_markdown": "# Consent\n\nBody text.",
                "published_at_0": "2026-01-01",
                "published_at_1": "00:00:00",
            },
        )

        assert response.status_code == 302
        assert PolicyDocument.objects.filter(
            document_type=PolicyDocumentType.SMS_CONSENT, version=1, title="SMS Messaging Consent"
        ).exists()

    def test_add_form_pre_suggests_next_version_in_help_text(self, admin_client):
        PolicyDocumentFactory().create(document_type=PolicyDocumentType.PRIVACY_POLICY, version=1)
        add_url = reverse("admin:legal_policydocument_add")

        response = admin_client.get(add_url)

        assert response.status_code == 200
        content = response.content.decode()
        assert "Suggested next version per type" in content
        # privacy_policy already has version 1 published -> suggest 2.
        assert "Privacy Policy (privacy_policy): 2" in content
        # No versions published yet for the other two types -> suggest 1.
        assert "Terms of Use (terms_of_use): 1" in content
        assert "SMS Messaging Consent (sms_consent): 1" in content


class TestPolicyDocumentAdminReadOnly:
    def test_published_row_has_no_editable_fields_on_change_form(self, admin_client):
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY, version=1
        )
        change_url = reverse("admin:legal_policydocument_change", args=[document.pk])

        response = admin_client.get(change_url)

        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="title"' not in content
        assert 'name="version"' not in content
        assert 'name="body_markdown"' not in content
        assert 'name="document_type"' not in content

    def test_published_row_change_post_does_not_modify_fields(self, admin_client):
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY,
            version=1,
            title="Original title",
        )
        change_url = reverse("admin:legal_policydocument_change", args=[document.pk])

        admin_client.post(
            change_url,
            data={
                "title": "Attempted new title",
                "version": "99",
                "document_type": PolicyDocumentType.TERMS_OF_USE,
                "body_markdown": "attempted change",
            },
        )

        document.refresh_from_db()
        assert document.title == "Original title"
        assert document.version == 1
        assert document.document_type == PolicyDocumentType.PRIVACY_POLICY


class TestPolicyDocumentAdminDelete:
    def test_has_delete_permission_returns_false(self, admin_client):
        """Attempting to GET the delete view for a published row must be rejected."""
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY, version=1
        )
        delete_url = reverse("admin:legal_policydocument_delete", args=[document.pk])

        response = admin_client.get(delete_url)

        assert response.status_code in (403, 302)

    def test_post_to_delete_url_is_rejected(self, admin_client):
        """POST to the admin delete URL must be rejected and the row must still exist."""
        document = PolicyDocumentFactory().create(
            document_type=PolicyDocumentType.PRIVACY_POLICY, version=1
        )
        delete_url = reverse("admin:legal_policydocument_delete", args=[document.pk])

        response = admin_client.post(delete_url, {"post": "yes"})

        assert response.status_code in (403, 302)
        assert PolicyDocument.objects.filter(pk=document.pk).exists()
