import datetime
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount
from model_bakery import baker

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    CalendarType,
    CalendarVisibility,
)
from calendar_integration.models import Calendar, CalendarOwnership
from organizations.exceptions import (
    InvalidInvitationTokenError,
    InvitationNotFoundError,
    UserAlreadyHasMembershipError,
)
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from organizations.services import OrganizationService
from users.models import User


@pytest.mark.django_db
class TestOrganizationService:
    """Test suite for OrganizationService."""

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="test@example.com")

    @pytest.fixture
    def mock_calendar_service(self):
        """Create a mock CalendarService."""
        mock_service = Mock()
        mock_service.initialize_without_provider.return_value = None
        mock_service.request_organization_calendar_resources_import.return_value = None
        return mock_service

    @pytest.fixture
    def organization_service(self, mock_calendar_service):
        from di_core.containers import container

        """Create OrganizationService instance with mocked dependencies."""
        with container.calendar_service.override(mock_calendar_service):
            yield OrganizationService()

    def test_create_organization_without_sync_rooms(
        self, organization_service, user, mock_calendar_service
    ):
        """Test creating an organization without room syncing."""
        organization_name = "Test Organization"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=False
        )

        # Verify organization was created correctly
        assert isinstance(organization, Organization)
        assert organization.name == organization_name
        assert organization.should_sync_rooms is False
        assert organization.id is not None

        # Verify organization exists in database
        db_organization = Organization.objects.get(id=organization.id)
        assert db_organization.name == organization_name
        assert db_organization.should_sync_rooms is False

        # Verify calendar service methods were not called when should_sync_rooms=False
        mock_calendar_service.initialize_without_provider.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    def test_create_organization_with_sync_rooms(
        self, organization_service, user, mock_calendar_service
    ):
        """Test creating an organization with room syncing enabled.

        Phase 18: a newly-created org has no GoogleCalendarServiceAccount yet.
        ``request_rooms_sync`` raises ``NoServiceAccountConfiguredError`` which
        ``create_organization`` catches and logs; the org is still created and
        the calendar service is NOT called (no import enqueued until the admin
        configures a service account via PATCH).
        """
        organization_name = "Test Organization with Rooms"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=True
        )

        # Verify organization was created correctly
        assert isinstance(organization, Organization)
        assert organization.name == organization_name
        assert organization.should_sync_rooms is True
        assert organization.id is not None

        # Verify organization exists in database
        db_organization = Organization.objects.get(id=organization.id)
        assert db_organization.name == organization_name
        assert db_organization.should_sync_rooms is True

        # Phase 18: no service account → neither authenticate nor the import is called.
        mock_calendar_service.authenticate.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    def test_create_organization_default_sync_rooms_false(
        self, organization_service, user, mock_calendar_service
    ):
        """Test that should_sync_rooms defaults to False when not specified."""
        organization_name = "Test Organization Default"

        organization = organization_service.create_organization(
            creator=user, name=organization_name
        )

        # Verify organization was created with default should_sync_rooms=False
        assert organization.should_sync_rooms is False

        # Verify calendar service methods were not called
        mock_calendar_service.initialize_without_provider.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    def test_create_organization_sets_service_organization_attribute(
        self, organization_service, user, mock_calendar_service
    ):
        """Test that the service stores the created organization in self.organization."""
        organization_name = "Test Organization Attribute"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=False
        )

        # Verify that the service stores the organization instance
        assert hasattr(organization_service, "organization")
        assert organization_service.organization == organization
        assert organization_service.organization.name == organization_name

    def test_create_organization_multiple_calls(
        self, organization_service, user, mock_calendar_service
    ):
        """Test that multiple calls to create_organization work correctly.

        Phase 1: OrganizationMembership.user is now a ForeignKey (not OneToOne),
        so a user may be admin of multiple organizations. Each call creates a
        distinct org and a distinct (user, org) membership — no IntegrityError.
        The unique constraint is (user, organization), not (user,) alone.
        """
        # Create first organization
        org1 = organization_service.create_organization(
            creator=user, name="Organization 1", should_sync_rooms=False
        )

        # Verify first organization was created successfully
        assert org1.name == "Organization 1"
        assert org1.should_sync_rooms is False

        # Create second organization with same user — allowed post-Phase 1.
        org2 = organization_service.create_organization(
            creator=user, name="Organization 2", should_sync_rooms=False
        )

        assert org2.name == "Organization 2"
        assert org1.id != org2.id
        # User now holds two memberships, one in each org.
        from organizations.models import OrganizationMembership

        assert OrganizationMembership.objects.filter(user=user).count() == 2

    def test_create_organization_with_sync_rooms_calendar_service_exception(
        self, user, mock_calendar_service
    ):
        """Test behavior when calendar service raises an exception during room sync.

        Phase 18: ``request_rooms_sync`` raises ``NoServiceAccountConfiguredError``
        (a DRF ValidationError) before touching the calendar service because no
        ``GoogleCalendarServiceAccount`` exists for a brand-new org.
        ``create_organization`` catches ONLY that error and swallows it; the org
        is still created successfully.

        A generic, unexpected exception from the calendar service would still
        propagate (not tested here — that path requires a service account to be
        present, so it belongs in a unit test for ``request_rooms_sync`` itself).
        """
        from di_core.containers import container

        with container.calendar_service.override(mock_calendar_service):
            service = OrganizationService()

            # Phase 18: org creation must succeed even though no SA is configured
            # (the NoServiceAccountConfiguredError is caught and logged internally).
            org = service.create_organization(
                creator=user, name="Test Organization Exception", should_sync_rooms=True
            )

        assert org is not None
        assert org.should_sync_rooms is True
        # Calendar service must NOT have been touched (no SA → guard fires early).
        mock_calendar_service.authenticate.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    @pytest.mark.parametrize("should_sync_rooms", [True, False])
    def test_create_organization_parametrized(
        self, organization_service, user, mock_calendar_service, should_sync_rooms
    ):
        """Parametrized test for both sync_rooms scenarios.

        Phase 18: when ``should_sync_rooms=True`` and no service account is
        configured (as is always the case for a brand-new org), the import is
        skipped gracefully — the calendar service is never called.
        """
        organization_name = f"Test Organization Sync={should_sync_rooms}"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=should_sync_rooms
        )

        # Verify organization was created correctly
        assert organization.should_sync_rooms == should_sync_rooms

        # Phase 18: whether should_sync_rooms is True or False, the calendar service
        # is never called during org creation because there is no service account yet.
        mock_calendar_service.authenticate.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization, name="Test Organization")

    @pytest.fixture
    def mock_notification_service(self):
        """Create a mock NotificationService."""
        mock_service = Mock()
        mock_service.create_one_off_notification.return_value = None
        return mock_service

    @pytest.fixture
    def organization_service_with_mocks(self, mock_calendar_service, mock_notification_service):
        """Create OrganizationService with both mocked dependencies."""
        from di_core.containers import container

        with (
            container.calendar_service.override(mock_calendar_service),
            container.notification_service.override(mock_notification_service),
        ):
            service = OrganizationService()
            yield service

    def test_invite_user_to_organization_new_invitation(
        self, organization_service_with_mocks, user, organization, mock_notification_service
    ):
        """Test inviting a user to an organization (new invitation)."""
        email = "newuser@example.com"
        first_name = "John"
        last_name = "Doe"

        # Mock the transaction.on_commit so the notification fires synchronously
        with patch("organizations.services.transaction.on_commit") as mock_on_commit:
            mock_on_commit.side_effect = lambda func: func()

            # Mock the NotificationContextDict to avoid the tuple issue
            with patch("organizations.services.NotificationContextDict") as mock_context_dict:
                mock_context_dict.return_value = {
                    "organization_invitation_id": 1,
                    "invitation_url": "http://example.com/invitation/test-token/",
                }

                organization_service_with_mocks.invite_user_to_organization(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    invited_by=user,
                    organization=organization,
                )

        # Verify invitation was created
        from organizations.models import OrganizationInvitation

        invitation = OrganizationInvitation.objects.get(email=email, organization=organization)
        assert invitation.invited_by == user
        assert invitation.accepted_at is None
        assert invitation.membership is None
        assert invitation.expires_at > datetime.datetime.now(tz=datetime.UTC)
        assert invitation.token_hash is not None

        # Verify notification service was called
        mock_notification_service.create_one_off_notification.assert_called_once()
        call_args = mock_notification_service.create_one_off_notification.call_args
        assert call_args[1]["email_or_phone"] == email
        assert call_args[1]["first_name"] == first_name
        assert call_args[1]["last_name"] == last_name

    def test_invite_user_to_organization_existing_invitation(
        self, organization_service_with_mocks, user, organization, mock_notification_service
    ):
        """Test inviting a user who already has a pending invitation."""
        from organizations.models import OrganizationInvitation

        email = "existing@example.com"
        first_name = "Jane"
        last_name = "Smith"

        # Create an existing invitation
        old_token_hash = "old_hash"
        old_expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=1)
        existing_invitation = baker.make(
            OrganizationInvitation,
            email=email,
            organization=organization,
            invited_by=user,
            token_hash=old_token_hash,
            expires_at=old_expires_at,
            accepted_at=None,
            membership=None,
        )

        # Mock transaction.on_commit so the notification fires synchronously
        with patch("organizations.services.transaction.on_commit") as mock_on_commit:
            mock_on_commit.side_effect = lambda func: func()

            # Mock the NotificationContextDict to avoid the tuple issue
            with patch("organizations.services.NotificationContextDict") as mock_context_dict:
                mock_context_dict.return_value = {
                    "organization_invitation_id": existing_invitation.id,
                    "invitation_url": "http://example.com/invitation/test-token/",
                }

                organization_service_with_mocks.invite_user_to_organization(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    invited_by=user,
                    organization=organization,
                )  # Verify invitation was updated, not created new
        updated_invitation = OrganizationInvitation.objects.get(id=existing_invitation.id)
        assert updated_invitation.token_hash != old_token_hash
        assert updated_invitation.expires_at > old_expires_at
        assert updated_invitation.invited_by == user
        assert updated_invitation.accepted_at is None
        assert updated_invitation.membership is None

        # Verify only one invitation exists for this email/organization
        assert (
            OrganizationInvitation.objects.filter(email=email, organization=organization).count()
            == 1
        )

    def test_accept_invitation_valid_token(self, organization_service, user, organization):
        """Test accepting an invitation with a valid token."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )
        from organizations.models import OrganizationInvitation, OrganizationMembership

        # Create an invitation with a known token
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        invitation = baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # Accept the invitation
        membership = organization_service.accept_invitation(token=token, user=user)

        # Verify membership was created
        assert isinstance(membership, OrganizationMembership)
        assert membership.user == user
        assert membership.organization == organization

        # Verify invitation was updated
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert invitation.membership == membership

    def test_accept_invitation_propagates_admin_role(self, organization_service, organization):
        """BLOCKER fix: accept_invitation must honour the invitation's role.

        An ADMIN invitation accepted via the token/REST path must produce an
        OrganizationMembership with role=ADMIN, not the default MEMBER.
        """
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        admin_user = baker.make(User, email="admin_accept@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=admin_user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
            role=OrganizationRole.ADMIN,
        )

        membership = organization_service.accept_invitation(token=token, user=admin_user)

        assert membership.role == OrganizationRole.ADMIN

    def test_accept_invitation_member_role_default(self, organization_service, organization):
        """accept_invitation with MEMBER role (default) produces MEMBER membership."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        member_user = baker.make(User, email="member_accept@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=member_user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
            role=OrganizationRole.MEMBER,
        )

        membership = organization_service.accept_invitation(token=token, user=member_user)

        assert membership.role == OrganizationRole.MEMBER

    def test_accept_invitation_invalid_token(self, organization_service, user, organization):
        """Test accepting an invitation with an invalid token."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )
        from organizations.models import OrganizationInvitation

        # Create an invitation with a different token
        real_token = generate_long_lived_token()
        token_hash = hash_long_lived_token(real_token)
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # Try to accept with wrong token
        wrong_token = generate_long_lived_token()
        with pytest.raises(InvalidInvitationTokenError, match="Invalid or expired token"):
            organization_service.accept_invitation(token=wrong_token, user=user)

    def test_accept_invitation_expired_token(self, organization_service, user, organization):
        """Test accepting an invitation with an expired token."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )
        from organizations.models import OrganizationInvitation

        # Create an expired invitation
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1),
            accepted_at=None,
            membership=None,
        )

        # Try to accept expired invitation
        with pytest.raises(InvalidInvitationTokenError, match="Invalid or expired token"):
            organization_service.accept_invitation(token=token, user=user)

    def test_accept_invitation_no_matching_email(self, organization_service, organization):
        """Test accepting an invitation when user email doesn't match any invitations."""
        from common.utils.authentication_utils import generate_long_lived_token

        # Create a user with different email
        different_user = baker.make(User, email="different@example.com")

        # Try to accept with a random token
        token = generate_long_lived_token()
        with pytest.raises(InvalidInvitationTokenError, match="Invalid or expired token"):
            organization_service.accept_invitation(token=token, user=different_user)

    def test_revoke_invitation_existing_invitation(self, organization_service, user, organization):
        """Test revoking an existing invitation."""
        from organizations.models import OrganizationInvitation

        # Create an invitation
        invitation = baker.make(
            OrganizationInvitation,
            email="revoke@example.com",
            organization=organization,
            invited_by=user,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # Revoke the invitation
        organization_service.revoke_invitation(invitation_id=str(invitation.id))

        # Verify invitation was revoked (expires_at set to now or past)
        invitation.refresh_from_db()
        assert invitation.expires_at <= datetime.datetime.now(tz=datetime.UTC)

    def test_revoke_invitation_nonexistent_invitation(self, organization_service):
        """Test revoking a non-existent invitation."""
        fake_id = "999999"  # Use a string that can be converted to int

        with pytest.raises(InvitationNotFoundError, match="Invitation does not exist"):
            organization_service.revoke_invitation(invitation_id=fake_id)

    def test_accept_invitation_already_accepted(self, organization_service, organization):
        """Test accepting an invitation for a user who already has a membership.

        The hardened accept_invitation raises UserAlreadyHasMembershipError before
        attempting the DB create, so we get a typed error instead of a raw IntegrityError.
        """
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        # Create a unique user for this test to avoid membership conflicts
        test_user = baker.make(User, email="unique_accepted@example.com")

        # Create an already accepted invitation
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        membership = baker.make(OrganizationMembership, user=test_user, organization=organization)
        baker.make(
            OrganizationInvitation,
            email=test_user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=datetime.datetime.now(tz=datetime.UTC),
            membership=membership,
        )

        # The hardened path now raises UserAlreadyHasMembershipError, not IntegrityError.
        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.accept_invitation(token=token, user=test_user)

    # -----------------------------------------------------------------------
    # Tests for provision_tenant_for_user
    # -----------------------------------------------------------------------

    def _make_invitation(
        self,
        *,
        email: str,
        organization: Organization,
        invited_by: User,
        expired: bool = False,
        accepted: bool = False,
    ) -> OrganizationInvitation:
        """Helper: create an OrganizationInvitation for the given parameters."""
        now = datetime.datetime.now(tz=datetime.UTC)
        expires_at = (
            now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(days=7)
        )
        membership = (
            baker.make(OrganizationMembership, organization=organization) if accepted else None
        )
        return baker.make(
            OrganizationInvitation,
            email=email,
            organization=organization,
            invited_by=invited_by,
            expires_at=expires_at,
            accepted_at=now if accepted else None,
            membership=membership,
        )

    def test_provision_tenant_for_user_with_pending_invite(
        self, organization_service, organization
    ):
        """Branch (a): pending invite → MEMBER membership in inviting org, invitation marked accepted."""
        inviter = baker.make(User, email="inviter@example.com")
        invitee = baker.make(User, email="invitee@example.com")
        invitation = self._make_invitation(
            email=invitee.email, organization=organization, invited_by=inviter
        )

        membership = organization_service.provision_tenant_for_user(invitee)

        assert membership is not None
        assert membership.user == invitee
        assert membership.organization == organization
        assert membership.role == OrganizationRole.MEMBER

        # Invitation must be marked accepted and linked.
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert invitation.membership == membership

        # No new org should have been created.
        assert Organization.objects.count() == 1

    def test_provision_tenant_for_user_name_only_no_invite(self, organization_service):
        """Branch (b): no invite, name supplied → new org created with user as ADMIN."""
        user = baker.make(User, email="creator@example.com")

        membership = organization_service.provision_tenant_for_user(
            user, organization_name="My Org"
        )

        assert membership is not None
        assert membership.user == user
        assert membership.role == OrganizationRole.ADMIN
        assert membership.organization.name == "My Org"

    def test_provision_tenant_for_user_already_has_membership_no_invite_no_name_returns_none(
        self, organization_service, organization
    ):
        """Phase 4: user with a membership, no pending invite, no org name → returns None.

        The top-level blanket guard is gone.  With no pending invitation and no
        organization_name, the function falls through to the no-op branch and returns None.
        """
        user = baker.make(User, email="member@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)

        # Reload user from DB so the related manager cache is warm.
        user.refresh_from_db()

        result = organization_service.provision_tenant_for_user(user)
        assert result is None
        # Still exactly one membership — nothing was created.
        assert OrganizationMembership.objects.filter(user=user).count() == 1

    def test_provision_tenant_for_user_no_invite_no_name_returns_none(self, organization_service):
        """Branch (d): no invite, no name → returns None, no membership created."""
        user = baker.make(User, email="nobody@example.com")

        result = organization_service.provision_tenant_for_user(user)

        assert result is None
        assert not OrganizationMembership.objects.filter(user=user).exists()

    def test_provision_tenant_for_user_expired_invite_ignored(
        self, organization_service, organization
    ):
        """Branch (e): expired invitation is ignored; falls through to name/None branch."""
        inviter = baker.make(User, email="inviter2@example.com")
        invitee = baker.make(User, email="invitee2@example.com")
        self._make_invitation(
            email=invitee.email, organization=organization, invited_by=inviter, expired=True
        )

        # No name → should return None (not join via the expired invite).
        result = organization_service.provision_tenant_for_user(invitee)
        assert result is None
        assert not OrganizationMembership.objects.filter(user=invitee).exists()

    def test_accept_invitation_user_in_org_a_can_accept_invite_to_org_b(
        self, organization_service, organization
    ):
        """Phase 4 / Use-case 6: a user already in org A can accept an invitation to org B.

        The old blanket guard ("any membership → refuse") is relaxed.  The per-org guard
        only refuses a duplicate in the SAME organization.  Accepting into a DIFFERENT org
        must succeed and yield a second OrganizationMembership.
        """
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        user = baker.make(User, email="already_member@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)
        user.refresh_from_db()

        # Valid invitation to a DIFFERENT org.
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        other_org = baker.make(Organization, name="Other Org")
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=other_org,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # Phase 4: this must SUCCEED — user joins other_org as their second org.
        membership = organization_service.accept_invitation(token=token, user=user)
        assert membership.user == user
        assert membership.organization == other_org
        assert OrganizationMembership.objects.filter(user=user).count() == 2

    def test_accept_invitation_same_org_duplicate_raises(self, organization_service, organization):
        """Phase 4: accepting an invitation to an org the user already belongs to raises UserAlreadyHasMembershipError."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        user = baker.make(User, email="same_org_member@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)
        user.refresh_from_db()

        # Valid invitation to the SAME org the user is already in.
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.accept_invitation(token=token, user=user)

    def test_provision_tenant_for_user_already_member_guard(
        self, organization_service, organization
    ):
        """Integration: the hasattr guard fires on the second sequential call.

        Two sequential calls on the same user verify that the upfront hasattr check
        raises UserAlreadyHasMembershipError before any DB write on the second attempt.
        This exercises the guard path but NOT the except-IntegrityError backstop — see
        the dedicated backstop tests below.
        """
        user = baker.make(User, email="concurrent@example.com")

        first_membership = organization_service.provision_tenant_for_user(
            user, organization_name="Concurrent Org"
        )
        assert first_membership is not None

        # Reload user so the related-manager attribute is fresh.
        user.refresh_from_db()

        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.provision_tenant_for_user(user, organization_name="Second Org")

        # Exactly one membership in the DB.
        assert OrganizationMembership.objects.filter(user=user).count() == 1

    # -----------------------------------------------------------------------
    # Backstop tests: IntegrityError → UserAlreadyHasMembershipError
    # These tests genuinely exercise the except IntegrityError path by mocking
    # OrganizationMembership.objects.create to raise django.db.IntegrityError.
    # They would FAIL if the import were psycopg.IntegrityError instead of the
    # Django one, proving that the correct class is caught.
    # -----------------------------------------------------------------------

    def test_provision_tenant_for_user_invite_branch_integrity_error_backstop(
        self, organization_service, organization
    ):
        """Backstop: IntegrityError from create() on the invite branch → UserAlreadyHasMembershipError.

        Simulates a race-condition duplicate-key error that bypasses the upfront hasattr guard
        (e.g. concurrent requests both passed the guard before either committed).
        """
        from django.db import IntegrityError as DjangoIntegrityError

        inviter = baker.make(User, email="inviter_backstop@example.com")
        invitee = baker.make(User, email="invitee_backstop@example.com")
        self._make_invitation(email=invitee.email, organization=organization, invited_by=inviter)

        with patch.object(
            OrganizationMembership.objects,
            "create",
            side_effect=DjangoIntegrityError("duplicate key"),
        ):
            with pytest.raises(UserAlreadyHasMembershipError):
                organization_service.provision_tenant_for_user(invitee)

    def test_provision_tenant_for_user_name_branch_integrity_error_backstop(
        self, organization_service
    ):
        """Backstop: IntegrityError from create() on the name-only branch → UserAlreadyHasMembershipError.

        Simulates a race where create_organization raises IntegrityError (duplicate membership).
        """
        from django.db import IntegrityError as DjangoIntegrityError

        user = baker.make(User, email="race_org_creator@example.com")

        with patch.object(
            OrganizationMembership.objects,
            "create",
            side_effect=DjangoIntegrityError("duplicate key"),
        ):
            with pytest.raises(UserAlreadyHasMembershipError):
                organization_service.provision_tenant_for_user(user, organization_name="Race Org")

    def test_provision_tenant_for_user_case_insensitive_email_match(
        self, organization_service, organization
    ):
        """Regression: invitation email with different-case local part matches user.email via iexact.

        User.email domain is normalized (lowercased) on save, but the local part is not.
        An invitation stored as 'Recruit@example.com' must match a user whose email is
        'recruit@example.com' — the filter must use email__iexact, not email=.
        """
        inviter = baker.make(User, email="inviter_case@example.com")
        # Invitation stored with mixed-case local part.
        invitee = baker.make(User, email="recruit@example.com")
        invitation = self._make_invitation(
            email="Recruit@example.com",
            organization=organization,
            invited_by=inviter,
        )

        membership = organization_service.provision_tenant_for_user(invitee)

        assert membership is not None
        assert membership.user == invitee
        assert membership.organization == organization
        assert membership.role == OrganizationRole.MEMBER

        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert invitation.membership == membership

    def test_accept_invitation_integrity_error_backstop(self, organization_service, organization):
        """Backstop: IntegrityError from create() in accept_invitation → UserAlreadyHasMembershipError.

        Simulates a race where the user gets a membership between the hasattr check and the
        DB create call. Under ATOMIC_REQUESTS the savepoint protects the outer transaction.
        """
        from django.db import IntegrityError as DjangoIntegrityError

        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        user = baker.make(User, email="accept_race@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        with patch.object(
            OrganizationMembership.objects,
            "create",
            side_effect=DjangoIntegrityError("duplicate key"),
        ):
            with pytest.raises(UserAlreadyHasMembershipError):
                organization_service.accept_invitation(token=token, user=user)

    # -----------------------------------------------------------------------
    # Savepoint hygiene tests (FIX 1)
    # Prove that the transaction is NOT left poisoned after the IntegrityError
    # backstop fires in provision_tenant_for_user.  The savepoint wrapping in
    # the invite branch and the name branch must roll back only the inner
    # savepoint, leaving the outer @transaction.atomic() transaction usable.
    # -----------------------------------------------------------------------

    def test_provision_tenant_invite_branch_transaction_not_poisoned_after_integrity_error(
        self, organization_service, organization
    ):
        """After IntegrityError backstop on invite branch, the transaction is NOT poisoned.

        A savepoint (via ``with transaction.atomic():`` inside the try) rolls back
        cleanly on IntegrityError.  The outer transaction must remain usable —
        a subsequent DB query must succeed without raising
        TransactionManagementError or InternalError.
        """
        from django.db import IntegrityError as DjangoIntegrityError

        inviter = baker.make(User, email="inviter_sp@example.com")
        invitee = baker.make(User, email="invitee_sp@example.com")
        self._make_invitation(email=invitee.email, organization=organization, invited_by=inviter)

        with patch.object(
            OrganizationMembership.objects,
            "create",
            side_effect=DjangoIntegrityError("duplicate key"),
        ):
            with pytest.raises(UserAlreadyHasMembershipError):
                organization_service.provision_tenant_for_user(invitee)

        # The transaction must still be usable — this query must NOT raise
        # TransactionManagementError or InternalError.
        count = Organization.objects.count()
        assert count >= 1, "DB query after backstop must succeed (transaction not poisoned)"

    def test_provision_tenant_name_branch_transaction_not_poisoned_after_integrity_error(
        self, organization_service
    ):
        """After IntegrityError backstop on name branch, the transaction is NOT poisoned.

        Same as the invite-branch savepoint test but for the name-only branch,
        where ``create_organization`` is wrapped in ``with transaction.atomic():``.
        """
        from django.db import IntegrityError as DjangoIntegrityError

        user = baker.make(User, email="race_sp_creator@example.com")

        with patch.object(
            OrganizationMembership.objects,
            "create",
            side_effect=DjangoIntegrityError("duplicate key"),
        ):
            with pytest.raises(UserAlreadyHasMembershipError):
                organization_service.provision_tenant_for_user(user, organization_name="SP Org")

        # The transaction must still be usable — this query must NOT raise
        # TransactionManagementError or InternalError.
        count = Organization.objects.count()
        assert count >= 0, "DB query after backstop must succeed (transaction not poisoned)"

    # -----------------------------------------------------------------------
    # Phase 4 — Multi-org invite accept (Use-case 6)
    # -----------------------------------------------------------------------

    def test_provision_pending_invite_org_b_user_already_in_org_a(
        self, organization_service, organization
    ):
        """Phase 4: pending invite for org B + user already in org A → creates org-B membership.

        The per-org guard on the invite branch only fires when the user is already in the
        INVITATION's org.  A user already in org A joining org B via a pending invite must
        succeed.
        """
        user = baker.make(User, email="multi_org_invite@example.com")
        # User is already a member of org A (the fixture `organization`).
        baker.make(OrganizationMembership, user=user, organization=organization)
        user.refresh_from_db()

        # A pending invitation from org B (a different org).
        org_b = baker.make(Organization, name="Org B")
        inviter = baker.make(User, email="inviter_orb@example.com")
        self._make_invitation(email=user.email, organization=org_b, invited_by=inviter)

        membership = organization_service.provision_tenant_for_user(user)

        assert membership is not None
        assert membership.user == user
        assert membership.organization == org_b
        assert membership.role == OrganizationRole.MEMBER
        # User now has TWO memberships.
        assert OrganizationMembership.objects.filter(user=user).count() == 2

    def test_provision_pending_invite_same_org_raises(self, organization_service, organization):
        """Phase 4: pending invite for org A + user already in org A → UserAlreadyHasMembershipError."""
        user = baker.make(User, email="same_org_invite@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)
        user.refresh_from_db()

        inviter = baker.make(User, email="inviter_same@example.com")
        self._make_invitation(email=user.email, organization=organization, invited_by=inviter)

        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.provision_tenant_for_user(user)

    def test_provision_organization_name_user_already_has_membership_raises(
        self, organization_service, organization
    ):
        """Phase 4: organization_name branch retains the blanket guard — no second-org auto-create.

        Creating an additional org on the signup path is Phase 5's concern.  A user with
        an existing membership supplying organization_name must still be refused.
        """
        user = baker.make(User, email="has_org_name@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)
        user.refresh_from_db()

        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.provision_tenant_for_user(user, organization_name="New Org")

        # No new org or membership was created.
        assert OrganizationMembership.objects.filter(user=user).count() == 1

    def test_concurrent_orgs_can_both_invite_same_email_coexist(self, organization):
        """Phase 4: two orgs can each have a pending invitation for the same email simultaneously.

        The relaxed unique constraint ``uniq_invitation_email_organization`` allows the
        same email to appear once per org — both rows must coexist without IntegrityError.
        """
        org_b = baker.make(Organization, name="Org B")
        inviter = baker.make(User, email="dual_inviter@example.com")
        email = "target@example.com"

        baker.make(
            OrganizationInvitation,
            email=email,
            organization=organization,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )
        baker.make(
            OrganizationInvitation,
            email=email,
            organization=org_b,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # Both invitation rows coexist — the constraint only prevents duplicates per org.
        assert OrganizationInvitation.objects.filter(email=email).count() == 2

    def test_invite_user_to_organization_second_call_resets_not_duplicates(
        self, organization_service_with_mocks, user, organization
    ):
        """Finding 4: calling invite_user_to_organization twice for the SAME (email, org) resets
        the existing invitation (new token/expiry), rather than creating a duplicate row.

        Verifies the get_or_create path: created=False means the row already existed and
        was updated in-place. The count must remain 1 (no duplicate row).
        """
        email = "reset_invite@example.com"

        with (
            patch("organizations.services.transaction.on_commit") as mock_on_commit,
            patch("organizations.services.NotificationContextDict"),
        ):
            mock_on_commit.side_effect = lambda func: func()

            first_invitation = organization_service_with_mocks.invite_user_to_organization(
                email=email,
                first_name="Reset",
                last_name="Me",
                invited_by=user,
                organization=organization,
            )
            first_token_hash = first_invitation.token_hash
            first_expires_at = first_invitation.expires_at

            second_invitation = organization_service_with_mocks.invite_user_to_organization(
                email=email,
                first_name="Reset",
                last_name="Me",
                invited_by=user,
                organization=organization,
            )

        # Same row, not a new one.
        assert first_invitation.pk == second_invitation.pk
        assert (
            OrganizationInvitation.objects.filter(email=email, organization=organization).count()
            == 1
        )

        # Token and expiry must be regenerated on the second call.
        second_invitation.refresh_from_db()
        assert second_invitation.token_hash != first_token_hash, (
            "token must be rotated on re-invite"
        )
        assert second_invitation.expires_at > first_expires_at, (
            "expires_at must be extended on re-invite"
        )

    def test_invite_user_same_email_different_orgs_allowed(
        self, organization_service_with_mocks, user, organization
    ):
        """Finding 4: the same email can be invited to two different orgs — one row per org.

        The per-org unique constraint ``uniq_invitation_email_organization`` allows (email,
        org_A) and (email, org_B) to coexist. This test confirms invite_user_to_organization
        does not blow up with DuplicateInvitationError on the second org.
        """
        email = "multi_org_invitee@example.com"
        org_b = baker.make(Organization, name="Other Org")

        with (
            patch("organizations.services.transaction.on_commit") as mock_on_commit,
            patch("organizations.services.NotificationContextDict"),
        ):
            mock_on_commit.side_effect = lambda func: func()

            organization_service_with_mocks.invite_user_to_organization(
                email=email,
                first_name="Multi",
                last_name="Org",
                invited_by=user,
                organization=organization,
            )
            organization_service_with_mocks.invite_user_to_organization(
                email=email,
                first_name="Multi",
                last_name="Org",
                invited_by=user,
                organization=org_b,
            )

        assert OrganizationInvitation.objects.filter(email=email).count() == 2
        assert (
            OrganizationInvitation.objects.filter(email=email, organization=organization).count()
            == 1
        )
        assert OrganizationInvitation.objects.filter(email=email, organization=org_b).count() == 1

    def test_accept_invitation_inactive_membership_blocks_re_accept(
        self, organization_service, organization
    ):
        """Finding 3: the per-org guard does NOT filter on is_active, so an inactive membership
        in the inviting org still blocks re-accept (prevents a silent second-row scenario where
        a deactivated user re-accepts an invitation to regain access).

        If someone later adds is_active=True to the filter, this test turns red.
        """
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        user = baker.make(User, email="inactive_block@example.com")
        # An inactive (deactivated) membership in the SAME org as the invitation.
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=False)
        user.refresh_from_db()

        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # The guard checks membership(organization=...) without is_active=True, so even an
        # inactive membership blocks the accept.
        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.accept_invitation(token=token, user=user)

        # No second membership row was created.
        assert OrganizationMembership.objects.filter(user=user).count() == 1

    def test_provision_tenant_inactive_membership_blocks_same_org_invite(
        self, organization_service, organization
    ):
        """Finding 3 (provision_tenant level): inactive membership in the inviting org blocks
        provision_tenant_for_user just as accept_invitation does — no second row is created.
        """
        user = baker.make(User, email="inactive_provision@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=False)
        user.refresh_from_db()

        inviter = baker.make(User, email="inviter_inactive@example.com")
        self._make_invitation(email=user.email, organization=organization, invited_by=inviter)

        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.provision_tenant_for_user(user)

        assert OrganizationMembership.objects.filter(user=user).count() == 1

    # -----------------------------------------------------------------------
    # Phase 2 — organization_member_created webhook emission on accept_invitation
    # -----------------------------------------------------------------------

    @pytest.fixture
    def mock_webhook_membership_side_effects_service(self):
        """Return a MagicMock that stands in for WebhookMembershipSideEffectsService."""
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.on_member_created.return_value = None
        return mock

    @pytest.fixture
    def organization_service_with_webhook_mock(
        self, mock_calendar_service, mock_webhook_membership_side_effects_service
    ):
        """OrganizationService with both calendar and webhook side-effects mocked."""
        from di_core.containers import container

        with (
            container.calendar_service.override(mock_calendar_service),
            container.webhook_membership_side_effects_service.override(
                mock_webhook_membership_side_effects_service
            ),
        ):
            yield OrganizationService()

    def test_accept_invitation_calls_on_member_created_for_active_membership(
        self,
        organization_service_with_webhook_mock,
        mock_webhook_membership_side_effects_service,
        organization,
    ):
        """Integration: accepting a valid invitation calls on_member_created with the created membership."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        invitee = baker.make(User, email="webhook_invitee@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=invitee.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
            role=OrganizationRole.MEMBER,
        )

        membership = organization_service_with_webhook_mock.accept_invitation(
            token=token, user=invitee
        )

        mock_webhook_membership_side_effects_service.on_member_created.assert_called_once_with(
            membership
        )
        # The created membership must be active (default).
        assert membership.is_active is True

    def test_accept_invitation_webhook_payload_carries_correct_role(
        self,
        organization_service_with_webhook_mock,
        mock_webhook_membership_side_effects_service,
        organization,
    ):
        """Integration: the membership passed to on_member_created has the invitation's role."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        admin_invitee = baker.make(User, email="webhook_admin_invitee@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=admin_invitee.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
            role=OrganizationRole.ADMIN,
        )

        membership = organization_service_with_webhook_mock.accept_invitation(
            token=token, user=admin_invitee
        )

        call_args = mock_webhook_membership_side_effects_service.on_member_created.call_args
        passed_membership = call_args[0][0]
        assert passed_membership == membership
        assert passed_membership.role == OrganizationRole.ADMIN

    def test_accept_invitation_no_webhook_emission_when_no_subscribed_config(
        self,
        organization,
        django_capture_on_commit_callbacks,
    ):
        """Integration: no WebhookEvent rows when no WebhookConfiguration subscribes to the event type.

        This test exercises the full stack down to WebhookService.send_event — no mock —
        and asserts that accepting an invitation with no matching configuration produces
        zero WebhookEvent rows.
        """
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )
        from di_core.containers import container
        from webhooks.models import WebhookEvent

        invitee = baker.make(User, email="no_config_invitee@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=invitee.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        from unittest.mock import patch

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                service.accept_invitation(token=token, user=invitee)

        assert WebhookEvent.objects.filter(organization=organization).count() == 0

    def test_accept_invitation_webhook_emission_with_subscribed_config(
        self,
        organization,
        django_capture_on_commit_callbacks,
    ):
        """Integration: exactly one WebhookEvent row per subscribed config on invitation-accept.

        Creates a WebhookConfiguration for ORGANIZATION_MEMBER_CREATED, accepts an
        invitation that creates an active membership, and asserts that exactly one
        WebhookEvent row exists with the correct payload and organization scope.
        """
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )
        from di_core.containers import container
        from webhooks.constants import WebhookEventType
        from webhooks.models import WebhookConfiguration, WebhookEvent

        invitee = baker.make(User, email="with_config_invitee@example.com")
        token = generate_long_lived_token()
        token_hash = hash_long_lived_token(token)
        baker.make(
            OrganizationInvitation,
            email=invitee.email,
            organization=organization,
            token_hash=token_hash,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
            role=OrganizationRole.MEMBER,
        )
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
            url="https://example.com/webhook",
            headers={},
            deleted_at=None,
        )

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        from unittest.mock import patch

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                membership = service.accept_invitation(token=token, user=invitee)

        events = WebhookEvent.objects.filter(
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
        )
        assert events.count() == 1

        event = events.first()
        assert event is not None
        assert event.payload["user_id"] == invitee.id
        assert event.payload["email"] == invitee.email
        assert event.payload["organization_id"] == organization.id
        assert event.payload["organization_name"] == organization.name
        assert event.payload["membership_role"] == OrganizationRole.MEMBER
        assert event.payload["membership_id"] == membership.id

    # -----------------------------------------------------------------------
    # Phase 3 — organization_member_created webhook emission on create_organization
    # -----------------------------------------------------------------------

    def test_create_organization_calls_on_member_created_for_admin_membership(
        self,
        organization_service_with_webhook_mock,
        mock_webhook_membership_side_effects_service,
    ):
        """Integration: creating an organization calls on_member_created with the admin membership."""
        creator = baker.make(User, email="org_creator_webhook@example.com")

        organization_service_with_webhook_mock.create_organization(
            creator=creator, name="Webhook Test Org"
        )

        mock_webhook_membership_side_effects_service.on_member_created.assert_called_once()
        passed_membership = (
            mock_webhook_membership_side_effects_service.on_member_created.call_args[0][0]
        )
        assert passed_membership.user == creator
        assert passed_membership.role == OrganizationRole.ADMIN
        assert passed_membership.is_active is True

    def test_create_organization_no_webhook_emission_when_no_subscribed_config(
        self,
        django_capture_on_commit_callbacks,
    ):
        """Integration: no WebhookEvent rows when no WebhookConfiguration subscribes to the event type.

        Exercises the full stack (no mocks on the webhook path) to confirm that creating an
        organization with no matching config produces zero WebhookEvent rows.
        """
        from unittest.mock import patch

        from di_core.containers import container
        from webhooks.models import WebhookEvent

        creator = baker.make(User, email="no_config_creator@example.com")

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                org = service.create_organization(creator=creator, name="No Config Org")

        assert WebhookEvent.objects.filter(organization=org).count() == 0

    def test_create_organization_webhook_emission_payload_and_role(
        self,
        django_capture_on_commit_callbacks,
    ):
        """Integration: on_commit fires send_event with membership_role='admin' and correct payload.

        Since the org does not exist until create_organization runs, we cannot pre-create a
        WebhookConfiguration for it. Instead we intercept WebhookService.send_event to capture
        the call and assert the payload without needing a config row. The no-config gate is
        already covered by test_create_organization_no_webhook_emission_when_no_subscribed_config.
        """
        from unittest.mock import patch

        from di_core.containers import container
        from webhooks.constants import WebhookEventType

        creator = baker.make(User, email="with_config_creator@example.com")

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        captured_calls: list[dict] = []

        def fake_send_event(self_svc, organization, event_type, payload):
            captured_calls.append(
                {"organization": organization, "event_type": event_type, "payload": payload}
            )

        with patch("webhooks.services.webhook_service.WebhookService.send_event", fake_send_event):
            with django_capture_on_commit_callbacks(execute=True):
                org = service.create_organization(creator=creator, name="With Config Org")

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["organization"] == org
        assert call["event_type"] == WebhookEventType.ORGANIZATION_MEMBER_CREATED
        assert call["payload"]["user_id"] == creator.id
        assert call["payload"]["email"] == creator.email
        assert call["payload"]["organization_id"] == org.id
        assert call["payload"]["organization_name"] == org.name
        assert call["payload"]["membership_role"] == OrganizationRole.ADMIN

    # -----------------------------------------------------------------------
    # Phase 4 — provision_tenant_for_user pending-invitation branch + multi-org
    # -----------------------------------------------------------------------

    def test_provision_pending_invite_calls_on_member_created(
        self,
        organization_service_with_webhook_mock,
        mock_webhook_membership_side_effects_service,
        organization,
    ):
        """Integration: provision via pending-invitation calls on_member_created exactly once."""
        user = baker.make(User, email="provision_webhook_invitee@example.com")
        inviter = baker.make(User, email="provision_webhook_inviter@example.com")
        self._make_invitation(email=user.email, organization=organization, invited_by=inviter)

        membership = organization_service_with_webhook_mock.provision_tenant_for_user(user)

        assert membership is not None
        mock_webhook_membership_side_effects_service.on_member_created.assert_called_once_with(
            membership
        )
        assert membership.is_active is True

    def test_provision_pending_invite_emits_member_role_event(
        self,
        organization,
        django_capture_on_commit_callbacks,
    ):
        """Integration: provision via pending-invitation emits exactly one ORGANIZATION_MEMBER_CREATED
        event scoped to the invitation's org, with membership_role=MEMBER."""
        from unittest.mock import Mock, patch

        from di_core.containers import container
        from webhooks.constants import WebhookEventType
        from webhooks.models import WebhookConfiguration, WebhookEvent

        invitee = baker.make(User, email="provision_with_config_invitee@example.com")
        inviter = baker.make(User, email="provision_with_config_inviter@example.com")
        self._make_invitation(email=invitee.email, organization=organization, invited_by=inviter)
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
            url="https://example.com/webhook",
            headers={},
            deleted_at=None,
        )

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                membership = service.provision_tenant_for_user(invitee)

        assert membership is not None
        events = WebhookEvent.objects.filter(
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
        )
        assert events.count() == 1
        event = events.first()
        assert event is not None
        assert event.payload["user_id"] == invitee.id
        assert event.payload["email"] == invitee.email
        assert event.payload["organization_id"] == organization.id
        assert event.payload["membership_role"] == OrganizationRole.MEMBER
        assert event.payload["membership_id"] == membership.id

    def test_provision_org_creation_emits_exactly_once_not_twice(
        self,
        organization_service_with_webhook_mock,
        mock_webhook_membership_side_effects_service,
    ):
        """Integration: provision via org-creation delegates to create_organization which already
        emits — on_member_created must be called exactly once, not twice.

        Guards against double-emission across provision_tenant_for_user → create_organization.
        """
        creator = baker.make(User, email="provision_org_no_double_emit@example.com")

        organization_service_with_webhook_mock.provision_tenant_for_user(
            creator, organization_name="Double Emit Guard Org"
        )

        # Must be called exactly once (from create_organization) — the org-creation branch in
        # provision_tenant_for_user does NOT add a second call.
        mock_webhook_membership_side_effects_service.on_member_created.assert_called_once()
        passed_membership = (
            mock_webhook_membership_side_effects_service.on_member_created.call_args[0][0]
        )
        assert passed_membership.role == OrganizationRole.ADMIN
        assert passed_membership.is_active is True

    def test_provision_multi_org_emits_to_second_org_only(
        self,
        organization,
        django_capture_on_commit_callbacks,
    ):
        """Integration: a user already active in org A who provisions into org B via pending invitation
        produces exactly one delivery scoped to org B's config — zero deliveries to org A."""
        from unittest.mock import Mock, patch

        from di_core.containers import container
        from webhooks.constants import WebhookEventType
        from webhooks.models import WebhookConfiguration, WebhookEvent

        user = baker.make(User, email="multi_org_provision@example.com")
        # User already has an active membership in org A (fixture `organization`).
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=True)
        user.refresh_from_db()

        # Subscribe org A to webhook events — we expect ZERO deliveries to it.
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
            url="https://org-a.example.com/webhook",
            headers={},
            deleted_at=None,
        )

        # Org B has a pending invitation for the user.
        org_b = baker.make(Organization, name="Org B Multi")
        inviter = baker.make(User, email="inviter_multi_orb@example.com")
        self._make_invitation(email=user.email, organization=org_b, invited_by=inviter)

        # Subscribe org B to webhook events — we expect exactly ONE delivery here.
        baker.make(
            WebhookConfiguration,
            organization=org_b,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
            url="https://org-b.example.com/webhook",
            headers={},
            deleted_at=None,
        )

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        with patch("webhooks.services.webhook_service.process_webhook_event.delay"):
            with django_capture_on_commit_callbacks(execute=True):
                membership = service.provision_tenant_for_user(user)

        assert membership is not None
        assert membership.organization == org_b

        # Exactly one event scoped to org B.
        org_b_events = WebhookEvent.objects.filter(
            organization=org_b,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
        )
        assert org_b_events.count() == 1

        # Zero events scoped to org A.
        org_a_events = WebhookEvent.objects.filter(
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
        )
        assert org_a_events.count() == 0

        # Total events across both orgs is exactly one (org B only).
        total_events = org_b_events.count() + org_a_events.count()
        assert total_events == 1, (
            f"Expected exactly 1 total ORGANIZATION_MEMBER_CREATED event, got {total_events}"
        )

    def test_provision_multi_org_emit_count_exactly_once(
        self,
        organization,
        django_capture_on_commit_callbacks,
    ):
        """Integration: send_event is called exactly once when user with existing org A membership
        provisions into org B via pending invitation — guards against any double-call."""
        from unittest.mock import Mock, patch

        from di_core.containers import container
        from webhooks.constants import WebhookEventType

        user = baker.make(User, email="multi_org_count_check@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=True)
        user.refresh_from_db()

        org_b = baker.make(Organization, name="Org B Count")
        inviter = baker.make(User, email="inviter_count@example.com")
        self._make_invitation(email=user.email, organization=org_b, invited_by=inviter)

        with container.calendar_service.override(Mock()):
            service = OrganizationService()

        captured_calls: list[dict] = []

        def fake_send_event(self_svc, organization, event_type, payload):
            captured_calls.append(
                {"organization": organization, "event_type": event_type, "payload": payload}
            )

        with patch("webhooks.services.webhook_service.WebhookService.send_event", fake_send_event):
            with django_capture_on_commit_callbacks(execute=True):
                membership = service.provision_tenant_for_user(user)

        assert membership is not None
        member_created_calls = [
            c
            for c in captured_calls
            if c["event_type"] == WebhookEventType.ORGANIZATION_MEMBER_CREATED
        ]
        assert len(member_created_calls) == 1, (
            f"Expected exactly 1 send_event call for ORGANIZATION_MEMBER_CREATED, "
            f"got {len(member_created_calls)}"
        )
        assert member_created_calls[0]["organization"] == org_b


@pytest.mark.django_db
class TestRequestAllCalendarsSync:
    """OrganizationService.request_all_calendars_sync — owner-account fan-out."""

    @pytest.fixture
    def mock_calendar_service(self):
        mock_service = Mock()
        mock_service.authenticate.return_value = None
        # A non-None return means the sync was enqueued; None now signals the
        # calendar has sync disabled and is reported under "skipped".
        mock_service.request_calendar_sync.return_value = Mock()
        return mock_service

    @pytest.fixture
    def organization_service(self, mock_calendar_service):
        from di_core.containers import container

        with container.calendar_service.override(mock_calendar_service):
            yield OrganizationService()

    def _make_calendar(self, organization, **overrides):
        defaults = {
            "organization": organization,
            "name": "Cal",
            "provider": CalendarProvider.GOOGLE,
            "calendar_type": CalendarType.PERSONAL,
            "visibility": CalendarVisibility.ACTIVE,
        }
        defaults.update(overrides)
        return baker.make(Calendar, **defaults)

    def test_syncs_calendars_with_owner_and_social_account(
        self, organization_service, mock_calendar_service
    ):
        org = baker.make(Organization, name="Sync Org")
        admin = baker.make(User, email="admin-sync@example.com")
        owner = baker.make(User, email="owner@example.com")

        calendar = self._make_calendar(org, external_id="cal-1")
        baker.make(
            CalendarOwnership,
            organization=org,
            calendar=calendar,
            user=owner,
            is_default=True,
        )
        SocialAccount.objects.create(user=owner, provider=CalendarProvider.GOOGLE)

        start = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 6, 30, tzinfo=datetime.UTC)
        result = organization_service.request_all_calendars_sync(
            organization=org,
            requested_by=admin,
            start_datetime=start,
            end_datetime=end,
            should_update_events=True,
        )

        assert result == {"synced": [calendar.id], "skipped": []}
        mock_calendar_service.authenticate.assert_called_once()
        _, auth_kwargs = mock_calendar_service.authenticate.call_args
        assert auth_kwargs["account"].user_id == owner.id
        assert auth_kwargs["organization"].id == org.id
        mock_calendar_service.request_calendar_sync.assert_called_once_with(
            calendar=calendar,
            start_datetime=start,
            end_datetime=end,
            should_update_events=True,
            trigger_source=CalendarSyncTriggerSource.ADMIN,
        )

    def test_skips_calendar_without_owner(self, organization_service, mock_calendar_service):
        org = baker.make(Organization, name="No Owner Org")
        admin = baker.make(User, email="admin-noowner@example.com")
        calendar = self._make_calendar(org, external_id="cal-noowner")

        result = organization_service.request_all_calendars_sync(
            organization=org,
            requested_by=admin,
            start_datetime=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
            end_datetime=datetime.datetime(2026, 6, 30, tzinfo=datetime.UTC),
        )

        assert result["synced"] == []
        assert result["skipped"] == [{"calendar_id": calendar.id, "reason": "no owner"}]
        mock_calendar_service.request_calendar_sync.assert_not_called()

    def test_skips_calendar_without_linked_account(
        self, organization_service, mock_calendar_service
    ):
        org = baker.make(Organization, name="No Link Org")
        admin = baker.make(User, email="admin-nolink@example.com")
        owner = baker.make(User, email="owner-nolink@example.com")
        calendar = self._make_calendar(org, external_id="cal-nolink")
        baker.make(
            CalendarOwnership,
            organization=org,
            calendar=calendar,
            user=owner,
            is_default=True,
        )
        # No SocialAccount created for the owner.

        result = organization_service.request_all_calendars_sync(
            organization=org,
            requested_by=admin,
            start_datetime=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
            end_datetime=datetime.datetime(2026, 6, 30, tzinfo=datetime.UTC),
        )

        assert result["synced"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["calendar_id"] == calendar.id
        assert "no linked" in result["skipped"][0]["reason"]
        mock_calendar_service.request_calendar_sync.assert_not_called()

    def test_empty_organization_returns_empty_summary(
        self, organization_service, mock_calendar_service
    ):
        org = baker.make(Organization, name="Empty Org")
        admin = baker.make(User, email="admin-empty@example.com")

        result = organization_service.request_all_calendars_sync(
            organization=org,
            requested_by=admin,
            start_datetime=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
            end_datetime=datetime.datetime(2026, 6, 30, tzinfo=datetime.UTC),
        )

        assert result == {"synced": [], "skipped": []}
        mock_calendar_service.request_calendar_sync.assert_not_called()

    def test_inactive_calendar_excluded(self, organization_service, mock_calendar_service):
        org = baker.make(Organization, name="Inactive Cal Org")
        admin = baker.make(User, email="admin-inactive@example.com")
        owner = baker.make(User, email="owner-inactive@example.com")
        calendar = self._make_calendar(
            org, external_id="cal-inactive", visibility=CalendarVisibility.INACTIVE
        )
        baker.make(
            CalendarOwnership,
            organization=org,
            calendar=calendar,
            user=owner,
            is_default=True,
        )
        SocialAccount.objects.create(user=owner, provider=CalendarProvider.GOOGLE)

        result = organization_service.request_all_calendars_sync(
            organization=org,
            requested_by=admin,
            start_datetime=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
            end_datetime=datetime.datetime(2026, 6, 30, tzinfo=datetime.UTC),
        )

        assert result == {"synced": [], "skipped": []}
        mock_calendar_service.request_calendar_sync.assert_not_called()
