"""
Phase 3 — Integration tests: Create own org on email verification (no invite).

These tests exercise the real allauth confirmation code path
(allauth.account.internal.flows.email_verification.verify_email) so the
email_confirmed signal fires just as it would at runtime.

Three scenarios are covered:
1. Uninvited user with pending_organization_name → org created, user is ADMIN,
   pending_organization_name cleared.
2. Re-confirmation (signal re-fires) is a no-op: no second org, no error.
3. Blank pending_organization_name + no invite → no org, user stays gated,
   no exception.
"""

import datetime

import pytest
from allauth.account.internal.flows.email_verification import verify_email
from allauth.account.models import EmailAddress
from model_bakery import baker

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from users.factories import UserFactory


def _create_email_address(user, verified: bool = False) -> EmailAddress:
    """Create (and persist) an allauth EmailAddress for *user*."""
    return EmailAddress.objects.create(
        user=user,
        email=user.email,
        verified=verified,
        primary=True,
    )


def _confirm_email(rf, email_address: EmailAddress) -> bool:
    """Run the real allauth verify_email flow for *email_address*.

    Uses a minimal GET request so add_message() has a request object and the
    message storage backend doesn't raise. CookieStorage is used because the
    RequestFactory doesn't set up session middleware.  The signal fires inside
    this call, exercising the same code path as the headless verify-email endpoint.
    """
    from django.contrib.messages.storage.cookie import CookieStorage

    request = rf.get("/")
    request._messages = CookieStorage(request)
    return verify_email(request, email_address)


@pytest.mark.django_db
class TestProvisionOnEmailConfirmation:
    """Integration: provisioning logic wired via the email_confirmed signal."""

    def test_uninvited_user_creates_org_on_confirmation(self, rf):
        """Uninvited user with pending_organization_name → org + ADMIN membership."""
        user = UserFactory().create_user(email="alice@example.com")
        profile = user.profile
        profile.pending_organization_name = "Alice's Workshop"
        profile.save()

        email_address = _create_email_address(user)
        confirmed = _confirm_email(rf, email_address)

        assert confirmed is True

        # Org was created and user is ADMIN.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization.name == "Alice's Workshop"
        assert membership.role == "admin"

        # pending_organization_name was cleared.
        profile.refresh_from_db()
        assert profile.pending_organization_name == ""

    def test_re_confirmation_is_no_op(self, rf):
        """Re-firing the confirmation event for an already-provisioned user is a no-op."""
        user = UserFactory().create_user(email="bob@example.com")
        profile = user.profile
        profile.pending_organization_name = "Bob's Place"
        profile.save()

        email_address = _create_email_address(user)

        # First confirmation → creates the org.
        _confirm_email(rf, email_address)
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        first_org_id = OrganizationMembership.objects.get(user=user).organization_id

        # allauth re-emits email_confirmed even when already verified:
        # set_verified() returns True for an already-verified address, and
        # verify_email() only early-returns when set_verified(commit=False) is
        # falsy.  A real second confirmation therefore re-fires the signal, so
        # the handler's idempotency guard (swallowing UserAlreadyHasMembershipError)
        # must absorb it.  Reset the verified flag so verify_email proceeds, then
        # call the real path.
        email_address.verified = False
        email_address.save(update_fields=["verified"])
        _confirm_email(rf, email_address)

        # Still exactly one membership, pointing at the same org.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        assert OrganizationMembership.objects.get(user=user).organization_id == first_org_id
        # No extra organizations created for this user.
        assert Organization.objects.count() == 1

    def test_blank_org_name_no_invite_no_org_created(self, rf):
        """Blank pending_organization_name + no invite → no org, user stays gated."""
        user = UserFactory().create_user(email="carol@example.com")
        profile = user.profile
        profile.pending_organization_name = ""
        profile.save()

        email_address = _create_email_address(user)
        confirmed = _confirm_email(rf, email_address)

        assert confirmed is True

        # No org, no membership — user is gated.
        assert not OrganizationMembership.objects.filter(user=user).exists()
        assert Organization.objects.count() == 0

    def test_invited_user_is_provisioned_as_member(self, rf):
        """User with a pending invite (and blank org name) joins as MEMBER on confirmation.

        This verifies that Phase 1's invite-first branch works end-to-end through the
        signal handler even when Phase 4 tests haven't landed yet.
        """
        inviter = UserFactory().create_user(email="boss@example.com")
        org = baker.make(Organization, name="Invite Corp")

        invited_user = UserFactory().create_user(email="dave@example.com")
        # Invited signup → pending_organization_name is blank (Phase 2 behaviour).
        profile = invited_user.profile
        profile.pending_organization_name = ""
        profile.save()

        baker.make(
            OrganizationInvitation,
            email="dave@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        email_address = _create_email_address(invited_user)
        confirmed = _confirm_email(rf, email_address)

        assert confirmed is True

        # User joined the inviting org as MEMBER.
        assert OrganizationMembership.objects.filter(user=invited_user).count() == 1
        membership = OrganizationMembership.objects.get(user=invited_user)
        assert membership.organization == org
        assert membership.role == "member"

        # No new org was created.
        assert Organization.objects.count() == 1

    def test_no_profile_guard_does_not_raise(self, rf):
        """Signal handler is robust when the user somehow has no profile."""
        from unittest.mock import patch

        user = UserFactory().create_user(email="noProfile@example.com")
        email_address = _create_email_address(user)

        # Simulate missing profile by patching the profile descriptor to raise.
        from users.models import Profile as ProfileModel

        def _raise_does_not_exist(self):
            raise ProfileModel.DoesNotExist()

        with patch.object(
            type(user),
            "profile",
            new_callable=lambda: property(_raise_does_not_exist),
        ):
            from allauth.account.signals import email_confirmed

            # Should not raise even with no profile.
            email_confirmed.send(
                sender=EmailAddress,
                request=None,
                email_address=email_address,
            )

        # Nothing was created.
        assert not OrganizationMembership.objects.filter(user=user).exists()
