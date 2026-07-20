import datetime
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from dateutil.relativedelta import relativedelta

from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import PaymentProviders
from payments.exceptions import BillingRootCycleError, NoDefaultBillingPlanError
from payments.models import (
    BillingPlan,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)


logger = logging.getLogger(__name__)


def is_billing_root(organization: Organization) -> bool:
    """True when ``organization`` holds its own ``Subscription`` rather than
    pooling against an ancestor's.

    The single predicate for "is a billing root", used everywhere that decision
    is made: here, by ``resolve_billing_root``'s cycle-guarded walk;
    ``SubscriptionService.create_subscription_for_organization``; the
    ``payments.0009`` backfill migration (as the ``Q``-object form,
    ``billing_root_filter``); and the "no plan-less state" acceptance query. Keep
    all of those in sync with this definition if it ever changes.

    An organization is its own billing root if it has no parent (top of its
    tree), **or** it can itself invite/create organizations — a nested reseller
    (``can_invite_organizations=True`` with a ``parent`` set) is its own billing
    root, not a child pooling against a grandparent's subscription.
    """
    return organization.parent_id is None or organization.can_invite_organizations


def billing_root_filter() -> Q:
    """``Q``-object equivalent of ``is_billing_root``, for queryset filtering where
    per-instance iteration is infeasible (bulk backfill migration, acceptance
    query). Keep in sync with ``is_billing_root``.
    """
    return Q(parent__isnull=True) | Q(can_invite_organizations=True)


def resolve_billing_root(organization: Organization) -> Organization:
    """Resolve the organization whose ``Subscription`` pays for ``organization``.

    Every billing root (see ``is_billing_root``) holds its own ``Subscription``. A
    reseller child pools against the nearest ancestor that is itself a billing
    root — the reseller root that pays for the whole subtree. Modeled on the
    cycle-guarded walk in ``Organization.get_branding_root`` for the same reason:
    ``parent`` is user-mutable data.

    Unlike ``get_branding_root`` (which falls back to ``None`` — "no reseller, use
    vinta defaults"), this walk always returns an organization if one is found:
    a parent-less organization is always a billing root, so a chain that never
    hits a cycle is guaranteed to terminate at one. If the walk revisits an
    organization it already passed through, the ``parent`` chain is a cycle and
    ``BillingRootCycleError`` is raised — returning an arbitrary node from a
    cycle would silently leave every organization on it without a resolvable
    billing root.
    """
    seen: set[int] = set()
    org: Organization | None = organization
    while org is not None:
        if org.pk in seen:
            raise BillingRootCycleError(organization.pk, seen)
        seen.add(org.pk)
        if is_billing_root(org):
            return org
        org = org.parent
    # Unreachable: a parent-less organization always satisfies is_billing_root
    # and returns above, so the walk only continues while org.parent is set (and
    # therefore non-None). Kept as a defensive fallback rather than an assert.
    return organization


class SubscriptionService:
    """Places organizations on a ``BillingPlan`` and keeps their per-subscription
    limit/entitlement copies in sync with plan changes.

    Per the plan's "no plan-less state" rule, every organization that is its own
    billing root (see ``resolve_billing_root``) has exactly one ``Subscription``.
    A reseller child never gets one of its own — it pools against its root's.
    """

    def create_subscription_for_organization(
        self, organization: Organization, plan: BillingPlan | None = None
    ) -> Subscription | None:
        """Create ``organization``'s ``Subscription`` (+ its ``SubscriptionPlanLimit``
        / ``SubscriptionEntitlement`` copies), unless ``organization`` is a reseller
        child — in which case this is a no-op and ``None`` is returned, since a
        child organization pools against its billing root's subscription instead
        (``resolve_billing_root``). A nested reseller (``can_invite_organizations=True``
        with ``parent`` set) is its own billing root and *does* get a subscription
        here — see ``is_billing_root``.

        Idempotent: if ``organization`` already has a ``Subscription``, it is
        returned unchanged rather than duplicated. Uses ``get_or_create`` so two
        concurrent calls (e.g. two requests racing to provision the same
        organization) resolve to the same row instead of one raising
        ``IntegrityError`` on the ``OneToOneField``.

        :param organization: The organization to place on a plan.
        :param plan: The catalog plan to subscribe to. Defaults to the catalog's
            active ``is_default_for_new_organizations=True`` plan (the ``unlimited``
            plan at rollout — the plan's declared "no feature flag" rollout switch).
        """
        if not is_billing_root(organization):
            logger.debug(
                "Skipping subscription creation for organization %s: it is a reseller "
                "child and pools against its billing root.",
                organization.pk,
            )
            return None

        if plan is None:
            plan = self._get_default_plan()

        now = timezone.now()
        period_end = self._period_end(now, BillingInterval.MONTHLY)

        with transaction.atomic():
            subscription, created = Subscription.objects.get_or_create(
                organization=organization,
                defaults={
                    "plan": plan,
                    "billing_state": BillingState.FREE,
                    "billing_interval": BillingInterval.MONTHLY,
                    "current_period_start": now,
                    "current_period_end": period_end,
                    # Placeholder: this subscription never touches a payment gateway
                    # (unlimited/free plans are $0). Phase 2b adds Stripe as a second
                    # provider; a real paid subscription will set this per the
                    # organization's chosen provider instead of hardcoding it here.
                    "payment_provider": PaymentProviders.MERCADOPAGO,
                },
            )
            # Also sync when an existing Subscription has no limit/entitlement
            # rows yet (e.g. one created via SubscriptionAdmin with empty
            # inlines, or payment_service.create_subscription) — otherwise it
            # is returned silently untouched with no limits to enforce.
            if created or not subscription.limits.exists():
                self._sync_limits(subscription, plan)
            if created or not subscription.entitlements.exists():
                self._sync_entitlements(subscription, plan)
        return subscription

    def _get_default_plan(self) -> BillingPlan:
        """Return the catalog's active default plan for new organizations.

        Raises ``NoDefaultBillingPlanError`` rather than an uncaught
        ``BillingPlan.DoesNotExist`` — a deactivated default plan (e.g. via admin)
        must not 500 every organization-creation request.
        """
        plan = BillingPlan.objects.filter(
            is_active=True, is_default_for_new_organizations=True
        ).first()
        if plan is None:
            raise NoDefaultBillingPlanError()
        return plan

    @transaction.atomic
    def change_plan(self, subscription: Subscription, plan: BillingPlan) -> Subscription:
        """Move ``subscription`` onto ``plan`` and re-copy its limits/entitlements.

        Non-overridden ``SubscriptionPlanLimit`` / ``SubscriptionEntitlement`` rows
        are refreshed from the new plan's catalog rows. Rows an admin hand-edited
        (``is_overridden=True``) are left untouched — the support lever for a stuck
        organization must survive a plan change.

        Atomic: a ``save`` + two ``bulk_create`` + two ``delete`` run as one unit
        so a mid-way failure cannot leave the subscription on the new plan with
        half-synced limits/entitlements.
        """
        subscription.plan = plan
        subscription.save(update_fields=["plan"])
        self._sync_limits(subscription, plan)
        self._sync_entitlements(subscription, plan)
        return subscription

    def _period_end(self, start: datetime.datetime, billing_interval: str) -> datetime.datetime:
        if billing_interval == BillingInterval.ANNUAL:
            return start + relativedelta(years=1)
        return start + relativedelta(months=1)

    def _sync_limits(self, subscription: Subscription, plan: BillingPlan) -> None:
        overridden_keys = set(
            subscription.limits.filter(is_overridden=True).values_list("resource_key", flat=True)
        )
        plan_limits = list(plan.limits.all())
        plan_resource_keys = {plan_limit.resource_key for plan_limit in plan_limits}
        rows_to_sync = [
            plan_limit
            for plan_limit in plan_limits
            if plan_limit.resource_key not in overridden_keys
        ]
        if rows_to_sync:
            SubscriptionPlanLimit.objects.bulk_create(
                [
                    SubscriptionPlanLimit(
                        subscription=subscription,
                        resource_key=plan_limit.resource_key,
                        limit_value=plan_limit.limit_value,
                        kind=plan_limit.kind,
                        overage_unit_price=plan_limit.overage_unit_price,
                        is_overridden=False,
                    )
                    for plan_limit in rows_to_sync
                ],
                update_conflicts=True,
                update_fields=["limit_value", "kind", "overage_unit_price", "is_overridden"],
                unique_fields=["subscription", "resource_key"],
            )
        # A resource_key with no PlanLimit row on the new plan, and never
        # hand-overridden, is a grant the organization no longer pays for — leaving
        # it behind on a downgrade would silently keep an entitlement (or a NULL /
        # unlimited row) alive. Overridden rows are exempt: the support lever must
        # survive a plan change untouched.
        subscription.limits.exclude(resource_key__in=plan_resource_keys).filter(
            is_overridden=False
        ).delete()

    def _sync_entitlements(self, subscription: Subscription, plan: BillingPlan) -> None:
        overridden_keys = set(
            subscription.entitlements.filter(is_overridden=True).values_list(
                "entitlement_key", flat=True
            )
        )
        plan_entitlements = list(plan.entitlements.all())
        plan_entitlement_keys = {
            plan_entitlement.entitlement_key for plan_entitlement in plan_entitlements
        }
        rows_to_sync = [
            plan_entitlement
            for plan_entitlement in plan_entitlements
            if plan_entitlement.entitlement_key not in overridden_keys
        ]
        if rows_to_sync:
            SubscriptionEntitlement.objects.bulk_create(
                [
                    SubscriptionEntitlement(
                        subscription=subscription,
                        entitlement_key=plan_entitlement.entitlement_key,
                        is_enabled=plan_entitlement.is_enabled,
                        is_overridden=False,
                    )
                    for plan_entitlement in rows_to_sync
                ],
                update_conflicts=True,
                update_fields=["is_enabled", "is_overridden"],
                unique_fields=["subscription", "entitlement_key"],
            )
        # Same stale-grant cleanup as `_sync_limits`, keyed on entitlement_key.
        subscription.entitlements.exclude(entitlement_key__in=plan_entitlement_keys).filter(
            is_overridden=False
        ).delete()
