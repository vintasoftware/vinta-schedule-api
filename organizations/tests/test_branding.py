"""Tests for OrganizationBranding model and resolve_branding function (Phase 6)."""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import (
    Organization,
    OrganizationBranding,
    resolve_branding,
    resolve_branding_for_display,
)
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


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


def _reseller_with_entitlement(entitlement_key: str, is_enabled: bool) -> Organization:
    """A reseller organization whose subscription carries an explicit
    ``SubscriptionEntitlement`` row for ``entitlement_key``."""
    reseller = baker.make(Organization, can_invite_organizations=True)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=reseller,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionEntitlement,
        subscription=subscription,
        entitlement_key=entitlement_key,
        is_enabled=is_enabled,
    )
    return reseller


@pytest.mark.django_db
class TestResolveBrandingForDisplayEntitlementGate:
    """Phase 6c: ``white_label_branding`` gates branding resolution for *presentation*.

    A reseller whose plan does not grant the entitlement is treated identically to one
    with no branding row at all -- every presentation caller already falls back to the
    vinta default in that case, so this degrades gracefully rather than erroring.

    The gate lives on ``resolve_branding_for_display``, not on ``resolve_branding``:
    ``validate_return_url`` reads the same row to make an auth-flow decision and must
    not be gated. ``TestResolveBrandingIsUngated`` below pins that split.
    """

    def test_branding_is_hidden_when_the_entitlement_is_disabled(self):
        reseller = _reseller_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=False)
        baker.make(OrganizationBranding, organization=reseller)

        assert resolve_branding_for_display(reseller) is None

    def test_branding_is_hidden_when_the_entitlement_row_is_missing(self):
        """No row at all is how a revoked grant is represented -- same outcome as
        an explicit ``is_enabled=False`` row."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        now = timezone.now()
        baker.make(
            Subscription,
            organization=reseller,
            plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
            billing_state=BillingState.FREE,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
        )
        baker.make(OrganizationBranding, organization=reseller)

        assert resolve_branding_for_display(reseller) is None

    def test_branding_is_hidden_when_the_reseller_has_no_subscription(self):
        """``has_entitlement`` fails closed on a plan-less organization, and this
        caller inherits that. Cosmetic degradation, not a lockout -- which is exactly
        why the *ungated* ``resolve_branding`` exists for the allowlist reader."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        baker.make(OrganizationBranding, organization=reseller)

        assert resolve_branding_for_display(reseller) is None

    def test_branding_is_returned_when_the_entitlement_is_enabled(self):
        reseller = _reseller_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True)
        branding = baker.make(OrganizationBranding, organization=reseller)

        result = resolve_branding_for_display(reseller)
        assert result is not None
        assert result.id == branding.id

    def test_unlimited_plan_reseller_is_never_blocked(self):
        """The rollout's kill switch: every organization is on ``unlimited`` until
        deliberately migrated, so this must see byte-for-byte unchanged behavior."""
        from payments.services.subscription_service import SubscriptionService

        reseller = baker.make(Organization, can_invite_organizations=True)
        plan = BillingPlan.objects.get(slug="unlimited")
        SubscriptionService().create_subscription_for_organization(reseller, plan=plan)
        branding = baker.make(OrganizationBranding, organization=reseller)

        result = resolve_branding_for_display(reseller)
        assert result is not None
        assert result.id == branding.id


@pytest.mark.django_db
class TestResolveBrandingIsUngated:
    """``resolve_branding`` must stay entitlement-free.

    ``public_api.queries.validate_return_url`` reads ``return_url_allowlist`` off this
    row to decide whether an OAuth return URL may be honoured. If the cosmetic
    ``white_label_branding`` entitlement gated it, a reseller downgrading off that
    entitlement would return ``{allowed: False, sanitized_url: None}`` for every tenant
    underneath it -- breaking the OAuth return flow across the whole subtree. An
    auth-flow lockout caused by a billing change to a logo is not an acceptable
    degradation, so these two resolvers are deliberately separate functions.
    """

    def test_returns_the_row_even_without_the_entitlement(self):
        reseller = _reseller_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=False)
        branding = baker.make(OrganizationBranding, organization=reseller)

        result = resolve_branding(reseller)
        assert result is not None
        assert result.id == branding.id
        # ... and the gated sibling does hide it, so the split is real, not incidental.
        assert resolve_branding_for_display(reseller) is None

    def test_returns_the_row_for_a_reseller_with_no_subscription(self):
        reseller = baker.make(Organization, can_invite_organizations=True)
        branding = baker.make(OrganizationBranding, organization=reseller)

        result = resolve_branding(reseller)
        assert result is not None
        assert result.id == branding.id
