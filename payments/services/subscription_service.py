import datetime
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from dateutil.relativedelta import relativedelta
from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import PaymentProviders
from payments.exceptions import (
    AddOnNotPurchasableError,
    BillingPeriodResolutionError,
    BillingRootCycleError,
    IncompleteBillingPlanError,
    NoDefaultBillingPlanError,
    PaymentTokenRequiredError,
)
from payments.models import (
    BillingPlan,
    PaymentMethod,
    Subscription,
    SubscriptionAddOn,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from payments.services.dataclasses import CreatedPlan, Plan


if TYPE_CHECKING:
    from payments.services.payment_service import PaymentService


logger = logging.getLogger(__name__)

#: Fallback grace window (days) stamped on a downgrade when the target plan
#: carries no ``grace_period_days`` of its own. A per-plan, settings-backed
#: global default (``BillingPlan.grace_period_days`` falling back to a
#: ``BILLING_DEFAULT_GRACE_PERIOD_DAYS`` setting) is Phase 10's own change --
#: this constant only covers the one place *this* phase stamps
#: ``grace_period_ends_at`` (a scheduled downgrade) ahead of that.
_DOWNGRADE_DEFAULT_GRACE_PERIOD_DAYS = 7


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

    @inject
    def __init__(
        self,
        payment_service: Annotated["PaymentService | None", Provide["payment_service"]] = None,
    ) -> None:
        """``payment_service`` drives the provider round-trips Phase 9's plan-change
        and add-on purchase flows need (creating/updating the provider-side plan,
        attaching or moving a subscription onto it). Injected via DI, like every
        other cross-service dependency in this codebase (``OrganizationService``'s
        constructor is the model for this) — deliberately **not** the other
        direction: ``PaymentService`` does not depend on ``SubscriptionService``,
        which would make the two circular. The webhook views orchestrate calling
        into both instead (see ``PaymentsViewSet``).

        Defaults to ``None`` so every existing bare ``SubscriptionService()``
        call across the codebase and test suite keeps working — ``@inject``
        resolves ``Provide["payment_service"]`` from the wired container
        automatically once Django has started (``payments`` is in
        ``INTERNAL_INSTALLED_APPS``, which ``DICoreConfig.ready()`` wires), the
        same pattern ``CalendarService.__init__`` uses.
        """
        self.payment_service = payment_service

    def _require_payment_service(self) -> "PaymentService":
        if self.payment_service is None:
            raise RuntimeError(
                "SubscriptionService.payment_service is not set -- construct via "
                "the DI container (or pass payment_service=...) before driving "
                "the provider."
            )
        return self.payment_service

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

    def _plan_price(self, plan: BillingPlan, billing_interval: str) -> Decimal:
        """The price ``plan`` is sold at on ``billing_interval``.

        The **one** computation ``request_plan_change`` uses to decide upgrade vs.
        downgrade — see that method's docstring for why this must not be
        re-derived a second way anywhere else in the flow.
        """
        if billing_interval == BillingInterval.ANNUAL and plan.annual_price is not None:
            return plan.annual_price
        return plan.monthly_price

    def request_plan_change(
        self,
        subscription: Subscription,
        plan: BillingPlan,
        billing_interval: str,
        payment_token: str = "",
    ) -> Subscription:
        """Move ``subscription`` toward ``plan`` — the single entry point behind
        ``POST /billing/subscription/change-plan/``.

        Upgrade vs. downgrade is decided **once**, from ``_plan_price`` compared
        against ``subscription``'s current plan/interval, and that one decision
        is what both the provider is charged against and what capacity is
        eventually granted from — the plan's recurring "two predicates that must
        agree" failure shape, avoided by having only one.

        - **Upgrade** (``_initiate_upgrade``): drives the provider (proration
          computed server-side); capacity is granted later, when the resulting
          charge is confirmed via the subscription-payment webhook
          (``confirm_plan_change``). Nothing here re-copies
          ``SubscriptionPlanLimit``/``SubscriptionEntitlement`` — an
          initiated-but-unconfirmed upgrade must grant no capacity.
        - **Downgrade or lateral move** (``_schedule_downgrade``, also covers an
          equal-price interval change): no cash refund. The lower limits apply
          **immediately** (re-copied here); the plan itself, and what the org is
          billed, do not flip until the next period boundary — recorded on
          ``pending_plan``/``pending_billing_interval``/``pending_plan_effective_at``
          for a future cycle-close sweep (Phase 13) to apply.

        A request for the plan/interval ``subscription`` is *already* fully
        settled on (no pending change either) is a no-op.
        """
        assert_plan_is_complete(plan)
        already_settled = (
            subscription.plan_id == plan.pk
            and subscription.billing_interval == billing_interval
            and subscription.pending_plan_id is None
        )
        if already_settled:
            return subscription

        current_price = self._plan_price(subscription.plan, subscription.billing_interval)
        new_price = self._plan_price(plan, billing_interval)
        if new_price > current_price:
            return self._initiate_upgrade(subscription, plan, billing_interval, payment_token)
        return self._schedule_downgrade(subscription, plan, billing_interval)

    def _initiate_upgrade(
        self,
        subscription: Subscription,
        plan: BillingPlan,
        billing_interval: str,
        payment_token: str,
    ) -> Subscription:
        # Checked *before* any write: a subscription with no provider-side
        # instrument yet needs a token to attach one, and that is knowable
        # up front, with nothing to unwind if it is missing. (Everything past
        # this point does write, and depends on the caller's transaction --
        # `ATOMIC_REQUESTS` when called from a request -- to unwind atomically
        # on any *later* failure, same as every other provider round trip in
        # this codebase.)
        if not subscription.external_id and not payment_token:
            raise PaymentTokenRequiredError(subscription.organization_id)

        with transaction.atomic():
            # An upgrade supersedes any downgrade previously scheduled.
            subscription.pending_plan = None
            subscription.pending_billing_interval = ""
            subscription.pending_plan_effective_at = None
            subscription.plan = plan
            subscription.billing_interval = billing_interval
            subscription.save(
                update_fields=[
                    "plan",
                    "billing_interval",
                    "pending_plan",
                    "pending_billing_interval",
                    "pending_plan_effective_at",
                ]
            )
            # NOTE deliberately *not* called here: `_sync_limits`/`_sync_entitlements`
            # are what grant capacity, and this method must not grant it
            # synchronously. `subscription.plan` alone grants nothing --
            # `EntitlementService` reads `SubscriptionPlanLimit`, not this FK.

        payment_service = self._require_payment_service()
        created_plan = self._ensure_provider_plan(subscription, plan, billing_interval)
        if not subscription.external_id:
            payment_service.process_subscription(subscription, payment_token)
        else:
            payment_service.change_subscription_plan(subscription, created_plan)
        return subscription

    def _ensure_provider_plan(
        self, subscription: Subscription, plan: BillingPlan, billing_interval: str
    ) -> CreatedPlan:
        """(Re)create ``plan``'s provider-side plan/price object and stamp its id
        onto ``subscription.plan_external_id`` — the field
        ``BillingPlanFactory.make_plan_from_subscription`` reads back.

        Always creates a fresh provider-side object rather than caching one per
        catalog ``BillingPlan``: the catalog carries no per-provider external id
        of its own (``plan_external_id`` lives on ``Subscription``, one per
        subscriber, not on ``BillingPlan``), and there is no live provider to
        validate a caching scheme against in this environment (see the phase
        report). Correct, if not maximally efficient — a follow-up could add a
        provider-keyed external id to the catalog plan itself.
        """
        payment_service = self._require_payment_service()
        created = payment_service.create_subscription_plan(
            Plan(
                id=plan.pk,
                name=plan.name,
                value=self._plan_price(plan, billing_interval),
                currency=plan.currency,
                billing_day=min(subscription.current_period_start.day, 28),
                billing_interval=billing_interval,
            )
        )
        subscription.plan_external_id = created.external_id
        subscription.save(update_fields=["plan_external_id"])
        return created

    def _schedule_downgrade(
        self, subscription: Subscription, plan: BillingPlan, billing_interval: str
    ) -> Subscription:
        """No cash refund: schedule ``plan`` to take over at the next period
        boundary while applying its (lower, or equal-price) limits immediately.

        ``grace_period_ends_at`` is stamped so the org has a window to reduce
        usage before anything past Phase 8/6's ordinary "no new creates over the
        ceiling" enforcement applies -- nothing here evicts or deletes existing
        over-count resources; ``check_limit`` never has.
        """
        assert_plan_is_complete(plan)
        with transaction.atomic():
            subscription.pending_plan = plan
            subscription.pending_billing_interval = billing_interval
            subscription.pending_plan_effective_at = subscription.current_period_end
            grace_days = plan.grace_period_days
            if grace_days is None:
                grace_days = _DOWNGRADE_DEFAULT_GRACE_PERIOD_DAYS
            subscription.grace_period_ends_at = timezone.now() + datetime.timedelta(days=grace_days)
            subscription.save(
                update_fields=[
                    "pending_plan",
                    "pending_billing_interval",
                    "pending_plan_effective_at",
                    "grace_period_ends_at",
                ]
            )
            self._sync_limits(subscription, plan)
            self._sync_entitlements(subscription, plan)
        return subscription

    def confirm_plan_change(self, subscription: Subscription) -> Subscription:
        """Grant the capacity for ``subscription``'s current ``plan`` once a
        charge against it is confirmed ``APPROVED`` by the provider.

        Called from the subscription-payment webhook path
        (``PaymentsViewSet.subscription_payment_update``) — never synchronously
        from the request that initiates an upgrade. Reuses ``change_plan`` to
        re-copy against ``subscription.plan``, the exact field
        ``_initiate_upgrade`` already set at initiation time: there is exactly
        one place that decides which plan an upgrade is for, and this is the
        other end of it, not a second one.

        Idempotent / safe to call on every approved subscription payment (not
        only the first one after an upgrade) — ``change_plan`` is itself
        idempotent (bulk upsert), so a routine renewal charge simply re-affirms
        the org is on the plan it is already on.
        """
        self.change_plan(subscription, subscription.plan)
        if subscription.billing_state != BillingState.ACTIVE:
            subscription.billing_state = BillingState.ACTIVE
            subscription.save(update_fields=["billing_state"])
        return subscription

    def cancel_subscription(self, subscription: Subscription) -> Subscription:
        """Cancel ``subscription``. Runs the provider-side cancellation
        (best-effort skipped when the org never attached a payment method) and
        moves ``billing_state`` to ``CANCELLED`` immediately.

        The spec's full "runs to the end of the paid cycle, then reverts to
        FREE" lifecycle is Phase 10's dunning state machine
        (``BillingState`` transition table) -- this method only exposes the
        action Phase 9's endpoint needs; it does not re-implement that machine.
        """
        payment_service = self._require_payment_service()
        if subscription.external_id:
            payment_service.cancel_subscription(subscription)
        subscription.billing_state = BillingState.CANCELLED
        subscription.save(update_fields=["billing_state"])
        return subscription

    def _resolve_add_on_unit_price(self, subscription: Subscription, resource_key: str) -> Decimal:
        limit = subscription.limits.filter(resource_key=resource_key).first()
        if limit is None or limit.overage_unit_price is None:
            raise AddOnNotPurchasableError(resource_key)
        return limit.overage_unit_price

    def purchase_add_on(
        self,
        subscription: Subscription,
        resource_key: str,
        quantity: int,
        is_recurring: bool,
        idempotency_key: str,
        payment_token: str,
    ) -> SubscriptionAddOn:
        """Buy ``quantity`` more of ``resource_key``'s capacity.

        **Idempotent on ``idempotency_key``**
        (``SubscriptionAddOn.purchase_idempotency_key``, unique at the database
        level): the same key posted twice always resolves to the same row and
        the provider is charged **at most once**, regardless of whether the
        first attempt's charge is still pending, already succeeded, or already
        failed -- this is the phase's fail-closed-for-money rule: when in doubt
        whether a prior attempt already charged, do not charge again. The
        ``get_or_create`` below is the single decision both "was this already
        purchased" and "was this already charged" hang off, on purpose -- a
        second, independently-derived answer to either question is exactly the
        "two predicates" defect shape this plan keeps producing.

        Capacity is **not** granted here: the returned row is ``is_active=False``
        (``EntitlementService.get_effective_limit`` only sums active add-ons)
        until ``activate_add_on`` is called from the webhook path once the
        resulting one-time payment is confirmed ``APPROVED``.

        :raises AddOnNotPurchasableError: ``resource_key`` has no
            ``overage_unit_price`` on the subscription's current plan.
        """
        # Resolved *before* any write, like `_initiate_upgrade`'s payment-token
        # check: a resource with no catalog price is knowable up front, with
        # nothing to unwind if it turns out unpurchasable.
        unit_price = self._resolve_add_on_unit_price(subscription, resource_key)

        with transaction.atomic():
            add_on, created = SubscriptionAddOn.objects.get_or_create(
                purchase_idempotency_key=idempotency_key,
                defaults={
                    "subscription": subscription,
                    "resource_key": resource_key,
                    "quantity": quantity,
                    "is_recurring": is_recurring,
                    "is_active": False,
                },
            )
        if not created:
            return add_on

        payment_service = self._require_payment_service()
        payment = payment_service.create_payment(
            organization=subscription.organization,
            currency=subscription.plan.currency,
            amount=unit_price * quantity,
            description=f"Add-on purchase: {quantity} x {resource_key}",
            payment_method="add_on_purchase",
            payment_token=payment_token,
        )
        add_on.payment = payment
        add_on.external_id = payment.external_id
        add_on.save(update_fields=["payment", "external_id"])
        return add_on

    def activate_add_on(self, add_on: SubscriptionAddOn) -> SubscriptionAddOn:
        """Grant ``add_on``'s capacity once its payment is confirmed ``APPROVED``.

        Called from the payment webhook path
        (``PaymentsViewSet.payment_update``). Idempotent: re-activating an
        already-active add-on is a no-op, so a provider redelivery (already
        deduped by ``ProviderWebhookEvent``, but this stays safe even without
        that) cannot double-grant.
        """
        if not add_on.is_active:
            add_on.is_active = True
            add_on.save(update_fields=["is_active"])
        return add_on

    def cancel_add_on(self, add_on: SubscriptionAddOn) -> SubscriptionAddOn:
        """Stop a recurring add-on from renewing at the next period boundary.

        Behind ``DELETE /billing/add-ons/{id}/`` ("cancel a recurring add-on at
        period end"). Flips ``is_recurring`` off rather than deactivating
        immediately: capacity already purchased for the current period must
        stay in effect, and there is no cycle-close sweep yet (Phase 13) to
        apply an immediate deactivation against a period boundary anyway -- a
        future renewal-processing sweep simply has nothing left to renew.
        """
        if add_on.is_recurring:
            add_on.is_recurring = False
            add_on.save(update_fields=["is_recurring"])
        return add_on

    def record_payment_method(
        self, organization: Organization, provider: str, external_id: str
    ) -> PaymentMethod | None:
        """Record that ``organization`` (its billing root) has a confirmed,
        chargeable payment instrument on file with ``provider``.

        The write behind ``EntitlementService.has_payment_method``'s real
        source of truth (Phase 9's re-point away from the ``billing_state``
        proxy). Called only from the webhook path, once a charge against the
        instrument is confirmed ``APPROVED`` -- never synchronously from a
        request that merely attempts to attach one.

        ``external_id`` is whatever the provider returned for the confirmed
        charge (a payment's or a subscription's external id) -- there is no
        separate "tokenize a card" step in this codebase's provider adapters,
        so a confirmed charge is the strongest signal available that the
        instrument behind it is real and chargeable. A blank id means there is
        nothing to record (should not happen for a confirmed charge; logged and
        skipped rather than writing a meaningless row).
        """
        if not external_id:
            logger.warning(
                "record_payment_method called with no external_id for organization %s "
                "provider %s; nothing recorded.",
                organization.pk,
                provider,
            )
            return None
        payment_method, _created = PaymentMethod.objects.get_or_create(
            organization=organization,
            provider=provider,
            external_id=external_id,
            defaults={"is_active": True},
        )
        if not payment_method.is_active:
            payment_method.is_active = True
            payment_method.save(update_fields=["is_active"])
        return payment_method

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
