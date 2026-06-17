from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


@pytest.fixture
def organization():
    """Create a test organization."""
    return baker.make(Organization, name="Test Organization")


@pytest.fixture
def system_user_with_resources(organization):
    """Create a system user with all necessary resource permissions."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="test_integration", organization=organization
    )

    # Grant access to all required resources
    resources = [
        PublicAPIResources.CALENDAR,
        PublicAPIResources.CALENDAR_EVENT,
        PublicAPIResources.BLOCKED_TIME,
        PublicAPIResources.AVAILABLE_TIME,
        PublicAPIResources.AVAILABILITY_WINDOWS,
        PublicAPIResources.UNAVAILABLE_WINDOWS,
        PublicAPIResources.USER,
    ]

    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)

    return system_user, token


@pytest.fixture
def user_with_organization(organization):
    """Create a regular user within the organization for testing user queries."""
    user_model = get_user_model()
    user = baker.make(user_model, email="test@example.com")
    baker.make(OrganizationMembership, user=user, organization=organization)
    return user


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestGraphQLQueries:
    """Test GraphQL queries through the API endpoint."""


@pytest.mark.django_db
class TestGraphQLMutations:
    """Test GraphQL mutation endpoints through the API endpoint."""

    def setup_method(self):
        self.client = APIClient()

    def test_create_organization_success_reseller(self):
        """Test successful creation of a child organization by a reseller."""
        from di_core.containers import container

        # Create a reseller org with the can_invite_organizations flag
        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)

        # Create a system user for the reseller with ORGANIZATION resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name="organization",
        )

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
                    "Authorization": f"Bearer {system_user.id}:{token}",
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["createOrganization"]["organization"]["name"] == "Child Org"

        # Verify the child org was created with correct parent and flag
        child_org = Organization.objects.get(name="Child Org")
        assert child_org.parent_id == reseller_org.id
        assert child_org.can_invite_organizations is False

        # Verify no membership was created for the child
        assert not OrganizationMembership.objects.filter(organization=child_org).exists()

    def test_create_organization_fails_flag_off(self):
        """Test that createOrganization fails when acting org has flag off."""
        from di_core.containers import container

        # Create a non-reseller org (flag is False by default)
        non_reseller_org = baker.make(Organization, name="Non-Reseller Org")

        # Create a system user with ORGANIZATION resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=non_reseller_org
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name="organization",
        )

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
        # Should get a GraphQL error with the permission message
        assert "errors" in response_data
        assert len(response_data["errors"]) > 0
        assert (
            "does not have permission to invite or create" in str(response_data["errors"]).lower()
        )
        # Verify no organization with the attempted name was created
        assert not Organization.objects.filter(name="Child Org").exists()

    def test_create_organization_fails_no_scope(self):
        """Test that createOrganization fails without ORGANIZATION scope."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)

        # Create a system user without ORGANIZATION resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        # Don't grant ORGANIZATION resource

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
        # Should get a GraphQL error for permission denied
        assert "errors" in response_data
        assert len(response_data["errors"]) > 0
        # Verify the OrganizationResourceAccess permission message
        assert "don't have access" in str(response_data["errors"]).lower()
        # Verify no organization was created
        assert not Organization.objects.filter(name="Child Org").exists()

    def test_create_organization_duplicate_name_under_parent(self):
        """Test that duplicate child names under the same parent are rejected."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)

        # Create an existing child with the same name
        baker.make(Organization, name="Child Org", parent=reseller_org)

        # Create a system user with ORGANIZATION resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name="organization",
        )

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
        # Should get a GraphQL error for duplicate name
        assert "errors" in response_data
        assert len(response_data["errors"]) > 0
        assert "already exists" in str(response_data["errors"])

    def test_check_token_success(self, system_user_with_resources):
        # Test check_token mutation with valid credentials
        system_user, token = system_user_with_resources

        # Import the container and override with real service
        from di_core.containers import container

        auth_service = PublicAPIAuthService()

        mutation = """
        mutation CheckToken($systemUserId: Int!, $token: String!) {
            checkToken(systemUserId: $systemUserId, token: $token) {
                tokenValid
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"systemUserId": system_user.id, "token": token},
                },
                format="json",
            )

        assert response.status_code == 200
        data = response.json()["data"]["checkToken"]
        assert data["tokenValid"] is True

    def test_check_token_failure(self):
        # Invalid token should fail
        from di_core.containers import container

        auth_service = PublicAPIAuthService()

        mutation = """
        mutation CheckToken($systemUserId: Int!, $token: String!) {
            checkToken(systemUserId: $systemUserId, token: $token) {
                tokenValid
            }
        }
        """
        # Using invalid system_user_id and token
        invalid_system_user_id = 99999
        invalid_token = "invalid_token"

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"systemUserId": invalid_system_user_id, "token": invalid_token},
                },
                format="json",
            )

        assert response.status_code == 200
        # Should get GraphQL error for invalid credentials
        response_data = response.json()
        assert "errors" in response_data
        assert len(response_data["errors"]) > 0

    def test_check_token_edge_case_empty_token(self, system_user_with_resources):
        # Edge case: empty token string
        system_user, _ = system_user_with_resources

        from di_core.containers import container

        auth_service = PublicAPIAuthService()

        mutation = """
        mutation CheckToken($systemUserId: Int!, $token: String!) {
            checkToken(systemUserId: $systemUserId, token: $token) {
                tokenValid
            }
        }
        """
        empty_token = ""

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"systemUserId": system_user.id, "token": empty_token},
                },
                format="json",
            )

        assert response.status_code == 200
        # Should get GraphQL error for invalid credentials
        response_data = response.json()
        assert "errors" in response_data
        assert len(response_data["errors"]) > 0


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


@pytest.mark.django_db
class TestCreateInvitationMutation:
    """Unit tests for the createInvitation mutation (Phase 3: branded-email path)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_reseller(self, resources: list[str] | None = None):
        """Create a reseller org + system user with the given resource scopes."""
        if resources is None:
            resources = ["invitation"]
        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return reseller_org, system_user, token, auth_service

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": CREATE_INVITATION_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_create_invitation_creates_pending_invite_with_default_role(self):
        """A reseller creates an invitation to a child org; default role is MEMBER."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)

        user_email = "invitee@example.com"
        with patch(
            "organizations.services.OrganizationService.invite_user_to_organization"
        ) as mock_invite:
            mock_invitation = baker.prepare(
                OrganizationInvitation,
                id=1,
                email=user_email,
                organization=child_org,
            )
            import datetime

            mock_invitation.expires_at = datetime.datetime.now(
                tz=datetime.UTC
            ) + datetime.timedelta(days=7)
            mock_invite.return_value = mock_invitation

            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {"input": {"userEmail": user_email, "organizationId": str(child_org.id)}},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        assert data["data"]["createInvitation"]["invitation"]["email"] == user_email
        # token and invite_url are null in the sendEmail=true (Phase 3) path
        assert data["data"]["createInvitation"]["token"] is None
        assert data["data"]["createInvitation"]["inviteUrl"] is None

        # Verify invite_user_to_organization was called with MEMBER role (default)
        mock_invite.assert_called_once()
        call_kwargs = mock_invite.call_args.kwargs
        assert call_kwargs["email"] == user_email
        assert call_kwargs["organization"] == child_org
        assert call_kwargs["role"] == OrganizationRole.MEMBER
        assert call_kwargs["invited_by"] is None

    def test_create_invitation_with_explicit_admin_role(self):
        """A reseller creates an invitation with an explicit ADMIN role."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)

        user_email = "admin_invitee@example.com"
        with patch(
            "organizations.services.OrganizationService.invite_user_to_organization"
        ) as mock_invite:
            mock_invitation = baker.prepare(
                OrganizationInvitation,
                id=2,
                email=user_email,
                organization=child_org,
            )
            import datetime

            mock_invitation.expires_at = datetime.datetime.now(
                tz=datetime.UTC
            ) + datetime.timedelta(days=7)
            mock_invite.return_value = mock_invitation

            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {
                    "input": {
                        "userEmail": user_email,
                        "organizationId": str(child_org.id),
                        "role": "ADMIN",
                    }
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        mock_invite.assert_called_once()
        call_kwargs = mock_invite.call_args.kwargs
        assert call_kwargs["role"] == OrganizationRole.ADMIN

    def test_create_invitation_already_active_member_returns_error(self):
        """createInvitation for an already-active member of the target org → typed error."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)

        user_model = get_user_model()
        existing_user = user_model.objects.create(email="member@example.com")
        baker.make(
            OrganizationMembership,
            user=existing_user,
            organization=child_org,
            is_active=True,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "userEmail": "member@example.com",
                    "organizationId": str(child_org.id),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        # Should carry the UserAlreadyHasMembershipError message
        assert "already a member" in str(data["errors"]).lower()

    def test_create_invitation_off_subtree_org_rejected(self):
        """An organizationId not in the acting org's subtree is rejected."""
        _reseller_org, system_user, token, auth_service = self._setup_reseller()
        # org with no relation to reseller_org
        unrelated_org = baker.make(Organization, name="Unrelated Org")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "userEmail": "someone@example.com",
                    "organizationId": str(unrelated_org.id),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "subtree" in str(data["errors"]).lower()

    def test_create_invitation_flag_off_acting_org_rejected(self):
        """createInvitation is denied when the acting org's can_invite_organizations flag is off."""
        # Non-reseller org (flag off by default)
        non_reseller_org = baker.make(Organization, name="Non-Reseller Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=non_reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")

        child_org = baker.make(Organization, name="Child Org")

        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": "someone@example.com",
                            "organizationId": str(child_org.id),
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "does not have permission to invite or create" in str(data["errors"]).lower()

    def test_create_invitation_missing_invitation_scope_denied(self):
        """createInvitation is denied when the token lacks the INVITATION scope."""
        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=reseller_org
        )
        # Grant a different scope, not INVITATION
        baker.make(ResourceAccess, system_user=system_user, resource_name="user")

        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)

        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": "someone@example.com",
                            "organizationId": str(child_org.id),
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_create_invitation_send_email_true_returns_null_token_and_url(self):
        """sendEmail=true (default) always returns token=null, inviteUrl=null."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)

        with patch(
            "organizations.services.OrganizationService.invite_user_to_organization"
        ) as mock_invite:
            mock_invitation = baker.prepare(
                OrganizationInvitation,
                id=3,
                email="invitee@example.com",
                organization=child_org,
            )
            import datetime

            mock_invitation.expires_at = datetime.datetime.now(
                tz=datetime.UTC
            ) + datetime.timedelta(days=7)
            mock_invite.return_value = mock_invitation

            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {
                    "input": {
                        "userEmail": "invitee@example.com",
                        "organizationId": str(child_org.id),
                        "sendEmail": True,
                    }
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        assert data["data"]["createInvitation"]["token"] is None
        assert data["data"]["createInvitation"]["inviteUrl"] is None

    def test_create_invitation_acting_org_can_invite_itself(self):
        """The acting org can invite a user into itself (organization_id == acting org)."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()

        with patch(
            "organizations.services.OrganizationService.invite_user_to_organization"
        ) as mock_invite:
            mock_invitation = baker.prepare(
                OrganizationInvitation,
                id=4,
                email="self_invitee@example.com",
                organization=reseller_org,
            )
            import datetime

            mock_invitation.expires_at = datetime.datetime.now(
                tz=datetime.UTC
            ) + datetime.timedelta(days=7)
            mock_invite.return_value = mock_invitation

            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {
                    "input": {
                        "userEmail": "self_invitee@example.com",
                        "organizationId": str(reseller_org.id),
                    }
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

    def test_create_invitation_cross_reseller_tree_rejected_no_invite_created(self):
        """Cross-reseller-tree isolation: R1 calling createInvitation with R2's child is rejected.

        This is the highest-risk multi-tenant failure mode: a reseller R1 must not be able to
        invite users into any organization belonging to a different reseller R2's tree.

        Asserts:
        - The mutation returns a GraphQL error referencing 'subtree'.
        - NO OrganizationInvitation row is created for that email+org combination.
        """
        # Build two independent reseller trees
        r1_org = baker.make(Organization, name="Reseller1", can_invite_organizations=True)
        r2_org = baker.make(Organization, name="Reseller2", can_invite_organizations=True)
        r2_child = baker.make(Organization, name="R2Child", parent=r2_org)

        # System user authenticated as R1
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="r1_integration", organization=r1_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")

        target_email = "victim@example.com"
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "userEmail": target_email,
                    "organizationId": str(r2_child.id),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "subtree" in str(data["errors"]).lower()

        # No invitation must have been created
        assert not OrganizationInvitation.objects.filter(
            email=target_email, organization=r2_child
        ).exists()

    # -------------------------------------------------------------------------
    # Phase 4: self-managed invitations (sendEmail=false path)
    # -------------------------------------------------------------------------

    def test_send_email_false_no_email_sent_returns_token_and_invite_url(self):
        """sendEmail=false sends no email and returns a non-null token + inviteUrl.

        This is the core Phase-4 happy path: the reseller opts out of vinta's invitation
        email and receives the raw token + invite URL once so it can render the link in
        its own UI.

        Asserts:
        - transaction.on_commit is NOT called (no email scheduled).
        - token and inviteUrl are non-null in the response.
        - inviteUrl contains the raw token.
        """
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)
        user_email = "self_managed@example.com"

        from di_core.containers import container

        with (
            container.public_api_auth_service.override(auth_service),
            patch("organizations.services.transaction.on_commit") as mock_on_commit,
        ):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": user_email,
                            "organizationId": str(child_org.id),
                            "sendEmail": False,
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        # token and invite_url must be non-null in the sendEmail=false path
        returned_token = data["data"]["createInvitation"]["token"]
        invite_url = data["data"]["createInvitation"]["inviteUrl"]
        assert returned_token is not None, "token must be non-null when sendEmail=false"
        assert invite_url is not None, "inviteUrl must be non-null when sendEmail=false"
        # inviteUrl must embed the raw token
        assert returned_token in invite_url

        # transaction.on_commit must NOT have been called — no email was scheduled
        mock_on_commit.assert_not_called()

    def test_send_email_false_returned_token_validates_via_accept_invitation(self):
        """The raw token returned when sendEmail=false must be usable with accept_invitation.

        This is the security-critical proof: the plaintext token the mutation returned
        matches the stored hash and drives a successful accept → active membership.

        Asserts:
        - Calling OrganizationService.accept_invitation(returned_token, user) succeeds.
        - An active OrganizationMembership is created for the user in the target org.
        """
        from common.utils.authentication_utils import verify_long_lived_token
        from di_core.containers import container
        from organizations.services import OrganizationService

        reseller_org, system_user, api_token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)
        user_email = "token_validate@example.com"

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": user_email,
                            "organizationId": str(child_org.id),
                            "sendEmail": False,
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{api_token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        returned_token = data["data"]["createInvitation"]["token"]
        assert returned_token is not None

        # Retrieve the DB row and verify the plaintext was NOT persisted
        invitation = OrganizationInvitation.objects.get(email=user_email, organization=child_org)
        assert invitation.token_hash != returned_token, (
            "Plaintext token must not equal the stored hash — only the hash is persisted"
        )
        # Confirm the hash matches the returned token (proves token_hash is derived from token)
        assert verify_long_lived_token(returned_token, invitation.token_hash), (
            "The returned token must verify against the stored hash"
        )

        # Accept the invitation using the raw token — proves end-to-end correctness
        user_model = get_user_model()
        accepting_user = user_model.objects.create(email=user_email)

        org_service = OrganizationService()
        membership = org_service.accept_invitation(returned_token, accepting_user)

        assert membership is not None
        assert membership.organization == child_org
        assert OrganizationMembership.objects.filter(
            user=accepting_user, organization=child_org
        ).exists()

    def test_send_email_false_no_plaintext_in_db(self):
        """No plaintext token is stored in the OrganizationInvitation row.

        The raw token returned by sendEmail=false must not appear verbatim in any
        DB column — only the derived token_hash is stored.
        """
        from di_core.containers import container

        reseller_org, system_user, api_token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)
        user_email = "no_plaintext@example.com"

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": user_email,
                            "organizationId": str(child_org.id),
                            "sendEmail": False,
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{api_token}"},
            )

        assert response.status_code == 200
        data = response.json()
        returned_token = data["data"]["createInvitation"]["token"]
        assert returned_token is not None

        invitation = OrganizationInvitation.objects.get(email=user_email, organization=child_org)

        # The plaintext token must not appear in any DB-backed field
        assert invitation.token_hash != returned_token
        # email, first_name, last_name, role are unrelated; verify the hash column is distinct
        assert returned_token not in (
            invitation.token_hash,
            invitation.email,
            invitation.first_name,
            invitation.last_name,
        )

    def test_send_email_false_flag_off_acting_org_rejected(self):
        """sendEmail=false is denied when the acting org's can_invite_organizations flag is off.

        The flag gate applies regardless of the sendEmail value; a non-reseller cannot
        suppress the email to bypass any constraint.
        """
        from di_core.containers import container

        non_reseller_org = baker.make(Organization, name="Non-Reseller")
        auth_service = PublicAPIAuthService()
        system_user, api_token = auth_service.create_system_user(
            integration_name="test_integration", organization=non_reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="invitation")

        child_org = baker.make(Organization, name="Child Org")

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_INVITATION_MUTATION,
                    "variables": {
                        "input": {
                            "userEmail": "someone@example.com",
                            "organizationId": str(child_org.id),
                            "sendEmail": False,
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{api_token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "does not have permission to invite or create" in str(data["errors"]).lower()


CREATE_SYSTEM_USER_TOKEN_MUTATION = """
mutation CreateSystemUserToken($input: CreateSystemUserTokenInput!) {
    createSystemUserToken(input: $input) {
        systemUserId
        token
    }
}
"""


@pytest.mark.django_db
class TestCreateSystemUserTokenMutation:
    """Unit tests for the createSystemUserToken mutation (Phase 5: token delegation)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_reseller(self, resources: list[str] | None = None):
        """Create a reseller org + system user with the given resource scopes."""
        if resources is None:
            resources = ["system_user"]
        reseller_org = baker.make(Organization, name="Reseller Org", can_invite_organizations=True)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="reseller_integration", organization=reseller_org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return reseller_org, system_user, token, auth_service

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": CREATE_SYSTEM_USER_TOKEN_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_create_system_user_token_success_returns_system_user_id_and_token(self):
        """Happy path: reseller mints a token; returns systemUserId + plaintext token once."""
        from public_api.models import SystemUser

        reseller_org, system_user, token, auth_service = self._setup_reseller()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "minted_integration",
                    "resources": ["calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createSystemUserToken"]
        assert result["systemUserId"] is not None
        assert result["token"] is not None
        assert len(result["token"]) > 0

        # Verify the SystemUser was created in the DB
        minted_user = SystemUser.objects.get(id=int(result["systemUserId"]))
        assert minted_user.integration_name == "minted_integration"
        assert minted_user.organization == reseller_org

    def test_created_system_user_organization_equals_target_org(self):
        """The created SystemUser's organization == the target org passed in organizationId."""
        from public_api.models import SystemUser

        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(Organization, name="Child Org", parent=reseller_org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(child_org.id),
                    "integrationName": "child_integration",
                    "resources": ["user"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        minted_id = int(data["data"]["createSystemUserToken"]["systemUserId"])
        minted_user = SystemUser.objects.get(id=minted_id)
        assert minted_user.organization == child_org

    def test_requested_resource_access_rows_attached_exact_set(self):
        """The exact requested ResourceAccess rows are attached to the minted SystemUser."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        requested = ["calendar", "user", "organization"]

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "multi_resource_integration",
                    "resources": requested,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        minted_id = int(data["data"]["createSystemUserToken"]["systemUserId"])
        attached = set(
            ResourceAccess.objects.filter(system_user_id=minted_id).values_list(
                "resource_name", flat=True
            )
        )
        assert attached == set(requested)

    def test_minted_token_cannot_set_can_invite_organizations_flag(self):
        """Minting a token with ORGANIZATION scope does NOT flip can_invite_organizations.

        This is the core no-flag-delegation invariant: a minted token may carry the ORGANIZATION
        scope (so it can create child orgs) but it can never set the DB flag.
        The target org's can_invite_organizations is unchanged by the token-mint operation.
        """
        reseller_org, system_user, token, auth_service = self._setup_reseller()
        child_org = baker.make(
            Organization, name="Child Org", parent=reseller_org, can_invite_organizations=False
        )
        flag_before = child_org.can_invite_organizations

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(child_org.id),
                    "integrationName": "org_scoped_integration",
                    "resources": ["organization"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        # The target org's flag must not have changed
        child_org.refresh_from_db()
        assert child_org.can_invite_organizations == flag_before
        assert child_org.can_invite_organizations is False

    def test_off_subtree_target_rejected(self):
        """A target org not in the acting org's subtree is rejected."""
        _reseller_org, system_user, token, auth_service = self._setup_reseller()
        unrelated_org = baker.make(Organization, name="Unrelated Org")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(unrelated_org.id),
                    "integrationName": "off_subtree_integration",
                    "resources": ["calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "subtree" in str(data["errors"]).lower()

    def test_flag_off_acting_org_rejected_with_system_user_scope(self):
        """Gate error: flag-off org is rejected even when the token has SYSTEM_USER scope."""
        non_reseller_org = baker.make(Organization, name="Non-Reseller")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="flagoff_integration", organization=non_reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="system_user")

        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_SYSTEM_USER_TOKEN_MUTATION,
                    "variables": {
                        "input": {
                            "organizationId": str(non_reseller_org.id),
                            "integrationName": "should_fail",
                            "resources": ["calendar"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "does not have permission to invite or create" in str(data["errors"]).lower()

    def test_missing_system_user_scope_denied(self):
        """A token without SYSTEM_USER scope cannot call createSystemUserToken."""
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_scope_integration", organization=reseller_org
        )
        # Grant calendar scope but not system_user
        baker.make(ResourceAccess, system_user=system_user, resource_name="calendar")

        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_SYSTEM_USER_TOKEN_MUTATION,
                    "variables": {
                        "input": {
                            "organizationId": str(reseller_org.id),
                            "integrationName": "no_scope_minted",
                            "resources": ["calendar"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_invalid_resource_returns_validation_error(self):
        """Invalid resource name in resources list → GraphQL error."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "invalid_resource_integration",
                    "resources": ["not_a_valid_resource"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "invalid resource" in str(data["errors"]).lower()

    def test_empty_resources_returns_validation_error(self):
        """Empty resources list → GraphQL error."""
        reseller_org, system_user, token, auth_service = self._setup_reseller()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "empty_resources_integration",
                    "resources": [],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_duplicate_integration_name_returns_error_and_no_orphan(self):
        """Minting a second token with the same integration_name returns a GraphQL error.

        Asserts:
        - The mutation returns a GraphQL error mentioning 'integration_name'.
        - Exactly ONE SystemUser with that integration_name exists in the DB (the first one).
        - The ResourceAccess count did not grow from the failed attempt.
        """
        from public_api.models import SystemUser

        reseller_org, system_user, token, auth_service = self._setup_reseller()

        # First call — must succeed
        r1 = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "dup",
                    "resources": ["calendar"],
                }
            },
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert "errors" not in d1 or len(d1.get("errors", [])) == 0

        # Snapshot counts after the successful first call
        su_count_after_first = SystemUser.objects.filter(
            integration_name="dup", organization=reseller_org
        ).count()
        ra_count_after_first = ResourceAccess.objects.filter(
            system_user__integration_name="dup", system_user__organization=reseller_org
        ).count()
        assert su_count_after_first == 1

        # Second call with the same integration_name — must fail
        r2 = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "dup",
                    "resources": ["calendar"],
                }
            },
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert "errors" in d2
        assert len(d2["errors"]) > 0
        assert "integration_name" in str(d2["errors"]).lower()

        # No orphan SystemUser — still exactly one with that integration_name
        assert (
            SystemUser.objects.filter(integration_name="dup", organization=reseller_org).count()
            == su_count_after_first
        )
        # ResourceAccess count must not have grown
        assert (
            ResourceAccess.objects.filter(
                system_user__integration_name="dup", system_user__organization=reseller_org
            ).count()
            == ra_count_after_first
        )

    def test_duplicate_resources_in_list_creates_single_resource_access_row(self):
        """Duplicate resource names in the resources list produce exactly one ResourceAccess row.

        Asserts no crash, no unique-constraint violation, and exactly one ResourceAccess row
        per deduplicated resource value.
        """
        reseller_org, system_user, token, auth_service = self._setup_reseller()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": str(reseller_org.id),
                    "integrationName": "dedup_integration",
                    "resources": ["calendar", "calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        minted_id = int(data["data"]["createSystemUserToken"]["systemUserId"])
        calendar_rows = ResourceAccess.objects.filter(
            system_user_id=minted_id, resource_name="calendar"
        ).count()
        assert calendar_rows == 1

    def test_cross_reseller_tree_target_rejected(self):
        """R1 cannot mint a token for R2's child — cross-reseller isolation."""
        r1_org = baker.make(Organization, name="Reseller1", can_invite_organizations=True)
        r2_org = baker.make(Organization, name="Reseller2", can_invite_organizations=True)
        r2_child = baker.make(Organization, name="R2Child", parent=r2_org)

        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="r1_system_user_integration", organization=r1_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="system_user")

        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_SYSTEM_USER_TOKEN_MUTATION,
                    "variables": {
                        "input": {
                            "organizationId": str(r2_child.id),
                            "integrationName": "cross_reseller_integration",
                            "resources": ["calendar"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "subtree" in str(data["errors"]).lower()
