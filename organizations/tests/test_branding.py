"""Tests for OrganizationBranding model and resolve_branding function (Phase 6)."""

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationBranding, resolve_branding


@pytest.mark.django_db
class TestResolveBranding:
    """Unit tests for the resolve_branding function."""

    def test_resolve_branding_for_reseller_with_branding(self):
        """resolve_branding returns the branding row for a reseller that has one."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        branding = baker.make(OrganizationBranding, organization=reseller)

        result = resolve_branding(reseller)
        assert result is not None
        assert result.id == branding.id
        assert result.organization_id == reseller.id

    def test_resolve_branding_for_reseller_without_branding(self):
        """resolve_branding returns None for a reseller with no branding row."""
        reseller = baker.make(Organization, can_invite_organizations=True)

        result = resolve_branding(reseller)
        assert result is None

    def test_resolve_branding_for_child_walks_to_reseller(self):
        """resolve_branding for a child walks up the parent chain to the reseller's branding."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        branding = baker.make(OrganizationBranding, organization=reseller)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)

        result = resolve_branding(child)
        assert result is not None
        assert result.id == branding.id

    def test_resolve_branding_for_grandchild_walks_to_reseller(self):
        """resolve_branding for a grandchild walks up multiple levels to the reseller."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        branding = baker.make(OrganizationBranding, organization=reseller)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)

        result = resolve_branding(grandchild)
        assert result is not None
        assert result.id == branding.id

    def test_resolve_branding_returns_none_when_no_reseller_ancestor(self):
        """resolve_branding returns None for an org with no reseller ancestor."""
        standalone = baker.make(Organization, can_invite_organizations=False)

        result = resolve_branding(standalone)
        assert result is None

    def test_resolve_branding_for_child_of_non_reseller_returns_none(self):
        """resolve_branding returns None when walking up stops at a non-reseller root."""
        parent = baker.make(Organization, can_invite_organizations=False)
        child = baker.make(Organization, parent=parent, can_invite_organizations=False)

        result = resolve_branding(child)
        assert result is None

    def test_upsert_updates_in_place(self):
        """update_or_create on the same organization updates the row (one row, updated values)."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        branding1, _ = OrganizationBranding.objects.update_or_create(
            organization=reseller,
            defaults={
                "app_name": "First",
                "logo_url": "https://example.com/logo1.png",
                "primary_color": "#FF0000",
                "secondary_color": "#00FF00",
                "support_email": "first@example.com",
                "return_url_allowlist": ["https://example.com"],
            },
        )

        # Update the same org
        branding2, _ = OrganizationBranding.objects.update_or_create(
            organization=reseller,
            defaults={
                "app_name": "Second",
                "logo_url": "https://example.com/logo2.png",
                "primary_color": "#0000FF",
                "secondary_color": "#FFFF00",
                "support_email": "second@example.com",
                "return_url_allowlist": ["https://example.com", "https://other.com"],
            },
        )

        # Should be the same row
        assert branding1.id == branding2.id

        # Should have updated values
        refreshed = OrganizationBranding.objects.get(id=branding1.id)
        assert refreshed.app_name == "Second"
        assert refreshed.logo_url == "https://example.com/logo2.png"
        assert refreshed.primary_color == "#0000FF"
        assert refreshed.secondary_color == "#FFFF00"
        assert refreshed.support_email == "second@example.com"
        assert refreshed.return_url_allowlist == [
            "https://example.com",
            "https://other.com",
        ]

        # Should only have one OrganizationBranding row for this org
        assert OrganizationBranding.objects.filter(organization=reseller).count() == 1
