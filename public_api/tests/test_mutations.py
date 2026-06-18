import datetime
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService
from users.models import User


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


CREATE_SCOPED_SYSTEM_USER_MUTATION = """
mutation CreateScopedSystemUser($input: CreateScopedSystemUserInput!) {
    createScopedSystemUser(input: $input) {
        id
        integrationName
        isActive
        availableResources
        scopedToUserId
        token
    }
}
"""


@pytest.mark.django_db
class TestCreateScopedSystemUserMutation:
    """Integration tests for the createScopedSystemUser mutation (Phase 2)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_caller(self, resources: list[str] | None = None):
        """Create an organization + system user token with the given resource scopes."""
        if resources is None:
            resources = ["system_user"]
        org = baker.make(Organization, name="Caller Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="caller_integration", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return org, system_user, token, auth_service

    def _make_org_member(self, org):
        """Create a user and make them an active member of the given org."""
        user_model = get_user_model()
        user = baker.make(user_model, email=f"member_{uuid4().hex}@example.com")
        baker.make(OrganizationMembership, user=user, organization=org, is_active=True)
        return user

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": CREATE_SCOPED_SYSTEM_USER_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_happy_path_mints_scoped_token_returns_once(self):
        """A caller with SYSTEM_USER scope mints a scoped token; token returned exactly once.

        Asserts:
        - The response contains a non-null token.
        - The persisted SystemUser has scoped_to_user == the given owner.
        - The persisted ResourceAccess rows exactly match the requested grants.
        - The plaintext token is NOT stored in the DB (only the hash).
        """
        from common.utils.authentication_utils import verify_long_lived_token
        from public_api.models import SystemUser

        org, caller_su, caller_token, auth_service = self._setup_caller()
        owner = self._make_org_member(org)

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "provider_integration",
                    "scopedToUserId": owner.id,
                    "availableResources": ["calendar", "available_time"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createScopedSystemUser"]
        assert result["id"] is not None
        assert result["integrationName"] == "provider_integration"
        assert result["isActive"] is True
        assert result["scopedToUserId"] == owner.id
        assert set(result["availableResources"]) == {"calendar", "available_time"}

        # Token must be non-null and non-empty
        returned_token = result["token"]
        assert returned_token is not None
        assert len(returned_token) > 0

        # Verify persisted SystemUser has the correct owner (stored as membership FK)
        minted = SystemUser.objects.get(id=int(result["id"]))
        assert minted.scoped_to_membership_fk.user_id == owner.id
        assert minted.scoped_to_membership_fk.organization_id == org.id
        assert minted.organization_id == org.id

        # Verify ResourceAccess rows exactly match the request
        attached = set(
            ResourceAccess.objects.filter(system_user=minted).values_list(
                "resource_name", flat=True
            )
        )
        assert attached == {"calendar", "available_time"}

        # Verify plaintext token was NOT stored — only the hash is in the DB
        assert verify_long_lived_token(returned_token, minted.long_lived_token_hash)
        assert minted.long_lived_token_hash != returned_token

    def test_missing_system_user_scope_denied(self):
        """A token lacking SYSTEM_USER scope cannot call createScopedSystemUser."""
        org, caller_su, caller_token, auth_service = self._setup_caller(resources=["calendar"])
        owner = self._make_org_member(org)

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "no_scope_provider",
                    "scopedToUserId": owner.id,
                    "availableResources": ["calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # No SystemUser should have been created
        from public_api.models import SystemUser

        assert not SystemUser.objects.filter(integration_name="no_scope_provider").exists()

    def test_owner_not_member_of_org_rejected(self):
        """Owner id that belongs to a different org is rejected; no token created."""
        from public_api.models import SystemUser

        _org, caller_su, caller_token, auth_service = self._setup_caller()

        # Create a user in a different org — not a member of the caller's org
        other_org = baker.make(Organization, name="Other Org")
        user_model = get_user_model()
        outsider = baker.make(user_model, email="outsider@example.com")
        baker.make(OrganizationMembership, user=outsider, organization=other_org, is_active=True)

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "cross_org_provider",
                    "scopedToUserId": outsider.id,
                    "availableResources": ["calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "not an active member" in str(data["errors"]).lower()

        # No SystemUser should have been created
        assert not SystemUser.objects.filter(integration_name="cross_org_provider").exists()

    def test_nonexistent_owner_id_rejected(self):
        """A totally nonexistent user id is rejected; no token created."""
        from public_api.models import SystemUser

        _org, caller_su, caller_token, auth_service = self._setup_caller()

        nonexistent_user_id = 99999999

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "nonexistent_owner_provider",
                    "scopedToUserId": nonexistent_user_id,
                    "availableResources": ["calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "not an active member" in str(data["errors"]).lower()

        assert not SystemUser.objects.filter(integration_name="nonexistent_owner_provider").exists()

    def test_over_grant_resource_outside_allow_list_rejected(self):
        """availableResources containing a resource NOT in PROVIDER_SCOPED_RESOURCES is rejected.

        E.g. USER or SYSTEM_USER are not in the provider allow-list.
        """
        from public_api.models import SystemUser

        org, caller_su, caller_token, auth_service = self._setup_caller()
        owner = self._make_org_member(org)

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "over_grant_provider",
                    "scopedToUserId": owner.id,
                    "availableResources": ["calendar", "user"],  # "user" is NOT in allow-list
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "not permitted for provider-scoped tokens" in str(data["errors"]).lower()

        assert not SystemUser.objects.filter(integration_name="over_grant_provider").exists()

    def test_system_user_resource_in_available_resources_rejected(self):
        """SYSTEM_USER in availableResources is not in the provider allow-list → rejected."""
        from public_api.models import SystemUser

        org, caller_su, caller_token, auth_service = self._setup_caller()
        owner = self._make_org_member(org)

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "system_user_grant_provider",
                    "scopedToUserId": owner.id,
                    "availableResources": ["system_user"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "not permitted for provider-scoped tokens" in str(data["errors"]).lower()

        assert not SystemUser.objects.filter(integration_name="system_user_grant_provider").exists()

    def test_duplicate_integration_name_rejected_no_orphan(self):
        """Minting a second token with the same integration_name is rejected.

        Asserts:
        - The mutation returns a GraphQL error mentioning 'already exists'.
        - Exactly ONE SystemUser with that integration_name exists.
        - ResourceAccess count did not grow from the failed attempt.
        """
        from public_api.models import SystemUser

        org, caller_su, caller_token, auth_service = self._setup_caller()
        owner = self._make_org_member(org)

        # First call — must succeed
        r1 = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "dup_scoped",
                    "scopedToUserId": owner.id,
                    "availableResources": ["calendar"],
                }
            },
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert "errors" not in d1 or len(d1.get("errors", [])) == 0

        su_count_after_first = SystemUser.objects.filter(integration_name="dup_scoped").count()
        ra_count_after_first = ResourceAccess.objects.filter(
            system_user__integration_name="dup_scoped"
        ).count()
        assert su_count_after_first == 1

        # Second call with the same integration_name — must fail
        r2 = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "dup_scoped",
                    "scopedToUserId": owner.id,
                    "availableResources": ["calendar"],
                }
            },
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert "errors" in d2
        assert len(d2["errors"]) > 0
        assert "already exists" in str(d2["errors"]).lower()

        # No orphan SystemUser — still exactly one
        assert (
            SystemUser.objects.filter(integration_name="dup_scoped").count() == su_count_after_first
        )
        assert (
            ResourceAccess.objects.filter(system_user__integration_name="dup_scoped").count()
            == ra_count_after_first
        )

    def test_empty_available_resources_rejected(self):
        """Empty availableResources list is rejected; no token created."""
        from public_api.models import SystemUser

        org, caller_su, caller_token, auth_service = self._setup_caller()
        owner = self._make_org_member(org)

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "empty_resources_provider",
                    "scopedToUserId": owner.id,
                    "availableResources": [],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

        assert not SystemUser.objects.filter(integration_name="empty_resources_provider").exists()

    def test_scoped_provider_token_cannot_mint(self):
        """A provider-scoped token (CALENDAR/AVAILABLE_TIME only) cannot call
        createScopedSystemUser — it lacks the SYSTEM_USER resource.

        This proves a provider token cannot self-escalate by minting new tokens.
        The scoped SystemUser's ResourceAccess grants are only provider resources
        (CALENDAR, AVAILABLE_TIME), NOT SYSTEM_USER.
        """
        from public_api.models import SystemUser

        # Build the org + a master caller token that has SYSTEM_USER scope
        org, _master_su, _master_token, auth_service = self._setup_caller(resources=["system_user"])
        owner = self._make_org_member(org)

        # Directly create a provider-scoped SystemUser with only CALENDAR + AVAILABLE_TIME grants
        owner_membership = OrganizationMembership.objects.get(user=owner, organization=org)
        scoped_su, scoped_token = auth_service.create_system_user(
            integration_name="provider_scoped_caller",
            organization=org,
            scoped_to_membership=owner_membership,
        )
        baker.make(ResourceAccess, system_user=scoped_su, resource_name="calendar")
        baker.make(ResourceAccess, system_user=scoped_su, resource_name="available_time")
        # Note: SYSTEM_USER is intentionally NOT granted

        # Attempt to mint another token authenticated as the provider-scoped token.
        # The permission check fires before owner validation, so reusing owner is fine.
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_SCOPED_SYSTEM_USER_MUTATION,
                    "variables": {
                        "input": {
                            "integrationName": "escalated_token",
                            "scopedToUserId": owner.id,
                            "availableResources": ["calendar"],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {scoped_su.id}:{scoped_token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # No new SystemUser should have been created from the escalation attempt
        assert not SystemUser.objects.filter(integration_name="escalated_token").exists()

    def test_inactive_member_owner_rejected(self):
        """createScopedSystemUser with an inactive-membership owner is rejected.

        An OrganizationMembership with is_active=False must not satisfy the owner
        validation, and no SystemUser/token row must be created.
        """
        from public_api.models import SystemUser

        org, caller_su, caller_token, auth_service = self._setup_caller()

        # Create a user with an INACTIVE membership in the caller's org
        user_model = get_user_model()
        inactive_user = baker.make(user_model, email="inactive_member@example.com")
        baker.make(
            OrganizationMembership,
            user=inactive_user,
            organization=org,
            is_active=False,
        )

        response = self._post_mutation(
            caller_su,
            caller_token,
            auth_service,
            {
                "input": {
                    "integrationName": "inactive_owner_provider",
                    "scopedToUserId": inactive_user.id,
                    "availableResources": ["calendar"],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "not an active member" in str(data["errors"]).lower()

        # No SystemUser or token row must have been created
        assert not SystemUser.objects.filter(integration_name="inactive_owner_provider").exists()


CREATE_AVAILABLE_TIME_MUTATION = """
mutation CreateAvailableTime(
    $calendarId: Int!,
    $startTime: DateTime!,
    $endTime: DateTime!,
    $timezone: String!,
    $rruleString: String
) {
    createAvailableTime(
        calendarId: $calendarId,
        startTime: $startTime,
        endTime: $endTime,
        timezone: $timezone,
        rruleString: $rruleString
    ) {
        id
        startTime
        endTime
    }
}
"""


def _make_scoped_available_time_client(
    organization: Organization,
    owner: User,
) -> tuple[APIClient, SystemUser]:
    """Create a scoped API client with AVAILABLE_TIME resource grant."""
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=owner, organization=organization, defaults={"is_active": True}
    )
    token = generate_long_lived_token()
    system_user = baker.make(
        SystemUser,
        organization=organization,
        scoped_to_membership_fk=membership,
        integration_name=f"scoped_at_{organization.pk}_{owner.pk}",
        long_lived_token_hash=hash_long_lived_token(token),
        is_active=True,
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.AVAILABLE_TIME
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _make_org_wide_available_time_client(
    organization: Organization,
) -> tuple[APIClient, SystemUser]:
    """Create an org-wide API client with AVAILABLE_TIME resource grant."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"org_wide_at_{organization.pk}", organization=organization
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.AVAILABLE_TIME
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


@pytest.mark.django_db
class TestCreateAvailableTimeMutation:
    """Integration tests for the createAvailableTime mutation (Phase 4a).

    Covers:
    - Scoped token creates a one-off available time (no rrule).
    - Scoped token creates a recurring available time (with rrule_string).
    - Scoped token attempting a cross-owner calendar gets not-found (no existence leak).
    - Org-wide token can create on any calendar in its org (owner guard is scoped-only).
    - Token without AVAILABLE_TIME resource is denied.
    """

    def setup_method(self) -> None:
        self.client = APIClient()

    def _make_owner_with_calendar(self, organization: Organization) -> tuple[User, Calendar]:
        """Create a user + calendar (manage_available_windows=True) owned by that user."""
        owner = baker.make(User, email=f"owner_{uuid4().hex}@test.com")
        cal = baker.make(
            Calendar,
            organization=organization,
            name="Owner Cal",
            external_id=f"ext-{organization.pk}-{owner.pk}",
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=organization)
        return owner, cal

    def test_scoped_token_creates_one_off_available_time(self) -> None:
        """A scoped token creates a one-off (no rrule) available time on its owned calendar.

        Asserts:
        - The mutation returns success (id, startTime, endTime).
        - An AvailableTime row is persisted on that calendar.
        - The persisted row has no recurrence_rule (one-off).
        """
        org = baker.make(Organization, name="Scoped AT Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_available_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["createAvailableTime"]
        assert result["id"] is not None

        # Confirm AvailableTime was persisted in the DB
        at = AvailableTime.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert at.calendar_fk_id == cal.id
        assert at.recurrence_rule_fk_id is None, "One-off must have no recurrence rule"

    def test_scoped_token_creates_recurring_available_time(self) -> None:
        """A scoped token creates a recurring available time (with rrule_string).

        Asserts:
        - The mutation returns success.
        - An AvailableTime row is persisted with a recurrence_rule attached.
        """
        org = baker.make(Organization, name="Scoped AT Recurring Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_available_time_client(org, owner)

        start = datetime.datetime(2026, 7, 7, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 7, 17, 0, tzinfo=datetime.UTC)
        rrule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "rruleString": rrule,
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["createAvailableTime"]
        assert result["id"] is not None

        # Confirm AvailableTime was persisted with a recurrence rule
        at = AvailableTime.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert at.calendar_fk_id == cal.id
        assert at.recurrence_rule_fk_id is not None, "Recurring must have a recurrence rule"

    def test_scoped_token_cross_owner_calendar_not_found(self) -> None:
        """A scoped token attempting to create on another provider's calendar gets not-found.

        The response must be identical to a genuinely missing calendar — no existence leak.

        Asserts:
        - The mutation returns a GraphQL error.
        - The error message matches the not-found path (does NOT reveal the calendar exists).
        - No AvailableTime row is created for that calendar.
        """
        org = baker.make(Organization, name="Cross-Owner AT Org")
        owner, _owner_cal = self._make_owner_with_calendar(org)

        # Another provider's calendar in the same org — the scoped token must not touch it
        other_owner = baker.make(User, email="other_provider@test.com")
        other_cal = baker.make(
            Calendar,
            organization=org,
            name="Other Provider Cal",
            external_id="other-ext-cross",
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=other_cal, user=other_owner, organization=org)

        client, _ = _make_scoped_available_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": other_cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        # The error must be a not-found path — same message as a missing calendar
        assert "does not exist" in str(data["errors"]).lower()

        # No AvailableTime row must have been created for the other calendar
        assert (
            not AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=other_cal.id)
            .exists()
        ), "No AvailableTime must be created on another owner's calendar"

    def test_scoped_token_missing_calendar_same_not_found_response(self) -> None:
        """Cross-owner and genuinely-missing calendar produce identical not-found errors.

        This confirms no existence leak: comparing the error text for a cross-owner
        calendar vs. a nonexistent id must yield the same message.
        """
        org = baker.make(Organization, name="No-Leak AT Org")
        owner = baker.make(User, email="no_leak_owner@test.com")
        client, _ = _make_scoped_available_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        nonexistent_id = 999999999

        response_missing = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": nonexistent_id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        # Create a calendar in the same org owned by someone else
        other_owner = baker.make(User, email="other_no_leak@test.com")
        other_cal = baker.make(
            Calendar,
            organization=org,
            name="Other No Leak Cal",
            external_id="other-no-leak-ext",
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=other_cal, user=other_owner, organization=org)

        response_cross_owner = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": other_cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        # Both responses must be errors
        assert "errors" in response_missing.json()
        assert "errors" in response_cross_owner.json()

        # The error messages must be identical (no existence leak)
        missing_msg = str(response_missing.json()["errors"])
        cross_owner_msg = str(response_cross_owner.json()["errors"])
        assert missing_msg == cross_owner_msg, (
            f"Cross-owner response must be identical to missing-calendar response.\n"
            f"Missing: {missing_msg}\nCross-owner: {cross_owner_msg}"
        )

    def test_org_wide_token_creates_on_any_calendar(self) -> None:
        """An org-wide token (scoped_to_user IS NULL) can create on any calendar.

        This proves the owner guard is scoped-only: org-wide tokens are unaffected.

        Asserts:
        - The mutation succeeds.
        - An AvailableTime row is persisted on the calendar.
        """
        org = baker.make(Organization, name="Org-Wide AT Org")
        _owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_org_wide_available_time_client(org)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["createAvailableTime"]
        assert result["id"] is not None

        at = AvailableTime.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert at.calendar_fk_id == cal.id

    def test_token_without_available_time_resource_denied(self) -> None:
        """A token lacking the AVAILABLE_TIME resource is denied by OrganizationResourceAccess.

        Asserts:
        - The mutation returns a GraphQL error with a permission-denied message.
        - No AvailableTime row is created.
        """
        org = baker.make(Organization, name="No-Scope AT Org")
        _owner, cal = self._make_owner_with_calendar(org)

        # Create a token with a DIFFERENT resource — not AVAILABLE_TIME
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_at_scope", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # No AvailableTime must have been created
        assert (
            not AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        )

    def test_create_available_time_calendar_not_managing_windows_returns_error(self) -> None:
        """createAvailableTime on a calendar with manage_available_windows=False returns a
        user-facing GraphQL error, not a 500, and no AvailableTime row is created.

        Args:
            None — uses a scoped token whose owner has a calendar that does not manage
            its own available windows.

        Returns:
            N/A — assertion test.

        Raises:
            N/A — verifies that ValueError from CalendarService is surfaced as GraphQLError.
        """
        org = baker.make(Organization, name="No-Manage-Windows AT Org")
        owner = baker.make(User, email=f"owner_{uuid4().hex}@test.com")
        cal = baker.make(
            Calendar,
            organization=org,
            name="No-Manage Cal",
            external_id=f"ext-no-manage-{uuid4().hex}",
            manage_available_windows=False,
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=org)
        client, _ = _make_scoped_available_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABLE_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data, "Expected a GraphQL error when calendar does not manage windows"
        assert len(data["errors"]) > 0

        # No AvailableTime row must have been written
        assert not AvailableTime.objects.filter(calendar_fk=cal.id).exists()


CREATE_BLOCKED_TIME_MUTATION = """
mutation CreateBlockedTime(
    $calendarId: Int!,
    $startTime: DateTime!,
    $endTime: DateTime!,
    $timezone: String!,
    $reason: String,
    $rruleString: String
) {
    createBlockedTime(
        calendarId: $calendarId,
        startTime: $startTime,
        endTime: $endTime,
        timezone: $timezone,
        reason: $reason,
        rruleString: $rruleString
    ) {
        id
        startTime
        endTime
    }
}
"""


def _make_scoped_blocked_time_client(
    organization: Organization,
    owner: User,
) -> tuple[APIClient, SystemUser]:
    """Create a scoped API client with BLOCKED_TIME resource grant."""
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=owner, organization=organization, defaults={"is_active": True}
    )
    token = generate_long_lived_token()
    system_user = baker.make(
        SystemUser,
        organization=organization,
        scoped_to_membership_fk=membership,
        integration_name=f"scoped_bt_{organization.pk}_{owner.pk}",
        long_lived_token_hash=hash_long_lived_token(token),
        is_active=True,
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.BLOCKED_TIME
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _make_org_wide_blocked_time_client(
    organization: Organization,
) -> tuple[APIClient, SystemUser]:
    """Create an org-wide API client with BLOCKED_TIME resource grant."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"org_wide_bt_{organization.pk}", organization=organization
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.BLOCKED_TIME
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


@pytest.mark.django_db
class TestCreateBlockedTimeMutation:
    """Integration tests for the createBlockedTime mutation (Phase 4b).

    Covers:
    - Scoped token creates a one-off blocked time (no rrule).
    - Scoped token creates a recurring blocked time (with rrule_string).
    - Scoped token attempting a cross-owner calendar gets not-found (no existence leak).
    - Org-wide token can create on any calendar in its org (owner guard is scoped-only).
    - Token without BLOCKED_TIME resource is denied.
    """

    def setup_method(self) -> None:
        self.client = APIClient()

    def _make_owner_with_calendar(self, organization: Organization) -> tuple[User, Calendar]:
        """Create a user + calendar owned by that user."""
        owner = baker.make(User, email=f"owner_bt_{uuid4().hex}@test.com")
        cal = baker.make(
            Calendar,
            organization=organization,
            name="Owner BT Cal",
            external_id=f"ext-bt-{organization.pk}-{owner.pk}",
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=organization)
        return owner, cal

    def test_scoped_token_creates_one_off_blocked_time(self) -> None:
        """A scoped token creates a one-off (no rrule) blocked time on its owned calendar.

        Asserts:
        - The mutation returns success (id, startTime, endTime).
        - A BlockedTime row is persisted on that calendar.
        - The persisted row has no recurrence_rule (one-off).
        """
        org = baker.make(Organization, name="Scoped BT Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_blocked_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "reason": "Do not book",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["createBlockedTime"]
        assert result["id"] is not None

        # Confirm BlockedTime was persisted in the DB
        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert bt.calendar_fk_id == cal.id
        assert bt.recurrence_rule_fk_id is None, "One-off must have no recurrence rule"
        assert bt.reason == "Do not book"

    def test_scoped_token_creates_recurring_blocked_time(self) -> None:
        """A scoped token creates a recurring blocked time (with rrule_string).

        Asserts:
        - The mutation returns success.
        - A BlockedTime row is persisted with a recurrence_rule attached.
        """
        org = baker.make(Organization, name="Scoped BT Recurring Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_blocked_time_client(org, owner)

        start = datetime.datetime(2026, 7, 7, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 7, 17, 0, tzinfo=datetime.UTC)
        rrule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "rruleString": rrule,
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["createBlockedTime"]
        assert result["id"] is not None

        # Confirm BlockedTime was persisted with a recurrence rule
        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert bt.calendar_fk_id == cal.id
        assert bt.recurrence_rule_fk_id is not None, "Recurring must have a recurrence rule"

    def test_scoped_token_cross_owner_calendar_not_found(self) -> None:
        """A scoped token attempting to create on another provider's calendar gets not-found.

        The response must be identical to a genuinely missing calendar — no existence leak.

        Asserts:
        - The mutation returns a GraphQL error.
        - The error message matches the not-found path (does NOT reveal the calendar exists).
        - No BlockedTime row is created for that calendar.
        """
        org = baker.make(Organization, name="Cross-Owner BT Org")
        owner, _owner_cal = self._make_owner_with_calendar(org)

        # Another provider's calendar in the same org — the scoped token must not touch it
        other_owner = baker.make(User, email=f"other_bt_provider_{uuid4().hex}@test.com")
        other_cal = baker.make(
            Calendar,
            organization=org,
            name="Other BT Provider Cal",
            external_id=f"other-ext-bt-cross-{uuid4().hex}",
        )
        baker.make(CalendarOwnership, calendar=other_cal, user=other_owner, organization=org)

        client, _ = _make_scoped_blocked_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": other_cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        # The error must be a not-found path — same message as a missing calendar
        assert "does not exist" in str(data["errors"]).lower()

        # No BlockedTime row must have been created for the other calendar
        assert (
            not BlockedTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=other_cal.id)
            .exists()
        ), "No BlockedTime must be created on another owner's calendar"

    def test_org_wide_token_creates_on_any_calendar(self) -> None:
        """An org-wide token (scoped_to_user IS NULL) can create on any calendar.

        This proves the owner guard is scoped-only: org-wide tokens are unaffected.

        Asserts:
        - The mutation succeeds.
        - A BlockedTime row is persisted on the calendar.
        """
        org = baker.make(Organization, name="Org-Wide BT Org")
        _owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_org_wide_blocked_time_client(org)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["createBlockedTime"]
        assert result["id"] is not None

        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert bt.calendar_fk_id == cal.id

    def test_token_without_blocked_time_resource_denied(self) -> None:
        """A token lacking the BLOCKED_TIME resource is denied by OrganizationResourceAccess.

        Asserts:
        - The mutation returns a GraphQL error with a permission-denied message.
        - No BlockedTime row is created.
        """
        org = baker.make(Organization, name="No-Scope BT Org")
        _owner, cal = self._make_owner_with_calendar(org)

        # Create a token with a DIFFERENT resource — not BLOCKED_TIME
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_bt_scope", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # No BlockedTime must have been created
        assert (
            not BlockedTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        )

    def test_scoped_token_missing_calendar_same_not_found_response(self) -> None:
        """Cross-owner and genuinely-missing calendar produce identical not-found errors.

        This confirms no existence leak: comparing the error text for a cross-owner
        calendar vs. a nonexistent id must yield the same message.
        """
        org = baker.make(Organization, name="No-Leak BT Org")
        owner = baker.make(User, email=f"no_leak_bt_owner_{uuid4().hex}@test.com")
        client, _ = _make_scoped_blocked_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)

        nonexistent_id = 999999999

        response_missing = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": nonexistent_id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        # Create a calendar in the same org owned by someone else
        other_owner = baker.make(User, email=f"other_no_leak_bt_{uuid4().hex}@test.com")
        other_cal = baker.make(
            Calendar,
            organization=org,
            name="Other No Leak BT Cal",
            external_id=f"other-no-leak-bt-ext-{uuid4().hex}",
        )
        baker.make(CalendarOwnership, calendar=other_cal, user=other_owner, organization=org)

        response_cross_owner = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": other_cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        # Both responses must be errors
        assert "errors" in response_missing.json()
        assert "errors" in response_cross_owner.json()

        # The error messages must be identical (no existence leak)
        missing_msg = str(response_missing.json()["errors"])
        cross_owner_msg = str(response_cross_owner.json()["errors"])
        assert missing_msg == cross_owner_msg, (
            f"Cross-owner response must be identical to missing-calendar response.\n"
            f"Missing: {missing_msg}\nCross-owner: {cross_owner_msg}"
        )

    def test_create_blocked_time_reason_too_long_returns_error(self) -> None:
        """A reason longer than 255 chars returns a clean GraphQL error; no BlockedTime created.

        Confirms the Fix-1 guard fires before the service call — without the guard a
        DataError (not ValueError) would bubble up as an uncaught 500.
        """
        org = baker.make(Organization, name="Reason Too Long BT Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_blocked_time_client(org, owner)

        start = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 7, 1, 17, 0, tzinfo=datetime.UTC)
        long_reason = "x" * 256  # 256 chars — one over the max

        response = client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "reason": long_reason,
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "255 characters or fewer" in str(data["errors"])

        # No BlockedTime row must have been created
        assert (
            not BlockedTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        )


SCHEDULE_EVENT_MUTATION = """
mutation ScheduleEvent(
    $calendarId: Int!,
    $title: String!,
    $startTime: DateTime!,
    $endTime: DateTime!,
    $timezone: String!,
    $description: String,
    $rruleString: String,
    $attendances: [EventAttendanceInput!],
    $externalAttendances: [EventExternalAttendanceInput!]
) {
    scheduleEvent(
        calendarId: $calendarId,
        title: $title,
        startTime: $startTime,
        endTime: $endTime,
        timezone: $timezone,
        description: $description,
        rruleString: $rruleString,
        attendances: $attendances,
        externalAttendances: $externalAttendances
    ) {
        id
        title
        startTime
        endTime
    }
}
"""


def _make_scoped_calendar_event_client(
    organization: Organization,
    owner: User,
) -> tuple[APIClient, SystemUser]:
    """Create a scoped API client with CALENDAR_EVENT resource grant."""
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=owner, organization=organization, defaults={"is_active": True}
    )
    token = generate_long_lived_token()
    system_user = baker.make(
        SystemUser,
        organization=organization,
        scoped_to_membership_fk=membership,
        integration_name=f"scoped_ce_{organization.pk}_{owner.pk}",
        long_lived_token_hash=hash_long_lived_token(token),
        is_active=True,
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _make_org_wide_calendar_event_client(
    organization: Organization,
) -> tuple[APIClient, SystemUser]:
    """Create an org-wide API client with CALENDAR_EVENT resource grant."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"org_wide_ce_{organization.pk}", organization=organization
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


@pytest.mark.django_db
class TestScheduleEventMutation:
    """Integration tests for the scheduleEvent mutation (Phase 4c).

    Covers:
    - Scoped owner on a non-public-scheduling calendar succeeds (the core owner-path proof).
    - Scoped token schedules a recurring event (with rrule_string).
    - Attendees persisted: external attendee persists; out-of-org internal user_id → clean error.
    - Cross-owner calendar_id → not-found, identical to genuinely missing, no row created.
    - Org-wide token (scoped_to_user IS NULL) → DENIED (intentional design; event creation
      requires either owner-scoped token or public-scheduling / management-token path).
    - No availability window covering the slot → clean GraphQL error, no row.
    - Token without CALENDAR_EVENT resource → denied.
    - A too-long title returns a clean GraphQL error, no row created.
    """

    def setup_method(self) -> None:
        self.client = APIClient()

    def _make_owner_with_calendar(self, organization: Organization) -> tuple[User, Calendar]:
        """Create a user + calendar (manage_available_windows=False) owned by that user.

        The calendar has manage_available_windows=False — the owner-scoped path skips
        the AvailableTime check in this configuration (service always returns a window
        when manage_available_windows is False).  No dead AvailableTime rows are created.
        Tests that require the managed-window path should set manage_available_windows=True
        and add a covering AvailableTime window explicitly.
        """
        owner = baker.make(User, email=f"owner_ce_{uuid4().hex}@test.com")
        cal = baker.make(
            Calendar,
            organization=organization,
            name="Owner CE Cal",
            external_id=f"ext-ce-{organization.pk}-{owner.pk}",
            accepts_public_scheduling=False,
            manage_available_windows=False,
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=organization)
        return owner, cal

    def test_scoped_owner_on_non_public_calendar_succeeds(self) -> None:
        """Core proof: scoped owner schedules on a calendar with managed availability windows.

        The calendar has accepts_public_scheduling=False and manage_available_windows=True
        with an AvailableTime row that covers the event slot.  The owner-scoped path must
        succeed via the CalendarOwnership check — it does NOT rely on accepts_public_scheduling.
        This exercises the managed-window provider flow end-to-end.

        Asserts:
        - The mutation returns success (id, title, startTime, endTime).
        - A CalendarEvent row is persisted on that calendar with the given title.
        - The persisted row has no recurrence_rule (one-off).
        """
        org = baker.make(Organization, name="Scoped CE Org")
        owner = baker.make(User, email=f"owner_ce_{uuid4().hex}@test.com")
        cal = baker.make(
            Calendar,
            organization=org,
            name="Owner CE Cal",
            external_id=f"ext-ce-managed-{org.pk}-{owner.pk}",
            accepts_public_scheduling=False,
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=org)
        # Add an AvailableTime window that covers the event slot below
        AvailableTime.objects.create(
            organization=org,
            calendar=cal,
            start_time_tz_unaware=datetime.datetime(2026, 8, 1, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 8, 1, 23, 59),
            timezone="UTC",
        )
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Team Sync",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "description": "Weekly team sync meeting",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["scheduleEvent"]
        assert result["id"] is not None
        assert result["title"] == "Team Sync"

        # Confirm CalendarEvent was persisted in the DB
        event = CalendarEvent.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert event.calendar_fk_id == cal.id
        assert event.title == "Team Sync"
        assert event.recurrence_rule_fk_id is None, "One-off event must have no recurrence rule"

    def test_scoped_token_schedules_recurring_event(self) -> None:
        """A scoped token schedules a recurring event (rrule_string) on a non-public calendar.

        Asserts:
        - The mutation returns success.
        - A CalendarEvent row is persisted with a recurrence_rule attached.
        """
        org = baker.make(Organization, name="Scoped CE Recurring Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 4, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 4, 10, 0, tzinfo=datetime.UTC)
        rrule = "FREQ=WEEKLY;BYDAY=MO"

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Weekly Monday Meeting",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "rruleString": rrule,
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["scheduleEvent"]
        assert result["id"] is not None

        # Confirm CalendarEvent was persisted with a recurrence rule
        event = CalendarEvent.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert event.calendar_fk_id == cal.id
        assert event.recurrence_rule_fk_id is not None, (
            "Recurring event must have a recurrence rule"
        )

    def test_scoped_token_schedules_event_with_external_attendee_persisted(self) -> None:
        """External attendee is persisted on a successful schedule.

        Asserts:
        - The mutation returns success.
        - A CalendarEvent row is persisted with an EventExternalAttendance record.
        """
        from calendar_integration.models import EventExternalAttendance

        org = baker.make(Organization, name="Scoped CE Attendee Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 5, 14, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 5, 15, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Client Call",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "externalAttendances": [
                        {"externalAttendee": {"email": "client@external.com", "name": "Client"}}
                    ],
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0, data.get("errors")
        result = data["data"]["scheduleEvent"]
        assert result["id"] is not None

        # Confirm CalendarEvent persisted with at least one external attendance
        event = CalendarEvent.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert event.calendar_fk_id == cal.id
        assert EventExternalAttendance.objects.filter(event=event).exists(), (
            "At least one EventExternalAttendance must be created"
        )

    def test_nonexistent_internal_attendee_user_id_returns_clean_error(self) -> None:
        """An attendances user_id that does not exist → clean GraphQL error, no row created.

        Attendees are now pre-validated against active org membership before any DB writes.
        A nonexistent user_id is caught by the pre-validation check and surfaces a clean
        GraphQL error.  No CalendarEvent row must be left behind.
        """
        from calendar_integration.models import EventAttendance as EventAttendanceModel

        org = baker.make(Organization, name="Nonexistent-User Attendee Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        # Use a user_id that does not exist in the users table at all
        nonexistent_user_id = 999999999

        start = datetime.datetime(2026, 8, 6, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 6, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Meeting with Ghost",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "attendances": [{"userId": nonexistent_user_id}],
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        # Must be a clean GraphQL error (not a 500)
        assert "errors" in data
        assert len(data["errors"]) > 0
        # The pre-validation error message must identify the problem clearly
        assert "not active members" in str(data["errors"]).lower()
        # No CalendarEvent row must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        ), "No CalendarEvent must be created when the attendee pre-validation fails"
        # No EventAttendance row either
        assert not EventAttendanceModel.objects.filter_by_organization(org.id).exists()

    def test_scoped_token_cross_owner_calendar_not_found(self) -> None:
        """A scoped token attempting to schedule on another provider's calendar gets not-found.

        The response must be identical to a genuinely missing calendar — no existence leak.

        Asserts:
        - The mutation returns a GraphQL error.
        - The error message matches the not-found path (does NOT reveal the calendar exists).
        - No CalendarEvent row is created for that calendar.
        """
        org = baker.make(Organization, name="Cross-Owner CE Org")
        owner, _owner_cal = self._make_owner_with_calendar(org)

        # Another provider's calendar in the same org — the scoped token must not touch it
        other_owner = baker.make(User, email=f"other_ce_provider_{uuid4().hex}@test.com")
        other_cal = baker.make(
            Calendar,
            organization=org,
            name="Other CE Provider Cal",
            external_id=f"other-ext-ce-cross-{uuid4().hex}",
        )
        baker.make(CalendarOwnership, calendar=other_cal, user=other_owner, organization=org)

        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": other_cal.id,
                    "title": "Unauthorized Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        # The error must be a not-found path — same message as a missing calendar
        assert "does not exist" in str(data["errors"]).lower()

        # No CalendarEvent row must have been created for the other calendar
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=other_cal.id)
            .exists()
        ), "No CalendarEvent must be created on another owner's calendar"

    def test_scoped_token_missing_calendar_same_not_found_response(self) -> None:
        """Cross-owner and genuinely-missing calendar produce identical not-found errors.

        The owner first OWNS their own calendar (populated allowed-set = {own_cal.id}).
        Then we assert that requesting a different provider's calendar id and a nonexistent
        id (999999999) both yield the IDENTICAL error — proving no existence leak even with
        a non-empty allowed-set.
        """
        org = baker.make(Organization, name="No-Leak CE Org")
        # Give the owner their own calendar so the allowed-set is populated
        owner, _own_cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        nonexistent_id = 999999999

        response_missing = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": nonexistent_id,
                    "title": "Ghost Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        # Create a calendar in the same org owned by someone else
        other_owner = baker.make(User, email=f"other_no_leak_ce_{uuid4().hex}@test.com")
        other_cal = baker.make(
            Calendar,
            organization=org,
            name="Other No Leak CE Cal",
            external_id=f"other-no-leak-ce-ext-{uuid4().hex}",
        )
        baker.make(CalendarOwnership, calendar=other_cal, user=other_owner, organization=org)

        response_cross_owner = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": other_cal.id,
                    "title": "Ghost Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        # Both responses must be errors
        assert "errors" in response_missing.json()
        assert "errors" in response_cross_owner.json()

        # The error messages must be identical (no existence leak)
        missing_msg = str(response_missing.json()["errors"])
        cross_owner_msg = str(response_cross_owner.json()["errors"])
        assert missing_msg == cross_owner_msg, (
            f"Cross-owner response must be identical to missing-calendar response.\n"
            f"Missing: {missing_msg}\nCross-owner: {cross_owner_msg}"
        )

    def test_org_wide_token_event_creation_is_denied(self) -> None:
        """An org-wide token (scoped_to_user IS NULL) is DENIED for event creation.

        By design, org-wide Public API tokens cannot create events directly — event
        creation requires either an owner-scoped token (CalendarOwnership check) or
        the public-scheduling / management-token path (User branch).  This test
        confirms org-wide tokens remain blocked and asserts no CalendarEvent row is
        created.

        Asserts:
        - The mutation returns a GraphQL error (PermissionDenied → GraphQLError).
        - No CalendarEvent row is created.
        """
        org = baker.make(Organization, name="Org-Wide CE Org")
        _owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_org_wide_calendar_event_client(org)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Org-Wide Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

        # No CalendarEvent must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        ), "No CalendarEvent must be created via an org-wide token"

    def test_no_availability_window_returns_clean_error(self) -> None:
        """No AvailableTime covering the event slot → clean GraphQL error, no row created.

        The calendar is owner-scoped (CalendarOwnership authorizes the token) but has
        manage_available_windows=True and NO AvailableTime window — so the service's
        availability check raises NoAvailableTimeWindowsError.  The mutation must catch
        it and surface a clean GraphQL error.

        Asserts:
        - The mutation returns a GraphQL error.
        - No CalendarEvent row is created.
        """
        org = baker.make(Organization, name="No-Window CE Org")
        owner = baker.make(User, email=f"no_window_owner_{uuid4().hex}@test.com")
        # manage_available_windows=True forces the service to check AvailableTime rows.
        # No AvailableTime rows → NoAvailableTimeWindowsError.
        cal = baker.make(
            Calendar,
            organization=org,
            name="No Window Cal",
            external_id=f"no-window-ext-{uuid4().hex}",
            accepts_public_scheduling=False,
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=org)
        # Intentionally do NOT create any AvailableTime rows
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "No Window Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

        # No CalendarEvent row must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        ), "No CalendarEvent must be created when there are no available time windows"

    def test_token_without_calendar_event_resource_denied(self) -> None:
        """A token lacking the CALENDAR_EVENT resource is denied by OrganizationResourceAccess.

        Asserts:
        - The mutation returns a GraphQL error with a permission-denied message.
        - No CalendarEvent row is created.
        """
        org = baker.make(Organization, name="No-Scope CE Org")
        _owner, cal = self._make_owner_with_calendar(org)

        # Create a token with a DIFFERENT resource — not CALENDAR_EVENT
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_ce_scope", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Unauthorized Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # No CalendarEvent must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        )

    def test_title_too_long_returns_error_no_row_created(self) -> None:
        """A title longer than 255 chars returns a clean GraphQL error; no CalendarEvent created.

        Confirms the title-length guard fires before the service call — without the guard
        a DataError would bubble up as an uncaught 500.
        """
        org = baker.make(Organization, name="Long Title CE Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)
        long_title = "x" * 256  # 256 chars — one over the max

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": long_title,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "255 characters or fewer" in str(data["errors"])

        # No CalendarEvent row must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        )

    def test_scoped_token_on_bundle_calendar_denied(self) -> None:
        """scheduleEvent with an owner-scoped token on a BUNDLE calendar returns a clean
        GraphQL error and creates NO CalendarEvent row.

        Provider-scoped tokens are limited to PERSONAL/RESOURCE calendars they own.
        A bundle calendar would fan-out to child calendars owned by other providers and
        fail confusingly; the explicit guard fires first.

        Asserts:
        - The mutation returns a GraphQL error.
        - No CalendarEvent row is created.
        """
        from calendar_integration.constants import CalendarType

        org = baker.make(Organization, name="Bundle Guard Org")
        owner = baker.make(User, email=f"bundle_owner_{uuid4().hex}@test.com")
        bundle_cal = baker.make(
            Calendar,
            organization=org,
            name="Bundle Cal",
            external_id=f"ext-bundle-{org.pk}-{owner.pk}",
            calendar_type=CalendarType.BUNDLE,
            accepts_public_scheduling=False,
        )
        baker.make(CalendarOwnership, calendar=bundle_cal, user=owner, organization=org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": bundle_cal.id,
                    "title": "Bundle Event",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

        # No CalendarEvent row must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=bundle_cal.id)
            .exists()
        ), "No CalendarEvent must be created on a bundle calendar via a scoped token"

    def test_out_of_org_attendee_rejected(self) -> None:
        """scheduleEvent with an attendances user_id that is NOT a member of the caller's
        org returns a clean GraphQL error and creates NO CalendarEvent row.

        This prevents a provider token from injecting an arbitrary out-of-org user as
        an event attendee.

        Asserts:
        - The mutation returns a GraphQL error.
        - No CalendarEvent row is created.
        """
        org = baker.make(Organization, name="Attendee Org-Check Org")
        owner, cal = self._make_owner_with_calendar(org)
        client, _ = _make_scoped_calendar_event_client(org, owner)

        # Create a user in a completely different org — not a member of `org`
        other_org = baker.make(Organization, name="Other Org")
        user_model = get_user_model()
        outsider = baker.make(user_model, email=f"outsider_{uuid4().hex}@other.com")
        baker.make(OrganizationMembership, user=outsider, organization=other_org, is_active=True)

        start = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.UTC)

        response = client.post(
            "/graphql/",
            data={
                "query": SCHEDULE_EVENT_MUTATION,
                "variables": {
                    "calendarId": cal.id,
                    "title": "Event with Outsider",
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "attendances": [{"userId": outsider.id}],
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "not active members" in str(data["errors"]).lower()

        # No CalendarEvent row must have been created
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk=cal.id)
            .exists()
        ), "No CalendarEvent must be created when an out-of-org attendee is supplied"
