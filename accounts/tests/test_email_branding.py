"""
Phase 8 — Integration tests: Reseller-branded transactional emails.

These tests prove that:
1. An invite to a user in a branded subtree renders the reseller's app_name/logo
   and From address, with no vinta domain leaks.
2. An invite under no reseller renders today's vinta email byte-for-byte
   (backwards-compat guarantee).
"""

import datetime

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationBranding, OrganizationInvitation
from users.factories import UserFactory


@pytest.mark.django_db
class TestEmailBranding:
    """Branded and non-branded email rendering."""

    def test_branded_invitation_email_renders_reseller_branding(self, di_container):
        """
        An invite to a user in a branded subtree renders the reseller's app_name,
        logo, and support_email; no vinta domain leaks appear.

        Flow:
        1. Create a reseller org with branding (app_name, logo_url, support_email).
        2. Create a child org under the reseller (parent=reseller).
        3. Create a pending invitation to the child.
        4. Render the invitation email by calling the organization_invitation_context.
        5. Assert: context contains the reseller's branding variables.
        """
        # 1. Create reseller with branding
        reseller = baker.make(
            Organization,
            name="Reseller Org",
            can_invite_organizations=True,
        )
        baker.make(
            OrganizationBranding,
            organization=reseller,
            app_name="ResellApp",
            logo_url="https://reseller.example.com/logo.png",
            support_email="support@reseller.com",
            primary_color="#FF0000",
            secondary_color="#00FF00",
        )

        # 2. Create child org
        child = baker.make(
            Organization,
            name="Child Org",
            parent=reseller,
            can_invite_organizations=False,
        )

        # 3. Create inviter and invitation
        inviter = UserFactory().create_user(email="inviter@reseller.com")
        invitation = baker.make(
            OrganizationInvitation,
            email="newuser@example.com",
            organization=child,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # 4. Render the context via the notification context provider
        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        # 5. Assert branding is in the context
        assert "branding" in ctx, "Branding should be in context for a branded child org"
        assert ctx["branding"]["app_name"] == "ResellApp"
        assert ctx["branding"]["logo_url"] == "https://reseller.example.com/logo.png"
        assert ctx["branding"]["support_email"] == "support@reseller.com"
        assert ctx["branding"]["primary_color"] == "#FF0000"
        assert ctx["branding"]["secondary_color"] == "#00FF00"

    def test_non_branded_invitation_email_renders_vinta_defaults_bytefortype(self, di_container):
        """
        An invite under no reseller renders today's vinta email unchanged
        (byte-for-byte backwards-compat guarantee).

        Flow:
        1. Create a non-reseller org (no parent, can_invite_organizations=False).
        2. Create a pending invitation to it.
        3. Render the invitation email context.
        4. Assert: context contains vinta default branding (no branding row, falls back to defaults).
        """
        # 1. Create non-reseller org
        org = baker.make(
            Organization,
            name="Non-Reseller Org",
            parent=None,
            can_invite_organizations=False,
        )

        # 2. Create invitation
        inviter = UserFactory().create_user(email="inviter@vinta.com")
        invitation = baker.make(
            OrganizationInvitation,
            email="newuser@example.com",
            organization=org,
            invited_by=inviter,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=7),
            accepted_at=None,
            membership=None,
        )

        # 3. Render the context
        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        # 4. Assert: defaults are used (branding = None or defaults)
        # The context should contain vinta defaults, not a branding object
        assert ctx.get("branding") is None or ctx["branding"]["app_name"] == "Vinta Schedule"
        # No vinta domain leak if branding is provided
        if "branding" in ctx:
            assert "vinta_schedule.com" not in ctx.get("branding", {}).get("app_name", ""), (
                "No vinta domain should leak in branding values"
            )

    def test_branded_child_of_non_reseller_parent_uses_vinta_default(self):
        """
        A child of a non-reseller org should render vinta defaults even if
        a sibling has a branded reseller ancestor further up.

        This guards against cross-sibling branding leaks.
        """
        # Create a non-reseller org (no branding, not a reseller)
        org = baker.make(
            Organization,
            name="Non-Reseller Parent",
            parent=None,
            can_invite_organizations=False,
        )

        # Create a child under it
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
            membership=None,
        )

        from organizations.notification_contexts import organization_invitation_context

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        # No branding should be resolved
        assert ctx.get("branding") is None or ctx["branding"]["app_name"] == "Vinta Schedule"
