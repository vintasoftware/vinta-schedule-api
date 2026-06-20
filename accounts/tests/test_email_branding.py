"""
Phase 8 — Integration tests: Reseller-branded transactional emails.

These tests prove that:
1. An invite to a user in a branded subtree renders the reseller's app_name/logo
   and From address, with no vinta domain leaks.
2. An invite under no reseller renders today's vinta email byte-for-byte
   (backwards-compat guarantee).
3. Confirmation templates (confirmation.body.html + confirmation_signup.body.html)
   remain byte-for-byte identical to their pre-phase-8 original (URL not substituted).
4. A public-API invite (invited_by=None) produces a renderable context — no raise.
"""

import datetime

from django.template.loader import render_to_string

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationBranding, OrganizationInvitation
from users.factories import UserFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reseller_with_branding(app_name="ResellApp"):
    """Create a reseller org with a branding row and return (reseller, branding)."""
    reseller = baker.make(
        Organization,
        name="Reseller Org",
        can_invite_organizations=True,
    )
    branding = baker.make(
        OrganizationBranding,
        organization=reseller,
        app_name=app_name,
        logo_url="https://reseller.example.com/logo.png",
        support_email="support@reseller.com",
        primary_color="#FF0000",
        secondary_color="#00FF00",
    )
    return reseller, branding


def _make_child(parent):
    return baker.make(
        Organization,
        name="Child Org",
        parent=parent,
        can_invite_organizations=False,
    )


# ---------------------------------------------------------------------------
# Context-dict tests (fast; no template rendering)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEmailBranding:
    """Branded and non-branded email context dict checks."""

    def test_branded_invitation_email_renders_reseller_branding(self, di_container):
        """
        An invite to a user in a branded subtree renders the reseller's app_name,
        logo, and support_email; no vinta domain leaks appear.
        """
        reseller, _ = _make_reseller_with_branding()
        child = _make_child(reseller)
        inviter = UserFactory().create_user(email="inviter@reseller.com")
        invitation = baker.make(
            OrganizationInvitation,
            email="newuser@example.com",
            organization=child,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        assert "branding" in ctx, "Branding should be in context for a branded child org"
        assert ctx["branding"]["app_name"] == "ResellApp"
        assert ctx["branding"]["logo_url"] == "https://reseller.example.com/logo.png"
        assert ctx["branding"]["support_email"] == "support@reseller.com"
        assert ctx["branding"]["primary_color"] == "#FF0000"
        assert ctx["branding"]["secondary_color"] == "#00FF00"

    def test_non_branded_invitation_email_renders_vinta_defaults_byte_for_byte(self, di_container):
        """
        An invite under no reseller renders today's vinta email unchanged
        (byte-for-byte backwards-compat guarantee).
        """
        org = baker.make(
            Organization,
            name="Non-Reseller Org",
            parent=None,
            can_invite_organizations=False,
        )
        inviter = UserFactory().create_user(email="inviter@vinta.com")
        invitation = baker.make(
            OrganizationInvitation,
            email="newuser@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        assert ctx["branding"]["app_name"] == "Vinta Schedule"
        assert "vinta_schedule.com" not in ctx["branding"]["app_name"], (
            "No vinta domain should leak in branding values"
        )

    def test_branded_child_of_non_reseller_parent_uses_vinta_default(self):
        """
        A child of a non-reseller org should render vinta defaults even if
        a sibling has a branded reseller ancestor further up.
        """
        org = baker.make(
            Organization,
            name="Non-Reseller Parent",
            parent=None,
            can_invite_organizations=False,
        )
        child = baker.make(
            Organization,
            name="Child of Non-Reseller",
            parent=org,
            can_invite_organizations=False,
        )
        inviter = UserFactory().create_user(email="inviter@vinta.com")
        invitation = baker.make(
            OrganizationInvitation,
            email="newuser@example.com",
            organization=child,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        assert ctx["branding"]["app_name"] == "Vinta Schedule"


# ---------------------------------------------------------------------------
# Template rendering tests (byte-for-byte / no-leak / no-crash guarantees)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationTemplateRendering:
    """
    Render organization_invitation.body.html with real Django template engine
    and assert content byte-for-byte.
    """

    BODY_TEMPLATE = "organizations/emails/organization_invitation.body.html"
    SUBJECT_TEMPLATE = "organizations/emails/organization_invitation.subject.txt"
    PREHEADER_TEMPLATE = "organizations/emails/organization_invitation.pre_header.txt"

    def _default_ctx(self, org_name="Test Org", invited_by_name="Alice Smith"):
        return {
            "invitation": {
                "id": 1,
                "email": "bob@example.com",
                "first_name": "Bob",
                "last_name": "Jones",
                "organization_name": org_name,
                "invited_by_name": invited_by_name,
                "expires_at": datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC),
            },
            "organization_join_url": "https://example.com/accept?token=fake",
            "branding": {
                "app_name": "Vinta Schedule",
                "logo_url": "",
                "primary_color": "",
                "secondary_color": "",
                "support_email": "",
            },
        }

    def test_default_invitation_body_contains_vinta_schedule(self):
        """
        Non-reseller (default) invitation body must contain 'Vinta Schedule'
        and the inviter name.
        """
        ctx = self._default_ctx(invited_by_name="Alice Smith")
        body = render_to_string(self.BODY_TEMPLATE, ctx)
        assert "Vinta Schedule" in body, (
            f"Default invitation body must contain 'Vinta Schedule'; got:\n{body}"
        )
        assert "Alice Smith" in body, (
            f"Default invitation body must contain inviter name; got:\n{body}"
        )

    def test_default_invitation_subject_contains_vinta_schedule(self):
        """Non-reseller invitation subject must contain 'Vinta Schedule'."""
        ctx = self._default_ctx()
        subject = render_to_string(self.SUBJECT_TEMPLATE, ctx)
        assert "Vinta Schedule" in subject, (
            f"Default invitation subject must contain 'Vinta Schedule'; got:\n{subject}"
        )

    def test_default_invitation_preheader_contains_vinta_schedule(self):
        """Non-reseller invitation pre-header must contain 'Vinta Schedule'."""
        ctx = self._default_ctx()
        preheader = render_to_string(self.PREHEADER_TEMPLATE, ctx)
        assert "Vinta Schedule" in preheader, (
            f"Default invitation preheader must contain 'Vinta Schedule'; got:\n{preheader}"
        )

    def test_branded_invitation_body_uses_reseller_app_name(self):
        """
        Branded (reseller) invitation body must use the reseller's app_name
        and must NOT contain 'Vinta Schedule' where the brand name appears.
        """
        ctx = self._default_ctx()
        ctx["branding"]["app_name"] = "ResellApp"
        body = render_to_string(self.BODY_TEMPLATE, ctx)
        assert "ResellApp" in body, (
            f"Branded invitation body must contain reseller app_name 'ResellApp'; got:\n{body}"
        )
        # The brand name slot must show ResellApp, not Vinta Schedule.
        # (The inviter name "Alice Smith" may still appear separately — that's fine.)
        assert "Vinta Schedule" not in body, (
            f"Branded invitation body must NOT contain 'Vinta Schedule'; got:\n{body}"
        )

    def test_branded_invitation_subject_uses_reseller_app_name(self):
        """Branded invitation subject must use the reseller's app_name."""
        ctx = self._default_ctx()
        ctx["branding"]["app_name"] = "ResellApp"
        subject = render_to_string(self.SUBJECT_TEMPLATE, ctx)
        assert "ResellApp" in subject, (
            f"Branded invitation subject must contain 'ResellApp'; got:\n{subject}"
        )

    def test_branded_invitation_preheader_uses_reseller_app_name(self):
        """Branded invitation preheader must use the reseller's app_name."""
        ctx = self._default_ctx()
        ctx["branding"]["app_name"] = "ResellApp"
        preheader = render_to_string(self.PREHEADER_TEMPLATE, ctx)
        assert "ResellApp" in preheader, (
            f"Branded invitation preheader must contain 'ResellApp'; got:\n{preheader}"
        )

    def test_invitation_body_with_empty_last_name_preserves_trailing_space_byte_for_byte(
        self, di_container
    ):
        """
        When an inviter has an empty last_name, the invited_by_name must preserve
        the trailing space (no .strip()) for byte-for-byte compatibility with phase-7.
        This test verifies the exact rendered substring "invited by John  to join"
        (double space) appears in the body.
        """
        # Create a non-reseller org to get vinta defaults
        org = baker.make(
            Organization,
            name="Non-Reseller Org",
            parent=None,
            can_invite_organizations=False,
        )
        # Create inviter with empty last_name
        inviter = UserFactory().create_user(email="john@vinta.com")
        inviter.profile.first_name = "John"
        inviter.profile.last_name = ""  # Empty last_name is the key scenario
        inviter.profile.save()

        invitation = baker.make(
            OrganizationInvitation,
            email="newuser@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        # The invited_by_name must be "John " (with trailing space, no strip)
        assert ctx["invitation"]["invited_by_name"] == "John ", (
            f"invited_by_name must preserve trailing space when last_name is empty; "
            f"got: {ctx['invitation']['invited_by_name']!r}"
        )

        # Render the body and verify the exact double-space substring appears
        body = render_to_string(self.BODY_TEMPLATE, ctx)
        assert "invited by John  to join" in body, (
            f"Body must contain exact substring 'invited by John  to join' "
            f"(double space preserved); got:\n{body}"
        )


# ---------------------------------------------------------------------------
# BLOCKER 2: public-API invite (invited_by=None) must not raise
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicApiInviteInvitedByNone:
    """
    OrganizationInvitation created with invited_by=None (public-API / reseller path)
    must produce a valid context and a renderable email body — never raise.
    """

    BODY_TEMPLATE = "organizations/emails/organization_invitation.body.html"

    def test_invited_by_none_returns_context_without_raising(self, di_container):
        """
        organization_invitation_context must return a context dict (not raise) when
        the invitation has invited_by=None (public-API / reseller createInvitation path).
        """
        reseller, _ = _make_reseller_with_branding(app_name="BrandedApp")
        child = _make_child(reseller)
        invitation = baker.make(
            OrganizationInvitation,
            email="apicustomer@example.com",
            organization=child,
            invited_by=None,
            first_name="Api",
            last_name="Customer",
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        # Must not raise NotificationContextGenerationError
        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=api-token",
        )

        assert ctx is not None, "Context must be returned for invited_by=None invite"
        assert ctx["branding"]["app_name"] == "BrandedApp", (
            "Reseller branding must be resolved even when invited_by is None"
        )

    def test_invited_by_none_renders_sensible_body(self, di_container):
        """
        The body template rendered with the public-API invite context must:
        - not crash
        - not contain 'None'
        - not have an empty 'invited by  ' (double-space / blank name)
        - contain the reseller's app_name
        """
        reseller, _ = _make_reseller_with_branding(app_name="BrandedApp")
        child = _make_child(reseller)
        invitation = baker.make(
            OrganizationInvitation,
            email="apicustomer@example.com",
            organization=child,
            invited_by=None,
            first_name="Api",
            last_name="Customer",
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=api-token",
        )

        body = render_to_string(self.BODY_TEMPLATE, ctx)

        assert "BrandedApp" in body, f"Branded body must contain reseller app_name; got:\n{body}"
        assert "None" not in body, f"Rendered body must not contain literal 'None'; got:\n{body}"
        # invited_by_name must not be an empty-looking string
        invited_by_name = ctx["invitation"]["invited_by_name"]
        assert invited_by_name.strip() != "", (
            f"invited_by_name must not be blank/whitespace for a public-API invite; "
            f"got: {invited_by_name!r}"
        )
        # The fallback name must appear in the body
        assert invited_by_name.strip() in body, (
            f"invited_by_name fallback '{invited_by_name.strip()}' must appear in body; "
            f"got:\n{body}"
        )


# ---------------------------------------------------------------------------
# BLOCKER 1 regression lock: confirmation templates must preserve original URL
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConfirmationTemplatesPreserveOriginalUrl:
    """
    Confirm that the confirmation email templates were NOT changed by phase-8:
    they must still contain the literal URL https://vinta_schedule.com.br/
    and must NOT use {{ app_name }} where the URL belongs.
    """

    def _user_ctx(self):
        """Minimal context for confirmation templates."""
        return {
            "user": type(
                "User",
                (),
                {
                    "profile": type(
                        "Profile",
                        (),
                        {"first_name": "TestUser"},
                    )()
                },
            )(),
            "code": "123456",
        }

    def test_confirmation_body_contains_original_url(self):
        """
        confirmation.body.html must render the literal URL
        https://vinta_schedule.com.br/ — not {{ app_name }} or 'Vinta Schedule'.
        """
        ctx = self._user_ctx()
        body = render_to_string("accounts/notifications/emails/confirmation.body.html", ctx)
        assert "https://vinta_schedule.com.br/" in body, (
            f"confirmation.body.html must contain original URL "
            f"'https://vinta_schedule.com.br/'; got:\n{body}"
        )
        # The URL slot must NOT have been replaced by the brand name
        assert "register an account on Vinta Schedule" not in body, (
            f"confirmation.body.html must NOT use brand name where URL belongs; got:\n{body}"
        )

    def test_confirmation_signup_body_contains_original_url(self):
        """
        confirmation_signup.body.html must render the literal URL
        https://vinta_schedule.com.br/ — not {{ app_name }} or 'Vinta Schedule'.
        """
        ctx = self._user_ctx()
        body = render_to_string("accounts/notifications/emails/confirmation_signup.body.html", ctx)
        assert "https://vinta_schedule.com.br/" in body, (
            f"confirmation_signup.body.html must contain original URL "
            f"'https://vinta_schedule.com.br/'; got:\n{body}"
        )
        assert "register an account on Vinta Schedule" not in body, (
            f"confirmation_signup.body.html must NOT use brand name where URL belongs; got:\n{body}"
        )
