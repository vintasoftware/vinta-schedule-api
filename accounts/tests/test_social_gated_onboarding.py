"""
Phase 5 — Integration tests: Gated onboarding for uninvited social signup.

Use-case 3: A social user who signed up without a pending invitation lands
authenticated but membership-less (gated).  They then create their own
organisation via the existing OrganizationViewSet create endpoint and become
ADMIN.  A second create attempt is rejected by OrganizationManagementPermission.

Four scenarios are covered:

1. Uninvited social signup (save_user) leaves the user membership-less — no
   Organisation is created, no OrganizationMembership row exists.
2. Gated → create org → ADMIN: a membership-less authenticated user POSTs the
   org-create endpoint, succeeds, and is recorded as ADMIN of the new org.
3. Second create rejected: the now-member user POSTs org-create again and is
   refused with 403; no second org or membership is created.
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

from unittest.mock import MagicMock, patch

from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialLogin
from rest_framework import status
from rest_framework.test import APIClient

from accounts.account_adapters import SocialAccountAdapter
from organizations.models import Organization, OrganizationMembership, OrganizationRole
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

    def test_uninvited_social_user_has_no_organization_membership_attribute(self):
        """
        Accessing user.organization_membership on a gated user raises
        RelatedObjectDoesNotExist (the OneToOne reverse descriptor raises when absent).
        The views guard against this with hasattr / try-except, so this confirms the
        gating invariant at the model level.
        """
        from django.core.exceptions import ObjectDoesNotExist

        user = _social_save_user("gated@social.example.com")

        with pytest.raises(ObjectDoesNotExist):
            _ = user.organization_membership

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

    def test_second_org_create_rejected_after_first_succeeds(self):
        """
        After a social user creates their org (now ADMIN / member), a second
        POST to org-create returns 403 and no second Organisation or membership
        is created.
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

        membership_count_after_first = OrganizationMembership.objects.filter(user=user).count()

        # Second create — must be rejected by OrganizationManagementPermission.
        second_response = client.post(
            url, {"name": "Second Org", "should_sync_rooms": False}, format="json"
        )
        assert second_response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403, got {second_response.status_code}: {second_response.json()}"
        )

        # No extra membership or org created.
        assert (
            OrganizationMembership.objects.filter(user=user).count() == membership_count_after_first
        )
        assert not Organization.objects.filter(name="Second Org").exists()

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
