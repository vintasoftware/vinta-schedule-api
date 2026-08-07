"""Audit-emission tests for the ``OrganizationBrandingView`` REST write paths
(Organization Auth-Area Branding plan, Phase 4).

Mirrors ``organizations/tests/test_audit.py``'s approach for ``OrganizationService``
and ``public_api/tests/test_booking_policy_graphql.py``'s ``test_create_audited`` /
``test_update_audited`` for the GraphQL surface: patch
``audit.services.persist_audit_record``, drive the real endpoint through
``django_capture_on_commit_callbacks(execute=True)``, and inspect the serialized
payload(s) the enqueue call received.
"""

from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationMembership,
    OrganizationRole,
)
from users.factories import UserFactory


BRANDING_URL = "/branding/"


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def eligible_org():
    """A parentless, entitled (autouse default subscription), slugged organization
    -- admits both the write gate and this endpoint's admin permission once an
    ADMIN membership is attached."""
    return baker.make(Organization, can_invite_organizations=False, slug="audit-eligible-org")


@pytest.fixture
def admin_user(eligible_org):
    user = UserFactory().create_user(email="brand-admin@example.com")
    baker.make(
        OrganizationMembership,
        user=user,
        organization=eligible_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )
    return user


@pytest.mark.django_db
class TestOrganizationBrandingViewAudit:
    def _authed_client(self, client, user, organization) -> APIClient:
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))
        return client

    def test_put_create_records_one_create_entry(
        self, client, admin_user, eligible_org, django_capture_on_commit_callbacks
    ):
        """A first-time PUT (no existing row) writes exactly one CREATE audit
        entry naming the acting organization and the admin's membership as
        actor, with no diff."""
        self._authed_client(client, admin_user, eligible_org)
        payload = {
            "app_name": "AuditApp",
            "logo_url": "",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "support@example.com",
            "redirect_url": "https://example.com/return",
        }

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.put(BRANDING_URL, data=payload, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        record = payloads[0]
        assert record["organization_id"] == eligible_org.id
        assert record["action"] == "create"
        assert record["subject"]["subject_type"] == "organizations.OrganizationBranding"
        assert record["actor"]["actor_type"] == "membership"
        assert record["actor"]["actor_id"] == admin_user.id
        assert record["actor"]["actor_role"] == OrganizationRole.ADMIN
        assert record["diff"] is None

    def test_put_over_existing_row_records_update_with_diff_of_changed_fields_only(
        self, client, admin_user, eligible_org, django_capture_on_commit_callbacks
    ):
        """A PUT that replaces an existing row writes an UPDATE entry whose
        diff names only the fields that actually changed -- an unchanged field
        (``support_email`` here) is absent from the diff."""
        baker.make(
            OrganizationBranding,
            organization=eligible_org,
            app_name="Before",
            primary_color="#111111",
            secondary_color="#222222",
            support_email="same@example.com",
            redirect_url="https://example.com/before",
        )
        self._authed_client(client, admin_user, eligible_org)
        payload = {
            "app_name": "After",
            "logo_url": "",
            "primary_color": "#333333",
            "secondary_color": "#222222",
            "support_email": "same@example.com",
            "redirect_url": "https://example.com/before",
        }

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.put(BRANDING_URL, data=payload, format="json")

        assert response.status_code == status.HTTP_200_OK
        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        record = payloads[0]
        assert record["action"] == "update"
        diff = record["diff"]
        assert diff is not None
        assert set(diff.keys()) == {"app_name", "primary_color"}
        assert diff["app_name"] == {"old": "Before", "new": "After"}
        assert diff["primary_color"] == {"old": "#111111", "new": "#333333"}

    def test_patch_records_update_with_diff_of_changed_fields_only(
        self, client, admin_user, eligible_org, django_capture_on_commit_callbacks
    ):
        baker.make(
            OrganizationBranding,
            organization=eligible_org,
            app_name="Same",
            primary_color="#111111",
            secondary_color="#222222",
            support_email="same@example.com",
            redirect_url="https://example.com/before",
        )
        self._authed_client(client, admin_user, eligible_org)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.patch(
                    BRANDING_URL, data={"secondary_color": "#444444"}, format="json"
                )

        assert response.status_code == status.HTTP_200_OK
        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        record = payloads[0]
        assert record["action"] == "update"
        diff = record["diff"]
        assert diff is not None
        assert set(diff.keys()) == {"secondary_color"}
        assert diff["secondary_color"] == {"old": "#222222", "new": "#444444"}

    def test_refused_write_records_nothing(self, client, django_capture_on_commit_callbacks):
        """A write refused by the branding gate (here: an organization with a
        parent) records NO audit entry -- the gate raises before the write, so
        the audit call is never reached."""
        parent_org = baker.make(Organization, can_invite_organizations=False, slug="audit-parent")
        child_org = baker.make(Organization, parent=parent_org, slug="audit-child")
        user = UserFactory().create_user(email="child-admin@example.com")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=child_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        self._authed_client(client, user, child_org)
        payload = {
            "app_name": "ShouldNotPersist",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.put(BRANDING_URL, data=payload, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not mock_task.delay.called
        assert not OrganizationBranding.objects.filter(organization=child_org).exists()

    def test_refused_write_via_validation_error_records_nothing(
        self, client, admin_user, eligible_org, django_capture_on_commit_callbacks
    ):
        """A write that passes the gate but fails serializer validation (an
        invalid color format here) also records nothing -- the validation
        error raises before the upsert."""
        self._authed_client(client, admin_user, eligible_org)
        payload = {
            "app_name": "Invalid",
            "logo_url": "",
            "primary_color": "not-a-color",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.put(BRANDING_URL, data=payload, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not mock_task.delay.called
