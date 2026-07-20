import datetime
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from dateutil.relativedelta import relativedelta

from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import PaymentProviders
from payments.exceptions import (
    BillingPeriodResolutionError,
    BillingRootCycleError,
    IncompleteBillingPlanError,
    NoDefaultBillingPlanError,
)
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


#: Ceiling on how many whole intervals ``resolve_billing_period`` will step before
#: giving up. 1200 months is a century of monthly cycles — far past any real
#: subscription, and small enough that a corrupt period pair fails fast instead of
#: spinning.
MAX_BILLING_PERIOD_STEPS = 1200


def billing_interval_step(billing_interval: str) -> relativedelta:
    """The length of one billing cycle, as a calendar-aware delta.

    ``relativedelta`` rather than ``timedelta`` so a monthly cycle anchored on the
    31st, or one spanning a DST transition, lands on the same wall-clock anchor
    instead of drifting by a day.
    """
    if billing_interval == BillingInterval.ANNUAL:
        return relativedelta(years=1)
    return relativedelta(months=1)


def resolve_billing_period(
    subscription: Subscription, moment: datetime.datetime
) -> tuple[datetime.datetime, datetime.datetime]:
    """The ``[start, end)`` billing cycle that ``moment`` falls in.

    **The single definition of "which cycle does this belong to".** The meter
    stamps ``MeteredOccurrence.billing_period_start`` from it, the usage counter
    behind ``LimitedResource.EVENT_OCCURRENCES`` reads rows back by it, and
    ``reconcile_period`` recomputes a closed cycle's bounds with it. Three
    hand-written date comparisons that are supposed to agree is precisely how a
    charge lands on the wrong invoice.

    A ``Subscription`` stores only its *current* period, so past and future cycles
    are reconstructed by stepping whole intervals from it. That assumes cycle
    boundaries are regular, which is the shape ``SubscriptionService`` creates them
    in (``_period_end`` adds exactly one interval). A subscription whose stored
    period is not one interval long — a mid-cycle plan change that moved the
    boundary, say — will reconstruct *neighbouring* periods from the current
    anchor rather than from history; the current period, which is the one anything
    live reads, is always exact.

    Half-open on purpose: an occurrence starting exactly at ``current_period_end``
    belongs to the next cycle, and is billed there rather than twice or not at all.
    """
    step = billing_interval_step(subscription.billing_interval)
    start = subscription.current_period_start
    end = subscription.current_period_end
    steps = 0
    while moment < start:
        end, start = start, start - step
        steps += 1
        if steps > MAX_BILLING_PERIOD_STEPS:
            raise BillingPeriodResolutionError(subscription.pk, moment, steps)
    while moment >= end:
        start, end = end, end + step
        steps += 1
        if steps > MAX_BILLING_PERIOD_STEPS:
            raise BillingPeriodResolutionError(subscription.pk, moment, steps)
    return start, end


def resolve_billing_period_start(
    subscription: Subscription, moment: datetime.datetime
) -> datetime.datetime:
    """The ``billing_period_start`` that ``moment`` belongs to.

    ``resolve_billing_period``'s first element, as a named function, because the
    *stamp* and the *read-back* of ``MeteredOccurrence.billing_period_start`` must
    be the same expression and previously were not. ``MeteringService`` stamped
    ``resolve_billing_period(subscription, occurrence_start)[0]`` while the
    ``event_occurrences`` usage counter read back
    ``subscription.current_period_start`` directly. Those agree only while the
    stored period happens to contain "now" — and nothing advances
    ``current_period_start`` (cycle close is Phase 13), so once the stored period
    elapses the meter writes one period and the counter asks for another, and the
    counter reads zero forever.

    Callers differ only in the ``moment`` they pass: the meter passes each
    occurrence's own start (so an occurrence is billed to the cycle it happened
    in), the counter passes ``timezone.now()`` (the cycle in progress). Anything
    needing "the current cycle" should go through
    ``current_billing_period_start`` rather than reading the column.
    """
    period_start, _period_end = resolve_billing_period(subscription, moment)
    return period_start


def current_billing_period_start(subscription: Subscription) -> datetime.datetime:
    """The start of the cycle in progress *now*.

    Deliberately derived from ``timezone.now()`` rather than read off
    ``Subscription.current_period_start``. That column records the cycle the
    subscription was created or last advanced into; until Phase 13 introduces
    cycle close, nothing ever moves it forward, so it goes stale as soon as one
    interval elapses.
    """
    return resolve_billing_period_start(subscription, timezone.now())


def assert_plan_is_complete(plan: BillingPlan) -> None:
    """Refuse to place a subscription on a plan that omits a ``LimitedResource``.

    The invariant — every plan carries a ``PlanLimit`` row for every
    ``LimitedResource`` member — used to be enforced only by a test over *seed
    data*, which cannot see a plan an admin authors at runtime. This is that
    invariant in code, on the two paths that put a subscription on a plan
    (``create_subscription_for_organization`` and ``change_plan``).

    Why refusing is the only correct outcome. An omitted resource leaves the
    subscription's row for it either absent or stale, and both read as
    **unlimited** in ``EntitlementService`` — so a downgrade onto an incomplete
    plan grants an infinite ceiling, the exact inverse of a downgrade. The two
    obvious alternatives are worse: materializing the gap as ``limit_value=0``
    blocks an organization on a resource nobody agreed to restrict (the rollout's
    "no organization is blocked as a consequence of the rollout itself" rule), and
    keeping the stale row is the bug itself whenever that row is ``NULL`` — which
    is the dominant real state, since every organization is on ``unlimited``
    (every ``limit_value`` NULL) for the whole rollout.

    An incomplete plan is a catalog authoring error, so it fails loudly at the
    point of use and, via ``BillingPlan.clean`` / ``BillingPlanAdmin``, at the
    point of authoring — where a support admin can fix it.
    """
    missing = plan.get_missing_limited_resource_keys()
    if missing:
        raise IncompleteBillingPlanError(plan.slug, missing)


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
        assert_plan_is_complete(plan)

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

        Raises ``IncompleteBillingPlanError`` when ``plan`` omits a
        ``LimitedResource``, *before* anything is written — see
        ``assert_plan_is_complete``. A downgrade onto an incomplete plan has no
        correct outcome, so it is refused rather than resolved arbitrarily.
        """
        assert_plan_is_complete(plan)
        subscription.plan = plan
        subscription.save(update_fields=["plan"])
        self._sync_limits(subscription, plan)
        self._sync_entitlements(subscription, plan)
        return subscription

    def _period_end(self, start: datetime.datetime, billing_interval: str) -> datetime.datetime:
        """One cycle after ``start``.

        Shares ``billing_interval_step`` with ``resolve_billing_period`` on purpose:
        the cycle length used to *create* a period and the one used to reconstruct
        past periods have to be the same expression, or reconstructed boundaries
        drift away from the ones that were actually billed.
        """
        return start + billing_interval_step(billing_interval)

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
        self._prune_stale_limits(subscription, plan_resource_keys)

    def _prune_stale_limits(
        self,
        subscription: Subscription,
        plan_resource_keys: set[str],
    ) -> None:
        """Drop ``SubscriptionPlanLimit`` rows the new plan no longer accounts for.

        By the time this runs, ``assert_plan_is_complete`` has already established
        that ``plan_resource_keys`` covers every ``LimitedResource`` member, so
        every row left here is a **retired key** — a resource that left the enum.
        Nothing can ever consult one again, so deleting is the only sensible
        outcome, and deleting cannot raise anybody's ceiling: an absent row reads
        as unlimited in ``EntitlementService``, but no code path asks about a key
        that is not a ``LimitedResource`` member.

        That guard is what makes this safe. Without it, deleting a row for a key
        that *is* a ``LimitedResource`` member but is missing from the plan would
        compose with the fail-open-on-absence rule into *downgrading to a plan that
        omits a resource grants that resource an infinite ceiling* — the exact
        inverse of a downgrade. Each half is correct alone; only together are they
        wrong. The fix is to reject the incomplete plan up front, not to guess a
        ceiling for it here.

        Overridden rows are exempt: the support lever for a stuck organization must
        survive a plan change untouched.
        """
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
        # Unconditional delete here, unlike `_prune_stale_limits`. Entitlements fail
        # *closed* in `EntitlementService.has_entitlement` — an absent row means "not
        # granted" — so deleting a row the new plan omits revokes the grant, which is
        # what a downgrade means. The limits side cannot do this because absence
        # there means "unlimited"; see `_prune_stale_limits`.
        subscription.entitlements.exclude(entitlement_key__in=plan_entitlement_keys).filter(
            is_overridden=False
        ).delete()
