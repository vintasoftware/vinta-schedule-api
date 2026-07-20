"""``SubscriptionService`` — placing organizations on a plan and moving them
between plans.

The load-bearing behavior under test: plan change re-copies non-overridden
``SubscriptionPlanLimit`` / ``SubscriptionEntitlement`` rows and leaves
``is_overridden=True`` rows untouched, and catalog edits never propagate to an
already-sold subscription. See the plan's "Per-subscription limit copy" guiding
decision.
"""

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingState, Entitlement, LimitedResource, LimitKind
from payments.exceptions import (
    BillingRootCycleError,
    IncompleteBillingPlanError,
    NoDefaultBillingPlanError,
)
from payments.models import (
    BillingPlan,
    PlanEntitlement,
    PlanLimit,
    Subscription,
    SubscriptionPlanLimit,
)
from payments.services.subscription_service import (
    SubscriptionService,
    is_billing_root,
    resolve_billing_root,
)


@pytest.fixture
def service():
    return SubscriptionService()


@pytest.fixture
def entitlement_service():
    from di_core.containers import container

    return container.entitlement_service()


def make_complete_plan(limit_values: dict[str, int | None] | None = None) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for **every** ``LimitedResource``
    member, which is what ``assert_plan_is_complete`` requires of any plan a
    subscription is placed on.

    Resources not named in ``limit_values`` get ``limit_value=0`` — the catalog's
    way of saying "not included". Omitting the row instead is the authoring error
    the guard exists to reject, so a test that wants an *incomplete* plan builds
    one by hand rather than through this helper.
    """
    limit_values = limit_values or {}
    plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


@pytest.fixture
def plan():
    catalog_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 5})
    baker.make(
        PlanEntitlement,
        plan=catalog_plan,
        entitlement_key=Entitlement.PARTNER_API,
        is_enabled=True,
    )
    return catalog_plan


@pytest.mark.django_db
class TestResolveBillingRoot:
    def test_standalone_organization_resolves_to_itself(self):
        org = baker.make(Organization, parent=None, can_invite_organizations=False)

        assert resolve_billing_root(org) == org

    def test_reseller_root_resolves_to_itself(self):
        org = baker.make(Organization, parent=None, can_invite_organizations=True)

        assert resolve_billing_root(org) == org

    def test_direct_child_resolves_to_reseller_root(self):
        root = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)

        assert resolve_billing_root(child) == root

    def test_child_of_non_reseller_top_org_resolves_to_that_top_org(self):
        """A malformed tree (parent set, but no ancestor is ever flagged
        can_invite_organizations=True) still terminates at the topmost ancestor —
        which, being parent-less, is guaranteed to hold its own subscription."""
        top = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=top, can_invite_organizations=False)

        assert resolve_billing_root(child) == top

    def test_cyclic_parent_chain_raises_billing_root_cycle_error(self):
        """A revisited organization means the ``parent`` chain is a cycle.

        Returning an arbitrary node from the cycle (the previous behavior this
        test asserted: ``result.pk in (org_a.pk, org_b.pk)``) passed while the
        invariant was broken — every organization on the cycle would be left
        without a resolvable billing root, and therefore without a `Subscription`
        from the backfill / creation paths. This must raise instead."""
        org_a = baker.make(Organization, can_invite_organizations=False)
        org_b = baker.make(Organization, parent=org_a, can_invite_organizations=False)
        org_a.parent = org_b
        org_a.save(update_fields=["parent"])

        with pytest.raises(BillingRootCycleError) as exc_info:
            resolve_billing_root(org_a)

        assert {org_a.pk, org_b.pk} <= exc_info.value.visited_ids

    def test_nested_reseller_is_its_own_billing_root(self):
        """A nested reseller (``can_invite_organizations=True`` with ``parent``
        set) is its own billing root, not a child pooling against a
        grandparent's subscription (BLOCKER 1, Phase 4 review)."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        mid = baker.make(Organization, parent=root, can_invite_organizations=True)
        leaf = baker.make(Organization, parent=mid, can_invite_organizations=False)

        assert is_billing_root(mid) is True
        assert resolve_billing_root(mid) == mid
        assert resolve_billing_root(leaf) == mid


@pytest.mark.django_db
class TestCreateSubscriptionForOrganization:
    def test_creates_subscription_with_default_plan_when_none_given(self, service):
        org = baker.make(Organization, parent=None)

        subscription = service.create_subscription_for_organization(org)

        assert subscription is not None
        assert subscription.organization == org
        assert subscription.plan.slug == "unlimited"
        assert subscription.billing_state == BillingState.FREE

    def test_copies_plan_limits_and_entitlements(self, service, plan):
        org = baker.make(Organization, parent=None)

        subscription = service.create_subscription_for_organization(org, plan=plan)

        limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 5
        assert limit.is_overridden is False

        entitlement = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        assert entitlement.is_enabled is True
        assert entitlement.is_overridden is False

    def test_reseller_child_gets_no_subscription(self, service, plan):
        root = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)

        result = service.create_subscription_for_organization(child, plan=plan)

        assert result is None
        assert not Subscription.objects.filter(organization=child).exists()

    def test_nested_reseller_gets_its_own_subscription(self, service, plan):
        """root(can_invite=True) -> mid(can_invite=True) -> leaf: mid is its own
        billing root and must get its own `Subscription`, not pool against
        root's (BLOCKER 1, Phase 4 review)."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        mid = baker.make(Organization, parent=root, can_invite_organizations=True)
        leaf = baker.make(Organization, parent=mid, can_invite_organizations=False)
        service.create_subscription_for_organization(root, plan=plan)

        result = service.create_subscription_for_organization(mid, plan=plan)

        assert result is not None
        assert result.organization == mid
        assert not Subscription.objects.filter(organization=leaf).exists()
        assert resolve_billing_root(leaf) == mid

    def test_default_plan_lookup_ignores_inactive_default_plan(self, service):
        """A deactivated default plan must not 500 organization creation with an
        uncaught `BillingPlan.DoesNotExist` (SHOULD-FIX 2, Phase 4 review)."""
        BillingPlan.objects.filter(slug="unlimited").update(is_active=False)
        org = baker.make(Organization, parent=None)

        with pytest.raises(NoDefaultBillingPlanError):
            service.create_subscription_for_organization(org)

    def test_syncs_limits_and_entitlements_for_an_existing_subscription_with_none(
        self, service, plan
    ):
        """A `Subscription` that already exists but has no `SubscriptionPlanLimit`
        / `SubscriptionEntitlement` rows (e.g. created via `SubscriptionAdmin`
        with empty inlines, or `payment_service.create_subscription`) must not be
        returned silently untouched (SHOULD-FIX 1, Phase 4 verification review)."""
        org = baker.make(Organization, parent=None)
        subscription = baker.make(
            Subscription,
            organization=org,
            plan=plan,
            billing_state=BillingState.FREE,
        )
        assert not subscription.limits.exists()
        assert not subscription.entitlements.exists()

        result = service.create_subscription_for_organization(org, plan=plan)

        assert result is not None
        assert result.pk == subscription.pk
        limit = result.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 5
        entitlement = result.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        assert entitlement.is_enabled is True

    def test_idempotent_returns_existing_subscription(self, service, plan):
        org = baker.make(Organization, parent=None)

        first = service.create_subscription_for_organization(org, plan=plan)
        second = service.create_subscription_for_organization(org, plan=plan)

        assert first is not None
        assert second is not None
        assert first.pk == second.pk
        assert Subscription.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
class TestChangePlan:
    def test_replans_non_overridden_limits_and_entitlements(self, service, plan):
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        new_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 20})
        baker.make(
            PlanEntitlement,
            plan=new_plan,
            entitlement_key=Entitlement.PARTNER_API,
            is_enabled=False,
        )

        service.change_plan(subscription, new_plan)
        subscription.refresh_from_db()

        assert subscription.plan == new_plan
        limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 20
        assert limit.is_overridden is False
        entitlement = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        assert entitlement.is_enabled is False

    def test_overridden_limit_survives_plan_change_untouched(self, service, plan):
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        overridden = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        overridden.limit_value = 999
        overridden.is_overridden = True
        overridden.save()

        new_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 20})

        service.change_plan(subscription, new_plan)
        overridden.refresh_from_db()

        assert overridden.limit_value == 999
        assert overridden.is_overridden is True

    def test_overridden_entitlement_survives_plan_change_untouched(self, service, plan):
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        overridden = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        overridden.is_enabled = False
        overridden.is_overridden = True
        overridden.save()

        new_plan = make_complete_plan()
        baker.make(
            PlanEntitlement,
            plan=new_plan,
            entitlement_key=Entitlement.PARTNER_API,
            is_enabled=True,
        )

        service.change_plan(subscription, new_plan)
        overridden.refresh_from_db()

        assert overridden.is_enabled is False
        assert overridden.is_overridden is True

    def test_catalog_edit_does_not_propagate_to_existing_subscription(self, service, plan):
        """The catalog `PlanLimit` is the source used at copy time only. Editing it
        after a subscription already copied its rows must not change what the
        subscription enforces — an org keeps what it was sold."""
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        catalog_limit = plan.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        catalog_limit.limit_value = 1
        catalog_limit.save()

        sub_limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert sub_limit.limit_value == 5

    def test_downgrade_revokes_an_entitlement_absent_from_the_new_plan(self, service):
        """SHOULD-FIX 4, Phase 4 review: a non-overridden `SubscriptionEntitlement`
        whose key the new plan does not carry is a grant the org no longer pays for.

        Entitlements fail *closed* in `has_entitlement` -- absence means "not
        granted" -- so deleting the row is exactly a revocation. The limits side
        cannot do the same thing; see
        `test_downgrade_does_not_turn_an_omitted_limit_into_an_unlimited_ceiling`.
        """
        org = baker.make(Organization, parent=None)
        old_plan = make_complete_plan()
        baker.make(
            PlanEntitlement,
            plan=old_plan,
            entitlement_key=Entitlement.PARTNER_API,
            is_enabled=True,
        )
        subscription = service.create_subscription_for_organization(org, plan=old_plan)
        assert subscription.entitlements.filter(entitlement_key=Entitlement.PARTNER_API).exists()

        # new_plan omits PARTNER_API entirely. Entitlement coverage is *not* an
        # invariant the way limit coverage is: absence fails closed, so an omitted
        # entitlement is a revocation rather than a data gap.
        new_plan = make_complete_plan()

        service.change_plan(subscription, new_plan)

        assert not subscription.entitlements.filter(
            entitlement_key=Entitlement.PARTNER_API
        ).exists()

    def test_downgrade_drops_a_retired_resource_key(self, service):
        """A `resource_key` that is no longer a `LimitedResource` member can never
        be consulted again -- nothing looks it up, so the row is pure dead weight
        and is deleted. Retired keys are the *only* thing the prune touches now:
        `assert_plan_is_complete` guarantees every `LimitedResource` member is
        carried by the plan, so no live key can ever reach it."""
        org = baker.make(Organization, parent=None)
        plan = make_complete_plan()
        subscription = service.create_subscription_for_organization(org, plan=plan)
        baker.make(
            SubscriptionPlanLimit,
            subscription=subscription,
            resource_key="retired_resource_from_an_older_release",
            limit_value=3,
            kind=LimitKind.PREPAID,
            is_overridden=False,
        )

        service.change_plan(subscription, plan)

        assert not subscription.limits.filter(
            resource_key="retired_resource_from_an_older_release"
        ).exists()

    def test_downgrade_from_unlimited_onto_an_incomplete_plan_is_refused(
        self, service, entitlement_service
    ):
        """BLOCKER 3, Phase 5 review -- the dominant real starting state.

        Every organization is on `unlimited` for the whole rollout, and every one of
        that plan's rows has `limit_value=None`. Downgrading onto a plan that omits
        a resource therefore has *no* correct resolution: deleting the stale row
        reads as unlimited, keeping it keeps a `None` that also reads as unlimited,
        and materializing a `0` blocks an organization on a resource nobody agreed
        to restrict. The incomplete plan is refused instead, before anything is
        written.

        The assertion that matters is the last one: an infinite ceiling must not
        survive a "downgrade" -- and here it survives only because the downgrade did
        not happen, with the subscription still coherently on `unlimited`.
        """
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org)
        assert subscription is not None
        assert subscription.plan.slug == "unlimited"
        stale_row = subscription.limits.get(resource_key=LimitedResource.RESOURCE_CALENDARS)
        assert stale_row.limit_value is None, (
            "This test is only meaningful while the retained row is NULL -- that is "
            "the state a retain-the-stale-row fix silently reads as unlimited."
        )

        # An incomplete plan: no PlanLimit row for RESOURCE_CALENDARS at all. The
        # catalog expresses "not included" with an explicit limit_value=0 row (as the
        # seeded `free` plan does for public_api_system_users), never by omission.
        incomplete_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        for resource_key in LimitedResource.values:
            if resource_key == LimitedResource.RESOURCE_CALENDARS:
                continue
            baker.make(
                PlanLimit,
                plan=incomplete_plan,
                resource_key=resource_key,
                limit_value=1,
                kind=LimitKind.PREPAID,
            )

        with pytest.raises(IncompleteBillingPlanError) as exc_info:
            service.change_plan(subscription, incomplete_plan)

        assert exc_info.value.missing_resource_keys == [LimitedResource.RESOURCE_CALENDARS]
        subscription.refresh_from_db()
        assert subscription.plan.slug == "unlimited", (
            "The plan change must not have been applied -- the guard runs before any "
            "write, so the subscription stays coherent with the limits it carries."
        )
        assert subscription.limits.filter(resource_key=LimitedResource.ORGANIZATION_MEMBERS).get(
            limit_value=None
        )
        assert entitlement_service.get_effective_limit(
            org, LimitedResource.RESOURCE_CALENDARS
        ).is_unlimited, (
            "Still on `unlimited`, so unlimited is the right answer here. The bug was "
            "reporting unlimited while the subscription claimed a restricted plan."
        )

    def test_change_plan_refuses_an_incomplete_plan_over_a_finite_ceiling(
        self, service, entitlement_service
    ):
        """The same guard when the stale row is *finite* rather than NULL.

        Before the guard this path silently retained the old `limit_value=3` row,
        leaving the subscription on a plan that says nothing about the resource
        while enforcing the previous plan's number. That is a quieter wrong answer
        than the NULL case, not a right one.
        """
        org = baker.make(Organization, parent=None)
        old_plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: 3})
        subscription = service.create_subscription_for_organization(org, plan=old_plan)
        assert (
            entitlement_service.get_effective_limit(
                org, LimitedResource.RESOURCE_CALENDARS
            ).limit_value
            == 3
        )

        incomplete_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        baker.make(
            PlanLimit,
            plan=incomplete_plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=1,
            kind=LimitKind.PREPAID,
        )

        with pytest.raises(IncompleteBillingPlanError) as exc_info:
            service.change_plan(subscription, incomplete_plan)

        assert LimitedResource.RESOURCE_CALENDARS in exc_info.value.missing_resource_keys
        subscription.refresh_from_db()
        assert subscription.plan == old_plan
        assert (
            entitlement_service.get_effective_limit(
                org, LimitedResource.RESOURCE_CALENDARS
            ).limit_value
            == 3
        )

    def test_creating_a_subscription_on_an_incomplete_plan_is_refused(self, service):
        """`change_plan` is not the only path that puts a subscription on a plan --
        the guard has to sit on creation too, or an organization is provisioned with
        a silent unlimited ceiling on whatever the plan forgot."""
        org = baker.make(Organization, parent=None)
        incomplete_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)

        with pytest.raises(IncompleteBillingPlanError):
            service.create_subscription_for_organization(org, plan=incomplete_plan)

        assert not Subscription.objects.filter(organization=org).exists()

    def test_downgrade_keeps_an_overridden_row_the_new_plan_does_not_carry(self, service, plan):
        """A support override survives the prune even when its `resource_key` is not
        in the new plan at all.

        A `LimitedResource` member can no longer be absent from a plan (the guard),
        so the only way to reach the prune with a live override is a retired key.
        """
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)
        assert subscription is not None
        baker.make(
            SubscriptionPlanLimit,
            subscription=subscription,
            resource_key="retired_resource_from_an_older_release",
            limit_value=42,
            kind=LimitKind.PREPAID,
            is_overridden=True,
        )
        overridden = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        overridden.is_overridden = True
        overridden.save()

        service.change_plan(subscription, make_complete_plan())

        assert subscription.limits.filter(
            resource_key=LimitedResource.ORGANIZATION_MEMBERS
        ).exists()
        assert subscription.limits.filter(
            resource_key="retired_resource_from_an_older_release"
        ).exists()
