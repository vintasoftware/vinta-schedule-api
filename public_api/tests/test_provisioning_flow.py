"""Integration tests for the provisioning flow (Phase 1: createOrganization)."""

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from organizations.models import Organization
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


@pytest.mark.django_db
class TestCreateOrganizationProvisioning:
    """Integration tests for the createOrganization mutation in the provisioning flow."""

    def setup_method(self):
        self.client = APIClient()

    def test_reseller_creates_child_organization_with_correct_parent_and_flag(self):
        """Test that a reseller can create a child org with parent set and flag False."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        # Create a system user for the reseller with ORGANIZATION scope
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="reseller_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="organization")

        mutation = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) {
                organization {
                    id
                    name
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"input": {"name": "Child Org"}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["createOrganization"]["organization"]["name"] == "Child Org"

        # Verify the child was created with the correct parent and flag
        child_org = Organization.objects.get(name="Child Org")
        assert child_org.parent_id == reseller_org.id
        assert child_org.can_invite_organizations is False

    def test_token_without_organization_scope_is_denied(self):
        """Test that a token without ORGANIZATION scope is denied."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        # Create a system user WITHOUT ORGANIZATION scope
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="reseller_integration", organization=reseller_org
        )
        # Grant a different resource instead
        baker.make(ResourceAccess, system_user=system_user, resource_name="calendar")

        mutation = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) {
                organization {
                    id
                    name
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"input": {"name": "Child Org"}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        response_data = response.json()
        # Should get permission denied
        assert "errors" in response_data
        assert len(response_data["errors"]) > 0

    def test_flag_can_only_be_set_in_database_not_via_mutation(self):
        """Test that the can_invite_organizations flag cannot be set via the mutation."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        # Create a system user with ORGANIZATION scope
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="reseller_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="organization")

        # Attempt to pass can_invite_organizations: true in the input
        # The GraphQL schema should reject this as an unknown field
        mutation_with_flag_attempt = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) {
                organization {
                    id
                    name
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation_with_flag_attempt,
                    "variables": {"input": {"name": "Child Org", "canInviteOrganizations": True}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        response_data = response.json()

        # The GraphQL schema should reject unknown input fields at validation time
        # This should produce a validation error
        if "errors" in response_data:
            # We got a validation error about the unknown field, which is the desired behavior
            # Verify that no child org was created
            assert not Organization.objects.filter(name="Child Org", parent=reseller_org).exists()
        else:
            # If no validation error (e.g., the field was silently ignored),
            # verify that the created child has flag False despite the attempt
            assert "data" in response_data
            child_org = Organization.objects.get(name="Child Org", parent=reseller_org)
            assert child_org.can_invite_organizations is False

    def test_multiple_children_same_reseller_different_names(self):
        """Test that a reseller can create multiple children with different names."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        # Create a system user with ORGANIZATION scope
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="reseller_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="organization")

        mutation = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) {
                organization {
                    id
                    name
                }
            }
        }
        """

        # Create first child
        with container.public_api_auth_service.override(auth_service):
            response1 = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"input": {"name": "Child A"}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response1.status_code == 200
        data1 = response1.json()
        assert data1["data"]["createOrganization"]["organization"]["name"] == "Child A"

        # Create second child
        with container.public_api_auth_service.override(auth_service):
            response2 = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"input": {"name": "Child B"}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["data"]["createOrganization"]["organization"]["name"] == "Child B"

        # Verify both children exist and have correct parent
        child_a = Organization.objects.get(name="Child A")
        child_b = Organization.objects.get(name="Child B")
        assert child_a.parent_id == reseller_org.id
        assert child_b.parent_id == reseller_org.id
        assert child_a.id != child_b.id
