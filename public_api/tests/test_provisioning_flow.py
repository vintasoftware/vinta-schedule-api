"""Integration tests for the provisioning flow (Phase 1 & 3: createOrganization, createInvitation)."""

import datetime
from unittest.mock import MagicMock, patch

import pytest
from allauth.socialaccount.models import SocialLogin
from model_bakery import baker
from rest_framework.test import APIClient

from accounts.account_adapters import SocialAccountAdapter
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import Profile, User


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


# ---------------------------------------------------------------------------
# Helper: simulate social login auto-join (mirrors test_social_invite_autojoin.py)
# ---------------------------------------------------------------------------


def _social_save_user(email: str) -> User:
    """Simulate the allauth social save_user path for *email*.

    Mirrors the helper in test_social_invite_autojoin.py: the super() call is
    replaced by a minimal stub that persists the user, then the real
    SocialAccountAdapter.save_user runs so that profile-creation and invite
    auto-join logic execute exactly as in production.
    """
    adapter = SocialAccountAdapter()
    new_user = User(email=email)
    new_user.profile = Profile(user=new_user, first_name="Invited", last_name="User")
    sociallogin = MagicMock(spec=SocialLogin)
    sociallogin.user = new_user
    sociallogin.account = MagicMock(extra_data={})

    def _super_save(request, sociallogin, form=None):
        sociallogin.user.save()
        return sociallogin.user

    with patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save):
        return adapter.save_user(None, sociallogin, form=None)


@pytest.mark.django_db
class TestCreateInvitationProvisioning:
    """Integration tests for the full createOrganization → createInvitation chain."""

    def setup_method(self):
        self.client = APIClient()

    CREATE_INVITATION_MUTATION = """
    mutation CreateInvitation($input: CreateInvitationInput!) {
        createInvitation(input: $input) {
            invitation {
                id
                email
                expiresAt
            }
            token
            inviteUrl
        }
    }
    """

    def test_full_provisioning_chain_leaves_pending_invite_in_child(self):
        """
        Full chain: createOrganization → createInvitation.

        After the chain:
        - A pending OrganizationInvitation exists in the child org addressed to the user email.
          (The invitation itself creates the user.)
        - The invitation has the requested role.
        - No stray org was created.
        """
        from di_core.containers import container

        # Reseller setup
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="organization")
        baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")

        headers = {"authorization": f"Bearer {system_user.id}:{token}"}

        create_org_mutation = """
        mutation CreateOrganization($input: CreateOrganizationInput!) {
            createOrganization(input: $input) {
                organization { id name }
            }
        }
        """

        org_count_before = Organization.objects.count()

        with container.public_api_auth_service.override(auth_service):
            # Step 1: createOrganization
            r1 = self.client.post(
                "/graphql/",
                data={"query": create_org_mutation, "variables": {"input": {"name": "Child Org"}}},
                format="json",
                headers=headers,
            )
            assert r1.status_code == 200
            d1 = r1.json()
            assert "errors" not in d1 or len(d1.get("errors", [])) == 0
            child_org_id = d1["data"]["createOrganization"]["organization"]["id"]

            invited_email = "invited.user@example.com"

            # Step 2: createInvitation (email is mocked to avoid real email send)
            with patch("organizations.services.NotificationService.create_one_off_notification"):
                r3 = self.client.post(
                    "/graphql/",
                    data={
                        "query": self.CREATE_INVITATION_MUTATION,
                        "variables": {
                            "input": {
                                "userEmail": invited_email,
                                "organizationId": str(child_org_id),
                                "role": "MEMBER",
                            }
                        },
                    },
                    format="json",
                    headers=headers,
                )

        assert r3.status_code == 200
        d3 = r3.json()
        assert "errors" not in d3 or len(d3.get("errors", [])) == 0

        # Verify pending invitation exists in the child org
        child_org = Organization.objects.get(id=child_org_id)
        pending_invites = OrganizationInvitation.objects.filter(
            email=invited_email,
            organization=child_org,
            accepted_at__isnull=True,
            membership__isnull=True,
        )
        assert pending_invites.count() == 1
        invite = pending_invites.first()
        assert invite is not None
        assert invite.role == OrganizationRole.MEMBER

        # Verify no stray org was created (only reseller + child)
        assert Organization.objects.count() == org_count_before + 1

        # token and invite_url are null in the sendEmail=true path
        assert d3["data"]["createInvitation"]["token"] is None
        assert d3["data"]["createInvitation"]["inviteUrl"] is None

    def test_social_login_auto_joins_invited_user_with_correct_role(self):
        """
        After createInvitation, social-login by that email yields an active membership
        in the child org with the invited role, and no stray org is created.
        """
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)
        invited_email = "socialjoin@example.com"

        # Create a pending invitation directly (simulates the createInvitation mutation result)
        baker.make(
            OrganizationInvitation,
            email=invited_email,
            organization=child_org,
            invited_by=None,
            role=OrganizationRole.ADMIN,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        org_count_before = Organization.objects.count()

        # Simulate social login (triggers auto-join via SocialAccountAdapter.save_user)
        user = _social_save_user(invited_email)

        # The user should have exactly one membership in the child org with ADMIN role.
        memberships = OrganizationMembership.objects.filter(user=user)
        assert memberships.count() == 1
        membership = memberships.first()
        assert membership is not None
        assert membership.organization == child_org
        assert membership.role == OrganizationRole.ADMIN

        # No stray org was created.
        assert Organization.objects.count() == org_count_before

        # The invitation should be marked accepted.
        invite = OrganizationInvitation.objects.get(email=invited_email, organization=child_org)
        assert invite.accepted_at is not None
        assert invite.membership_id == membership.pk
