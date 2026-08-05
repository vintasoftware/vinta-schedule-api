"""Tests for OrganizationBranding model, resolve_branding function, and redirect_url
validation."""

import datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.branding_logo import sign_branding_logo_upload
from organizations.exceptions import BrandingLogoUploadRejectedError
from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    resolve_branding,
    resolve_branding_for_display,
)
from organizations.notification_contexts import organization_invitation_context
from organizations.permissions import (
    BrandingWriteGateReason,
    evaluate_branding_write_gate,
    is_branding_eligible_organization,
)
from organizations.redirect_url_validation import validate_redirect_url
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement
from users.factories import UserFactory


User = get_user_model()


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
                "logo": "uploads/branding_logos/logo1.png",
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
                "logo": "uploads/branding_logos/logo2.png",
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
        assert refreshed.logo.name == "uploads/branding_logos/logo2.png"
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


def _org_with_entitlement(entitlement_key: str, is_enabled: bool, **org_kwargs) -> Organization:
    """A (by default parentless) organization whose subscription carries an explicit
    ``SubscriptionEntitlement`` row for ``entitlement_key``. Generalizes
    ``_reseller_with_entitlement`` above to an arbitrary org (this module opts out of
    the autouse ``provision_default_subscription`` fixture, so every entitlement-gated
    test must build its own subscription explicitly)."""
    org = baker.make(Organization, **org_kwargs)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=org,
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
    return org


@pytest.mark.django_db
class TestEvaluateBrandingWriteGate:
    """``organizations.permissions.evaluate_branding_write_gate`` -- the full
    three-condition write gate (Organization Auth-Area Branding plan, Phase 3):
    parentless AND entitled AND slug-set. Composes on top of the two-condition
    ``is_branding_eligible_organization`` (Phase 2b), which stays the
    logo-signing surface's gate -- see ``test_two_condition_helper_stays_free_
    of_the_slug_condition`` below for that split pinned as a regression test.
    """

    def test_admits_a_parentless_entitled_slugged_organization(self):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None, slug="eligible-org"
        )

        assert evaluate_branding_write_gate(org) is BrandingWriteGateReason.OK

    def test_refuses_an_organization_with_a_parent(self):
        parent_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None, slug="parent-org"
        )
        # No subscription needed on the child: the gate short-circuits on
        # `parent_id is not None` before ever checking entitlement or slug.
        child_org = baker.make(Organization, parent=parent_org, slug="child-org")

        assert evaluate_branding_write_gate(child_org) is BrandingWriteGateReason.HAS_PARENT

    def test_refuses_an_unentitled_organization(self):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None, slug="free-plan-org"
        )

        assert evaluate_branding_write_gate(org) is BrandingWriteGateReason.NOT_ENTITLED

    def test_refuses_an_organization_with_no_slug(self):
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None)
        assert org.slug is None

        assert evaluate_branding_write_gate(org) is BrandingWriteGateReason.NO_SLUG

    def test_the_three_refusals_are_distinguishable(self):
        """Each failure mode produces its own reason, not a bare False -- this is
        the whole point of the enum-returning gate over a boolean helper."""
        parent_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None, slug="parent-org-2"
        )
        child_org = baker.make(Organization, parent=parent_org)
        unentitled_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None, slug="unentitled-org"
        )
        no_slug_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None
        )

        reasons = {
            evaluate_branding_write_gate(child_org),
            evaluate_branding_write_gate(unentitled_org),
            evaluate_branding_write_gate(no_slug_org),
        }

        assert reasons == {
            BrandingWriteGateReason.HAS_PARENT,
            BrandingWriteGateReason.NOT_ENTITLED,
            BrandingWriteGateReason.NO_SLUG,
        }

    def test_two_condition_helper_stays_free_of_the_slug_condition(self):
        """The logo-signing surface's gate (`is_branding_eligible_organization`)
        must still admit a slug-less eligible organization -- requiring a slug
        before an admin can upload a logo would order the branding form around
        an implementation detail (Write gate guiding decision). Pins the split
        between the two-condition and three-condition gates as a regression
        test: a future change that folds the slug condition into
        `is_branding_eligible_organization` would flip this assertion."""
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None)
        assert org.slug is None

        assert is_branding_eligible_organization(org) is True
        assert evaluate_branding_write_gate(org) is BrandingWriteGateReason.NO_SLUG


@pytest.mark.django_db
class TestBrandingLogoDestinationAuth:
    """The ``branding_logos`` S3Direct destination's ``auth`` callable
    (``organizations.permissions.user_administers_branding_eligible_organization``,
    wired in via ``vinta_schedule_api.settings.base._user_administers_branding_eligible_organization``).

    Admits a user holding an active ADMIN membership in at least one parentless,
    ``white_label_branding``-entitled organization. Refuses a free-plan user, a
    non-admin member, and an admin of an organization that has a parent -- the same
    four cases the GraphQL logo-signing mutation's gate is tested against.
    """

    def _auth_callable(self):
        from django.conf import settings

        return settings.S3DIRECT_DESTINATIONS["branding_logos"]["auth"]

    def _make_admin(self, organization: Organization):
        user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        return user

    def test_admits_admin_of_an_eligible_organization(self):
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None)
        user = self._make_admin(org)

        assert self._auth_callable()(user) is True

    def test_refuses_a_free_plan_user(self):
        """Admin of a parentless organization that lacks the entitlement."""
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None)
        user = self._make_admin(org)

        assert self._auth_callable()(user) is False

    def test_refuses_a_non_admin_member(self):
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None)
        user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        assert self._auth_callable()(user) is False

    def test_refuses_an_admin_of_an_organization_with_a_parent(self):
        parent_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None
        )
        # No subscription needed on the child: `is_branding_eligible_organization`
        # short-circuits on `parent_id is not None` before ever checking entitlement.
        child_org = baker.make(Organization, parent=parent_org)
        user = self._make_admin(child_org)

        assert self._auth_callable()(user) is False

    def test_refuses_an_unauthenticated_user(self):
        assert self._auth_callable()(AnonymousUser()) is False

    def test_refuses_none_user(self):
        assert self._auth_callable()(None) is False


class TestSignBrandingLogoUpload:
    """``organizations.branding_logo.sign_branding_logo_upload`` -- the ``branding_logos``
    destination's content-type allowlist and size cap, re-checked independently of the
    shipped s3direct signing view (this is what the GraphQL signing mutation calls)."""

    @pytest.mark.parametrize("content_type", ["image/png", "image/jpeg", "image/webp"])
    def test_accepts_allowed_content_types(self, content_type):
        payload = sign_branding_logo_upload("logo.png", content_type, 1024)
        assert payload["object_key"]

    def test_rejects_svg_explicitly(self):
        """SVG is the one format a reviewer will assume works -- it does not."""
        with pytest.raises(BrandingLogoUploadRejectedError, match="image/svg"):
            sign_branding_logo_upload("logo.svg", "image/svg+xml", 1024)

    def test_rejects_an_unlisted_content_type(self):
        with pytest.raises(BrandingLogoUploadRejectedError):
            sign_branding_logo_upload("logo.gif", "image/gif", 1024)

    def test_rejects_an_oversized_file(self):
        from django.conf import settings

        oversized = settings.BRANDING_LOGO_MAX_SIZE_BYTES + 1
        with pytest.raises(BrandingLogoUploadRejectedError, match="size"):
            sign_branding_logo_upload("logo.png", "image/png", oversized)

    def test_accepts_a_file_at_exactly_the_size_cap(self):
        from django.conf import settings

        payload = sign_branding_logo_upload(
            "logo.png", "image/png", settings.BRANDING_LOGO_MAX_SIZE_BYTES
        )
        assert payload["object_key"]


@pytest.mark.django_db
class TestInvitationContextLogoUrl:
    """The invitation email context's ``logo_url`` is the logo delivery route's
    ABSOLUTE URL -- never a signed S3 URL (would carry query-string auth params) and
    never a bare key (would have no scheme/host). See
    ``organizations.branding_logo.build_logo_delivery_url``."""

    def _make_invitation(self, organization: Organization) -> OrganizationInvitation:
        inviter = UserFactory().create_user(email="inviter@example.com")
        return baker.make(
            OrganizationInvitation,
            email="invitee@example.com",
            organization=organization,
            invited_by=inviter,
            expires_at=timezone.now() + datetime.timedelta(days=7),
            accepted_at=None,
            membership_user_id=None,
        )

    def test_branded_organization_logo_is_an_absolute_delivery_url(self):
        reseller = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
        )
        reseller.slug = "brandco"
        reseller.save(update_fields=["slug"])
        baker.make(
            OrganizationBranding,
            organization=reseller,
            app_name="BrandCo",
            logo="uploads/branding_logos/brandco-logo.png",
        )
        invitation = self._make_invitation(reseller)

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        logo_url = ctx["branding"]["logo_url"]
        assert logo_url.startswith("http://") or logo_url.startswith("https://"), logo_url
        assert logo_url.endswith("/branding/logo/brandco/"), logo_url
        assert "?" not in logo_url, f"must not carry signed-URL query params: {logo_url}"
        assert "uploads/branding_logos" not in logo_url, f"must not be the bare key: {logo_url}"

    def test_unbranded_organization_logo_resolves_to_the_default_sentinel(self):
        org = baker.make(Organization, parent=None)
        invitation = self._make_invitation(org)

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        logo_url = ctx["branding"]["logo_url"]
        assert logo_url.startswith("http://") or logo_url.startswith("https://"), logo_url
        assert logo_url.endswith("/branding/logo/default/"), logo_url
