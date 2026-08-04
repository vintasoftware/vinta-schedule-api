"""Tests for OrganizationBranding model, resolve_branding function, and redirect_url
validation."""

import datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

import pytest
from model_bakery import baker
from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationTypes
from vintasend.services.dataclasses import NotificationContextDict
from vintasend.services.notification_service import NotificationService
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)

from notifications.notification_adapters.django_email import (
    ReplyToDjangoEmailNotificationAdapter,
)
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
from organizations.notification_contexts import (
    VINTA_DEFAULT_APP_NAME,
    VINTA_DEFAULT_PRIMARY_COLOR,
    VINTA_DEFAULT_SECONDARY_COLOR,
    organization_invitation_context,
)
from organizations.permissions import (
    BrandingWriteGateReason,
    evaluate_branding_write_gate,
    is_branding_eligible_organization,
)
from organizations.redirect_url_validation import validate_redirect_url
from organizations.serializers import CurrentMembershipSerializer, MyMembershipSerializer
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


@pytest.mark.django_db
class TestGetBrandingRootParentlessResolution:
    """``Organization.get_branding_root()`` -- Organization Auth-Area Branding plan,
    Phase 5. Widens resolution so a branded parentless organization can be its own
    branding root. The reseller branch is checked FIRST and is unchanged -- that
    ordering is what preserves reseller precedence; only a parentless organization
    that is NOT a reseller newly resolves to itself. A child under a non-reseller
    parent must still resolve to ``None`` -- it cannot brand itself (enforced by the
    write gate) and must not silently pick up its own identity as a fallback.
    """

    def test_parentless_non_reseller_returns_itself(self):
        """The one new behavior this phase adds."""
        org = baker.make(Organization, parent=None, can_invite_organizations=False)

        assert org.get_branding_root() == org

    def test_reseller_still_returns_itself_via_the_unchanged_reseller_branch(self):
        """A reseller is parentless too, but it must resolve via the FIRST branch
        (it is a reseller), not fall through to the new parentless fallback --
        pinned so a future refactor can't collapse the two branches and still pass
        the simpler ``test_parentless_non_reseller_returns_itself`` case above."""
        reseller = baker.make(Organization, parent=None, can_invite_organizations=True)

        assert reseller.get_branding_root() == reseller

    def test_child_under_a_reseller_still_returns_the_reseller(self):
        """Reseller precedence is unchanged: a child underneath a reseller resolves
        to the reseller, never to itself, even though the reseller is also
        parentless."""
        reseller = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)

        assert child.get_branding_root() == reseller

    def test_grandchild_under_a_reseller_still_returns_the_reseller(self):
        reseller = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)

        assert grandchild.get_branding_root() == reseller

    def test_child_under_a_non_reseller_parent_returns_none(self):
        """A child under a non-reseller parent cannot brand itself (enforced by the
        write gate) and has no reseller ancestor to inherit from -- it must NOT
        resolve to itself, unlike its parentless parent."""
        parent = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=parent, can_invite_organizations=False)

        assert child.get_branding_root() is None

    def test_grandchild_under_a_non_reseller_chain_returns_none(self):
        root = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)

        assert grandchild.get_branding_root() is None


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
class TestResolveBrandingForDisplayParentlessOrganization:
    """``resolve_branding_for_display`` for a parentless, non-reseller organization
    -- Organization Auth-Area Branding plan, Phase 5. Exercises the same entitlement
    gate as ``TestResolveBrandingForDisplayEntitlementGate`` above, but through the
    newly-widened root (a parentless org resolving to itself) rather than through a
    reseller ancestor -- pinning Use-case 6 (a branded organization downgrades): the
    saved values are retained in the database but stop being applied the moment the
    entitlement is lost, and re-apply with no re-entry once it returns.
    """

    def test_entitled_parentless_organization_own_branding_is_applied(self):
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None)
        branding = baker.make(OrganizationBranding, organization=org, app_name="SoloBrand")

        result = resolve_branding_for_display(org)
        assert result is not None
        assert result.id == branding.id

    def test_unentitled_parentless_organization_display_is_none_but_row_persists(self):
        """The downgrade case: display resolves to ``None`` (defaults apply
        upstream), while the row itself is untouched in the database -- nothing is
        deleted, only stops being read for presentation."""
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None)
        branding = baker.make(OrganizationBranding, organization=org, app_name="KeepMe")

        assert resolve_branding_for_display(org) is None
        # The raw row survives the downgrade untouched -- "stops applying" is not
        # "deleted".
        assert OrganizationBranding.objects.get(id=branding.id).app_name == "KeepMe"

    def test_reupgrade_applies_the_saved_values_with_no_re_entry(self):
        """On re-upgrade, the previously-saved values apply again -- there is no
        separate re-save step, since the row was never touched by the downgrade."""
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None)
        branding = baker.make(OrganizationBranding, organization=org, app_name="KeepMe")
        assert resolve_branding_for_display(org) is None

        subscription_entitlement = SubscriptionEntitlement.objects.get(
            subscription__organization=org, entitlement_key=Entitlement.WHITE_LABEL_BRANDING
        )
        subscription_entitlement.is_enabled = True
        subscription_entitlement.save(update_fields=["is_enabled"])

        result = resolve_branding_for_display(org)
        assert result is not None
        assert result.id == branding.id
        assert result.app_name == "KeepMe"


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
class TestCanManageBrandingCapabilityField:
    """``can_manage_branding`` on ``CurrentMembershipSerializer`` and
    ``MyMembershipSerializer`` (Organization Auth-Area Branding plan, Phase 4
    Capability signal guiding decision).

    Must track ``is_branding_eligible_organization`` (the two-condition,
    parentless-and-entitled gate) exactly -- NOT ``evaluate_branding_write_gate``
    (the three-condition write gate). The key regression this pins: a
    parentless, entitled organization with NO slug still reports
    ``can_manage_branding is True`` -- folding the slug condition in would hide
    the branding page from exactly the admins who are one step away from using
    it (spec: "Eligible org with no public identifier yet").
    """

    def _membership(self, organization: Organization, role: str = OrganizationRole.ADMIN):
        user = baker.make(User)
        return baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=role,
            is_active=True,
        )

    def test_true_for_a_parentless_entitled_organization(self):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None, slug="eligible-org"
        )
        membership = self._membership(org)

        assert CurrentMembershipSerializer(membership).data["can_manage_branding"] is True
        assert MyMembershipSerializer(membership).data["can_manage_branding"] is True

    def test_true_for_a_parentless_entitled_organization_with_no_slug(self):
        """The key case: NOT including the slug condition. Pins the split against
        `evaluate_branding_write_gate`, which would return NO_SLUG (falsy) for
        this exact organization."""
        org = _org_with_entitlement(Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None)
        assert org.slug is None
        membership = self._membership(org)

        assert CurrentMembershipSerializer(membership).data["can_manage_branding"] is True
        assert MyMembershipSerializer(membership).data["can_manage_branding"] is True
        # ... and pin the split explicitly: the three-condition gate refuses here.
        assert evaluate_branding_write_gate(org) is BrandingWriteGateReason.NO_SLUG

    def test_false_for_a_parented_organization(self):
        parent_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None, slug="parent-org-cap"
        )
        child_org = baker.make(Organization, parent=parent_org, slug="child-org-cap")
        membership = self._membership(child_org)

        assert CurrentMembershipSerializer(membership).data["can_manage_branding"] is False
        assert MyMembershipSerializer(membership).data["can_manage_branding"] is False

    def test_false_for_an_unentitled_organization(self):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None, slug="free-plan-cap"
        )
        membership = self._membership(org)

        assert CurrentMembershipSerializer(membership).data["can_manage_branding"] is False
        assert MyMembershipSerializer(membership).data["can_manage_branding"] is False

    def test_non_admin_membership_reports_the_same_org_level_capability(self):
        """`can_manage_branding` is an organization-level fact, not a per-role
        one -- a non-admin member's entry reports it identically to an admin's.
        Write authorization (who may actually POST/PATCH) is a separate,
        role-based check (`IsOrganizationAdmin`) on the branding endpoints
        themselves."""
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None, slug="member-org-cap"
        )
        admin_membership = self._membership(org, role=OrganizationRole.ADMIN)
        member_membership = self._membership(org, role=OrganizationRole.MEMBER)

        assert (
            CurrentMembershipSerializer(admin_membership).data["can_manage_branding"]
            is CurrentMembershipSerializer(member_membership).data["can_manage_branding"]
            is True
        )

    def test_tracks_the_shared_helper_rather_than_duplicating_it(self):
        """Every case above is also directly pinned against
        `is_branding_eligible_organization` itself, so a change to the shared
        helper's semantics is guaranteed to move this field too."""
        eligible = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=True, parent=None
        )
        ineligible = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING, is_enabled=False, parent=None
        )

        for org in (eligible, ineligible):
            membership = self._membership(org)
            expected = is_branding_eligible_organization(org)
            assert CurrentMembershipSerializer(membership).data["can_manage_branding"] == expected
            assert MyMembershipSerializer(membership).data["can_manage_branding"] == expected


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

    def test_branded_parentless_non_reseller_organization_carries_its_own_identity(self):
        """Organization Auth-Area Branding plan, Phase 5 -- Use-case 2. A parentless
        organization that is NOT a reseller can now be its own branding root, so its
        invitation email carries its own app name, logo, and colors -- not the vinta
        default and not some other organization's."""
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=False,
            parent=None,
        )
        org.slug = "solo-brand"
        org.save(update_fields=["slug"])
        baker.make(
            OrganizationBranding,
            organization=org,
            app_name="SoloBrand",
            logo="uploads/branding_logos/solo-logo.png",
            primary_color="#123456",
            secondary_color="#654321",
        )
        invitation = self._make_invitation(org)

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        branding_ctx = ctx["branding"]
        assert branding_ctx["app_name"] == "SoloBrand"
        assert branding_ctx["logo_url"].endswith("/branding/logo/solo-brand/")
        assert branding_ctx["primary_color"] == "#123456"
        assert branding_ctx["secondary_color"] == "#654321"

    def test_unentitled_parentless_organization_with_a_row_still_uses_vinta_defaults(self):
        """Use-case 6 -- a downgrade retains the saved values in the database but the
        invitation email must revert fully to the vinta defaults, not a mix of the
        two."""
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=False,
            parent=None,
        )
        org.slug = "downgraded-org"
        org.save(update_fields=["slug"])
        baker.make(
            OrganizationBranding,
            organization=org,
            app_name="ShouldNotAppear",
            logo="uploads/branding_logos/should-not-appear.png",
            primary_color="#ABCDEF",
            secondary_color="#FEDCBA",
        )
        invitation = self._make_invitation(org)

        ctx = organization_invitation_context(
            organization_invitation_id=invitation.id,
            invitation_url="https://example.com/accept?token=fake",
        )

        branding_ctx = ctx["branding"]
        assert branding_ctx["app_name"] == VINTA_DEFAULT_APP_NAME
        assert branding_ctx["logo_url"].endswith("/branding/logo/default/")
        assert branding_ctx["primary_color"] == VINTA_DEFAULT_PRIMARY_COLOR
        assert branding_ctx["secondary_color"] == VINTA_DEFAULT_SECONDARY_COLOR
        # The row itself is untouched -- retained, not deleted.
        assert OrganizationBranding.objects.filter(
            organization=org, app_name="ShouldNotAppear"
        ).exists()


def _build_email_notification_service() -> NotificationService:
    """A NotificationService wired with only the real email adapter (mirrors the
    email channel of the DI wiring in di_core/containers.py), so
    ``create_one_off_notification`` sends through the actual send path -- context
    resolution, template rendering, and ``ReplyToDjangoEmailNotificationAdapter`` --
    landing in ``django.core.mail.outbox`` under the test settings' locmem backend."""
    return NotificationService(
        notification_adapters=[
            ReplyToDjangoEmailNotificationAdapter(
                DjangoTemplatedEmailRenderer(),
                DjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=DjangoDbNotificationBackend(),
    )


@pytest.mark.django_db
class TestInvitationReplyToEmailSend:
    """Organization Auth-Area Branding plan, Phase 6 -- Use-case 2's reply-to half.

    Sends a real invitation email end-to-end (context resolution -> template
    rendering -> ReplyToDjangoEmailNotificationAdapter) and inspects
    ``django.core.mail.outbox``. The From address must be identical in every case
    -- branded, unbranded, or downgraded -- because Non-goals forbid a custom
    sender; only the reply-to may vary.
    """

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

    def _send_invitation_email(self, invitation: OrganizationInvitation) -> None:
        service = _build_email_notification_service()
        service.create_one_off_notification(
            email_or_phone=invitation.email,
            first_name=invitation.first_name,
            last_name=invitation.last_name,
            notification_type=NotificationTypes.EMAIL.value,
            title="Invitation to join organization",
            body_template="organizations/emails/organization_invitation.body.html",
            context_name="organization_invitation_context",
            context_kwargs=NotificationContextDict(
                {
                    "organization_invitation_id": invitation.id,
                    "invitation_url": "https://example.com/accept?token=fake",
                }
            ),
            subject_template="organizations/emails/organization_invitation.subject.txt",
            preheader_template="organizations/emails/organization_invitation.pre_header.txt",
        )

    def test_branded_entitled_organization_reply_to_is_its_support_address(self):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            parent=None,
        )
        org.slug = "brandco-replyto"
        org.save(update_fields=["slug"])
        baker.make(
            OrganizationBranding,
            organization=org,
            app_name="BrandCo",
            support_email="support@brandco.example",
        )
        invitation = self._make_invitation(org)

        self._send_invitation_email(invitation)

        assert len(mail.outbox) == 1
        sent = mail.outbox[0]
        assert sent.from_email == NotificationSettings().NOTIFICATION_DEFAULT_FROM_EMAIL
        assert sent.reply_to == ["support@brandco.example"]
        # The From address is the one accepted exception (spec Objective 1) -- it
        # must NOT be the organization's support address.
        assert sent.from_email != "support@brandco.example"

    def test_unbranded_organization_reply_to_is_our_address_same_as_from(self):
        """No branding row at all -- today's behavior, unchanged."""
        org = baker.make(Organization, parent=None)
        invitation = self._make_invitation(org)

        self._send_invitation_email(invitation)

        assert len(mail.outbox) == 1
        sent = mail.outbox[0]
        assert sent.from_email == NotificationSettings().NOTIFICATION_DEFAULT_FROM_EMAIL
        assert sent.reply_to == [sent.from_email]

    def test_unentitled_organization_with_a_branding_row_reply_to_is_our_address(self):
        """A downgraded organization keeps its saved branding row (support_email
        included), but the invitation must fully revert to vinta defaults -- reply-to
        included, not just app_name/logo/colors."""
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=False,
            parent=None,
        )
        org.slug = "downgraded-replyto"
        org.save(update_fields=["slug"])
        baker.make(
            OrganizationBranding,
            organization=org,
            app_name="ShouldNotAppear",
            support_email="support@shouldnotappear.example",
        )
        invitation = self._make_invitation(org)

        self._send_invitation_email(invitation)

        assert len(mail.outbox) == 1
        sent = mail.outbox[0]
        assert sent.from_email == NotificationSettings().NOTIFICATION_DEFAULT_FROM_EMAIL
        assert sent.reply_to == [sent.from_email]

    def test_branded_entitled_organization_with_no_support_email_falls_back(self):
        """An entitled, branded organization that never set a support email gets
        our address as reply-to too -- a blank support_email is falsy, not a
        distinct "empty reply-to" state."""
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            parent=None,
        )
        org.slug = "brandco-no-support-email"
        org.save(update_fields=["slug"])
        baker.make(
            OrganizationBranding,
            organization=org,
            app_name="BrandCo",
            support_email="",
        )
        invitation = self._make_invitation(org)

        self._send_invitation_email(invitation)

        sent = mail.outbox[0]
        assert sent.reply_to == [sent.from_email]
