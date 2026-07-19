import datetime
import logging

from django.utils import timezone

from dateutil.relativedelta import relativedelta

from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import PaymentProviders
from payments.models import (
    BillingPlan,
    Subscription,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)


logger = logging.getLogger(__name__)


def resolve_billing_root(organization: Organization) -> Organization:
    """Resolve the organization whose ``Subscription`` pays for ``organization``.

    Every organization without a parent holds its own ``Subscription``. A reseller
    child (``parent`` set) never holds one of its own — it pools against the nearest
    ancestor with ``can_invite_organizations=True``, the reseller root that pays for
    the whole subtree. Modeled on the cycle-guarded walk in
    ``Organization.get_branding_root`` for the same reason: ``parent`` is
    user-mutable data and a cycle must terminate rather than recurse forever.

    Unlike ``get_branding_root`` (which falls back to ``None`` — "no reseller, use
    vinta defaults"), this walk always returns an organization: if the chain never
    finds a ``can_invite_organizations=True`` ancestor before running out of
    parents, the topmost ancestor reached is the billing root — every
    parent-less organization holds its own subscription by the plan's invariant
    (``Organization.objects.filter(parent__isnull=True, subscription__isnull=True).count() == 0``).
    """
    seen: set[int] = set()
    org: Organization | None = organization
    root = organization
    while org is not None and org.pk not in seen:
        root = org
        if org.can_invite_organizations:
            return org
        seen.add(org.pk)
        org = org.parent
    return root


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
        child organization pools against its root's subscription instead
        (``resolve_billing_root``).

        Idempotent: if ``organization`` already has a ``Subscription``, it is
        returned unchanged rather than duplicated.

        :param organization: The organization to place on a plan.
        :param plan: The catalog plan to subscribe to. Defaults to the catalog's
            ``is_default_for_new_organizations=True`` plan (the ``unlimited`` plan
            at rollout — the plan's declared "no feature flag" rollout switch).
        """
        if resolve_billing_root(organization).pk != organization.pk:
            logger.debug(
                "Skipping subscription creation for organization %s: it is a reseller "
                "child and pools against its billing root.",
                organization.pk,
            )
            return None

        existing = Subscription.objects.filter(organization=organization).first()
        if existing is not None:
            return existing

        if plan is None:
            plan = BillingPlan.objects.get(is_default_for_new_organizations=True)

        now = timezone.now()
        period_end = self._period_end(now, BillingInterval.MONTHLY)

        subscription = Subscription.objects.create(
            organization=organization,
            plan=plan,
            billing_state=BillingState.FREE,
            billing_interval=BillingInterval.MONTHLY,
            current_period_start=now,
            current_period_end=period_end,
            payment_provider=PaymentProviders.MERCADOPAGO,
        )
        self._sync_limits(subscription, plan)
        self._sync_entitlements(subscription, plan)
        return subscription

    def change_plan(self, subscription: Subscription, plan: BillingPlan) -> Subscription:
        """Move ``subscription`` onto ``plan`` and re-copy its limits/entitlements.

        Non-overridden ``SubscriptionPlanLimit`` / ``SubscriptionEntitlement`` rows
        are refreshed from the new plan's catalog rows. Rows an admin hand-edited
        (``is_overridden=True``) are left untouched — the support lever for a stuck
        organization must survive a plan change.
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
        for plan_limit in plan.limits.all():
            if plan_limit.resource_key in overridden_keys:
                continue
            SubscriptionPlanLimit.objects.update_or_create(
                subscription=subscription,
                resource_key=plan_limit.resource_key,
                defaults={
                    "limit_value": plan_limit.limit_value,
                    "kind": plan_limit.kind,
                    "overage_unit_price": plan_limit.overage_unit_price,
                    "is_overridden": False,
                },
            )

    def _sync_entitlements(self, subscription: Subscription, plan: BillingPlan) -> None:
        overridden_keys = set(
            subscription.entitlements.filter(is_overridden=True).values_list(
                "entitlement_key", flat=True
            )
        )
        for plan_entitlement in plan.entitlements.all():
            if plan_entitlement.entitlement_key in overridden_keys:
                continue
            SubscriptionEntitlement.objects.update_or_create(
                subscription=subscription,
                entitlement_key=plan_entitlement.entitlement_key,
                defaults={
                    "is_enabled": plan_entitlement.is_enabled,
                    "is_overridden": False,
                },
            )
