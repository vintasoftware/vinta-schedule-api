"""
Integration tests: Create own org on email verification (no invite).

These tests exercise the AccountAdapter.confirm_email override, which is the
imperative provisioning hook for the email/password signup path — symmetric
with the social path (SocialAccountAdapter.save_user).

Three scenarios are covered:
1. Uninvited user with pending_organization_name → org created, user is ADMIN,
   pending_organization_name cleared.
2. Re-confirmation is a no-op: no second org, no error.
3. Blank pending_organization_name + no invite → no org, user stays gated,
   no exception.
"""

import datetime

from django.test import override_settings

import pytest
from allauth.account.adapter import get_adapter
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
    """Drive email confirmation through AccountAdapter.confirm_email.

    Uses a minimal GET request so add_message() has a request object and the
    message storage backend doesn't raise. CookieStorage is used because the
    RequestFactory doesn't set up session middleware. Provisioning fires inside
    this call via the adapter override, exercising the same hook as the headless
    verify-email endpoint.
    """
    from django.contrib.messages.storage.cookie import CookieStorage

    request = rf.get("/")
    request._messages = CookieStorage(request)
    return get_adapter(request).confirm_email(request, email_address)


@pytest.mark.django_db
class TestProvisionOnEmailConfirmation:
    """Integration: provisioning logic wired via AccountAdapter.confirm_email."""

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

        # Reset verified flag so allauth's verify_email proceeds on the second call
        # (it short-circuits when already verified). The adapter's idempotency guard
        # (swallowing UserAlreadyHasMembershipError) absorbs the second provisioning
        # attempt, so no second org is created.
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

        This verifies that the invite-first branch works end-to-end through the
        adapter override.
        """
        inviter = UserFactory().create_user(email="boss@example.com")
        org = baker.make(Organization, name="Invite Corp")

        invited_user = UserFactory().create_user(email="dave@example.com")
        # Invited signup → pending_organization_name is blank.
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
            membership_user_id=None,
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
        """Adapter confirm_email is robust when the user somehow has no profile."""
        from unittest.mock import patch

        from django.contrib.messages.storage.cookie import CookieStorage

        user = UserFactory().create_user(email="noProfile@example.com")
        email_address = _create_email_address(user)

        request = rf.get("/")
        request._messages = CookieStorage(request)

        # Simulate missing profile by patching the profile descriptor to raise.
        from users.models import Profile as ProfileModel

        def _raise_does_not_exist(self):
            raise ProfileModel.DoesNotExist()

        with patch.object(
            type(user),
            "profile",
            new_callable=lambda: property(_raise_does_not_exist),
        ):
            # Should not raise even with no profile.
            confirmed = get_adapter(request).confirm_email(request, email_address)

        # The call completes without error; email is confirmed but no membership created.
        assert confirmed is True
        assert not OrganizationMembership.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestProvisioningWaitsForEveryVerification:
    """An invitation is accepted (and a seat consumed) only once the user has
    proven every identity the signup flow asks for -- never on the strength of a
    submitted signup form. See ``accounts.account_adapters.
    is_verified_for_provisioning``.
    """

    def _invited_user(self, email: str, **user_kwargs):
        inviter = UserFactory().create_user(email=f"inviter+{email}")
        org = baker.make(Organization, name=f"Org for {email}")
        user = UserFactory().create_user(email=email)
        for field, value in user_kwargs.items():
            setattr(user, field, value)
        if user_kwargs:
            user.save(update_fields=list(user_kwargs))
        invitation = baker.make(
            OrganizationInvitation,
            email=email,
            organization=org,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )
        return user, org, invitation

    def test_signup_alone_does_not_accept_the_invitation(self, rf):
        """The signup form is submitted and the account exists, but nothing has
        been verified: the invitation must still be pending."""
        user, _org, invitation = self._invited_user("unverified@example.com")
        _create_email_address(user, verified=False)

        assert not OrganizationMembership.objects.filter(user=user).exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_email_verified_but_phone_pending_defers(self, rf):
        """With phone verification enabled, confirming the email is not enough --
        the seat is not taken until the phone is proven too."""
        user, _org, invitation = self._invited_user(
            "phonepending@example.com", phone_number="+15550000001"
        )
        email_address = _create_email_address(user)

        assert _confirm_email(rf, email_address) is True

        assert not OrganizationMembership.objects.filter(user=user).exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_phone_verification_completes_the_provisioning(self, rf):
        """Whichever verification lands last is the one that provisions: here the
        email was confirmed first (and deferred), so verifying the phone accepts
        the invitation."""
        user, org, invitation = self._invited_user(
            "phonelast@example.com", phone_number="+15550000002"
        )
        email_address = _create_email_address(user)
        _confirm_email(rf, email_address)

        get_adapter().set_phone_verified(user, "+15550000002")

        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization == org
        assert membership.role == "member"
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert invitation.membership_user_id == membership.user_id

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_phone_verified_first_still_waits_for_the_email(self):
        """The reverse order: the phone stage runs before the email stage in
        allauth's login pipeline, so this is the common case -- and it must not
        provision on the phone alone."""
        user, _org, invitation = self._invited_user(
            "phonefirst@example.com", phone_number="+15550000003"
        )
        _create_email_address(user, verified=False)

        get_adapter().set_phone_verified(user, "+15550000003")

        assert not OrganizationMembership.objects.filter(user=user).exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None

    @override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)
    def test_user_without_a_phone_is_not_blocked(self, rf):
        """Phone verification enabled, but this user has no phone on file (e.g. a
        social signup): there is no phone check to wait for, so the verified
        email alone provisions."""
        user, org, invitation = self._invited_user("nophone@example.com", phone_number="")
        email_address = _create_email_address(user)

        assert _confirm_email(rf, email_address) is True

        assert OrganizationMembership.objects.get(user=user).organization == org
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
