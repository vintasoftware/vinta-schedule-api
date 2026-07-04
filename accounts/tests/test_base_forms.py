"""
Tests for BaseVintaScheduleSignupForm (Phase 2).

Covers:
- org name stored on profile when no pending invitation matches the signup email
- org name left blank when a pending invitation exists for the signup email
- expired invitation does NOT count as a match → org name still captured
- integration: signup persists all fields; existing behavior (user + profile + names) unchanged
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from accounts.base_forms import BaseVintaScheduleSignupForm
from organizations.models import Organization, OrganizationInvitation
from users.factories import UserFactory
from users.models import Profile, User


def _make_form(
    first_name="Ada",
    last_name="Lovelace",
    organization_name="ACME Corp",
):
    """Return a bound, valid BaseVintaScheduleSignupForm."""
    data = {
        "first_name": first_name,
        "last_name": last_name,
        "organization_name": organization_name,
        "accepted_terms": True,
        "accepted_sms_consent": True,
    }
    form = BaseVintaScheduleSignupForm(data=data)
    assert form.is_valid(), form.errors
    return form


@pytest.mark.django_db
class TestBaseVintaScheduleSignupFormUnit:
    """Unit-level tests focused on pending_organization_name capture logic."""

    def test_org_name_stored_when_no_pending_invitation(self):
        """Uninvited signup → org name written to profile.pending_organization_name."""
        user = UserFactory().create_user(email="ada@example.com")
        form = _make_form(first_name="Ada", last_name="Lovelace", organization_name="ACME Corp")

        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.first_name == "Ada"
        assert profile.last_name == "Lovelace"
        assert profile.pending_organization_name == "ACME Corp"

    def test_org_name_blank_when_pending_invitation_exists(self):
        """Invited signup → pending_organization_name left blank even if name supplied."""
        user = UserFactory().create_user(email="invited@example.com")
        org = baker.make(Organization)
        inviter = UserFactory().create_user(email="boss@example.com")
        baker.make(
            OrganizationInvitation,
            email="invited@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        form = _make_form(organization_name="Should Be Ignored")
        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.pending_organization_name == ""

    def test_expired_invitation_does_not_block_org_name_capture(self):
        """Expired invitation is not a pending match → org name still captured."""
        user = UserFactory().create_user(email="expired-invite@example.com")
        org = baker.make(Organization)
        inviter = UserFactory().create_user(email="sender@example.com")
        baker.make(
            OrganizationInvitation,
            email="expired-invite@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=timezone.now() - datetime.timedelta(days=1),  # already expired
            accepted_at=None,
            membership_user_id=None,
        )

        form = _make_form(
            first_name="Grace",
            last_name="Hopper",
            organization_name="Navy Labs",
        )
        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.pending_organization_name == "Navy Labs"

    def test_org_name_blank_when_no_name_provided(self):
        """Empty organization_name → pending_organization_name stored as empty string."""
        user = UserFactory().create_user(email="noname@example.com")
        form = _make_form(organization_name="")

        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.pending_organization_name == ""

    def test_org_name_blank_when_invitation_email_differs_in_case(self):
        """Regression: invitation stored with different-case local part is still detected.

        User signs up as 'recruit@example.com'; invitation was stored as 'Recruit@example.com'.
        The filter must use email__iexact so the case difference in the local part does not
        cause the invite to be missed and org name to be incorrectly captured.
        """
        user = UserFactory().create_user(email="recruit@example.com")
        org = baker.make(Organization)
        inviter = UserFactory().create_user(email="boss_case@example.com")
        baker.make(
            OrganizationInvitation,
            email="Recruit@example.com",  # mixed-case local part
            organization=org,
            invited_by=inviter,
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        form = _make_form(organization_name="Should Be Ignored Due To Case-Insensitive Match")
        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.pending_organization_name == ""


@pytest.mark.django_db
class TestBaseVintaScheduleSignupFormIntegration:
    """Integration-level tests: full signup flow, backward compatibility."""

    def test_signup_creates_profile_with_names_and_org_name(self):
        """Full uninvited signup: user + profile + first/last/org name all persisted."""
        user = UserFactory().create_user(email="margaret@example.com")
        form = _make_form(
            first_name="Margaret",
            last_name="Hamilton",
            organization_name="Apollo Systems",
        )

        returned_user = form.signup(request=None, user=user)

        assert returned_user == user
        profile = Profile.objects.get(user=user)
        assert profile.first_name == "Margaret"
        assert profile.last_name == "Hamilton"
        assert profile.pending_organization_name == "Apollo Systems"

    def test_signup_updates_existing_profile(self):
        """signup() updates an already-created profile (no duplicate create)."""
        user = UserFactory().create_user(
            email="katherine@example.com",
            first_name="Old",
            last_name="Name",
        )
        # Profile already exists from factory — signup must update, not create.
        assert Profile.objects.filter(user=user).count() == 1

        form = _make_form(
            first_name="Katherine",
            last_name="Johnson",
            organization_name="NASA",
        )
        form.signup(request=None, user=user)

        assert Profile.objects.filter(user=user).count() == 1
        profile = Profile.objects.get(user=user)
        assert profile.first_name == "Katherine"
        assert profile.last_name == "Johnson"
        assert profile.pending_organization_name == "NASA"

    def test_signup_creates_profile_when_absent(self):
        """signup() creates the Profile from scratch if it doesn't exist yet."""
        user = User(email="fresh@example.com")
        user.set_password("testpass123")
        user.save()
        # No Profile yet
        assert not Profile.objects.filter(user=user).exists()

        form = _make_form(
            first_name="Dorothy",
            last_name="Vaughan",
            organization_name="Langley",
        )
        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.first_name == "Dorothy"
        assert profile.last_name == "Vaughan"
        assert profile.pending_organization_name == "Langley"

    def test_invited_signup_preserves_names_clears_org_name(self):
        """Invited signup stores names correctly but leaves org name blank."""
        user = UserFactory().create_user(email="recruit@example.com")
        org = baker.make(Organization)
        inviter = UserFactory().create_user(email="hr@example.com")
        baker.make(
            OrganizationInvitation,
            email="recruit@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=timezone.now() + datetime.timedelta(days=3),
            accepted_at=None,
            membership_user_id=None,
        )

        form = _make_form(
            first_name="Alice",
            last_name="Wonder",
            organization_name="Should Not Persist",
        )
        form.signup(request=None, user=user)

        profile = Profile.objects.get(user=user)
        assert profile.first_name == "Alice"
        assert profile.last_name == "Wonder"
        assert profile.pending_organization_name == ""
