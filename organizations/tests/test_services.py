import datetime
from unittest.mock import Mock, patch

from django.db.utils import IntegrityError

import pytest
from allauth.socialaccount.models import SocialAccount
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
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
        """Test that multiple calls to create_organization work correctly."""
        # Create first organization
        org1 = organization_service.create_organization(
            creator=user, name="Organization 1", should_sync_rooms=False
        )

        # Verify first organization was created successfully
        assert org1.name == "Organization 1"
        assert org1.should_sync_rooms is False
        # Store the ID before attempting the second call
        org1_id = org1.id

        # Create second organization with same user - this should fail due to unique constraint
        # since OrganizationMembership has a unique constraint on user_id
        with pytest.raises(IntegrityError):
            organization_service.create_organization(
                creator=user, name="Organization 2", should_sync_rooms=True
            )

        # The transaction is broken after the IntegrityError, so we need to verify in a way
        # that doesn't require a new query. The first organization should still exist conceptually
        # even though we can't query for it due to the broken transaction.
        assert org1.id == org1_id

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

        # Mock the transaction.on_commit and URL generation
        with (
            patch("organizations.services.transaction.on_commit") as mock_on_commit,
            patch("organizations.services.reverse") as mock_reverse,
            patch("organizations.services.build_absolute_uri") as mock_build_absolute_uri,
        ):
            mock_on_commit.side_effect = lambda func: func()
            mock_reverse.return_value = "/invitation/test-token/"
            mock_build_absolute_uri.return_value = "http://example.com/invitation/test-token/"

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

        # Mock transaction.on_commit and URL generation
        with (
            patch("organizations.services.transaction.on_commit") as mock_on_commit,
            patch("organizations.services.reverse") as mock_reverse,
            patch("organizations.services.build_absolute_uri") as mock_build_absolute_uri,
        ):
            mock_on_commit.side_effect = lambda func: func()
            mock_reverse.return_value = "/invitation/test-token/"
            mock_build_absolute_uri.return_value = "http://example.com/invitation/test-token/"

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

    def test_provision_tenant_for_user_already_has_membership(
        self, organization_service, organization
    ):
        """Branch (c): user already has a membership → raises UserAlreadyHasMembershipError."""
        user = baker.make(User, email="member@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)

        # Reload user from DB so the related manager cache is warm.
        user.refresh_from_db()

        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.provision_tenant_for_user(user)

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

    def test_accept_invitation_raises_for_user_with_existing_membership(
        self, organization_service, organization
    ):
        """accept_invitation raises UserAlreadyHasMembershipError when user already has a membership."""
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        user = baker.make(User, email="already_member@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)
        user.refresh_from_db()

        # Create a valid (non-expired) invitation for the same user.
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


@pytest.mark.django_db
class TestRequestAllCalendarsSync:
    """OrganizationService.request_all_calendars_sync — owner-account fan-out."""

    @pytest.fixture
    def mock_calendar_service(self):
        mock_service = Mock()
        mock_service.authenticate.return_value = None
        mock_service.request_calendar_sync.return_value = None
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
            "is_active": True,
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
        calendar = self._make_calendar(org, external_id="cal-inactive", is_active=False)
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
