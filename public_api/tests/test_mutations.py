from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model

import pytest
from graphql import GraphQLError
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
from public_api.mutations import (
    _get_org_and_init_calendar_service,
    get_calendar_mutation_dependencies,
)
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


@pytest.mark.django_db
class TestUpdateBranding:
    """Test updateBranding mutation (Phase 6)."""

    def setup_method(self):
        self.client = APIClient()

    def test_update_branding_success_reseller(self):
        """Test successful branding update by a reseller."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        # Create a system user with BRANDING resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="branding_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="branding")

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                    appName
                    logoUrl
                    primaryColor
                    secondaryColor
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "MyApp",
                            "logoUrl": "https://example.com/logo.png",
                            "primaryColor": "#FF0000",
                            "secondaryColor": "#00FF00",
                            "supportEmail": "support@example.com",
                            "returnUrlAllowlist": ["https://example.com"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["updateBranding"]["branding"] is not None
        assert data["data"]["updateBranding"]["branding"]["appName"] == "MyApp"
        assert (
            data["data"]["updateBranding"]["branding"]["logoUrl"] == "https://example.com/logo.png"
        )
        assert data["data"]["updateBranding"]["branding"]["primaryColor"] == "#FF0000"

    def test_update_branding_fails_flag_off(self):
        """Test that updateBranding fails when acting org has flag off."""
        from di_core.containers import container

        # Create a non-reseller org
        non_reseller_org = baker.make(Organization, name="Non-Reseller")

        # Create a system user with BRANDING resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="branding_integration", organization=non_reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="branding")

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                    appName
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"input": {"appName": "MyApp"}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "does not have permission" in str(data["errors"]).lower()

    def test_update_branding_fails_no_scope(self):
        """Test that updateBranding fails without BRANDING scope."""
        from di_core.containers import container

        # Create a reseller org
        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        # Create a system user without BRANDING resource access
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="branding_integration", organization=reseller_org
        )
        # Don't grant BRANDING resource

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                    appName
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {"input": {"appName": "MyApp"}},
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_branding_invalid_color_format(self):
        """Test that invalid color format is rejected."""
        from di_core.containers import container

        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="branding_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="branding")

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "MyApp",
                            "primaryColor": "red",  # Invalid — not hex
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert "Invalid primary_color format" in str(data["errors"])

    def test_update_branding_invalid_allowlist_url(self):
        """Test that invalid URLs in allowlist are rejected."""
        from di_core.containers import container

        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="branding_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="branding")

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                }
            }
        }
        """

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "MyApp",
                            "returnUrlAllowlist": ["not-a-url"],  # Invalid
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert "Invalid URL" in str(data["errors"])

    def test_update_branding_upsert(self):
        """Test that multiple updates to the same org create only one branding row."""
        from di_core.containers import container

        reseller_org = baker.make(Organization, name="Reseller", can_invite_organizations=True)

        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="branding_integration", organization=reseller_org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name="branding")

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                    appName
                }
            }
        }
        """

        # First update
        with container.public_api_auth_service.override(auth_service):
            response1 = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "First",
                            "primaryColor": "#FF0000",
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response1.status_code == 200
        data1 = response1.json()
        branding_id1 = data1["data"]["updateBranding"]["branding"]["id"]

        # Second update to the same org
        with container.public_api_auth_service.override(auth_service):
            response2 = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "Second",
                            "primaryColor": "#0000FF",
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response2.status_code == 200
        data2 = response2.json()
        branding_id2 = data2["data"]["updateBranding"]["branding"]["id"]

        # Should be the same branding row
        assert branding_id1 == branding_id2
        assert data2["data"]["updateBranding"]["branding"]["appName"] == "Second"

        # Should only have one branding row
        from organizations.models import OrganizationBranding

        assert OrganizationBranding.objects.filter(organization=reseller_org).count() == 1

    def test_update_branding_isolation_reseller_a_and_b(self):
        """Isolation regression test: reseller A's branding is never touched by reseller B.

        Reseller A creates branding via updateBranding. Then reseller B (separate org,
        can_invite_organizations=True, separate token + BRANDING scope) calls updateBranding.

        Asserts:
        - Reseller B gets its own OrganizationBranding row.
        - Reseller A's branding row is completely untouched (same values, still exactly one row).
        """
        from di_core.containers import container
        from organizations.models import OrganizationBranding

        # Create two independent reseller organizations
        reseller_a = baker.make(Organization, name="Reseller A", can_invite_organizations=True)
        reseller_b = baker.make(Organization, name="Reseller B", can_invite_organizations=True)

        # System user + token for Reseller A
        auth_service_a = PublicAPIAuthService()
        system_user_a, token_a = auth_service_a.create_system_user(
            integration_name="reseller_a_integration", organization=reseller_a
        )
        baker.make(ResourceAccess, system_user=system_user_a, resource_name="branding")

        # System user + token for Reseller B
        auth_service_b = PublicAPIAuthService()
        system_user_b, token_b = auth_service_b.create_system_user(
            integration_name="reseller_b_integration", organization=reseller_b
        )
        baker.make(ResourceAccess, system_user=system_user_b, resource_name="branding")

        mutation = """
        mutation UpdateBranding($input: UpdateBrandingInput!) {
            updateBranding(input: $input) {
                branding {
                    id
                    appName
                    primaryColor
                }
            }
        }
        """

        # Reseller A creates branding
        with container.public_api_auth_service.override(auth_service_a):
            response_a = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "App A",
                            "primaryColor": "#FF0000",
                            "supportEmail": "support_a@example.com",
                            "returnUrlAllowlist": ["https://a.example.com"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user_a.id}:{token_a}"},
            )

        assert response_a.status_code == 200
        data_a = response_a.json()
        assert "errors" not in data_a or len(data_a.get("errors", [])) == 0
        branding_id_a = data_a["data"]["updateBranding"]["branding"]["id"]

        # Verify A's branding row exists with correct values
        branding_row_a = OrganizationBranding.objects.get(id=branding_id_a)
        assert branding_row_a.organization == reseller_a
        assert branding_row_a.app_name == "App A"
        assert branding_row_a.primary_color == "#FF0000"
        assert branding_row_a.support_email == "support_a@example.com"
        assert "https://a.example.com" in branding_row_a.return_url_allowlist

        # Reseller B creates branding
        with container.public_api_auth_service.override(auth_service_b):
            response_b = self.client.post(
                "/graphql/",
                data={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "appName": "App B",
                            "primaryColor": "#0000FF",
                            "supportEmail": "support_b@example.com",
                            "returnUrlAllowlist": ["https://b.example.com"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user_b.id}:{token_b}"},
            )

        assert response_b.status_code == 200
        data_b = response_b.json()
        assert "errors" not in data_b or len(data_b.get("errors", [])) == 0
        branding_id_b = data_b["data"]["updateBranding"]["branding"]["id"]

        # Verify B's branding row exists and is different from A
        assert branding_id_b != branding_id_a, "B must get its own branding row, not A's"
        branding_row_b = OrganizationBranding.objects.get(id=branding_id_b)
        assert branding_row_b.organization == reseller_b
        assert branding_row_b.app_name == "App B"
        assert branding_row_b.primary_color == "#0000FF"

        # Verify A's row is completely untouched
        branding_row_a.refresh_from_db()
        assert branding_row_a.app_name == "App A", "A's app_name must not change"
        assert branding_row_a.primary_color == "#FF0000", "A's primary_color must not change"
        assert branding_row_a.support_email == "support_a@example.com", (
            "A's support_email must not change"
        )
        assert "https://a.example.com" in branding_row_a.return_url_allowlist, (
            "A's allowlist must not change"
        )

        # Verify there is exactly one branding row for A
        assert OrganizationBranding.objects.filter(organization=reseller_a).count() == 1
        # Verify there is exactly one branding row for B
        assert OrganizationBranding.objects.filter(organization=reseller_b).count() == 1


@pytest.mark.django_db
class TestGetCalendarMutationDependencies:
    """Unit tests for get_calendar_mutation_dependencies and helpers."""

    def test_get_calendar_mutation_dependencies_success(self):
        """Happy path: get_calendar_mutation_dependencies returns both services."""
        deps = get_calendar_mutation_dependencies()
        assert deps is not None
        assert deps.calendar_service is not None
        assert deps.calendar_group_service is not None

    def test_get_calendar_mutation_dependencies_missing_calendar_service(self):
        """Missing calendar_service raises GraphQLError."""
        from di_core.containers import container

        # Override with None to simulate missing dependency
        with container.calendar_service.override(None):
            with pytest.raises(GraphQLError) as exc_info:
                get_calendar_mutation_dependencies()
            # The error should mention the missing dependencies
            assert "Missing required dependencies" in str(exc_info.value)

    def test_get_calendar_mutation_dependencies_missing_calendar_group_service(self):
        """Missing calendar_group_service raises GraphQLError."""
        from di_core.containers import container

        # Override with None to simulate missing dependency
        with container.calendar_group_service.override(None):
            with pytest.raises(GraphQLError) as exc_info:
                get_calendar_mutation_dependencies()
            # The error should mention the missing dependencies
            assert "Missing required dependencies" in str(exc_info.value)

    def test_get_org_and_init_calendar_service_success(self):
        """Happy path: _get_org_and_init_calendar_service returns service and org."""
        # Create a test organization
        test_org = baker.make(Organization, name="Test Org")

        # Create a mock strawberry.Info with public_api_organization and public_api_system_user
        mock_request = Mock()
        mock_request.public_api_organization = test_org
        mock_system_user = baker.make("public_api.SystemUser", organization=test_org, id=999)
        mock_request.public_api_system_user = mock_system_user

        mock_info = Mock()
        mock_info.context = Mock()
        mock_info.context.request = mock_request

        # Call the function
        calendar_service, org = _get_org_and_init_calendar_service(mock_info)

        # Assert returns match expectations
        assert calendar_service is not None
        assert org == test_org
        # Assert the service was initialized with the org and user
        assert calendar_service.organization == test_org
        assert calendar_service.user_or_token == mock_system_user

    def test_get_org_and_init_calendar_service_missing_org_raises_error(self):
        """Missing organization in request context raises GraphQLError."""
        # Create a mock strawberry.Info with NO public_api_organization
        mock_request = Mock()
        mock_request.public_api_organization = None

        mock_info = Mock()
        mock_info.context = Mock()
        mock_info.context.request = mock_request

        # Call the function and expect GraphQLError
        with pytest.raises(GraphQLError) as exc_info:
            _get_org_and_init_calendar_service(mock_info)

        assert "Organization not found in request context" in str(exc_info.value)


CREATE_RESOURCE_CALENDAR_MUTATION = """
mutation CreateResourceCalendar($input: CreateResourceCalendarInput!) {
    createResourceCalendar(input: $input) {
        success
        errorMessage
        calendar {
            id
            name
            description
            calendarType
            capacity
            manageAvailableWindows
        }
    }
}
"""


@pytest.mark.django_db
class TestCreateResourceCalendarMutation:
    """Tests for the createResourceCalendar mutation (Phase 2a)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.CREATE_RESOURCE_CALENDAR]
        org = baker.make(Organization, name="Test Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return org, system_user, token, auth_service

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": CREATE_RESOURCE_CALENDAR_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_create_resource_calendar_happy_path(self):
        """A granted token creates a resource calendar; returns the calendar + DB row."""
        from calendar_integration.models import Calendar, CalendarType

        org, system_user, token, auth_service = self._setup_org_and_token()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Conference Room A",
                    "description": "Main conference room",
                    "capacity": 10,
                    "manageAvailableWindows": True,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createResourceCalendar"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["calendar"] is not None
        assert result["calendar"]["name"] == "Conference Room A"
        assert result["calendar"]["description"] == "Main conference room"
        assert result["calendar"]["calendarType"] == CalendarType.RESOURCE
        assert result["calendar"]["capacity"] == 10
        assert result["calendar"]["manageAvailableWindows"] is True

        # Verify the DB row exists and is scoped to the org
        calendar_id = int(result["calendar"]["id"])
        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.name == "Conference Room A"
        assert cal.calendar_type == CalendarType.RESOURCE
        assert cal.organization == org

    def test_create_resource_calendar_minimal_input(self):
        """Name-only input succeeds; optional fields default correctly."""
        from calendar_integration.models import CalendarType

        org, system_user, token, auth_service = self._setup_org_and_token()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "name": "Room B"}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["name"] == "Room B"
        assert result["calendar"]["calendarType"] == CalendarType.RESOURCE

    def test_create_resource_calendar_permission_denied_without_grant(self):
        """A token without CREATE_RESOURCE_CALENDAR grant is denied."""
        # Grant CALENDAR scope instead, NOT CREATE_RESOURCE_CALENDAR
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "name": "Room C"}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_create_resource_calendar_unauthenticated_denied(self):
        """An unauthenticated call is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": CREATE_RESOURCE_CALENDAR_MUTATION,
                "variables": {"input": {"organizationId": 1, "name": "Room D"}},
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_create_resource_calendar_org_scoping(self):
        """The created calendar belongs to the token's org, not the input organizationId.

        The organization is resolved from the token context (public_api_organization),
        not from the input field. The organizationId input is present for client
        convenience but the server always uses the token's org.
        """
        from calendar_integration.models import Calendar, CalendarType

        # Create the token's org
        org, system_user, token, auth_service = self._setup_org_and_token()
        # Create a different org that we pass as organizationId — should be ignored
        other_org = baker.make(Organization, name="Other Org")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,  # Deliberately different from token's org
                    "name": "Scoping Test Room",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        # The mutation should succeed (org context from token, not input)
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createResourceCalendar"]
        assert result["success"] is True

        # Verify calendar is scoped to the token's org
        calendar_id = int(result["calendar"]["id"])
        cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
        assert cal.organization == org
        assert cal.organization != other_org
        assert cal.calendar_type == CalendarType.RESOURCE

    def test_create_resource_calendar_error_path_service_raises_value_error(self):
        """An exception from CalendarService.create_resource_calendar returns success=False + errorMessage.

        This tests the error handler catching ValueError/ValidationError/IntegrityError
        and returning the failure result with the exception message.
        """
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a mock calendar service that raises ValueError
        from calendar_integration.services.calendar_service import CalendarService

        mock_calendar_service = Mock(spec=CalendarService)
        error_message = "boom"
        mock_calendar_service.create_resource_calendar.side_effect = ValueError(error_message)

        with (
            container.public_api_auth_service.override(auth_service),
            container.calendar_service.override(mock_calendar_service),
        ):
            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {
                    "input": {
                        "organizationId": org.id,
                        "name": "Will Fail Room",
                        "description": "This will trigger an error",
                    }
                },
            )

        assert response.status_code == 200
        data = response.json()
        # Should not have GraphQL errors; the mutation should return a result with success=False
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert error_message in result["errorMessage"]
        assert result["calendar"] is None


DISABLE_RESOURCE_CALENDAR_MUTATION = """
mutation DisableResourceCalendar($input: DisableResourceCalendarInput!) {
    disableResourceCalendar(input: $input) {
        success
        errorMessage
    }
}
"""


@pytest.mark.django_db
class TestDisableResourceCalendarMutation:
    """Tests for the disableResourceCalendar mutation (Phase 2b)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.DISABLE_RESOURCE_CALENDAR]
        org = baker.make(Organization, name="Test Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return org, system_user, token, auth_service

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": DISABLE_RESOURCE_CALENDAR_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_disable_resource_calendar_happy_path(self):
        """A granted token disables a resource calendar; visibility is set to INACTIVE in DB."""
        from calendar_integration.constants import CalendarType, CalendarVisibility
        from calendar_integration.models import Calendar

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar via the service (mirrors production path)
        from calendar_integration.services.calendar_service import CalendarService
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        resource_cal = calendar_service.create_resource_calendar(
            name="Room A",
            description="Test room",
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "calendarId": resource_cal.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["disableResourceCalendar"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # Verify the calendar is now INACTIVE in the DB
        resource_cal.refresh_from_db()
        assert resource_cal.visibility == CalendarVisibility.INACTIVE

        # Also verify via org-scoped query
        updated_cal = Calendar.objects.filter_by_organization(org.id).get(id=resource_cal.id)
        assert updated_cal.visibility == CalendarVisibility.INACTIVE
        assert updated_cal.calendar_type == CalendarType.RESOURCE

    def test_disable_resource_calendar_rejects_non_resource_calendar(self):
        """Attempting to disable a non-resource calendar (e.g. personal) returns success=False."""
        from calendar_integration.constants import CalendarType

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a personal (non-resource) calendar
        personal_cal = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.PERSONAL,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "calendarId": personal_cal.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["disableResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    def test_disable_resource_calendar_cross_org_rejected(self):
        """A calendar belonging to a different org returns success=False (Calendar.DoesNotExist)."""
        from calendar_integration.constants import CalendarType

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar in a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_cal = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "calendarId": other_cal.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["disableResourceCalendar"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

        # Verify the other org's calendar was NOT modified
        other_cal.refresh_from_db()
        from calendar_integration.constants import CalendarVisibility

        assert other_cal.visibility != CalendarVisibility.INACTIVE

    def test_disable_resource_calendar_permission_denied_without_grant(self):
        """A token without DISABLE_RESOURCE_CALENDAR grant is denied."""
        from calendar_integration.constants import CalendarType

        # Grant CALENDAR scope instead, NOT DISABLE_RESOURCE_CALENDAR
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_cal = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "calendarId": other_cal.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()


IMPORT_RESOURCE_CALENDARS_MUTATION = """
mutation ImportResourceCalendars($input: ImportResourceCalendarsInput!) {
    importResourceCalendars(input: $input) {
        success
        errorMessage
    }
}
"""


@pytest.mark.django_db
class TestImportResourceCalendarsMutation:
    """Tests for the importResourceCalendars mutation (Phase 2c)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.IMPORT_RESOURCE_CALENDARS]
        org = baker.make(Organization, name="Test Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return org, system_user, token, auth_service

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": IMPORT_RESOURCE_CALENDARS_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_import_resource_calendars_happy_path(self):
        """A granted token triggers import; OrganizationService.request_rooms_sync is called.

        The happy path mocks request_rooms_sync to avoid hitting Google APIs and asserts
        the method is called with the correct organization and optional time window.
        """
        org, system_user, token, auth_service = self._setup_org_and_token()

        with patch("organizations.services.OrganizationService.request_rooms_sync") as mock_sync:
            mock_sync.return_value = None

            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {"input": {"organizationId": org.id}},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["importResourceCalendars"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # Assert OrganizationService.request_rooms_sync was called with the org
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["organization"] == org
        assert call_kwargs["start_time"] is None
        assert call_kwargs["end_time"] is None

    def test_import_resource_calendars_with_time_window(self):
        """Supplying start_time and end_time passes them through to request_rooms_sync."""
        import datetime

        org, system_user, token, auth_service = self._setup_org_and_token()

        start = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)

        with patch("organizations.services.OrganizationService.request_rooms_sync") as mock_sync:
            mock_sync.return_value = None

            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {
                    "input": {
                        "organizationId": org.id,
                        "startTime": start.isoformat(),
                        "endTime": end.isoformat(),
                    }
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["importResourceCalendars"]
        assert result["success"] is True

        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["organization"] == org
        # start_time and end_time are passed through with the exact values supplied
        passed_start = call_kwargs["start_time"]
        passed_end = call_kwargs["end_time"]
        assert passed_start is not None
        assert passed_end is not None
        assert passed_start.year == start.year
        assert passed_start.month == start.month
        assert passed_start.day == start.day
        assert passed_start.hour == start.hour
        assert passed_end.year == end.year
        assert passed_end.month == end.month
        assert passed_end.day == end.day
        assert passed_end.hour == end.hour

    def test_import_resource_calendars_no_service_account_configured(self):
        """No service account → success=False with a descriptive error message.

        The error class (NoServiceAccountConfiguredError) is instantiated directly
        as the mock side_effect while the service path is mocked; we do not hit
        the real request_rooms_sync or Google APIs.
        """
        from organizations.exceptions import NoServiceAccountConfiguredError

        org, system_user, token, auth_service = self._setup_org_and_token()

        # request_rooms_sync is mocked to raise NoServiceAccountConfiguredError,
        # matching the real error the service raises when no GoogleCalendarServiceAccount
        # is configured for the org.
        with patch(
            "organizations.services.OrganizationService.request_rooms_sync",
            side_effect=NoServiceAccountConfiguredError(),
        ):
            response = self._post_mutation(
                system_user,
                token,
                auth_service,
                {"input": {"organizationId": org.id}},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["importResourceCalendars"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "service account" in result["errorMessage"].lower()

    def test_import_resource_calendars_permission_denied_without_grant(self):
        """A token without IMPORT_RESOURCE_CALENDARS grant is denied."""
        # Grant CALENDAR scope instead, NOT IMPORT_RESOURCE_CALENDARS
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_import_resource_calendars_unauthenticated_denied(self):
        """An unauthenticated call is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": IMPORT_RESOURCE_CALENDARS_MUTATION,
                "variables": {"input": {"organizationId": 1}},
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
