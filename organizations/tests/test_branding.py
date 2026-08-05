"""Tests for OrganizationBranding model, resolve_branding function, and redirect_url
validation."""

import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import (
    Organization,
    OrganizationBranding,
    resolve_branding,
    resolve_branding_for_display,
)
from organizations.redirect_url_validation import validate_redirect_url
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
                "redirect_url": "https://example.com/first",
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
                "redirect_url": "https://example.com/second",
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
        assert refreshed.redirect_url == "https://example.com/second"

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
    """``white_label_branding`` gates branding resolution for *presentation*.

    A reseller whose plan does not grant the entitlement is treated identically to one
    with no branding row at all -- every presentation caller already falls back to the
    vinta default in that case, so this degrades gracefully rather than erroring.

    The gate lives on ``resolve_branding_for_display``, not on ``resolve_branding``:
    the latter is deliberately kept entitlement-free for a non-cosmetic, auth-flow
    caller. ``TestResolveBrandingIsUngated`` below pins that split.
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
        why the *ungated* ``resolve_branding`` stays a separate, entitlement-free
        function for non-cosmetic callers."""
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

    Its former caller, ``public_api.queries.validate_return_url``, read
    ``return_url_allowlist`` off this row to decide whether an OAuth return URL may be
    honoured -- a non-cosmetic, auth-flow decision. That query and the allowlist it
    read are gone as of Phase 2a of the Organization Auth-Area Branding plan (see
    ``resolve_branding``'s docstring), which leaves this function with no caller today.
    It stays deliberately separate from ``resolve_branding_for_display`` (and
    deliberately ungated) so a future non-cosmetic caller -- e.g. an auth-flow
    decision -- is not silently broken by a reseller downgrading off the cosmetic
    ``white_label_branding`` entitlement, which is exactly the failure mode this split
    used to prevent for the OAuth return flow.
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


class TestValidateRedirectUrl:
    """Unit tests for ``organizations.redirect_url_validation.validate_redirect_url``.

    ``redirect_url`` replaces ``return_url_allowlist``: a single stored destination,
    never a caller-supplied value, so the rules guard against the destination itself
    becoming a pattern rather than a concrete URL.
    """

    def test_empty_value_is_valid(self):
        """redirect_url is optional; '' means 'no configured destination'."""
        validate_redirect_url("")

    def test_plain_https_url_is_accepted(self):
        validate_redirect_url("https://example.com/dashboard")

    def test_https_root_is_accepted(self):
        validate_redirect_url("https://example.com")

    def test_http_scheme_is_rejected(self):
        with pytest.raises(DjangoValidationError) as exc_info:
            validate_redirect_url("http://example.com/dashboard")
        assert "https scheme" in str(exc_info.value)

    def test_non_http_scheme_is_rejected(self):
        with pytest.raises(DjangoValidationError) as exc_info:
            validate_redirect_url("ftp://example.com/dashboard")
        assert "https scheme" in str(exc_info.value)

    def test_wildcard_character_is_rejected(self):
        with pytest.raises(DjangoValidationError) as exc_info:
            validate_redirect_url("https://*.example.com/dashboard")
        assert "wildcard" in str(exc_info.value)

    def test_wildcard_in_path_is_rejected(self):
        with pytest.raises(DjangoValidationError) as exc_info:
            validate_redirect_url("https://example.com/dashboard/*")
        assert "wildcard" in str(exc_info.value)

    def test_path_prefix_pattern_is_rejected(self):
        with pytest.raises(DjangoValidationError) as exc_info:
            validate_redirect_url("https://example.com/callback/")
        assert "path-prefix" in str(exc_info.value)

    def test_root_path_with_trailing_slash_is_accepted(self):
        """The bare root is not a path-prefix pattern -- there is no segment to prefix."""
        validate_redirect_url("https://example.com/")
