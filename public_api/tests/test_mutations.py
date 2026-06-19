import datetime
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model

import pytest
from graphql import GraphQLError
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType, CalendarVisibility
from calendar_integration.models import AvailableTime, Calendar, ChildrenCalendarRelationship
from calendar_integration.services.calendar_service import CalendarService
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
        minted_user = SystemUser.original_manager.get(id=int(result["systemUserId"]))
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
        minted_user = SystemUser.original_manager.get(id=minted_id)
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


CREATE_AVAILABILITY_WINDOW_MUTATION = """
mutation CreateAvailabilityWindow($input: CreateAvailableTimeInput!) {
    createAvailabilityWindow(input: $input) {
        success
        errorMessage
        availableTime {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestCreateAvailabilityWindowMutation:
    """Tests for the createAvailabilityWindow mutation (Phase 3a)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.CREATE_AVAILABILITY_WINDOW]
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
                data={"query": CREATE_AVAILABILITY_WINDOW_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_create_availability_window_happy_path(self):
        """A granted token creates an available time on a managing calendar; DB row + availableTime returned."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar with manage_available_windows=True via the service
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        managing_calendar = calendar_service.create_resource_calendar(
            name="Availability Room",
            description="",
            manage_available_windows=True,
        )
        assert managing_calendar.calendar_type == CalendarType.RESOURCE
        assert managing_calendar.manage_available_windows is True

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["availableTime"] is not None
        available_time_id = int(result["availableTime"]["id"])

        # Verify startTime and endTime in the response match the supplied inputs (ISO-normalized)
        assert result["availableTime"]["startTime"] == start.isoformat()
        assert result["availableTime"]["endTime"] == end.isoformat()

        # Verify DB row was created and is org-scoped
        at = AvailableTime.objects.filter_by_organization(org.id).get(id=available_time_id)
        assert at.calendar_fk_id == managing_calendar.id

    def test_create_availability_window_non_managing_calendar_returns_failure(self):
        """A calendar with manage_available_windows=False → success=False with service message."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar without manage_available_windows flag
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        non_managing_calendar = calendar_service.create_resource_calendar(
            name="Non-Managing Room",
            description="",
            manage_available_windows=False,
        )
        assert non_managing_calendar.calendar_type == CalendarType.RESOURCE
        assert non_managing_calendar.manage_available_windows is False

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": non_managing_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert result["availableTime"] is None

    def test_create_availability_window_cross_org_calendar_rejected(self):
        """A calendar belonging to a different org → success=False 'Calendar not found'."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a managing calendar in a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "calendar not found" in result["errorMessage"].lower()

    def test_create_availability_window_permission_denied_without_grant(self):
        """A token without CREATE_AVAILABILITY_WINDOW grant is denied."""
        # Grant CALENDAR scope instead, NOT CREATE_AVAILABILITY_WINDOW
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_create_availability_window_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": CREATE_AVAILABILITY_WINDOW_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "startTime": "2026-09-01T09:00:00Z",
                        "endTime": "2026-09-01T17:00:00Z",
                        "timezone": "UTC",
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_create_availability_window_recurring_rrule_creates_recurrence_rule(self):
        """Supplying rruleString on a managing calendar creates an AvailableTime with a recurrence_rule."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a managing resource calendar via the service
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        managing_calendar = calendar_service.create_resource_calendar(
            name="Recurring Availability Room",
            description="",
            manage_available_windows=True,
        )
        assert managing_calendar.manage_available_windows is True

        start = datetime.datetime(2026, 9, 7, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 7, 17, 0, 0, tzinfo=datetime.UTC)
        rrule = "FREQ=WEEKLY;BYDAY=MO"

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "rruleString": rrule,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is True
        assert result["availableTime"] is not None
        available_time_id = int(result["availableTime"]["id"])

        # Verify the DB row has a non-null recurrence_rule
        at = AvailableTime.objects.filter_by_organization(org.id).get(id=available_time_id)
        assert at.recurrence_rule is not None, (
            "AvailableTime must have a non-null recurrence_rule when rruleString is supplied"
        )


UPDATE_AVAILABILITY_WINDOW_MUTATION = """
mutation UpdateAvailabilityWindow($input: UpdateAvailableTimeInput!) {
    updateAvailabilityWindow(input: $input) {
        success
        errorMessage
        availableTime {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestUpdateAvailabilityWindowMutation:
    """Tests for the updateAvailabilityWindow mutation (Phase 3b)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
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
                data={"query": UPDATE_AVAILABILITY_WINDOW_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _create_managing_calendar_and_available_time(self, org, system_user):
        """Create a managing resource calendar and an AvailableTime row via the service."""
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        managing_calendar = calendar_service.create_resource_calendar(
            name="Availability Room",
            description="",
            manage_available_windows=True,
        )
        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        available_time = calendar_service.create_available_time(
            calendar=managing_calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
        )
        return managing_calendar, available_time

    def test_update_availability_window_happy_path(self):
        """A granted token updates start/end of an existing AvailableTime; DB row reflects new times."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, available_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        new_start = datetime.datetime(2026, 10, 1, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 1, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "availableTimeId": available_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["availableTime"] is not None
        assert int(result["availableTime"]["id"]) == available_time.id

        # Verify startTime and endTime in the response match the supplied new datetimes
        assert result["availableTime"]["startTime"] == new_start.isoformat()
        assert result["availableTime"]["endTime"] == new_end.isoformat()

    def test_update_availability_window_missing_id_returns_failure(self):
        """A missing/cross-calendar available_time_id → success=False (service raises ValueError)."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, _available_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        non_existent_id = 999999

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "availableTimeId": non_existent_id,
                    "startTime": datetime.datetime(
                        2026, 10, 1, 8, 0, 0, tzinfo=datetime.UTC
                    ).isoformat(),
                    "endTime": datetime.datetime(
                        2026, 10, 1, 16, 0, 0, tzinfo=datetime.UTC
                    ).isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert result["availableTime"] is None

    def test_update_availability_window_cross_org_available_time_rejected(self):
        """An available_time_id belonging to a different org's calendar → success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create an AvailableTime in a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        # Create a calendar in our org to avoid triggering "Calendar not found"
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        our_calendar = calendar_service.create_resource_calendar(
            name="Our Room",
            description="",
            manage_available_windows=True,
        )

        # Create an available time in the OTHER org's calendar directly
        other_available_time = baker.make(
            "calendar_integration.AvailableTime",
            calendar=other_calendar,
            organization=other_org,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": our_calendar.id,
                    "availableTimeId": other_available_time.id,
                    "startTime": datetime.datetime(
                        2026, 10, 1, 8, 0, 0, tzinfo=datetime.UTC
                    ).isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert result["availableTime"] is None

    def test_update_availability_window_permission_denied_without_grant(self):
        """A token without UPDATE_AVAILABILITY_WINDOW grant is denied."""
        # Grant CALENDAR scope instead, NOT UPDATE_AVAILABILITY_WINDOW
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "availableTimeId": 1,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_availability_window_non_managing_calendar_returns_failure(self):
        """A calendar with manage_available_windows=False → success=False with service message."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar without manage_available_windows flag
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        non_managing_calendar = calendar_service.create_resource_calendar(
            name="Non-Managing Room",
            description="",
            manage_available_windows=False,
        )
        assert non_managing_calendar.manage_available_windows is False

        # Create an AvailableTime outside the service (bypass the flag check at create time)
        available_time = baker.make(
            "calendar_integration.AvailableTime",
            calendar=non_managing_calendar,
            organization=org,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        new_start = datetime.datetime(2026, 10, 1, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 1, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": non_managing_calendar.id,
                    "availableTimeId": available_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert result["availableTime"] is None

    def test_update_availability_window_rrule_creates_recurrence_rule(self):
        """Supplying rruleString on update persists a non-null recurrence_rule on the AvailableTime."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, available_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        rrule = "FREQ=WEEKLY;BYDAY=MO"

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "availableTimeId": available_time.id,
                    "rruleString": rrule,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is True
        assert result["availableTime"] is not None

        # Verify the DB row has a non-null recurrence_rule
        available_time.refresh_from_db()
        assert available_time.recurrence_rule is not None, (
            "AvailableTime must have a non-null recurrence_rule when rruleString is supplied on update"
        )

    def test_update_availability_window_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": UPDATE_AVAILABILITY_WINDOW_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "availableTimeId": 1,
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0


DELETE_AVAILABILITY_WINDOW_MUTATION = """
mutation DeleteAvailabilityWindow($input: DeleteAvailableTimeInput!) {
    deleteAvailabilityWindow(input: $input) {
        success
        errorMessage
    }
}
"""


@pytest.mark.django_db
class TestDeleteAvailabilityWindowMutation:
    """Tests for the deleteAvailabilityWindow mutation (Phase 3c)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
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
                data={"query": DELETE_AVAILABILITY_WINDOW_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _create_managing_calendar_and_available_time(self, org, system_user):
        """Create a managing resource calendar and an AvailableTime row via the service."""
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        managing_calendar = calendar_service.create_resource_calendar(
            name="Availability Room",
            description="",
            manage_available_windows=True,
        )
        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        available_time = calendar_service.create_available_time(
            calendar=managing_calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
        )
        return managing_calendar, available_time

    def test_delete_availability_window_happy_path(self):
        """A granted token deletes an existing AvailableTime; DB row is gone."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, available_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )
        available_time_id = available_time.id

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "availableTimeId": available_time_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # Verify the AvailableTime row no longer exists in the DB
        assert (
            not AvailableTime.objects.filter_by_organization(org.id)
            .filter(id=available_time_id)
            .exists()
        )

    def test_delete_availability_window_missing_id_returns_failure(self):
        """A missing/non-existent available_time_id → success=False (service raises ValueError)."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, _available_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        non_existent_id = 999999

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "availableTimeId": non_existent_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    def test_delete_availability_window_cross_org_available_time_rejected(self):
        """An available_time_id belonging to a different org's calendar → success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create an AvailableTime in a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        # Create a calendar in our org to avoid triggering "Calendar not found"
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        our_calendar = calendar_service.create_resource_calendar(
            name="Our Room",
            description="",
            manage_available_windows=True,
        )

        # Create an available time in the OTHER org's calendar directly
        other_available_time = baker.make(
            "calendar_integration.AvailableTime",
            calendar=other_calendar,
            organization=other_org,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": our_calendar.id,
                    "availableTimeId": other_available_time.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

        # Verify the other org's available time was NOT deleted
        assert (
            AvailableTime.objects.filter_by_organization(other_org.id)
            .filter(id=other_available_time.id)
            .exists()
        )

    def test_delete_availability_window_non_managing_calendar_returns_failure(self):
        """A calendar with manage_available_windows=False → success=False (service raises ValueError)."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar without manage_available_windows flag
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        non_managing_calendar = calendar_service.create_resource_calendar(
            name="Non-Managing Room",
            description="",
            manage_available_windows=False,
        )
        assert non_managing_calendar.manage_available_windows is False

        # Create an AvailableTime outside the service (bypass the flag check at create time)
        available_time = baker.make(
            "calendar_integration.AvailableTime",
            calendar=non_managing_calendar,
            organization=org,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": non_managing_calendar.id,
                    "availableTimeId": available_time.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    def test_delete_availability_window_cross_calendar_same_org_rejected(self):
        """An AvailableTime from calendar B cannot be deleted via calendar A (same org).

        The service validates that the available_time_id belongs to the calendar passed
        in the operation; a cross-calendar id in the same org raises ValueError → success=False.
        The AvailableTime row must remain intact in the DB.
        """
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create calendar_a via the service (managing); calendar_b via baker to avoid
        # the unique constraint on (external_id, provider, organization_id) that fires
        # when the service creates a second internal calendar for the same org.
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar_a = calendar_service.create_resource_calendar(
            name="Room A",
            description="",
            manage_available_windows=True,
        )
        calendar_b = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
            external_id="calendar-b-unique-ext-id",
        )

        # Create an AvailableTime that belongs to calendar_b
        available_time_b = baker.make(
            "calendar_integration.AvailableTime",
            calendar=calendar_b,
            organization=org,
            start_time_tz_unaware=datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        # Call delete with calendarId=calendar_a but availableTimeId=calendar_b's row
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_a.id,
                    "availableTimeId": available_time_b.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

        # The AvailableTime row must still exist in the DB — it must not have been deleted
        assert (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(id=available_time_b.id)
            .exists()
        )

    def test_delete_availability_window_permission_denied_without_grant(self):
        """A token without DELETE_AVAILABILITY_WINDOW grant is denied."""
        # Grant CALENDAR scope instead, NOT DELETE_AVAILABILITY_WINDOW
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "availableTimeId": 1,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_delete_availability_window_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": DELETE_AVAILABILITY_WINDOW_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "availableTimeId": 1,
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0


BATCH_UPDATE_AVAILABILITY_WINDOWS_MUTATION = """
mutation BatchUpdateAvailabilityWindows($input: BatchAvailabilityInput!) {
    batchUpdateAvailabilityWindows(input: $input) {
        success
        errorMessage
        availableTimes {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestBatchUpdateAvailabilityWindowsMutation:
    """Tests for the batchUpdateAvailabilityWindows mutation (Phase 3d)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
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
                data={"query": BATCH_UPDATE_AVAILABILITY_WINDOWS_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _create_managing_calendar_and_available_time(self, org, system_user):
        """Create a managing resource calendar and an AvailableTime row via the service."""
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        managing_calendar = calendar_service.create_resource_calendar(
            name="Batch Availability Room",
            description="",
            manage_available_windows=True,
        )
        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        available_time = calendar_service.create_available_time(
            calendar=managing_calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
        )
        return managing_calendar, available_time

    def test_batch_update_mixed_operations_happy_path(self):
        """A mixed batch (create + update + delete) applies atomically; DB state correct.

        Asserts:
        - The created row exists in the DB after the batch.
        - The updated row has the new start/end times.
        - The deleted row is gone from the DB.
        - The returned availableTimes list reflects the post-batch state.
        """
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, existing_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        # Create a second AvailableTime that we will delete via the batch.
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        time_to_delete = calendar_service.create_available_time(
            calendar=managing_calendar,
            start_time=datetime.datetime(2026, 9, 2, 10, 0, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 9, 2, 18, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        new_start = datetime.datetime(2026, 10, 5, 8, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 5, 16, 0, 0, tzinfo=datetime.UTC)
        create_start = datetime.datetime(2026, 11, 1, 9, 0, 0, tzinfo=datetime.UTC)
        create_end = datetime.datetime(2026, 11, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "operations": [
                        # Create a new AvailableTime
                        {
                            "action": "create",
                            "startTime": create_start.isoformat(),
                            "endTime": create_end.isoformat(),
                            "timezone": "UTC",
                        },
                        # Update the existing time
                        {
                            "action": "update",
                            "availableTimeId": existing_time.id,
                            "startTime": new_start.isoformat(),
                            "endTime": new_end.isoformat(),
                        },
                        # Delete the second time
                        {
                            "action": "delete",
                            "availableTimeId": time_to_delete.id,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # The returned list should contain the surviving rows (created + updated).
        assert len(result["availableTimes"]) == 2

        # Verify the updated row's times changed in the DB.
        existing_time.refresh_from_db()
        assert existing_time.start_time_tz_unaware == new_start
        assert existing_time.end_time_tz_unaware == new_end

        # Verify the deleted row is gone from the DB.
        assert (
            not AvailableTime.objects.filter_by_organization(org.id)
            .filter(id=time_to_delete.id)
            .exists()
        )

        # Verify a new row was created (2 total: the updated one + the newly created one).
        all_times = AvailableTime.objects.filter_by_organization(org.id).filter(
            calendar_fk=managing_calendar
        )
        assert all_times.count() == 2

    def test_batch_rollback_on_bad_id(self):
        """A batch with one bad id rolls the whole batch back; no partial writes survive.

        We send a batch with TWO operations:
        1. A create (new row that does NOT yet exist in the DB).
        2. A delete referencing a non-existent id (triggers ValueError inside the service).

        Because batch_modify_available_times is decorated with @transaction.atomic(),
        the ValueError causes a full rollback of the batch. Asserts:
        - The mutation returns success=False.
        - The create did NOT persist (the calendar has the same number of rows as before).
        """
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, _existing_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        # Count existing rows before the attempted batch.
        rows_before = (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=managing_calendar)
            .count()
        )

        non_existent_id = 999999
        create_start = datetime.datetime(2026, 12, 1, 9, 0, 0, tzinfo=datetime.UTC)
        create_end = datetime.datetime(2026, 12, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "operations": [
                        # This create would succeed on its own...
                        {
                            "action": "create",
                            "startTime": create_start.isoformat(),
                            "endTime": create_end.isoformat(),
                            "timezone": "UTC",
                        },
                        # ...but this delete references a non-existent id and triggers rollback.
                        {
                            "action": "delete",
                            "availableTimeId": non_existent_id,
                        },
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        # The mutation result must signal failure (GraphQL-level success=False, not a GraphQL error).
        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

        # CRITICAL: the create from the same batch must NOT have persisted.
        rows_after = (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=managing_calendar)
            .count()
        )
        assert rows_after == rows_before, (
            f"Partial write detected: DB row count changed from {rows_before} to {rows_after} "
            "even though the batch should have rolled back entirely."
        )

    def test_batch_invalid_action_returns_failure(self):
        """An operation with an action not in {create, update, delete} → success=False.

        The validation happens before the service is called, so no DB state changes.
        """
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, _existing_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "operations": [
                        {
                            "action": "upsert",  # Invalid action
                            "startTime": "2026-09-01T09:00:00+00:00",
                            "endTime": "2026-09-01T17:00:00+00:00",
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "invalid operation action" in result["errorMessage"].lower()
        assert "upsert" in result["errorMessage"]

    def test_batch_permission_denied_without_grant(self):
        """A token without BATCH_UPDATE_AVAILABILITY_WINDOWS grant is denied."""
        # Grant CALENDAR scope instead, NOT BATCH_UPDATE_AVAILABILITY_WINDOWS
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        managing_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "operations": [],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_batch_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": BATCH_UPDATE_AVAILABILITY_WINDOWS_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "operations": [],
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_batch_update_availability_windows_cross_org_calendar_rejected(self):
        """A calendarId owned by a different org → success=False (Calendar.DoesNotExist path)."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Calendar belongs to a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
        )

        create_start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        create_end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "operations": [
                        {
                            "action": "create",
                            "startTime": create_start.isoformat(),
                            "endTime": create_end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "calendar not found" in result["errorMessage"].lower()

    def test_batch_update_availability_windows_create_missing_fields_returns_failure(self):
        """A create op missing startTime → success=False with validation message, no DB write."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        managing_calendar, _existing_time = self._create_managing_calendar_and_available_time(
            org, system_user
        )

        rows_before = (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=managing_calendar)
            .count()
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": managing_calendar.id,
                    "operations": [
                        # Missing startTime — should trigger fail-fast validation
                        {
                            "action": "create",
                            "endTime": "2026-09-01T17:00:00+00:00",
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "create operation requires" in result["errorMessage"].lower()

        # No AvailableTime row was created (proves no partial write and no 500).
        rows_after = (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk=managing_calendar)
            .count()
        )
        assert rows_after == rows_before


CREATE_BLOCKED_TIME_MUTATION = """
mutation CreateBlockedTime($input: CreateBlockedTimeInput!) {
    createBlockedTime(input: $input) {
        success
        errorMessage
        blockedTime {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestCreateBlockedTimeMutation:
    """Tests for the createBlockedTime mutation (Phase 3e)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.CREATE_BLOCKED_TIME]
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
                data={"query": CREATE_BLOCKED_TIME_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_create_blocked_time_happy_path(self):
        """A granted token creates a blocked time on a calendar; DB row + blockedTime returned."""
        from calendar_integration.models import BlockedTime
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource calendar via the service
        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Blocked Room",
            description="",
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "reason": "Staff meeting",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createBlockedTime"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["blockedTime"] is not None
        blocked_time_id = int(result["blockedTime"]["id"])

        # Verify startTime and endTime in the response match the supplied inputs
        assert result["blockedTime"]["startTime"] == start.isoformat()
        assert result["blockedTime"]["endTime"] == end.isoformat()

        # Verify DB row was created and is org-scoped, with reason persisted
        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=blocked_time_id)
        assert bt.calendar_fk_id == calendar.id
        assert bt.reason == "Staff meeting"

    def test_create_blocked_time_recurring_rrule_creates_recurrence_rule(self):
        """Supplying rruleString creates a BlockedTime with a non-null recurrence_rule."""
        from calendar_integration.models import BlockedTime
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Recurring Block Room",
            description="",
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 9, 7, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 7, 17, 0, 0, tzinfo=datetime.UTC)
        rrule = "FREQ=WEEKLY;BYDAY=MO"

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "rruleString": rrule,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createBlockedTime"]
        assert result["success"] is True
        assert result["blockedTime"] is not None
        blocked_time_id = int(result["blockedTime"]["id"])

        # Verify the DB row has a non-null recurrence_rule
        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=blocked_time_id)
        assert bt.recurrence_rule is not None, (
            "BlockedTime must have a non-null recurrence_rule when rruleString is supplied"
        )

    def test_create_blocked_time_cross_org_calendar_rejected(self):
        """A calendar belonging to a different org → success=False 'Calendar not found'."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a calendar in a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createBlockedTime"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "calendar not found" in result["errorMessage"].lower()

    def test_create_blocked_time_permission_denied_without_grant(self):
        """A token without CREATE_BLOCKED_TIME grant is denied."""
        # Grant CALENDAR scope instead, NOT CREATE_BLOCKED_TIME
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_create_blocked_time_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": CREATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "startTime": "2026-09-01T09:00:00Z",
                        "endTime": "2026-09-01T17:00:00Z",
                        "timezone": "UTC",
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_create_blocked_time_duplicate_returns_failure(self):
        """A duplicate blocked time (same calendar + same external_id) returns success=False.

        bulk_create_manual_blocked_times generates external_id as
        "manual-{start_time.isoformat()}-{index}". Two calls with the same calendar and
        the same start_time both produce external_id "manual-...-0", triggering the
        unique_together (calendar_fk_id, external_id) DB constraint (IntegrityError).
        The handler must catch IntegrityError and return success=False rather than a 500.

        Approach: patch the service to raise IntegrityError directly (the real collision
        requires the same calendar + same start_time AND the same index, which is always
        0 for single-blocked-time calls — but ATOMIC_REQUESTS wraps each request in its
        own transaction, so two separate HTTP calls both commit cleanly and do NOT
        collide at the DB level because Django's bulk_create on a unique-violating row
        silently skips or raises only within the same savepoint). Patching is therefore
        the reliable approach.
        """
        from django.db import IntegrityError as DjangoIntegrityError

        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service_real: CalendarService = container.calendar_service()
        calendar_service_real.initialize_without_provider(
            user_or_token=system_user, organization=org
        )
        calendar = calendar_service_real.create_resource_calendar(
            name="Blocked Room Dup",
            description="",
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)

        mock_calendar_service = Mock(spec=CalendarService)
        mock_calendar_service.organization = org
        mock_calendar_service.user_or_token = system_user
        mock_calendar_service.create_blocked_time.side_effect = DjangoIntegrityError(
            "UNIQUE constraint failed: calendar_integration_blockedtime.calendar_fk_id, "
            "calendar_integration_blockedtime.external_id"
        )

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
                        "calendarId": calendar.id,
                        "startTime": start.isoformat(),
                        "endTime": end.isoformat(),
                        "timezone": "UTC",
                    }
                },
            )

        assert response.status_code == 200
        data = response.json()
        # Must NOT surface as a GraphQL-level error (no 500); must be a typed result.
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createBlockedTime"]
        assert result["success"] is False
        assert result["errorMessage"] is not None


UPDATE_BLOCKED_TIME_MUTATION = """
mutation UpdateBlockedTime($input: UpdateBlockedTimeInput!) {
    updateBlockedTime(input: $input) {
        success
        errorMessage
        blockedTime {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestUpdateBlockedTimeMutation:
    """Tests for the updateBlockedTime mutation (Phase 3f)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.UPDATE_BLOCKED_TIME]
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
                data={"query": UPDATE_BLOCKED_TIME_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_update_blocked_time_happy_path(self):
        """A granted token updates a blocked time; DB reflects new values + payload returned."""
        from calendar_integration.models import BlockedTime
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Update Room",
            description="",
            manage_available_windows=True,
        )

        # Create a blocked time via the service
        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        blocked_time = calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
            reason="Original reason",
        )

        new_start = datetime.datetime(2026, 9, 2, 10, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 9, 2, 18, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "reason": "Updated reason",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateBlockedTime"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["blockedTime"] is not None
        returned_id = int(result["blockedTime"]["id"])

        # Verify DB row reflects the updated values
        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=returned_id)
        assert bt.reason == "Updated reason"
        assert bt.start_time_tz_unaware == new_start
        assert bt.end_time_tz_unaware == new_end

    def test_update_blocked_time_missing_id_returns_failure(self):
        """A non-existent blocked_time_id returns success=False."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Missing BT Room",
            description="",
            manage_available_windows=True,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": 999999,
                    "reason": "Should fail",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateBlockedTime"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "not found" in result["errorMessage"].lower()

    def test_update_blocked_time_cross_org_calendar_rejected(self):
        """A calendar belonging to a different org → success=False 'Calendar not found'."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "blockedTimeId": 1,
                    "reason": "Cross org attempt",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateBlockedTime"]
        assert result["success"] is False
        assert "calendar not found" in result["errorMessage"].lower()

    def test_update_blocked_time_permission_denied_without_grant(self):
        """A token without UPDATE_BLOCKED_TIME grant is denied."""
        # Grant CALENDAR scope instead, NOT UPDATE_BLOCKED_TIME
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "blockedTimeId": 1,
                    "reason": "Unauthorized",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_blocked_time_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": UPDATE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "blockedTimeId": 1,
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_update_blocked_time_rrule_creates_recurrence_rule(self):
        """Supplying rruleString via the mutation attaches a RecurrenceRule to the DB row."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Rrule Room",
            description="",
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 10, 6, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 6, 17, 0, 0, tzinfo=datetime.UTC)
        blocked_time = calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
            reason="No recurrence initially",
        )
        assert blocked_time.recurrence_rule is None

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time.id,
                    "rruleString": "FREQ=WEEKLY;BYDAY=MO",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateBlockedTime"]
        assert result["success"] is True

        blocked_time.refresh_from_db()
        assert blocked_time.recurrence_rule is not None


DELETE_BLOCKED_TIME_MUTATION = """
mutation DeleteBlockedTime($input: DeleteBlockedTimeInput!) {
    deleteBlockedTime(input: $input) {
        success
        errorMessage
    }
}
"""


@pytest.mark.django_db
class TestDeleteBlockedTimeMutation:
    """Tests for the deleteBlockedTime mutation (Phase 3g)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.DELETE_BLOCKED_TIME]
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
                data={"query": DELETE_BLOCKED_TIME_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_delete_blocked_time_happy_path(self):
        """A granted token deletes a blocked time; DB row is gone after deletion."""
        from calendar_integration.models import BlockedTime
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Delete Room",
            description="",
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        blocked_time = calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
            reason="To be deleted",
        )
        blocked_time_id = blocked_time.id

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["deleteBlockedTime"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # Verify row is gone from DB
        assert not BlockedTime.objects.filter(id=blocked_time_id).exists()

    def test_delete_blocked_time_missing_id_returns_failure(self):
        """A non-existent blocked_time_id returns success=False."""
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Missing BT Room",
            description="",
            manage_available_windows=True,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": 999999,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["deleteBlockedTime"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "not found" in result["errorMessage"].lower()

    def test_delete_blocked_time_cross_org_calendar_rejected(self):
        """A calendar belonging to a different org → success=False 'Calendar not found'."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=other_org,
            calendar_type=CalendarType.RESOURCE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "blockedTimeId": 1,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["deleteBlockedTime"]
        assert result["success"] is False
        assert "calendar not found" in result["errorMessage"].lower()

    def test_delete_blocked_time_permission_denied_without_grant(self):
        """A token without DELETE_BLOCKED_TIME grant is denied."""
        # Grant CALENDAR scope instead, NOT DELETE_BLOCKED_TIME
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR]
        )

        other_calendar = baker.make(
            "calendar_integration.Calendar",
            organization=org,
            calendar_type=CalendarType.RESOURCE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": other_calendar.id,
                    "blockedTimeId": 1,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_delete_blocked_time_recurring_removes_row(self):
        """A recurring blocked time (with rrule_string) is fully removed from the DB."""
        from calendar_integration.models import BlockedTime
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        calendar = calendar_service.create_resource_calendar(
            name="Recurring Delete Room",
            description="",
            manage_available_windows=True,
        )

        start = datetime.datetime(2026, 10, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 1, 17, 0, 0, tzinfo=datetime.UTC)
        blocked_time = calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
            reason="Recurring block",
            rrule_string="FREQ=WEEKLY;BYDAY=TH",
        )
        blocked_time_id = blocked_time.id

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["deleteBlockedTime"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # The row must be gone from the DB
        assert not BlockedTime.objects.filter(id=blocked_time_id).exists()

    def test_delete_blocked_time_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": DELETE_BLOCKED_TIME_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "calendarId": 1,
                        "blockedTimeId": 1,
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0


CREATE_CALENDAR_BUNDLE_MUTATION = """
mutation CreateCalendarBundle($input: CreateCalendarBundleInput!) {
    createCalendarBundle(input: $input) {
        success
        errorMessage
        bundle {
            id
            name
            description
            children {
                id
                name
            }
        }
    }
}
"""


@pytest.mark.django_db
class TestCreateCalendarBundleMutation:
    """Tests for the createCalendarBundle mutation (Phase 4b)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.CREATE_CALENDAR_BUNDLE]
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
                data={"query": CREATE_CALENDAR_BUNDLE_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _make_child_calendar(self, org, name="Child Calendar"):
        """Create an org-scoped resource calendar suitable as a bundle child."""
        return baker.make(
            Calendar,
            organization=org,
            name=name,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

    def test_create_calendar_bundle_happy_path(self):
        """A granted token creates a bundle with two children; result has calendar_type=BUNDLE."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        child1 = self._make_child_calendar(org, name="Child A")
        child2 = self._make_child_calendar(org, name="Child B")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "My Bundle",
                    "description": "Bundle desc",
                    "childrenIds": [child1.id, child2.id],
                    "primaryCalendarId": child1.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["bundle"] is not None
        assert result["bundle"]["name"] == "My Bundle"
        assert result["bundle"]["description"] == "Bundle desc"

        # Returned children include both child calendars
        returned_child_ids = {c["id"] for c in result["bundle"]["children"]}
        assert returned_child_ids == {str(child1.id), str(child2.id)}

        # The DB row must be a BUNDLE type, scoped to the org
        bundle_id = int(result["bundle"]["id"])
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.calendar_type == CalendarType.BUNDLE
        assert bundle_cal.organization == org

        # ChildrenCalendarRelationship rows exist and primary is set correctly
        rels = ChildrenCalendarRelationship.objects.filter_by_organization(org.id).filter(
            bundle_calendar_fk=bundle_cal
        )
        rel_ids = {r.child_calendar_fk_id for r in rels}
        assert rel_ids == {child1.id, child2.id}
        primary_rel = rels.get(child_calendar_fk_id=child1.id)
        assert primary_rel.is_primary is True

    def test_create_calendar_bundle_without_primary(self):
        """Bundle creation without a primary calendar succeeds; no child is marked primary."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        child1 = self._make_child_calendar(org, name="Child X")
        child2 = self._make_child_calendar(org, name="Child Y")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Bundle No Primary",
                    "childrenIds": [child1.id, child2.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        bundle_id = int(result["bundle"]["id"])

        # No child should be marked as primary
        primary_rels = ChildrenCalendarRelationship.objects.filter_by_organization(org.id).filter(
            bundle_calendar_fk=bundle_id, is_primary=True
        )
        assert primary_rels.count() == 0

    def test_create_calendar_bundle_cross_org_child_rejected(self):
        """A child id belonging to a different org → success=False (not created)."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Child calendar in a different organization
        other_org = baker.make(Organization, name="Other Org")
        cross_org_child = self._make_child_calendar(other_org, name="Cross-Org Child")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Bad Bundle",
                    "childrenIds": [cross_org_child.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "not found" in result["errorMessage"].lower()

        # No bundle was created in the org
        assert (
            not Calendar.objects.filter_by_organization(org.id)
            .filter(calendar_type=CalendarType.BUNDLE)
            .exists()
        )

    def test_create_calendar_bundle_primary_not_among_children_rejected(self):
        """primary_calendar_id not in children_ids → success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        child1 = self._make_child_calendar(org, name="Valid Child")
        # A different calendar that will be used as primary but NOT in children_ids
        unrelated = baker.make(
            Calendar,
            organization=org,
            name="Unrelated Calendar",
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Bad Primary Bundle",
                    "childrenIds": [child1.id],
                    "primaryCalendarId": unrelated.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert (
            "primary_calendar_id" in result["errorMessage"] or "children" in result["errorMessage"]
        )

    def test_create_calendar_bundle_permission_denied_without_grant(self):
        """A token without CREATE_CALENDAR_BUNDLE grant is denied."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR_BUNDLE]
        )
        child = self._make_child_calendar(org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Should Not Exist",
                    "childrenIds": [child.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_create_calendar_bundle_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": CREATE_CALENDAR_BUNDLE_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "name": "Should Fail",
                        "childrenIds": [],
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_create_calendar_bundle_duplicate_children_ids_deduped(self):
        """Duplicate entries in childrenIds are deduplicated; the bundle has exactly one child.

        Proves the dict.fromkeys dedup in the mutation: passing [child1.id, child1.id] must
        produce a bundle with one ChildrenCalendarRelationship row, not two.
        """
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Deduplicated Child")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Dedup Bundle",
                    "childrenIds": [child1.id, child1.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        assert result["bundle"] is not None

        # The returned children list must have exactly ONE entry (the duplicate was removed).
        assert len(result["bundle"]["children"]) == 1

        # Confirm at the DB level: exactly one ChildrenCalendarRelationship for this bundle.
        bundle_id = int(result["bundle"]["id"])
        rel_count = (
            ChildrenCalendarRelationship.objects.filter_by_organization(org.id)
            .filter(bundle_calendar_fk_id=bundle_id)
            .count()
        )
        assert rel_count == 1

    def test_create_calendar_bundle_none_description_normalized_to_empty_string(self):
        """Omitting description (None) normalizes to '' in the persisted bundle.

        Proves the None -> '' normalization: when description is not supplied the
        bundle's description field is an empty string, not null.
        """
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Desc Child")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Bundle No Desc",
                    # description deliberately omitted
                    "childrenIds": [child1.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        # The GraphQL response must return "" (not null) for description when omitted.
        assert result["bundle"]["description"] == ""

        # Confirm in the DB: description column is empty string, not null.
        bundle_id = int(result["bundle"]["id"])
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.description == ""

    def test_create_calendar_bundle_round_trip_appears_in_calendar_bundles_query(self):
        """Round-trip acceptance: a bundle created via mutation appears in calendarBundles query.

        Uses the same token (CREATE_CALENDAR_BUNDLE + CALENDAR_BUNDLE) to:
        1. Create the bundle via the mutation.
        2. Query calendarBundles with the same token.
        3. Assert the new bundle id is present in the query result.
        """
        import json

        from di_core.containers import container

        org = baker.make(Organization, name="Round-Trip Org")
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="round_trip_integration", organization=org
        )
        # Grant both the mutation resource and the query resource.
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name=PublicAPIResources.CREATE_CALENDAR_BUNDLE,
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name=PublicAPIResources.CALENDAR_BUNDLE,
        )

        child1 = self._make_child_calendar(org, name="Round-Trip Child")

        with container.public_api_auth_service.override(auth_service):
            # Step 1: create the bundle
            create_response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_CALENDAR_BUNDLE_MUTATION,
                    "variables": {
                        "input": {
                            "organizationId": org.id,
                            "name": "Round-Trip Bundle",
                            "childrenIds": [child1.id],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert create_response.status_code == 200
        create_data = create_response.json()
        assert "errors" not in create_data or len(create_data.get("errors", [])) == 0
        new_bundle_id = create_data["data"]["createCalendarBundle"]["bundle"]["id"]

        _calendar_bundles_query = """
            query GetCalendarBundles {
                calendarBundles {
                    id
                    name
                }
            }
        """

        with container.public_api_auth_service.override(auth_service):
            # Step 2: query calendarBundles with the same token
            query_response = self.client.post(
                "/graphql/",
                data=json.dumps({"query": _calendar_bundles_query}),
                content_type="application/json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert query_response.status_code == 200
        query_data = query_response.json()
        assert "errors" not in query_data or len(query_data.get("errors", [])) == 0

        # Step 3: assert the new bundle id appears in the result
        returned_bundle_ids = {b["id"] for b in query_data["data"]["calendarBundles"]}
        assert new_bundle_id in returned_bundle_ids, (
            f"Newly created bundle {new_bundle_id} was not returned by calendarBundles query. "
            f"Returned ids: {returned_bundle_ids}"
        )

    def test_create_calendar_bundle_defaults_is_private_true(self):
        """Test that is_private defaults to True (private) when omitted."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Child A")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Default Private Bundle",
                    "childrenIds": [child1.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        bundle_id = int(result["bundle"]["id"])

        # Verify the backing Calendar.accepts_public_scheduling is False (private)
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.accepts_public_scheduling is False

    def test_create_calendar_bundle_with_is_private_false(self):
        """Test that is_private=False sets accepts_public_scheduling=True."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Child A")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Public Bundle",
                    "childrenIds": [child1.id],
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        bundle_id = int(result["bundle"]["id"])

        # Verify the backing Calendar.accepts_public_scheduling is True (public)
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.accepts_public_scheduling is True

    def test_create_calendar_bundle_with_is_private_true(self):
        """Test that is_private=True sets accepts_public_scheduling=False."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Child A")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "name": "Explicit Private Bundle",
                    "childrenIds": [child1.id],
                    "isPrivate": True,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBundle"]
        assert result["success"] is True
        bundle_id = int(result["bundle"]["id"])

        # Verify the backing Calendar.accepts_public_scheduling is False (private)
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.accepts_public_scheduling is False


UPDATE_CALENDAR_BUNDLE_MUTATION = """
mutation UpdateCalendarBundle($input: UpdateCalendarBundleInput!) {
    updateCalendarBundle(input: $input) {
        success
        errorMessage
        bundle {
            id
            name
            description
            children {
                id
                name
            }
        }
    }
}
"""


@pytest.mark.django_db
class TestUpdateCalendarBundleMutation:
    """Tests for the updateCalendarBundle mutation (Phase 4c)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.UPDATE_CALENDAR_BUNDLE]
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
                data={"query": UPDATE_CALENDAR_BUNDLE_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _make_child_calendar(self, org, name="Child Calendar"):
        """Create an org-scoped calendar suitable as a bundle child."""
        return baker.make(
            Calendar,
            organization=org,
            name=name,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

    def _create_bundle_via_mutation(
        self,
        org,
        system_user,
        token,
        auth_service,
        child_ids,
        name="Initial Bundle",
        primary_id=None,
    ):
        """Helper: create a bundle through the createCalendarBundle mutation."""
        from di_core.containers import container

        variables: dict = {
            "input": {
                "organizationId": org.id,
                "name": name,
                "description": "Initial description",
                "childrenIds": child_ids,
            }
        }
        if primary_id is not None:
            variables["input"]["primaryCalendarId"] = primary_id

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={"query": CREATE_CALENDAR_BUNDLE_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )
        data = response.json()
        assert data["data"]["createCalendarBundle"]["success"] is True
        return int(data["data"]["createCalendarBundle"]["bundle"]["id"])

    def test_update_calendar_bundle_happy_path(self):
        """Update children set (add/remove) and rename; asserts ChildrenCalendarRelationship reconciled."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        child1 = self._make_child_calendar(org, name="Child A")
        child2 = self._make_child_calendar(org, name="Child B")
        child3 = self._make_child_calendar(org, name="Child C (new)")

        # Create bundle with child1 + child2
        bundle_id = self._create_bundle_via_mutation(
            org, system_user, token, auth_service, [child1.id, child2.id]
        )

        # Update: replace children with child2 + child3; rename the bundle
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle_id,
                    "name": "Updated Bundle Name",
                    "description": "Updated description",
                    "childrenIds": [child2.id, child3.id],
                    "primaryCalendarId": child3.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["bundle"] is not None
        assert result["bundle"]["name"] == "Updated Bundle Name"
        assert result["bundle"]["description"] == "Updated description"

        # Children should now be child2 + child3 (child1 removed)
        returned_child_ids = {c["id"] for c in result["bundle"]["children"]}
        assert returned_child_ids == {str(child2.id), str(child3.id)}
        assert str(child1.id) not in returned_child_ids

        # Verify DB: name/description updated
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.name == "Updated Bundle Name"
        assert bundle_cal.description == "Updated description"

        # Verify DB: ChildrenCalendarRelationship rows reconciled
        rels = ChildrenCalendarRelationship.objects.filter_by_organization(org.id).filter(
            bundle_calendar_fk=bundle_cal
        )
        rel_child_ids = {r.child_calendar_fk_id for r in rels}
        assert rel_child_ids == {child2.id, child3.id}
        assert child1.id not in rel_child_ids

        # Verify DB: primary is child3
        primary_rel = rels.get(child_calendar_fk_id=child3.id)
        assert primary_rel.is_primary is True

    def test_update_calendar_bundle_missing_bundle_id_returns_not_found(self):
        """A bundle_id that does not exist in the org → success=False, 'Bundle not found'."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child = self._make_child_calendar(org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": 999999,
                    "childrenIds": [child.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "bundle not found" in result["errorMessage"].lower()

    def test_update_calendar_bundle_cross_org_bundle_id_returns_not_found(self):
        """A bundle_id from a different org → success=False ('Bundle not found')."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        # Create a bundle in a different org
        other_org = baker.make(Organization, name="Other Org")
        other_auth_service = PublicAPIAuthService()
        other_system_user, other_token = other_auth_service.create_system_user(
            integration_name="other_integration", organization=other_org
        )
        baker.make(
            ResourceAccess,
            system_user=other_system_user,
            resource_name=PublicAPIResources.CREATE_CALENDAR_BUNDLE,
        )
        other_child = self._make_child_calendar(other_org, name="Other Child")
        from di_core.containers import container

        with container.public_api_auth_service.override(other_auth_service):
            other_response = self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_CALENDAR_BUNDLE_MUTATION,
                    "variables": {
                        "input": {
                            "organizationId": other_org.id,
                            "name": "Other Bundle",
                            "childrenIds": [other_child.id],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {other_system_user.id}:{other_token}"},
            )
        other_data = other_response.json()
        assert other_data["data"]["createCalendarBundle"]["success"] is True
        other_bundle_id = int(other_data["data"]["createCalendarBundle"]["bundle"]["id"])

        child = self._make_child_calendar(org)

        # Attempt to update the other org's bundle using our org's token
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": other_bundle_id,
                    "childrenIds": [child.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False
        assert "bundle not found" in result["errorMessage"].lower()

    def test_update_calendar_bundle_cross_org_child_rejected(self):
        """A child_id from a different org → success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        child1 = self._make_child_calendar(org, name="Child A")
        bundle_id = self._create_bundle_via_mutation(
            org, system_user, token, auth_service, [child1.id]
        )

        # A child from another org
        other_org = baker.make(Organization, name="Other Org")
        cross_org_child = self._make_child_calendar(other_org, name="Cross-Org Child")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle_id,
                    "childrenIds": [cross_org_child.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "not found" in result["errorMessage"].lower()

    def test_update_calendar_bundle_primary_not_in_children_rejected(self):
        """primary_calendar_id not in children_ids → success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        child1 = self._make_child_calendar(org, name="Valid Child")
        unrelated = self._make_child_calendar(org, name="Unrelated Calendar")
        bundle_id = self._create_bundle_via_mutation(
            org, system_user, token, auth_service, [child1.id]
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle_id,
                    "childrenIds": [child1.id],
                    "primaryCalendarId": unrelated.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert (
            "primary_calendar_id" in result["errorMessage"] or "children" in result["errorMessage"]
        )

    def test_update_calendar_bundle_none_name_leaves_name_unchanged(self):
        """name=None in input leaves the bundle's name unchanged in the DB."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        child1 = self._make_child_calendar(org, name="Child A")
        child2 = self._make_child_calendar(org, name="Child B")
        bundle_id = self._create_bundle_via_mutation(
            org, system_user, token, auth_service, [child1.id], name="Original Name"
        )

        # Update with name=None (not provided) → should preserve "Original Name"
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle_id,
                    "childrenIds": [child1.id, child2.id],
                    # name intentionally omitted → None
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is True

        # DB should still have the original name
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_cal.name == "Original Name"

    def test_update_calendar_bundle_none_description_leaves_description_unchanged(self):
        """description=None in input leaves the bundle's description unchanged."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        child1 = self._make_child_calendar(org, name="Child A")
        bundle_id = self._create_bundle_via_mutation(
            org, system_user, token, auth_service, [child1.id], name="Bundle With Desc"
        )

        # Confirm initial description
        bundle_cal = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        original_description = bundle_cal.description

        child2 = self._make_child_calendar(org, name="Child B")

        # Update with description=None (not provided) → should preserve original description
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle_id,
                    "childrenIds": [child1.id, child2.id],
                    # description intentionally omitted → None
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is True

        # DB should still have the original description
        bundle_cal.refresh_from_db()
        assert bundle_cal.description == original_description

    def test_update_calendar_bundle_permission_denied_without_grant(self):
        """A token without UPDATE_CALENDAR_BUNDLE grant is denied."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CREATE_CALENDAR_BUNDLE]
        )
        child = self._make_child_calendar(org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": 1,
                    "childrenIds": [child.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_calendar_bundle_unauthenticated_denied(self):
        """An unauthenticated call (no Authorization header) is denied."""
        response = self.client.post(
            "/graphql/",
            data={
                "query": UPDATE_CALENDAR_BUNDLE_MUTATION,
                "variables": {
                    "input": {
                        "organizationId": 1,
                        "bundleId": 1,
                        "childrenIds": [],
                    }
                },
            },
            format="json",
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0

    def test_update_calendar_bundle_non_bundle_id_returns_not_found(self):
        """Passing a RESOURCE calendar's id as bundleId → success=False 'Bundle not found'.

        The resolver filters by calendar_type=BUNDLE; a RESOURCE calendar in the same org
        must not be treated as a bundle — it should return the friendly not-found message.
        """
        from di_core.containers import container

        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a RESOURCE calendar (not a bundle) in the org
        resource_calendar = baker.make(
            Calendar,
            organization=org,
            name="Not A Bundle",
            calendar_type=CalendarType.RESOURCE,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )
        child = self._make_child_calendar(org)

        # Pass the RESOURCE calendar's id as bundleId
        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": UPDATE_CALENDAR_BUNDLE_MUTATION,
                    "variables": {
                        "input": {
                            "organizationId": org.id,
                            "bundleId": resource_calendar.id,
                            "childrenIds": [child.id],
                        }
                    },
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert "bundle not found" in result["errorMessage"].lower()

    def test_update_calendar_bundle_child_is_bundle_rejected(self):
        """Passing a BUNDLE calendar id inside childrenIds → success=False (service ValueError).

        The service rejects bundle-as-child to prevent nested bundles. The resolver must
        surface this as success=False, not as an unhandled exception.
        """
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        outer_child = self._make_child_calendar(org, name="Outer Child")

        # Create the bundle-under-test via the mutation (one bundle, no conflict)
        outer_bundle_id = self._create_bundle_via_mutation(
            org, system_user, token, auth_service, [outer_child.id], name="Outer Bundle"
        )

        # Create a second BUNDLE calendar directly in the DB (avoids the unique constraint
        # conflict that arises when two bundles share external_id="" in the same org).
        nested_bundle = baker.make(
            Calendar,
            organization=org,
            name="Inner Bundle",
            calendar_type=CalendarType.BUNDLE,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

        # Try to update outer bundle to include the inner bundle as a child (invalid)
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": outer_bundle_id,
                    "childrenIds": [nested_bundle.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    def test_update_calendar_bundle_rolls_back_name_on_service_failure(self):
        """bundle.save(name) is rolled back when the service raises ValueError.

        This test proves the transaction.atomic() block works: the name/description
        are saved to the DB inside the block, then the service raises (child-is-bundle),
        and the entire block — including the name change — must be rolled back.
        """
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.CREATE_CALENDAR_BUNDLE,
                PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
            ]
        )

        outer_child = self._make_child_calendar(org, name="Outer Child")

        # Create the bundle-under-test via the mutation with a known name/description
        bundle_id = self._create_bundle_via_mutation(
            org,
            system_user,
            token,
            auth_service,
            [outer_child.id],
            name="Original Name",
        )

        # Create a second BUNDLE calendar directly in the DB to use as an invalid child
        # (avoids the unique constraint conflict for external_id="" in the same org).
        nested_bundle = baker.make(
            Calendar,
            organization=org,
            name="Inner Bundle",
            calendar_type=CalendarType.BUNDLE,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

        # Capture current DB state before the failing update
        bundle_before = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        original_name = bundle_before.name
        original_description = bundle_before.description

        # Attempt to rename AND use a bundle-as-child (triggers service ValueError)
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle_id,
                    "name": "Should Not Persist",
                    "description": "Should not persist either",
                    "childrenIds": [nested_bundle.id],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is False

        # The name and description must be UNCHANGED — the atomic block was rolled back
        bundle_after = Calendar.objects.filter_by_organization(org.id).get(id=bundle_id)
        assert bundle_after.name == original_name, (
            f"Name was persisted despite service failure: got '{bundle_after.name}', "
            f"expected '{original_name}'"
        )
        assert bundle_after.description == original_description, (
            f"Description was persisted despite service failure: got '{bundle_after.description}', "
            f"expected '{original_description}'"
        )

    def test_update_calendar_bundle_toggle_is_private_true_to_false(self):
        """Test toggling is_private from True (private) to False (public)."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Child A")

        # Create a private bundle
        bundle = baker.make(
            Calendar,
            organization=org,
            name="Toggle Bundle",
            calendar_type=CalendarType.BUNDLE,
            provider="internal",
            external_id=str(uuid.uuid4()),
            accepts_public_scheduling=False,
        )
        baker.make(
            ChildrenCalendarRelationship,
            bundle_calendar=bundle,
            child_calendar=child1,
            organization=org,
            is_primary=True,
        )

        # Update to is_private=False (public)
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle.id,
                    "childrenIds": [child1.id],
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is True

        bundle.refresh_from_db()
        # is_private=False means accepts_public_scheduling=True
        assert bundle.accepts_public_scheduling is True

    def test_update_calendar_bundle_toggle_is_private_false_to_true(self):
        """Test toggling is_private from False (public) to True (private)."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Child A")

        # Create a public bundle
        bundle = baker.make(
            Calendar,
            organization=org,
            name="Toggle Bundle",
            calendar_type=CalendarType.BUNDLE,
            provider="internal",
            external_id=str(uuid.uuid4()),
            accepts_public_scheduling=True,
        )
        baker.make(
            ChildrenCalendarRelationship,
            bundle_calendar=bundle,
            child_calendar=child1,
            organization=org,
            is_primary=True,
        )

        # Update to is_private=True (private)
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle.id,
                    "childrenIds": [child1.id],
                    "isPrivate": True,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is True

        bundle.refresh_from_db()
        # is_private=True means accepts_public_scheduling=False
        assert bundle.accepts_public_scheduling is False

    def test_update_calendar_bundle_omitting_is_private_leaves_unchanged(self):
        """Test that omitting is_private (None) leaves accepts_public_scheduling unchanged."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        child1 = self._make_child_calendar(org, name="Child A")

        # Create a public bundle
        bundle = baker.make(
            Calendar,
            organization=org,
            name="Stable Bundle",
            calendar_type=CalendarType.BUNDLE,
            provider="internal",
            external_id=str(uuid.uuid4()),
            accepts_public_scheduling=True,
        )
        baker.make(
            ChildrenCalendarRelationship,
            bundle_calendar=bundle,
            child_calendar=child1,
            organization=org,
            is_primary=True,
        )

        # Update name and other fields but omit is_private
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "bundleId": bundle.id,
                    "name": "Stable Bundle Renamed",
                    "description": "Updated description",
                    "childrenIds": [child1.id],
                    # is_private is None (omitted)
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["updateCalendarBundle"]
        assert result["success"] is True

        bundle.refresh_from_db()
        # accepts_public_scheduling should remain True (unchanged)
        assert bundle.accepts_public_scheduling is True
        assert bundle.name == "Stable Bundle Renamed"
        assert bundle.description == "Updated description"


DISABLE_CALENDAR_BUNDLE_MUTATION = """
mutation DisableCalendarBundle($input: DisableCalendarBundleInput!) {
    disableCalendarBundle(input: $input) {
        success
        errorMessage
    }
}
"""

_CALENDAR_BUNDLES_QUERY = """
    query GetCalendarBundles($offset: Int, $limit: Int) {
        calendarBundles(offset: $offset, limit: $limit) {
            id
            name
        }
    }
"""


@pytest.mark.django_db
class TestDisableCalendarBundleMutation:
    """Tests for the disableCalendarBundle mutation (Phase 4d)."""

    def setup_method(self):
        self.client = APIClient()

    def _setup_org_and_token(self, resources: list[str] | None = None):
        """Create an org + system user with the given resource scopes."""
        if resources is None:
            resources = [PublicAPIResources.DISABLE_CALENDAR_BUNDLE]
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
                data={"query": DISABLE_CALENDAR_BUNDLE_MUTATION, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _make_bundle(self, org, name="Test Bundle"):
        """Create a BUNDLE Calendar directly in the DB."""
        return baker.make(
            Calendar,
            organization=org,
            name=name,
            calendar_type=CalendarType.BUNDLE,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

    def test_disable_calendar_bundle_happy_path(self):
        """A granted token disables a bundle calendar; visibility is set to INACTIVE in DB."""
        org, system_user, token, auth_service = self._setup_org_and_token()
        bundle = self._make_bundle(org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "bundleId": bundle.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["disableCalendarBundle"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # Verify the bundle is now INACTIVE in the DB
        bundle.refresh_from_db()
        assert bundle.visibility == CalendarVisibility.INACTIVE

    def test_disable_calendar_bundle_drops_from_calendar_bundles_query(self):
        """After disabling, the bundle no longer appears in the calendarBundles query."""
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[
                PublicAPIResources.DISABLE_CALENDAR_BUNDLE,
                PublicAPIResources.CALENDAR_BUNDLE,
            ]
        )
        bundle = self._make_bundle(org, name="Bundle To Disable")

        # Confirm the bundle is listed before disabling
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            before_response = self.client.post(
                "/graphql/",
                data={"query": _CALENDAR_BUNDLES_QUERY},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )
        before_data = before_response.json()
        before_bundles = before_data["data"]["calendarBundles"]
        assert any(b["id"] == str(bundle.id) for b in before_bundles)

        # Disable the bundle
        disable_response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "bundleId": bundle.id}},
        )
        disable_data = disable_response.json()
        assert "errors" not in disable_data or len(disable_data.get("errors", [])) == 0
        assert disable_data["data"]["disableCalendarBundle"]["success"] is True

        # Verify the bundle no longer appears in the listing
        with container.public_api_auth_service.override(auth_service):
            after_response = self.client.post(
                "/graphql/",
                data={"query": _CALENDAR_BUNDLES_QUERY},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )
        after_data = after_response.json()
        after_bundles = after_data["data"]["calendarBundles"]
        assert not any(b["id"] == str(bundle.id) for b in after_bundles)

    def test_disable_calendar_bundle_rejects_non_bundle_calendar(self):
        """Attempting to disable a non-bundle calendar returns success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a resource (non-bundle) calendar
        resource_cal = baker.make(
            Calendar,
            organization=org,
            calendar_type=CalendarType.RESOURCE,
            external_id=str(uuid.uuid4()),
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "bundleId": resource_cal.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["disableCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

    def test_disable_calendar_bundle_cross_org_rejected(self):
        """A bundle belonging to a different org returns success=False."""
        org, system_user, token, auth_service = self._setup_org_and_token()

        # Create a bundle in a DIFFERENT org
        other_org = baker.make(Organization, name="Other Org")
        other_bundle = self._make_bundle(other_org, name="Other Bundle")

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "bundleId": other_bundle.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["disableCalendarBundle"]
        assert result["success"] is False
        assert result["errorMessage"] is not None

        # Verify the other org's bundle was NOT modified
        other_bundle.refresh_from_db()
        assert other_bundle.visibility != CalendarVisibility.INACTIVE

    def test_disable_calendar_bundle_permission_denied_without_grant(self):
        """A token without DISABLE_CALENDAR_BUNDLE grant is denied."""
        # Grant CALENDAR_BUNDLE scope instead, NOT DISABLE_CALENDAR_BUNDLE
        org, system_user, token, auth_service = self._setup_org_and_token(
            resources=[PublicAPIResources.CALENDAR_BUNDLE]
        )
        bundle = self._make_bundle(org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": org.id, "bundleId": bundle.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_disable_calendar_bundle_unauthenticated(self):
        """An unauthenticated request returns a permission error."""
        org, _system_user, _token, auth_service = self._setup_org_and_token()
        bundle = self._make_bundle(org)

        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={
                    "query": DISABLE_CALENDAR_BUNDLE_MUTATION,
                    "variables": {"input": {"organizationId": org.id, "bundleId": bundle.id}},
                },
                format="json",
                # No authorization header
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "authenticated" in str(data["errors"]).lower()


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
        user = baker.make(user_model, email=f"member_{org.id}_{id(org)}@example.com")
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
        minted = SystemUser.original_manager.get(id=int(result["id"]))
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

        su_count_after_first = SystemUser.original_manager.filter(
            integration_name="dup_scoped"
        ).count()
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
            SystemUser.original_manager.filter(integration_name="dup_scoped").count()
            == su_count_after_first
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


# ---------------------------------------------------------------------------
# Phase 1 — Owner-guard blocked-time writes
# ---------------------------------------------------------------------------
#
# GraphQL mutations reused from the existing blocked-time test sections.
#
_CREATE_BLOCKED_TIME_SCOPED = """
mutation CreateBlockedTime($input: CreateBlockedTimeInput!) {
    createBlockedTime(input: $input) {
        success
        errorMessage
        blockedTime {
            id
            startTime
            endTime
        }
    }
}
"""

_UPDATE_BLOCKED_TIME_SCOPED = """
mutation UpdateBlockedTime($input: UpdateBlockedTimeInput!) {
    updateBlockedTime(input: $input) {
        success
        errorMessage
        blockedTime {
            id
            startTime
            endTime
        }
    }
}
"""

_DELETE_BLOCKED_TIME_SCOPED = """
mutation DeleteBlockedTime($input: DeleteBlockedTimeInput!) {
    deleteBlockedTime(input: $input) {
        success
        errorMessage
    }
}
"""


@pytest.mark.django_db
class TestScopedTokenBlockedTimeWrites:
    """Integration tests for owner-scoped blocked-time mutations (Phase 1).

    Coverage:
    - Scoped token can create/update/delete on its owned calendar (happy path).
    - Cross-owner write returns the same not-found message as a genuinely missing
      calendar, revealing nothing about the target's existence.
    - Org-wide token is structurally unchanged (no-regression assertion).
    - A scoped token missing the required resource grant is denied by the permission
      class before the guard even runs.
    """

    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _setup_org(self):
        """Create a base organization for the test scenario."""
        return baker.make(Organization, name="Test Org")

    def _make_owner_with_calendar(self, org):
        """Create a provider user, their membership, and a calendar they own.

        Returns (user, membership, calendar).
        """
        import uuid

        from calendar_integration.models import CalendarOwnership

        user_model = get_user_model()
        unique = uuid.uuid4().hex[:8]
        owner = baker.make(user_model, email=f"owner_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=owner, organization=org, is_active=True
        )
        calendar = baker.make(
            Calendar,
            organization=org,
            name="Provider Calendar",
            external_id=f"provider-cal-{unique}",
        )
        baker.make(CalendarOwnership, calendar=calendar, user=owner, organization=org)
        return owner, membership, calendar

    def _make_scoped_system_user(self, org, membership, resources):
        """Mint a scoped SystemUser with the given resource grants.

        Returns (system_user, plaintext_token, auth_service).
        """
        import uuid

        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_token_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _make_org_wide_system_user(self, org, resources):
        """Mint an org-wide (unscoped) SystemUser with the given resource grants.

        Returns (system_user, plaintext_token, auth_service).
        """
        import uuid

        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"org_wide_token_{uuid.uuid4().hex[:8]}",
            organization=org,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _create_blocked_time_on_calendar(self, org, system_user, calendar):
        """Create a BlockedTime row directly via the service (not the API).

        Used to set up rows for update/delete happy-path tests.
        """
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        return calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=datetime.datetime(2026, 10, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 10, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            reason="Scoped test block",
        )

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    # ------------------------------------------------------------------
    # createBlockedTime — scoped happy path
    # ------------------------------------------------------------------

    def test_create_blocked_time_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token creates a blocked time on its owner's calendar."""
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CREATE_BLOCKED_TIME]
        )

        start = datetime.datetime(2026, 10, 5, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 5, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _CREATE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "reason": "Scoped block",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createBlockedTime"]
        assert result["success"] is True
        assert result["blockedTime"] is not None
        bt_id = int(result["blockedTime"]["id"])

        # Verify the DB row exists and belongs to the correct calendar
        bt = BlockedTime.objects.filter_by_organization(org.id).get(id=bt_id)
        assert bt.calendar_fk_id == calendar.id
        assert bt.reason == "Scoped block"

    # ------------------------------------------------------------------
    # createBlockedTime — cross-owner not-found is identical to missing calendar
    # ------------------------------------------------------------------

    def test_create_blocked_time_cross_owner_same_not_found_as_missing_calendar(self):
        """Cross-owner createBlockedTime returns identical not-found as a truly missing calendar.

        A scoped token targeting another provider's calendar_id must get the same
        success=False / errorMessage as a genuinely nonexistent calendar — revealing
        nothing about the target's existence.
        """
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        # Two independent providers in the same org
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)

        # Token scoped to provider A
        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.CREATE_BLOCKED_TIME]
        )

        start = datetime.datetime(2026, 10, 5, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 5, 16, 0, 0, tzinfo=datetime.UTC)

        # Attempt against provider B's calendar (cross-owner)
        cross_owner_response = self._post(
            _CREATE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        # Attempt against a genuinely nonexistent calendar_id
        nonexistent_id = 999998
        missing_response = self._post(
            _CREATE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": nonexistent_id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        # Both must be success=False, no errors list, and IDENTICAL errorMessage
        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["createBlockedTime"]
            assert r["success"] is False
            assert r["errorMessage"] is not None

        cross_msg = cross_owner_response.json()["data"]["createBlockedTime"]["errorMessage"]
        missing_msg = missing_response.json()["data"]["createBlockedTime"]["errorMessage"]
        assert cross_msg == missing_msg, (
            f"Cross-owner error '{cross_msg}' must equal missing-calendar error '{missing_msg}'"
        )

        # No BlockedTime rows must have been created in either case
        assert not BlockedTime.objects.filter(calendar_fk_id=calendar_b.id).exists()

    # ------------------------------------------------------------------
    # updateBlockedTime — scoped happy path
    # ------------------------------------------------------------------

    def test_update_blocked_time_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token updates a blocked time on its owner's calendar."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        # Create row via org-wide service call so the calendar service can initialize freely
        org_wide_su, _org_wide_token, _org_wide_auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_BLOCKED_TIME]
        )
        blocked_time = self._create_blocked_time_on_calendar(org, org_wide_su, calendar)

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.UPDATE_BLOCKED_TIME]
        )

        new_start = datetime.datetime(2026, 10, 2, 10, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 2, 18, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _UPDATE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateBlockedTime"]
        assert result["success"] is True
        assert result["blockedTime"] is not None

    # ------------------------------------------------------------------
    # updateBlockedTime — cross-owner not-found
    # ------------------------------------------------------------------

    def test_update_blocked_time_cross_owner_same_not_found_as_missing_calendar(self):
        """Cross-owner updateBlockedTime returns identical not-found as a truly missing calendar."""
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        org_wide_su_b, _, _org_wide_auth_b = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_BLOCKED_TIME]
        )
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        # Create a blocked time on B's calendar using org-wide token
        blocked_time_b = self._create_blocked_time_on_calendar(org, org_wide_su_b, calendar_b)

        # Token scoped to provider A — must not update B's calendar
        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.UPDATE_BLOCKED_TIME]
        )

        new_start = datetime.datetime(2026, 10, 3, 9, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 3, 17, 0, 0, tzinfo=datetime.UTC)

        # Cross-owner attempt: calendar_id belongs to B, not A
        cross_owner_response = self._post(
            _UPDATE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "blockedTimeId": blocked_time_b.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        # Genuinely missing calendar_id attempt
        missing_response = self._post(
            _UPDATE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": 999997,
                    "blockedTimeId": blocked_time_b.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["updateBlockedTime"]
            assert r["success"] is False

        cross_msg = cross_owner_response.json()["data"]["updateBlockedTime"]["errorMessage"]
        missing_msg = missing_response.json()["data"]["updateBlockedTime"]["errorMessage"]
        assert cross_msg == missing_msg

        # The blocked time row must be unchanged
        blocked_time_b.refresh_from_db()
        assert blocked_time_b.reason == "Scoped test block"

    # ------------------------------------------------------------------
    # deleteBlockedTime — scoped happy path
    # ------------------------------------------------------------------

    def test_delete_blocked_time_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token deletes a blocked time on its owner's calendar."""
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        org_wide_su, _, _org_wide_auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_BLOCKED_TIME]
        )
        blocked_time = self._create_blocked_time_on_calendar(org, org_wide_su, calendar)
        bt_id = blocked_time.id

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.DELETE_BLOCKED_TIME]
        )

        response = self._post(
            _DELETE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": bt_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["deleteBlockedTime"]
        assert result["success"] is True

        # DB row must be gone
        assert not BlockedTime.objects.filter(id=bt_id).exists()

    # ------------------------------------------------------------------
    # deleteBlockedTime — cross-owner not-found
    # ------------------------------------------------------------------

    def test_delete_blocked_time_cross_owner_same_not_found_as_missing_calendar(self):
        """Cross-owner deleteBlockedTime returns identical not-found as a truly missing calendar.

        The blocked-time row must NOT be deleted when the token targets a cross-owner calendar.
        """
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_calendar(org)
        org_wide_su_b, _, _org_wide_auth_b = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_BLOCKED_TIME]
        )
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        blocked_time_b = self._create_blocked_time_on_calendar(org, org_wide_su_b, calendar_b)
        bt_b_id = blocked_time_b.id

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.DELETE_BLOCKED_TIME]
        )

        # Cross-owner attempt (calendar_b belongs to owner B)
        cross_owner_response = self._post(
            _DELETE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "blockedTimeId": bt_b_id,
                }
            },
        )

        # Genuinely missing calendar attempt
        missing_response = self._post(
            _DELETE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": 999996,
                    "blockedTimeId": bt_b_id,
                }
            },
        )

        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["deleteBlockedTime"]
            assert r["success"] is False

        cross_msg = cross_owner_response.json()["data"]["deleteBlockedTime"]["errorMessage"]
        missing_msg = missing_response.json()["data"]["deleteBlockedTime"]["errorMessage"]
        assert cross_msg == missing_msg

        # Row must still exist — the cross-owner delete must NOT have succeeded
        assert BlockedTime.objects.filter(id=bt_b_id).exists()

    # ------------------------------------------------------------------
    # blockedTimeId / calendarId mismatch — service-layer filter is load-bearing
    # ------------------------------------------------------------------

    def test_update_delete_blocked_time_in_scope_calendar_with_foreign_blocked_time_id_rejected(
        self,
    ):
        """A scoped token passing its OWN calendar_id but a foreign blocked_time_id is rejected.

        This pins the service-layer ``calendar_fk=calendar`` filter (the load-bearing
        constraint for the new threat model). The new owner guard does NOT trip here
        because ``calendarId`` is the token's in-scope calendar A; the only thing
        rejecting the write is the service scoping the BlockedTime lookup to the
        supplied calendar, so a foreign ``blocked_time_id`` on calendar B yields
        ``success=False`` and leaves B's row untouched.
        """
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        # Provider A: scoped token, owns calendar A (in-scope for the token)
        _owner_a, membership_a, calendar_a = self._make_owner_with_calendar(org)
        # Provider B: a different owner with a real BlockedTime row on calendar B
        org_wide_su_b, _, _org_wide_auth_b = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_BLOCKED_TIME, PublicAPIResources.DELETE_BLOCKED_TIME]
        )
        _owner_b, _membership_b, calendar_b = self._make_owner_with_calendar(org)
        blocked_time_b = self._create_blocked_time_on_calendar(org, org_wide_su_b, calendar_b)
        bt_b_id = blocked_time_b.id

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org,
            membership_a,
            [PublicAPIResources.UPDATE_BLOCKED_TIME, PublicAPIResources.DELETE_BLOCKED_TIME],
        )

        new_start = datetime.datetime(2026, 10, 9, 9, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 9, 17, 0, 0, tzinfo=datetime.UTC)

        # updateBlockedTime: in-scope calendar A id, foreign blocked_time id on B
        update_response = self._post(
            _UPDATE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_a.id,
                    "blockedTimeId": bt_b_id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert update_response.status_code == 200
        update_data = update_response.json()
        assert "errors" not in update_data or len(update_data.get("errors", [])) == 0
        assert update_data["data"]["updateBlockedTime"]["success"] is False
        # B's row must be unchanged by the rejected update
        blocked_time_b.refresh_from_db()
        assert blocked_time_b.reason == "Scoped test block"

        # deleteBlockedTime: in-scope calendar A id, foreign blocked_time id on B
        delete_response = self._post(
            _DELETE_BLOCKED_TIME_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_a.id,
                    "blockedTimeId": bt_b_id,
                }
            },
        )

        assert delete_response.status_code == 200
        delete_data = delete_response.json()
        assert "errors" not in delete_data or len(delete_data.get("errors", [])) == 0
        assert delete_data["data"]["deleteBlockedTime"]["success"] is False
        # B's row must still exist — the foreign delete must NOT have succeeded
        assert BlockedTime.objects.filter(id=bt_b_id).exists()

    # ------------------------------------------------------------------
    # Org-wide token — no-regression assertions for all three verbs
    # ------------------------------------------------------------------

    def test_create_blocked_time_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can create blocked times on any calendar (guard is no-op).

        This is the no-regression assertion: the guard must be a structural no-op for
        org-wide tokens, leaving their behavior byte-for-byte unchanged.
        """
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        # Calendar owned by a different provider — org-wide should not be blocked
        _other_owner, _other_membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CREATE_BLOCKED_TIME]
        )

        start = datetime.datetime(2026, 10, 6, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 6, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _CREATE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createBlockedTime"]
        assert result["success"] is True
        assert result["blockedTime"] is not None
        bt_id = int(result["blockedTime"]["id"])
        assert BlockedTime.objects.filter_by_organization(org.id).filter(id=bt_id).exists()

    def test_update_blocked_time_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can update blocked times on any calendar (guard is no-op)."""
        org = self._setup_org()
        _other_owner, _other_membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_BLOCKED_TIME]
        )
        blocked_time = self._create_blocked_time_on_calendar(org, system_user, calendar)

        new_start = datetime.datetime(2026, 10, 7, 10, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 7, 18, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _UPDATE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateBlockedTime"]
        assert result["success"] is True

    def test_delete_blocked_time_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can delete blocked times on any calendar (guard is no-op)."""
        from calendar_integration.models import BlockedTime

        org = self._setup_org()
        _other_owner, _other_membership, calendar = self._make_owner_with_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_BLOCKED_TIME]
        )
        blocked_time = self._create_blocked_time_on_calendar(org, system_user, calendar)
        bt_id = blocked_time.id

        response = self._post(
            _DELETE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": bt_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["deleteBlockedTime"]
        assert result["success"] is True
        assert not BlockedTime.objects.filter(id=bt_id).exists()

    # ------------------------------------------------------------------
    # Missing resource grant — scoped token denied
    # ------------------------------------------------------------------

    def test_create_blocked_time_scoped_token_missing_grant_denied(self):
        """A scoped token without CREATE_BLOCKED_TIME grant is denied by the permission class."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        # Grant CALENDAR instead of CREATE_BLOCKED_TIME
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        start = datetime.datetime(2026, 10, 8, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 8, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _CREATE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_blocked_time_scoped_token_missing_grant_denied(self):
        """A scoped token without UPDATE_BLOCKED_TIME grant is denied by the permission class."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        # Create row first using org-wide token
        org_wide_su, _, _auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_BLOCKED_TIME]
        )
        blocked_time = self._create_blocked_time_on_calendar(org, org_wide_su, calendar)
        # Scoped token with wrong resource
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        response = self._post(
            _UPDATE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_delete_blocked_time_scoped_token_missing_grant_denied(self):
        """A scoped token without DELETE_BLOCKED_TIME grant is denied by the permission class."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_calendar(org)
        org_wide_su, _, _auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_BLOCKED_TIME]
        )
        blocked_time = self._create_blocked_time_on_calendar(org, org_wide_su, calendar)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        response = self._post(
            _DELETE_BLOCKED_TIME_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "blockedTimeId": blocked_time.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0


# ---------------------------------------------------------------------------
# GraphQL query constants for scoped-availability-writes tests
# ---------------------------------------------------------------------------

_CREATE_AVAILABILITY_WINDOW_SCOPED = """
mutation CreateAvailabilityWindow($input: CreateAvailableTimeInput!) {
    createAvailabilityWindow(input: $input) {
        success
        errorMessage
        availableTime {
            id
            startTime
            endTime
        }
    }
}
"""

_UPDATE_AVAILABILITY_WINDOW_SCOPED = """
mutation UpdateAvailabilityWindow($input: UpdateAvailableTimeInput!) {
    updateAvailabilityWindow(input: $input) {
        success
        errorMessage
        availableTime {
            id
            startTime
            endTime
        }
    }
}
"""

_DELETE_AVAILABILITY_WINDOW_SCOPED = """
mutation DeleteAvailabilityWindow($input: DeleteAvailableTimeInput!) {
    deleteAvailabilityWindow(input: $input) {
        success
        errorMessage
    }
}
"""

_BATCH_UPDATE_AVAILABILITY_WINDOWS_SCOPED = """
mutation BatchUpdateAvailabilityWindows($input: BatchAvailabilityInput!) {
    batchUpdateAvailabilityWindows(input: $input) {
        success
        errorMessage
        availableTimes {
            id
            startTime
            endTime
        }
    }
}
"""


@pytest.mark.django_db
class TestScopedTokenAvailabilityWrites:
    """Integration tests for owner-scoped availability-window mutations (Phase 2).

    Coverage:
    - Scoped token can create (no rrule + rrule) / update / delete / batch on its owned calendar.
    - Cross-owner write returns the same not-found message as a genuinely missing
      calendar, revealing nothing about the target's existence.
    - Org-wide token is structurally unchanged (no-regression assertion).
    - A scoped token missing the required resource grant is denied by the permission
      class before the guard even runs.
    - Batch: a cross-owner calendar_id rejects the whole batch atomically (no partial write).
    """

    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers — mirror TestScopedTokenBlockedTimeWrites exactly
    # ------------------------------------------------------------------

    def _setup_org(self):
        """Create a base organization for the test scenario."""
        return baker.make(Organization, name="Test Org")

    def _make_owner_with_managing_calendar(self, org):
        """Create a provider user, their membership, and a manage_available_windows=True calendar.

        Returns (user, membership, calendar).
        """
        from calendar_integration.models import CalendarOwnership

        unique = uuid.uuid4().hex[:8]
        user_model = get_user_model()
        owner = baker.make(user_model, email=f"avail_owner_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=owner, organization=org, is_active=True
        )
        calendar = baker.make(
            Calendar,
            organization=org,
            name=f"Provider Avail Calendar {unique}",
            external_id=f"avail-cal-{unique}",
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=calendar, user=owner, organization=org)
        return owner, membership, calendar

    def _make_scoped_system_user(self, org, membership, resources):
        """Mint a scoped SystemUser with the given resource grants.

        Returns (system_user, plaintext_token, auth_service).
        """
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_avail_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _make_org_wide_system_user(self, org, resources):
        """Mint an org-wide (unscoped) SystemUser with the given resource grants.

        Returns (system_user, plaintext_token, auth_service).
        """
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"org_wide_avail_{uuid.uuid4().hex[:8]}",
            organization=org,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _create_available_time_on_calendar(self, org, system_user, calendar):
        """Create an AvailableTime row directly via the service (not the API).

        Used to set up rows for update/delete/batch happy-path tests.
        """
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        return calendar_service.create_available_time(
            calendar=calendar,
            start_time=datetime.datetime(2026, 10, 1, 9, 0, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 10, 1, 17, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    # ------------------------------------------------------------------
    # createAvailabilityWindow — scoped happy path (no rrule)
    # ------------------------------------------------------------------

    def test_create_availability_window_scoped_token_on_owned_calendar_succeeds_no_rrule(self):
        """A scoped token creates a one-off available time on its owner's calendar."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CREATE_AVAILABILITY_WINDOW]
        )

        start = datetime.datetime(2026, 10, 5, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 5, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _CREATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is True
        assert result["availableTime"] is not None
        at_id = int(result["availableTime"]["id"])

        at = AvailableTime.objects.filter_by_organization(org.id).get(id=at_id)
        assert at.calendar_fk_id == calendar.id
        assert at.recurrence_rule is None

    # ------------------------------------------------------------------
    # createAvailabilityWindow — scoped happy path (with rrule)
    # ------------------------------------------------------------------

    def test_create_availability_window_scoped_token_on_owned_calendar_succeeds_with_rrule(self):
        """A scoped token creates a recurring available time on its owner's calendar."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CREATE_AVAILABILITY_WINDOW]
        )

        start = datetime.datetime(2026, 10, 6, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 6, 17, 0, 0, tzinfo=datetime.UTC)
        rrule = "FREQ=WEEKLY;BYDAY=TU"

        response = self._post(
            _CREATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                    "rruleString": rrule,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is True
        assert result["availableTime"] is not None
        at_id = int(result["availableTime"]["id"])

        at = AvailableTime.objects.filter_by_organization(org.id).get(id=at_id)
        assert at.calendar_fk_id == calendar.id
        assert at.recurrence_rule is not None

    # ------------------------------------------------------------------
    # createAvailabilityWindow — cross-owner not-found identical to missing calendar
    # ------------------------------------------------------------------

    def test_create_availability_window_cross_owner_same_not_found_as_missing_calendar(self):
        """Cross-owner createAvailabilityWindow returns identical not-found as a truly missing calendar.

        A scoped token targeting another provider's calendar_id must get the same
        success=False / errorMessage as a genuinely nonexistent calendar — revealing
        nothing about the target's existence. No AvailableTime row is created.
        """
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_managing_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_managing_calendar(org)

        # Token scoped to provider A
        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.CREATE_AVAILABILITY_WINDOW]
        )

        start = datetime.datetime(2026, 10, 5, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 5, 16, 0, 0, tzinfo=datetime.UTC)

        # Attempt against provider B's calendar (cross-owner)
        cross_owner_response = self._post(
            _CREATE_AVAILABILITY_WINDOW_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        # Attempt against a genuinely nonexistent calendar_id
        nonexistent_id = 999994
        missing_response = self._post(
            _CREATE_AVAILABILITY_WINDOW_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": nonexistent_id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["createAvailabilityWindow"]
            assert r["success"] is False
            assert r["errorMessage"] is not None

        cross_msg = cross_owner_response.json()["data"]["createAvailabilityWindow"]["errorMessage"]
        missing_msg = missing_response.json()["data"]["createAvailabilityWindow"]["errorMessage"]
        assert cross_msg == missing_msg, (
            f"Cross-owner error '{cross_msg}' must equal missing-calendar error '{missing_msg}'"
        )

        # No AvailableTime rows must have been created for the cross-owner calendar
        assert not AvailableTime.objects.filter(calendar_fk_id=calendar_b.id).exists()

    # ------------------------------------------------------------------
    # updateAvailabilityWindow — scoped happy path
    # ------------------------------------------------------------------

    def test_update_availability_window_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token updates an available time on its owner's calendar."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        org_wide_su, _org_wide_token, _org_wide_auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
        )
        available_time = self._create_available_time_on_calendar(org, org_wide_su, calendar)

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
        )

        new_start = datetime.datetime(2026, 10, 2, 10, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 2, 18, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _UPDATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "availableTimeId": available_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is True
        assert result["availableTime"] is not None

    # ------------------------------------------------------------------
    # updateAvailabilityWindow — cross-owner not-found
    # ------------------------------------------------------------------

    def test_update_availability_window_cross_owner_same_not_found_as_missing_calendar(self):
        """Cross-owner updateAvailabilityWindow returns identical not-found as a truly missing calendar."""
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_managing_calendar(org)
        org_wide_su_b, _, _org_wide_auth_b = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
        )
        _owner_b, _membership_b, calendar_b = self._make_owner_with_managing_calendar(org)
        available_time_b = self._create_available_time_on_calendar(org, org_wide_su_b, calendar_b)

        # Capture B's row field values BEFORE the cross-owner attempt so the
        # unchanged-check compares like-for-like (naive stored field to naive).
        available_time_b.refresh_from_db()
        start_before = available_time_b.start_time_tz_unaware
        end_before = available_time_b.end_time_tz_unaware

        # Token scoped to provider A — must not update B's calendar
        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
        )

        new_start = datetime.datetime(2026, 10, 3, 9, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 3, 17, 0, 0, tzinfo=datetime.UTC)

        # Cross-owner attempt: calendar_id belongs to B, not A
        cross_owner_response = self._post(
            _UPDATE_AVAILABILITY_WINDOW_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "availableTimeId": available_time_b.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        # Genuinely missing calendar_id attempt
        missing_response = self._post(
            _UPDATE_AVAILABILITY_WINDOW_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": 999993,
                    "availableTimeId": available_time_b.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["updateAvailabilityWindow"]
            assert r["success"] is False

        cross_msg = cross_owner_response.json()["data"]["updateAvailabilityWindow"]["errorMessage"]
        missing_msg = missing_response.json()["data"]["updateAvailabilityWindow"]["errorMessage"]
        assert cross_msg == missing_msg

        # The available time row must be unchanged (compare against pre-attempt values).
        available_time_b.refresh_from_db()
        assert available_time_b.start_time_tz_unaware == start_before
        assert available_time_b.end_time_tz_unaware == end_before

    # ------------------------------------------------------------------
    # deleteAvailabilityWindow — scoped happy path
    # ------------------------------------------------------------------

    def test_delete_availability_window_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token deletes an available time on its owner's calendar."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        org_wide_su, _, _org_wide_auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
        )
        available_time = self._create_available_time_on_calendar(org, org_wide_su, calendar)
        at_id = available_time.id

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
        )

        response = self._post(
            _DELETE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "availableTimeId": at_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is True

        # DB row must be gone
        assert not AvailableTime.objects.filter(id=at_id).exists()

    # ------------------------------------------------------------------
    # deleteAvailabilityWindow — cross-owner not-found
    # ------------------------------------------------------------------

    def test_delete_availability_window_cross_owner_same_not_found_as_missing_calendar(self):
        """Cross-owner deleteAvailabilityWindow returns identical not-found as a truly missing calendar.

        The AvailableTime row must NOT be deleted when the token targets a cross-owner calendar.
        """
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_managing_calendar(org)
        org_wide_su_b, _, _org_wide_auth_b = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
        )
        _owner_b, _membership_b, calendar_b = self._make_owner_with_managing_calendar(org)
        available_time_b = self._create_available_time_on_calendar(org, org_wide_su_b, calendar_b)
        at_b_id = available_time_b.id

        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
        )

        # Cross-owner attempt (calendar_b belongs to owner B)
        cross_owner_response = self._post(
            _DELETE_AVAILABILITY_WINDOW_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "availableTimeId": at_b_id,
                }
            },
        )

        # Genuinely missing calendar attempt
        missing_response = self._post(
            _DELETE_AVAILABILITY_WINDOW_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": 999992,
                    "availableTimeId": at_b_id,
                }
            },
        )

        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["deleteAvailabilityWindow"]
            assert r["success"] is False

        cross_msg = cross_owner_response.json()["data"]["deleteAvailabilityWindow"]["errorMessage"]
        missing_msg = missing_response.json()["data"]["deleteAvailabilityWindow"]["errorMessage"]
        assert cross_msg == missing_msg

        # Row must still exist — the cross-owner delete must NOT have succeeded
        assert AvailableTime.objects.filter(id=at_b_id).exists()

    # ------------------------------------------------------------------
    # batchUpdateAvailabilityWindows — scoped happy path
    # ------------------------------------------------------------------

    def test_batch_update_availability_windows_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token applies a batch of availability operations on its owner's calendar."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        org_wide_su, _, _org_wide_auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
        )
        existing_time = self._create_available_time_on_calendar(org, org_wide_su, calendar)

        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
        )

        new_start = datetime.datetime(2026, 11, 1, 9, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 11, 1, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _BATCH_UPDATE_AVAILABILITY_WINDOWS_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "operations": [
                        {
                            "action": "update",
                            "availableTimeId": existing_time.id,
                            "startTime": new_start.isoformat(),
                            "endTime": new_end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is True
        assert result["availableTimes"] is not None

    # ------------------------------------------------------------------
    # batchUpdateAvailabilityWindows — cross-owner not-found + no partial write
    # ------------------------------------------------------------------

    def test_batch_update_availability_windows_cross_owner_rejected_wholesale(self):
        """Cross-owner batchUpdateAvailabilityWindows returns identical not-found as missing calendar.

        The entire batch must be rejected atomically — no AvailableTime rows must be
        created, updated, or deleted when the calendar_id is cross-owner. Because
        BatchAvailabilityInput carries a single calendar_id shared by all operations
        (individual BatchAvailabilityOperationInput entries have no per-op calendar_id),
        one guard call at the top rejects the whole batch before any operation runs.
        """
        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_managing_calendar(org)
        org_wide_su_b, _, _org_wide_auth_b = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
        )
        _owner_b, _membership_b, calendar_b = self._make_owner_with_managing_calendar(org)

        # Seed a pre-existing AvailableTime row on B's calendar so a partial
        # update/delete (not just a stray create) would be detectable.
        existing_time_b = self._create_available_time_on_calendar(org, org_wide_su_b, calendar_b)
        existing_time_b.refresh_from_db()
        existing_start_before = existing_time_b.start_time_tz_unaware
        existing_end_before = existing_time_b.end_time_tz_unaware

        # Count rows on B's calendar before the cross-owner attempt (org-scoped query)
        rows_before = (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar_b.id)
            .count()
        )

        # Token scoped to provider A
        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
        )

        create_start = datetime.datetime(2026, 12, 1, 9, 0, 0, tzinfo=datetime.UTC)
        create_end = datetime.datetime(2026, 12, 1, 17, 0, 0, tzinfo=datetime.UTC)

        update_start = datetime.datetime(2026, 12, 2, 9, 0, 0, tzinfo=datetime.UTC)
        update_end = datetime.datetime(2026, 12, 2, 17, 0, 0, tzinfo=datetime.UTC)

        # Cross-owner attempt: calendar_id = B's calendar, batch mixes create,
        # update, and delete ops — an update/delete targets B's pre-existing row,
        # so a partial write (not just a stray create) would be caught.
        cross_owner_response = self._post(
            _BATCH_UPDATE_AVAILABILITY_WINDOWS_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar_b.id,
                    "operations": [
                        {
                            "action": "create",
                            "startTime": create_start.isoformat(),
                            "endTime": create_end.isoformat(),
                            "timezone": "UTC",
                        },
                        {
                            "action": "update",
                            "availableTimeId": existing_time_b.id,
                            "startTime": update_start.isoformat(),
                            "endTime": update_end.isoformat(),
                        },
                        {
                            "action": "delete",
                            "availableTimeId": existing_time_b.id,
                        },
                    ],
                }
            },
        )

        # Genuinely missing calendar attempt (same batch shape)
        nonexistent_id = 999991
        missing_response = self._post(
            _BATCH_UPDATE_AVAILABILITY_WINDOWS_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": nonexistent_id,
                    "operations": [
                        {
                            "action": "create",
                            "startTime": create_start.isoformat(),
                            "endTime": create_end.isoformat(),
                            "timezone": "UTC",
                        },
                        {
                            "action": "update",
                            "availableTimeId": existing_time_b.id,
                            "startTime": update_start.isoformat(),
                            "endTime": update_end.isoformat(),
                        },
                        {
                            "action": "delete",
                            "availableTimeId": existing_time_b.id,
                        },
                    ],
                }
            },
        )

        for resp in [cross_owner_response, missing_response]:
            assert resp.status_code == 200
            d = resp.json()
            assert "errors" not in d or len(d.get("errors", [])) == 0
            r = d["data"]["batchUpdateAvailabilityWindows"]
            assert r["success"] is False
            assert r["errorMessage"] is not None

        cross_msg = cross_owner_response.json()["data"]["batchUpdateAvailabilityWindows"][
            "errorMessage"
        ]
        missing_msg = missing_response.json()["data"]["batchUpdateAvailabilityWindows"][
            "errorMessage"
        ]
        assert cross_msg == missing_msg, (
            f"Cross-owner error '{cross_msg}' must equal missing-calendar error '{missing_msg}'"
        )

        # No AvailableTime rows must have been created on B's calendar (no partial write)
        rows_after = (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar_b.id)
            .count()
        )
        assert rows_after == rows_before, (
            f"Partial write detected: row count on B's calendar changed from "
            f"{rows_before} to {rows_after}"
        )

        # The pre-existing B row must NOT have been updated or deleted: it still
        # exists and retains its original field values.
        assert (
            AvailableTime.objects.filter_by_organization(org.id)
            .filter(id=existing_time_b.id)
            .exists()
        ), "Partial write detected: pre-existing B row was deleted"
        existing_time_b.refresh_from_db()
        assert existing_time_b.start_time_tz_unaware == existing_start_before, (
            "Partial write detected: pre-existing B row's start time was updated"
        )
        assert existing_time_b.end_time_tz_unaware == existing_end_before, (
            "Partial write detected: pre-existing B row's end time was updated"
        )

    # ------------------------------------------------------------------
    # Org-wide token — no-regression assertions for all four verbs
    # ------------------------------------------------------------------

    def test_create_availability_window_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can create available times on any calendar (guard is no-op)."""
        org = self._setup_org()
        _other_owner, _other_membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CREATE_AVAILABILITY_WINDOW]
        )

        start = datetime.datetime(2026, 10, 6, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 6, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _CREATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["createAvailabilityWindow"]
        assert result["success"] is True
        assert result["availableTime"] is not None
        at_id = int(result["availableTime"]["id"])
        assert AvailableTime.objects.filter_by_organization(org.id).filter(id=at_id).exists()

    def test_update_availability_window_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can update available times on any calendar (guard is no-op)."""
        org = self._setup_org()
        _other_owner, _other_membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
        )
        available_time = self._create_available_time_on_calendar(org, system_user, calendar)

        new_start = datetime.datetime(2026, 10, 7, 10, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 10, 7, 18, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _UPDATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "availableTimeId": available_time.id,
                    "startTime": new_start.isoformat(),
                    "endTime": new_end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["updateAvailabilityWindow"]
        assert result["success"] is True

    def test_delete_availability_window_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can delete available times on any calendar (guard is no-op)."""
        org = self._setup_org()
        _other_owner, _other_membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
        )
        available_time = self._create_available_time_on_calendar(org, system_user, calendar)
        at_id = available_time.id

        response = self._post(
            _DELETE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "availableTimeId": at_id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["deleteAvailabilityWindow"]
        assert result["success"] is True
        assert not AvailableTime.objects.filter(id=at_id).exists()

    def test_batch_update_availability_windows_org_wide_token_unaffected_by_guard(self):
        """An org-wide token can batch-update available times on any calendar (guard is no-op)."""
        org = self._setup_org()
        _other_owner, _other_membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS]
        )
        existing_time = self._create_available_time_on_calendar(org, system_user, calendar)

        new_start = datetime.datetime(2026, 11, 2, 9, 0, 0, tzinfo=datetime.UTC)
        new_end = datetime.datetime(2026, 11, 2, 17, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _BATCH_UPDATE_AVAILABILITY_WINDOWS_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "operations": [
                        {
                            "action": "update",
                            "availableTimeId": existing_time.id,
                            "startTime": new_start.isoformat(),
                            "endTime": new_end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["batchUpdateAvailabilityWindows"]
        assert result["success"] is True

    # ------------------------------------------------------------------
    # Missing resource grant — scoped token denied (permission class fires first)
    # ------------------------------------------------------------------

    def test_create_availability_window_scoped_token_missing_grant_denied(self):
        """A scoped token without CREATE_AVAILABILITY_WINDOW grant is denied."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        start = datetime.datetime(2026, 10, 8, 8, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 10, 8, 16, 0, 0, tzinfo=datetime.UTC)

        response = self._post(
            _CREATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "startTime": start.isoformat(),
                    "endTime": end.isoformat(),
                    "timezone": "UTC",
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_update_availability_window_scoped_token_missing_grant_denied(self):
        """A scoped token without UPDATE_AVAILABILITY_WINDOW grant is denied."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        org_wide_su, _, _auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.UPDATE_AVAILABILITY_WINDOW]
        )
        available_time = self._create_available_time_on_calendar(org, org_wide_su, calendar)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        response = self._post(
            _UPDATE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "availableTimeId": available_time.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_delete_availability_window_scoped_token_missing_grant_denied(self):
        """A scoped token without DELETE_AVAILABILITY_WINDOW grant is denied."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        org_wide_su, _, _auth = self._make_org_wide_system_user(
            org, [PublicAPIResources.DELETE_AVAILABILITY_WINDOW]
        )
        available_time = self._create_available_time_on_calendar(org, org_wide_su, calendar)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        response = self._post(
            _DELETE_AVAILABILITY_WINDOW_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "availableTimeId": available_time.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_batch_update_availability_windows_scoped_token_missing_grant_denied(self):
        """A scoped token without BATCH_UPDATE_AVAILABILITY_WINDOWS grant is denied."""
        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR]
        )

        response = self._post(
            _BATCH_UPDATE_AVAILABILITY_WINDOWS_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": calendar.id,
                    "operations": [],
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()
        assert "don't have access" in str(data["errors"]).lower()


_SCHEDULE_EVENT_SCOPED = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) {
        id
        title
        description
        attendances {
            id
        }
        externalAttendances {
            id
        }
    }
}
"""

# recurrenceRule is a non-nullable field on CalendarEventGraphQLType, so only select it
# when the event is actually recurring (the non-recurring path returns null for it).
_SCHEDULE_EVENT_SCOPED_WITH_RRULE = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) {
        id
        title
        recurrenceRule {
            id
        }
    }
}
"""


@pytest.mark.django_db
class TestScopedTokenScheduleEvent:
    """Integration tests for the owner-scoped ``scheduleEvent`` mutation (Phase 3).

    Coverage:
    - Scoped token schedules on its owner's calendar (covering availability window) -> success.
    - Recurring (rrule) event persists a recurrence rule.
    - Internal + external attendees persisted.
    - Out-of-org internal attendee -> clean error, no row.
    - Cross-owner calendar -> not-found IDENTICAL to a missing calendar + no row.
    - Org-wide token -> denied, no row (event creation stays blocked).
    - Bundle calendar owned by the scoped owner -> clean error, no row.
    - No availability window -> clean error.
    - Title too long -> clean error.
    - Missing CALENDAR_EVENT grant -> denied by the permission class.
    """

    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers — mirror the blocked-time / availability scoped-token classes
    # ------------------------------------------------------------------

    def _setup_org(self):
        return baker.make(Organization, name="Schedule Event Org")

    def _make_owner_with_managing_calendar(self, org):
        """Create a provider user, membership, and a manage_available_windows=True calendar.

        manage_available_windows=True so availability is governed by declared windows; the
        happy path seeds a covering window, the no-availability test omits it.
        Returns (user, membership, calendar).
        """
        from calendar_integration.models import CalendarOwnership

        unique = uuid.uuid4().hex[:8]
        user_model = get_user_model()
        owner = baker.make(user_model, email=f"sched_owner_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=owner, organization=org, is_active=True
        )
        calendar = baker.make(
            Calendar,
            organization=org,
            name=f"Provider Sched Calendar {unique}",
            external_id=f"sched-cal-{unique}",
            manage_available_windows=True,
        )
        baker.make(CalendarOwnership, calendar=calendar, user=owner, organization=org)
        return owner, membership, calendar

    def _make_owner_with_bundle_calendar(self, org):
        """Create a provider who owns a BUNDLE calendar. Returns (user, membership, bundle)."""
        from calendar_integration.models import CalendarOwnership

        unique = uuid.uuid4().hex[:8]
        user_model = get_user_model()
        owner = baker.make(user_model, email=f"sched_bundle_owner_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=owner, organization=org, is_active=True
        )
        bundle = baker.make(
            Calendar,
            organization=org,
            name=f"Bundle Calendar {unique}",
            external_id=f"sched-bundle-{unique}",
            calendar_type=CalendarType.BUNDLE,
        )
        baker.make(CalendarOwnership, calendar=bundle, user=owner, organization=org)
        return owner, membership, bundle

    def _make_scoped_system_user(self, org, membership, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_sched_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _make_org_wide_system_user(self, org, resources):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"org_wide_sched_{uuid.uuid4().hex[:8]}",
            organization=org,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _seed_window(self, org, system_user, calendar):
        """Seed a covering AvailableTime window via the service (not the API)."""
        from di_core.containers import container

        calendar_service: CalendarService = container.calendar_service()
        calendar_service.initialize_without_provider(user_or_token=system_user, organization=org)
        return calendar_service.create_available_time(
            calendar=calendar,
            start_time=datetime.datetime(2026, 10, 1, 8, 0, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 10, 1, 18, 0, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _input(self, org, calendar, **overrides):
        base = {
            "organizationId": org.id,
            "calendarId": calendar.id,
            "startTime": datetime.datetime(2026, 10, 1, 10, 0, 0, tzinfo=datetime.UTC).isoformat(),
            "endTime": datetime.datetime(2026, 10, 1, 11, 0, 0, tzinfo=datetime.UTC).isoformat(),
            "timezone": "UTC",
            "title": "Scheduled Visit",
        }
        base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_schedule_event_scoped_token_on_owned_calendar_succeeds(self):
        """A scoped token schedules an event on its owner's calendar with a covering window."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["scheduleEvent"]
        assert result is not None
        assert result["title"] == "Scheduled Visit"
        ev = CalendarEvent.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert ev.calendar_fk_id == calendar.id

    def test_schedule_event_recurring_persists_recurrence_rule(self):
        """A scoped token schedules a recurring event (rrule -> RecurrenceRule persisted)."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED_WITH_RRULE,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar, rruleString="RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=TH")},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["scheduleEvent"]
        assert result["recurrenceRule"] is not None
        ev = CalendarEvent.objects.filter_by_organization(org.id).get(id=int(result["id"]))
        assert ev.recurrence_rule_fk_id is not None

    def test_schedule_event_persists_internal_and_external_attendees(self):
        """Internal (active member) + external (email/name) attendees are persisted."""
        from calendar_integration.models import EventAttendance, EventExternalAttendance

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        user_model = get_user_model()
        attendee = baker.make(user_model, email=f"attendee_{uuid.uuid4().hex[:8]}@example.com")
        baker.make(OrganizationMembership, user=attendee, organization=org, is_active=True)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {
                "input": self._input(
                    org,
                    calendar,
                    attendeeUserIds=[attendee.id],
                    externalAttendees=[{"email": "guest@example.com", "name": "Guest"}],
                )
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        ev_id = int(data["data"]["scheduleEvent"]["id"])
        assert (
            EventAttendance.objects.filter_by_organization(org.id)
            .filter(event_fk_id=ev_id, user_id=attendee.id)
            .exists()
        )
        assert (
            EventExternalAttendance.objects.filter_by_organization(org.id)
            .filter(event_fk_id=ev_id, external_attendee__email="guest@example.com")
            .exists()
        )

    # ------------------------------------------------------------------
    # Negative paths
    # ------------------------------------------------------------------

    def test_schedule_event_out_of_org_attendee_clean_error_no_row(self):
        """An attendee who is not an active member of the org -> clean error, no event row."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        other_org = baker.make(Organization, name="Other Org")
        user_model = get_user_model()
        outsider = baker.make(user_model, email=f"outsider_{uuid.uuid4().hex[:8]}@example.com")
        baker.make(OrganizationMembership, user=outsider, organization=other_org, is_active=True)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar, attendeeUserIds=[outsider.id])},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "active members" in str(data["errors"]).lower()
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .exists()
        )

    def test_schedule_event_cross_owner_not_found_same_as_missing_calendar(self):
        """Cross-owner calendar -> identical not-found as a genuinely missing calendar, no row."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner_a, membership_a, _calendar_a = self._make_owner_with_managing_calendar(org)
        _owner_b, _membership_b, calendar_b = self._make_owner_with_managing_calendar(org)
        system_user_a, token_a, auth_service_a = self._make_scoped_system_user(
            org, membership_a, [PublicAPIResources.CALENDAR_EVENT]
        )

        cross = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {"input": self._input(org, calendar_b)},
        )
        missing = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user_a,
            token_a,
            auth_service_a,
            {
                "input": {
                    "organizationId": org.id,
                    "calendarId": 999997,
                    "startTime": datetime.datetime(
                        2026, 10, 1, 10, 0, 0, tzinfo=datetime.UTC
                    ).isoformat(),
                    "endTime": datetime.datetime(
                        2026, 10, 1, 11, 0, 0, tzinfo=datetime.UTC
                    ).isoformat(),
                    "timezone": "UTC",
                    "title": "Scheduled Visit",
                }
            },
        )

        cross_msg = cross.json()["errors"][0]["message"]
        missing_msg = missing.json()["errors"][0]["message"]
        assert cross_msg == missing_msg == "Calendar not found."
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar_b.id)
            .exists()
        )

    def test_schedule_event_org_wide_token_denied_no_row(self):
        """An org-wide token is denied (event creation stays blocked) with no row."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, _membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_org_wide_system_user(
            org, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "public api" in str(data["errors"]).lower()
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .exists()
        )

    def test_schedule_event_bundle_calendar_clean_error_no_row(self):
        """A bundle calendar owned by the scoped owner -> clean error, no row."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, bundle = self._make_owner_with_bundle_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, bundle)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "bundle" in str(data["errors"]).lower()
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=bundle.id)
            .exists()
        )

    def test_schedule_event_no_availability_window_clean_error(self):
        """No covering availability window on a managed calendar -> clean error, no row."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        # No window seeded -> manage_available_windows=True means nothing is bookable.

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "available time window" in str(data["errors"]).lower()
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .exists()
        )

    def test_schedule_event_title_too_long_clean_error(self):
        """A title exceeding the model max_length -> clean error before any write."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar, title="x" * 256)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "at most" in str(data["errors"]).lower()
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .exists()
        )

    def test_schedule_event_missing_calendar_event_grant_denied(self):
        """A scoped token without the CALENDAR_EVENT grant is denied by the permission class."""
        from calendar_integration.models import CalendarEvent

        org = self._setup_org()
        _owner, membership, calendar = self._make_owner_with_managing_calendar(org)
        system_user, token, auth_service = self._make_scoped_system_user(
            org, membership, [PublicAPIResources.CREATE_BLOCKED_TIME]
        )
        self._seed_window(org, system_user, calendar)

        response = self._post(
            _SCHEDULE_EVENT_SCOPED,
            system_user,
            token,
            auth_service,
            {"input": self._input(org, calendar)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=calendar.id)
            .exists()
        )
