"""
Integration tests: Auto-join invited org on social signup.

A social user whose email matches a pending invitation automatically
joins the inviting organisation as MEMBER at save_user time — no create-org step
needed.  Four scenarios are covered:

1. Invited social signup auto-joins: pending invitation + social save_user →
   MEMBER membership in the inviting org, invitation marked accepted + linked,
   no new org created, user is no longer gated.
2. Uninvited social signup stays gated: no invitation → no membership, no org
   created.
3. Case-insensitive invite: invitation stored with a case-differing local part
   still triggers auto-join.
4. Re-entry / already-member is a no-op: calling save_user a second time for an
   already-member social user creates nothing extra and raises nothing.

The social save_user path is simulated exactly as in test_social_gated_onboarding.py
and test_account_adapters.py: super().save_user is replaced by a minimal stub that
persists the user, then the real SocialAccountAdapter.save_user runs so that all
profile-creation and provisioning logic executes as in production.
"""

import datetime
from unittest.mock import MagicMock, patch

import pytest
from allauth.socialaccount.models import SocialLogin
from model_bakery import baker

from accounts.account_adapters import SocialAccountAdapter
from organizations.exceptions import UserAlreadyHasMembershipError
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from users.factories import UserFactory
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _social_save_user(email: str) -> User:
    """Simulate the allauth social save_user path for *email*.

    Mirrors the helper in test_social_gated_onboarding.py: the super() call is
    replaced by a minimal stub that persists the user, then the real
    SocialAccountAdapter.save_user() runs so that profile creation and
    provisioning logic execute exactly as in production.
    """
    adapter = SocialAccountAdapter()
    new_user = User(email=email)
    new_user.profile = Profile(user=new_user, first_name="Grace", last_name="Hopper")
    sociallogin = MagicMock(spec=SocialLogin)
    sociallogin.user = new_user
    # No provider avatar → picture download won't be enqueued.
    sociallogin.account = MagicMock(extra_data={})

    def _super_save(request, sociallogin, form=None):
        sociallogin.user.save()
        return sociallogin.user

    with patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save):
        return adapter.save_user(None, sociallogin, form=None)


def _pending_invitation(
    org: Organization,
    inviter: User,
    email: str,
) -> OrganizationInvitation:
    """Create a non-expired, unaccepted OrganizationInvitation for *email*."""
    return baker.make(
        OrganizationInvitation,
        email=email,
        organization=org,
        invited_by=inviter,
        expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
        accepted_at=None,
        membership_user_id=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSocialInviteAutojoin:
    """Invited social signup auto-joins the inviting org."""

    # ------------------------------------------------------------------
    # Scenario 1 — invited social signup auto-joins
    # ------------------------------------------------------------------

    def test_invited_social_signup_auto_joins_as_member(self):
        """
        A pending invitation matches the social user's email.

        After save_user:
        - The user has exactly one MEMBER membership in the inviting org.
        - The invitation is marked accepted (accepted_at set, membership FK linked).
        - No new org was created — only the pre-existing inviting org exists.
        - The user is no longer gated (has a membership).
        """
        inviter = UserFactory().create_user(email="admin@socialinvite.example.com")
        org = baker.make(Organization, name="Social Invite Org")
        invited_email = "invited@socialinvite.example.com"

        invitation = _pending_invitation(org, inviter, invited_email)
        org_count_before = Organization.objects.count()

        user = _social_save_user(invited_email)

        # User and profile were persisted.
        assert User.objects.filter(pk=user.pk).exists()
        assert Profile.objects.filter(user=user).exists()

        # Exactly one MEMBER membership in the inviting org.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization == org
        assert membership.role == OrganizationRole.MEMBER

        # No new org was created.
        assert Organization.objects.count() == org_count_before

        # Invitation is marked accepted and linked to the membership.
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None, "invitation.accepted_at must be set"
        assert invitation.membership is not None, "invitation.membership FK must be linked"
        assert invitation.membership_user_id == membership.user_id

    # ------------------------------------------------------------------
    # Scenario 2 — uninvited social signup stays gated
    # ------------------------------------------------------------------

    def test_uninvited_social_signup_stays_gated(self):
        """
        No pending invitation exists for the social user's email.

        After save_user:
        - No OrganizationMembership row exists for the user.
        - No Organisation was created by the adapter.
        """
        org_count_before = Organization.objects.count()

        user = _social_save_user("uninvited@socialinvite.example.com")

        assert User.objects.filter(pk=user.pk).exists()
        assert Profile.objects.filter(user=user).exists()
        assert not OrganizationMembership.objects.filter(user=user).exists(), (
            "save_user must NOT create a membership for an uninvited social user"
        )
        assert Organization.objects.count() == org_count_before, (
            "save_user must NOT create an Organisation for an uninvited social user"
        )

    # ------------------------------------------------------------------
    # Scenario 3 — case-insensitive invite
    # ------------------------------------------------------------------

    def test_case_insensitive_invite_auto_joins(self):
        """
        Invitation stored with a case-differing local part still triggers auto-join
        (guards the email__iexact lookup in provision_tenant_for_user).
        """
        inviter = UserFactory().create_user(email="boss@casetest.socialinvite.example.com")
        org = baker.make(Organization, name="Case Invite Org")

        # Invitation stored with mixed-case local part.
        invitation_email = "Invited@CaseTest.SocialInvite.example.com"
        invitation = _pending_invitation(org, inviter, invitation_email)

        # Social signup with a differently-cased variant of the same address.
        signup_email = "invited@casetest.socialinvite.example.com"
        user = _social_save_user(signup_email)

        # Auto-join occurred despite the case difference.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization == org
        assert membership.role == OrganizationRole.MEMBER

        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert invitation.membership_user_id == membership.user_id

    # ------------------------------------------------------------------
    # Scenario 4 — re-entry / already-member is a no-op
    # ------------------------------------------------------------------

    def test_already_member_social_relogin_is_noop(self):
        """
        A social user who already has a membership goes through save_user again
        (e.g. re-login).  The second call must:
        - Not raise any exception.
        - Not create a second membership.
        - Leave the original membership unchanged.
        """
        inviter = UserFactory().create_user(email="lead@reentrytest.socialinvite.example.com")
        org = baker.make(Organization, name="Reentry Org")
        invited_email = "reentry@socialinvite.example.com"

        _pending_invitation(org, inviter, invited_email)

        # First social signup — user auto-joins.
        user = _social_save_user(invited_email)
        membership_after_first = OrganizationMembership.objects.get(user=user)
        assert membership_after_first.organization == org

        # Simulate a second save_user call (social re-login / adapter re-entry).
        # The invitation is now accepted so no second match; but even if a fresh
        # invitation existed, the already-member guard must prevent a second membership.
        adapter = SocialAccountAdapter()
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = user
        sociallogin.account = MagicMock(extra_data={})

        def _super_save_second(request, sl, form=None):
            # User is already persisted; just return it.
            return sl.user

        with patch.object(
            SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save_second
        ):
            # Must not raise.
            returned_user = adapter.save_user(None, sociallogin, form=None)

        assert returned_user == user

        # Still exactly one membership — nothing was created or destroyed.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership_after_second = OrganizationMembership.objects.get(user=user)
        assert membership_after_second.pk == membership_after_first.pk
        assert membership_after_second.organization == org

    # ------------------------------------------------------------------
    # Scenario 5 — UserAlreadyHasMembershipError guard actually fires
    # ------------------------------------------------------------------

    def test_already_member_guard_fires_on_second_provisioning_call(self):
        """
        User is already a member; calling provision_tenant_for_user again (e.g.
        a concurrent request or re-entry) must:
          (a) raise UserAlreadyHasMembershipError from provision_tenant_for_user,
          (b) not create a second membership,
          (c) the second org (created for the re-try) gains no membership for this user,
          (d) the user's original membership is unchanged.

        This test genuinely exercises the UserAlreadyHasMembershipError guard in
        provision_tenant_for_user (the hasattr check fires because the user already
        has a membership after the first social signup).  It also confirms that
        _provision_org_membership swallows UserAlreadyHasMembershipError silently
        when it is called via the social save_user path.
        """
        from di_core.containers import container as di_container

        inviter = UserFactory().create_user(email="admin@loadtest.socialinvite.example.com")
        org1 = baker.make(Organization, name="Load Test Org 1")
        email = "loadtest@socialinvite.example.com"

        # First invitation → user auto-joins org1.
        _pending_invitation(org1, inviter, email)
        user = _social_save_user(email)

        original_membership = OrganizationMembership.objects.get(user=user)
        assert original_membership.organization == org1

        # Reload user so the related-manager attribute is fresh.
        user.refresh_from_db()

        # Drive provisioning directly on the now-member user with a new org name.
        # The hasattr guard must fire and raise UserAlreadyHasMembershipError.
        org2 = baker.make(Organization, name="Load Test Org 2")
        organization_service = di_container.organization_service()

        # (a) raises UserAlreadyHasMembershipError — guard fires.
        with pytest.raises(UserAlreadyHasMembershipError):
            organization_service.provision_tenant_for_user(user=user, organization_name=org2.name)

        # (b) no second membership was created.
        assert OrganizationMembership.objects.filter(user=user).count() == 1

        # (c) org2 has no membership for this user.
        assert not OrganizationMembership.objects.filter(user=user, organization=org2).exists()

        # (d) original membership is unchanged.
        original_membership.refresh_from_db()
        assert original_membership.organization == org1

        # -------------------------------------------------------------------
        # Also verify that _provision_org_membership swallows
        # UserAlreadyHasMembershipError silently (the adapter-level no-op).
        # Call save_user again for the already-member user — it must NOT raise.
        # -------------------------------------------------------------------
        adapter = SocialAccountAdapter()
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = user
        sociallogin.account = MagicMock(extra_data={})

        def _super_save_reentry(request, sl, form=None):
            return sl.user

        with patch.object(
            SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save_reentry
        ):
            # Must not raise — UserAlreadyHasMembershipError is swallowed.
            returned_user = adapter.save_user(None, sociallogin, form=None)

        assert returned_user == user
        # Still exactly one membership.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
