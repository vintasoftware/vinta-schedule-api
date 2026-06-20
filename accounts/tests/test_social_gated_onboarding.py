"""
Integration tests: Gated onboarding for uninvited social signup.

Use-case 3: A social user who signed up without a pending invitation lands
authenticated but membership-less (gated).  They then create their own
organisation via the existing OrganizationViewSet create endpoint and become
ADMIN.

Phase 5 update: a second create attempt now also SUCCEEDS — any authenticated
user may POST /organizations/ and become ADMIN of the new org (Use-case 7).

Four scenarios are covered:

1. Uninvited social signup (save_user) leaves the user membership-less — no
   Organisation is created, no OrganizationMembership row exists.
2. Gated → create org → ADMIN: a membership-less authenticated user POSTs the
   org-create endpoint, succeeds, and is recorded as ADMIN of the new org.
3. Second create succeeds (Phase 5): the now-member user POSTs org-create again
   and creates a second org; they end up with two ADMIN memberships.
4. Membership-less user blocked from a member-only tenant endpoint: a
   membership-less user hitting OrganizationInvitationViewSet gets 403 (no
   membership → OrganizationInvitationPermission denies access).

The social save_user path is simulated by:
  - Calling SocialAccountAdapter.save_user() directly (with the real allauth
    super().save_user() replaced by a minimal stub that saves the user, matching
    what the existing test_account_adapters.py tests do).
  - This replicates the production codepath without spinning up a full OAuth
    round-trip, and is consistent with the existing adapter test pattern.
"""

import datetime
from unittest.mock import MagicMock, patch

from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialLogin
from rest_framework import status
from rest_framework.test import APIClient

from accounts.account_adapters import SocialAccountAdapter
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _social_save_user(email: str) -> User:
    """
    Simulate the allauth social save_user path for an uninvited user.

    Mirrors the stub used in test_account_adapters.py: replace the super()
    call with a minimal version that saves the user, then invoke the real
    SocialAccountAdapter.save_user() so profile creation logic runs exactly
    as it does in production.
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


def _membership_less_auth_client(user: User) -> APIClient:
    """Return an APIClient authenticated as *user* (no password needed — force)."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSocialGatedOnboarding:
    """Phase 5 / Use-case 3: uninvited social signup → gated → create org → ADMIN."""

    # ------------------------------------------------------------------
    # Scenario 1 — save_user leaves the user membership-less
    # ------------------------------------------------------------------

    def test_uninvited_social_signup_leaves_user_membership_less(self):
        """
        After save_user completes for an uninvited social user:
        - A User row exists.
        - A Profile row exists (the adapter guarantees it).
        - No OrganizationMembership row exists for the user.
        - No Organisation was created by the adapter.
        """
        org_count_before = Organization.objects.count()

        user = _social_save_user("uninvited@social.example.com")

        assert User.objects.filter(pk=user.pk).exists(), "User was not persisted"
        assert Profile.objects.filter(user=user).exists(), "Profile was not persisted"
        assert not OrganizationMembership.objects.filter(user=user).exists(), (
            "save_user must NOT create a membership for an uninvited social user"
        )
        assert Organization.objects.count() == org_count_before, (
            "save_user must NOT create an Organisation for an uninvited social user"
        )

    def test_uninvited_social_user_has_no_organization_membership(self):
        """
        A gated (membership-less) user has no OrganizationMembership rows.
        With user.organization_memberships now a FK manager (not a OneToOne
        descriptor), the gating invariant is confirmed by checking the queryset
        is empty rather than expecting RelatedObjectDoesNotExist.
        """
        from organizations.models import get_active_organization_membership

        user = _social_save_user("gated@social.example.com")

        assert user.organization_memberships.count() == 0
        assert get_active_organization_membership(user) is None

    # ------------------------------------------------------------------
    # Scenario 2 — gated user creates org and becomes ADMIN
    # ------------------------------------------------------------------

    def test_gated_social_user_can_create_org_and_becomes_admin(self):
        """
        A membership-less social user POSTs to the org-create endpoint:
        - Receives 201.
        - An Organisation with the given name exists.
        - An OrganizationMembership with role=ADMIN exists for the user.
        """
        user = _social_save_user("neworg@social.example.com")
        client = _membership_less_auth_client(user)

        url = reverse("api:Organizations-list")
        response = client.post(
            url, {"name": "Social Org", "should_sync_rooms": False}, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED, (
            f"Expected 201, got {response.status_code}: {response.json()}"
        )

        # Organisation created
        assert Organization.objects.filter(name="Social Org").exists()

        # User is now ADMIN
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.role == OrganizationRole.ADMIN

    # ------------------------------------------------------------------
    # Scenario 3 — second create attempt is rejected
    # ------------------------------------------------------------------

    def test_second_org_create_succeeds_after_first_succeeds(self):
        """Phase 5 / Use-case 7: after creating a first org, a member can create a second one.

        Prior to Phase 5 this was expected to return 403 (OrganizationManagementPermission
        blocked members from the create action).  Phase 5 relaxes the permission so any
        authenticated user may POST /organizations/ and become ADMIN of the new org.
        The second create must therefore:
        - Return HTTP 201.
        - Create the second org with the caller as ADMIN.
        - Leave the caller with TWO active memberships.
        """
        user = _social_save_user("onceonly@social.example.com")
        client = _membership_less_auth_client(user)

        url = reverse("api:Organizations-list")

        # First create — must succeed.
        first_response = client.post(
            url, {"name": "First Org", "should_sync_rooms": False}, format="json"
        )
        assert first_response.status_code == status.HTTP_201_CREATED, (
            f"First create failed unexpectedly: {first_response.status_code}"
        )
        assert OrganizationMembership.objects.filter(user=user, is_active=True).count() == 1

        # Second create — Phase 5: now succeeds for an authenticated member.
        second_response = client.post(
            url, {"name": "Second Org", "should_sync_rooms": False}, format="json"
        )
        assert second_response.status_code == status.HTTP_201_CREATED, (
            f"Expected 201, got {second_response.status_code}: {second_response.json()}"
        )

        # Caller now has TWO active memberships; both orgs exist.
        assert OrganizationMembership.objects.filter(user=user, is_active=True).count() == 2
        assert Organization.objects.filter(name="Second Org").exists()

        # The second membership is also ADMIN.
        second_org = Organization.objects.get(name="Second Org")
        second_membership = OrganizationMembership.objects.get(user=user, organization=second_org)
        assert second_membership.role == OrganizationRole.ADMIN

    # ------------------------------------------------------------------
    # Scenario 4 — membership-less user blocked from member-only endpoint
    # ------------------------------------------------------------------

    def test_membership_less_social_user_blocked_from_invitation_endpoint(self):
        """
        A membership-less (gated) social user cannot access OrganizationInvitationViewSet
        (which requires OrganizationInvitationPermission → membership).
        The list endpoint returns 403.
        """
        user = _social_save_user("blocked@social.example.com")
        client = _membership_less_auth_client(user)

        url = reverse("api:OrganizationInvitations-list")
        response = client.get(url)

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403 for membership-less user, got {response.status_code}"
        )

    def test_membership_less_social_user_blocked_from_invitation_create(self):
        """
        A membership-less (gated) social user cannot create invitations.
        The create endpoint returns 403.
        """
        user = _social_save_user("blockedcreate@social.example.com")
        client = _membership_less_auth_client(user)

        url = reverse("api:OrganizationInvitations-list")
        response = client.post(
            url,
            {"email": "someone@example.com", "first_name": "Some", "last_name": "One"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403 for membership-less user, got {response.status_code}"
        )


@pytest.mark.django_db
class TestSocialSignupCrossOrgInviteAccept:
    """Phase 4 / Finding 2 — Cross-org invite accept via the social signup adapter.

    An existing org-A member completes a SOCIAL SIGNUP (re-login or a new OAuth
    connection) while a pending org-B invitation exists for their email.  The
    adapter calls provision_tenant_for_user which finds the org-B invitation and
    creates a second membership.  The user ends with TWO active memberships.
    """

    def _social_save_existing_user(self, user: User) -> User:
        """Simulate save_user for a user who already exists in the DB (e.g. re-login).

        Uses the same stub pattern as _social_save_user but operates on an already-
        persisted user — the super().save_user stub just returns the pre-saved user.
        """
        adapter = SocialAccountAdapter()
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = user
        sociallogin.account = MagicMock(extra_data={})

        def _super_save(request, sociallogin, form=None):
            # User is already saved; just return it.
            return sociallogin.user

        with patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save):
            return adapter.save_user(None, sociallogin, form=None)

    def test_existing_org_a_member_social_signup_with_org_b_invite_gains_second_membership(self):
        """Finding 2 integration test: existing org-A member + pending org-B invite → TWO memberships.

        Simulates the real-world cross-org-via-invite path through the adapter:
        1. A user is already a member of org A.
        2. A pending invitation to org B exists for their email.
        3. The user completes a SOCIAL SIGNUP (adapter.save_user is called).
        4. The adapter calls provision_tenant_for_user which finds the org-B invitation
           and joins the user to org B as MEMBER.
        5. After save_user returns, the user has TWO active memberships (org A + org B).
        """
        from model_bakery import baker

        # Create user already a member of org A.
        user = baker.make(User, email="crossorg_social@example.com")
        from users.models import Profile

        Profile.objects.get_or_create(
            user=user, defaults={"first_name": "Cross", "last_name": "Org"}
        )
        org_a = baker.make(Organization, name="Org A Social")
        baker.make(OrganizationMembership, user=user, organization=org_a, is_active=True)

        # Pending invitation to org B for the same email.
        org_b = baker.make(Organization, name="Org B Social")
        inviter = baker.make(User, email="inviter_social@example.com")
        baker.make(
            OrganizationInvitation,
            email=user.email,
            organization=org_b,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        # Refresh so the related manager cache is warm.
        user.refresh_from_db()

        # Trigger the adapter's save_user (the cross-org invite accept path).
        returned_user = self._social_save_existing_user(user)

        assert returned_user.pk == user.pk, "save_user must return the same user"

        # User now has TWO active memberships.
        memberships = OrganizationMembership.objects.filter(user=user)
        assert memberships.count() == 2, (
            f"Expected 2 memberships, found {memberships.count()}: "
            f"{list(memberships.values('organization__name', 'is_active'))}"
        )

        org_ids = set(memberships.values_list("organization_id", flat=True))
        assert org_a.id in org_ids, "org A membership must be retained"
        assert org_b.id in org_ids, "org B membership must be created via invite"

        # The org-B invitation must be marked accepted.
        invite = OrganizationInvitation.objects.get(email=user.email, organization=org_b)
        assert invite.accepted_at is not None, "org-B invitation must be marked accepted"
        assert invite.membership is not None, (
            "org-B invitation must be linked to the new membership"
        )
