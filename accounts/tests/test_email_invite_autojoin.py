"""
Phase 4 — Integration tests: Auto-join invited org on email verification.

These tests prove Use-case 2 end-to-end: a user who signed up with an invited email
address automatically joins the inviting organisation as MEMBER the moment their email
is confirmed, with no separate accept step and no stray organisations created.

Four scenarios are covered:

1. Full end-to-end flow — pending invitation + real signup form + real
   AccountAdapter.confirm_email → MEMBER membership, invitation accepted and linked,
   zero new organisations created.
2. Invite wins over org name — even if pending_organization_name is non-blank at
   confirmation time, the invite-first branch wins and no stray org is created.
3. Case-insensitive invite — invitation stored with a case-differing local part still
   matches at confirmation (guards the email__iexact consistency).
4. Invitation marked accepted — accepted_at and membership FK are both set after
   auto-join.

All tests drive confirmation through AccountAdapter.confirm_email (the imperative
override introduced in Phase 3), which is the same hook invoked by the headless
verify-email endpoint at runtime (same pattern as test_email_confirmation_provisioning.py).
"""

import datetime

import pytest
from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from model_bakery import baker

from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# Helpers (mirror the pattern from test_email_confirmation_provisioning.py)
# ---------------------------------------------------------------------------


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
    message storage backend doesn't raise.  CookieStorage is used because the
    RequestFactory doesn't set up session middleware.  Provisioning fires inside
    this call via the adapter override, exercising the same hook as the headless
    verify-email endpoint.
    """
    from django.contrib.messages.storage.cookie import CookieStorage

    request = rf.get("/")
    request._messages = CookieStorage(request)
    return get_adapter(request).confirm_email(request, email_address)


def _pending_invitation(
    org: Organization,
    inviter,
    email: str,
) -> OrganizationInvitation:
    """Create a non-expired, unaccepted OrganizationInvitation."""
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
class TestInvitedEmailAutoJoin:
    """Integration: invited-email auto-join on confirmation (Use-case 2)."""

    def test_full_end_to_end_invited_signup_autojoin(self, rf):
        """
        Full end-to-end: pending invitation + real signup form + AccountAdapter.confirm_email
        → MEMBER membership in the inviting org, invitation accepted + linked,
        zero new organisations created.

        The signup form is invoked with signup_email matching the invitation so the
        Phase 2 capture-skip fires (pending_organization_name ends blank).  Then
        AccountAdapter.confirm_email is driven so the Phase 3 adapter override fires
        and hands off to provision_tenant_for_user, whose invite-first branch auto-joins.
        """
        from accounts.base_forms import BaseVintaScheduleSignupForm

        inviter = UserFactory().create_user(email="admin@invitetest.com")
        org = baker.make(Organization, name="Invited Org")
        invited_email = "newuser@invitetest.com"

        # A non-expired, unaccepted invitation exists for the signup email.
        invitation = _pending_invitation(org, inviter, invited_email)

        # Create the user and drive the REAL signup form so Phase 2 capture-skip runs.
        user = UserFactory().create_user(email=invited_email)

        form_data = {
            "first_name": "New",
            "last_name": "User",
            "organization_name": "",  # invited user wouldn't submit a name
            "accepted_policies": True,
        }
        form = BaseVintaScheduleSignupForm(data=form_data)
        assert form.is_valid(), form.errors

        # Drive signup() — Phase 2: detects the invitation and leaves
        # pending_organization_name blank.
        form.signup(request=None, user=user)

        profile = user.profile
        profile.refresh_from_db()
        assert profile.pending_organization_name == "", (
            "Signup form should leave pending_organization_name blank for invited users"
        )

        # Now confirm the email via AccountAdapter.confirm_email (Phase 3 adapter override).
        email_address = _create_email_address(user)
        confirmed = _confirm_email(rf, email_address)

        assert confirmed is True

        # User joined the inviting org as MEMBER.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization == org
        assert membership.role == "member"

        # Zero new organisations were created — only the pre-existing one exists.
        assert Organization.objects.count() == 1

        # Invitation is marked accepted and linked to the membership.
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None, "Invitation accepted_at must be set"
        assert invitation.membership is not None, "Invitation must be linked to the membership"
        assert invitation.membership_user_id == membership.user_id

    def test_invite_wins_over_pending_organization_name(self, rf):
        """
        Invite-first: even when pending_organization_name is non-blank at confirmation
        time, the invite branch fires and NO stray org is created.

        This guards the edge case described in the plan: a user who somehow carries a
        non-blank pending_organization_name (e.g. set directly, or a bug in the form)
        still ends up in the inviting org as MEMBER — never as ADMIN of a new org.
        """
        inviter = UserFactory().create_user(email="boss@strayorgtest.com")
        org = baker.make(Organization, name="Correct Org")
        invited_email = "member@strayorgtest.com"

        _pending_invitation(org, inviter, invited_email)

        user = UserFactory().create_user(email=invited_email)

        # Directly set pending_organization_name to simulate the edge where it is
        # non-blank despite there being a pending invite (the form would clear it,
        # but we test the service's invite-first guarantee directly here).
        profile = user.profile
        profile.pending_organization_name = "Stray Org Name"
        profile.save()

        email_address = _create_email_address(user)
        _confirm_email(rf, email_address)

        # User is MEMBER of the inviting org, not ADMIN of a new one.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization == org
        assert membership.role == "member"

        # No stray org was created.
        assert Organization.objects.count() == 1

    def test_case_insensitive_invite_autojoin(self, rf):
        """
        Case-insensitive invite: invitation stored with a case-differing local part
        still matches at confirmation (email__iexact).

        E.g. invitation stored for "Member@Example.com" but user signs up as
        "member@example.com" — the auto-join must still fire.
        """
        inviter = UserFactory().create_user(email="hr@casetest.com")
        org = baker.make(Organization, name="Case Org")

        # Invitation stored with mixed-case local part.
        invitation_email = "Member@casetest.com"
        invitation = _pending_invitation(org, inviter, invitation_email)

        # User signs up with lower-cased version of the same address.
        signup_email = "member@casetest.com"
        user = UserFactory().create_user(email=signup_email)

        profile = user.profile
        profile.pending_organization_name = ""
        profile.save()

        email_address = _create_email_address(user)
        _confirm_email(rf, email_address)

        # Auto-join occurred despite the case difference.
        assert OrganizationMembership.objects.filter(user=user).count() == 1
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization == org
        assert membership.role == "member"

        # No new org created.
        assert Organization.objects.count() == 1

        # Invitation is properly accepted.
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert invitation.membership_user_id == membership.user_id

    def test_invitation_marked_accepted_after_autojoin(self, rf):
        """
        Acceptance-marker assertions: after auto-join both accepted_at (not None)
        and the membership FK are set on the OrganizationInvitation row.
        """
        inviter = UserFactory().create_user(email="lead@accepttest.com")
        org = baker.make(Organization, name="Accept Org")
        invited_email = "joinee@accepttest.com"

        invitation = _pending_invitation(org, inviter, invited_email)

        # Pre-condition: invitation has not been accepted yet.
        assert invitation.accepted_at is None
        assert invitation.membership is None

        user = UserFactory().create_user(email=invited_email)
        profile = user.profile
        profile.pending_organization_name = ""
        profile.save()

        email_address = _create_email_address(user)
        _confirm_email(rf, email_address)

        invitation.refresh_from_db()

        # Post-condition: both markers are set.
        assert invitation.accepted_at is not None, "accepted_at should be set after auto-join"
        assert invitation.membership is not None, "membership FK should be linked after auto-join"

        # The linked membership belongs to the right user and org.
        assert invitation.membership.user == user
        assert invitation.membership.organization == org
        assert invitation.membership.role == "member"
