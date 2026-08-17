import datetime
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from dateutil.relativedelta import relativedelta
from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import PaymentProviders
from payments.exceptions import (
    AddOnNotPurchasableError,
    BillingPeriodResolutionError,
    BillingRootCycleError,
    ChargeDeclinedError,
    CollectionNotSupportedError,
    IllegalBillingStateTransitionError,
    IncompleteBillingPlanError,
    MissingBillingProfileError,
    NoDefaultBillingPlanError,
    NoOutstandingBalanceError,
    PaymentTokenRequiredError,
    RetryPaymentNotApplicableError,
    SubscriptionNotAttachedError,
    UnconfirmedPlanChangeError,
    UnknownPaymentProviderError,
)
from payments.models import (
    BillingPlan,
    BillingProfile,
    PaymentMethod,
    Subscription,
    SubscriptionAddOn,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from payments.services.billing_state_machine import transition_billing_state
from payments.services.dataclasses import CreatedPlan, Plan
from payments.services.dunning_service import is_downgrade_grace


if TYPE_CHECKING:
    from audit.services import AuditService
    from payments.services.payment_provider_resolver import PaymentProviderResolver
    from payments.services.payment_service import PaymentService
    from users.models import User


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


def overage_settlement_step() -> relativedelta:
    """The length of one **overage settlement** cycle: always one month.

    The single definition of the spec's *Time-bounded behavior* rule — "post-paid
    overage settles monthly regardless of whether the plan is billed annually"
    (Billing Plans and Limits spec §4.2). ``billing_interval`` governs the
    *recurring plan fee* (an annual plan is charged its fee once a year, by the
    provider's own subscription), but the overage that ``CycleCloseService`` sweeps
    settles every month for every plan — so the period cycle-close rolls forward by
    is deliberately independent of ``billing_interval``.

    Reuses ``billing_interval_step(MONTHLY)`` rather than a fresh ``relativedelta``
    so "one month" has exactly one definition shared with subscription creation
    (``SubscriptionService._period_end`` also anchors the stored period monthly) and
    ``resolve_billing_period``'s monthly branch. A subscription's stored period is
    created one month long (``create_subscription_for_organization``) and rolled one
    month forward here, so the current period the meter and the usage counter read
    stays monthly for every plan, matching what this step produces.
    """
    return billing_interval_step(BillingInterval.MONTHLY)


def resolve_billing_period(
    subscription: Subscription,
    moment: datetime.datetime,
    step: relativedelta | None = None,
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

    ``step`` overrides the reconstruction stride. It defaults to
    ``billing_interval_step(subscription.billing_interval)`` — one plan cycle — but a
    caller reconstructing a *settlement* period (which always advances monthly, even
    for an annually-billed plan) passes ``overage_settlement_step()`` instead; see
    ``resolve_settlement_period``. The stride only matters when ``moment`` falls
    outside the stored current period, since resolving the current cycle never steps.
    """
    if step is None:
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


def resolve_settlement_period(
    subscription: Subscription, moment: datetime.datetime
) -> tuple[datetime.datetime, datetime.datetime]:
    """The monthly *overage settlement* period ``moment`` falls in.

    Like ``resolve_billing_period`` but reconstructed with ``overage_settlement_step``
    (one month) rather than the plan's ``billing_interval``. Overage settles monthly
    for *every* plan, and a subscription's stored period is created one month long
    (``create_subscription_for_organization``) and rolled one month forward at close
    (``CycleCloseService._roll_period``) regardless of ``billing_interval`` — so an
    annually-billed subscription's past periods must be walked back monthly, not by
    twelve-month strides. Reconstructing an annual plan's history with the plan-cycle
    stride lands on the wrong bounds (or overshoots into
    ``BillingPeriodResolutionError``); this is the resolution that matches how close
    actually rolls and how the meter stamped those historical rows.
    """
    return resolve_billing_period(subscription, moment, step=overage_settlement_step())


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
    ``current_period_start`` (cycle close is not implemented yet), so once the
    stored period elapses the meter writes one period and the counter asks for
    another, and the counter reads zero forever.

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
    subscription was created or last advanced into; until cycle close is
    implemented, nothing ever moves it forward, so it goes stale as soon as one
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


def retry_payment_idempotency_key(subscription_pk: int, client_idempotency_key: str) -> str:
    """The idempotency key ``SubscriptionService.retry_payment`` forwards to
    the provider: ``retry-payment-{pk}-{client_key}``.

    Named and module-level, mirroring
    ``dunning_service.dunning_retry_idempotency_key``, so this format has
    exactly one definition -- structurally distinct from the dunning ladder's
    own ``dunning-retry-{pk}-{ordinal}`` namespace (see
    ``SubscriptionService.retry_payment``'s docstring for why the two must
    never collide). A test asserting the key reaching the provider should call
    this function directly, never re-derive the string.
    """
    return f"retry-payment-{subscription_pk}-{client_idempotency_key}"


class SubscriptionService:
    """Places organizations on a ``BillingPlan`` and keeps their per-subscription
    limit/entitlement copies in sync with plan changes.

    Under the "no plan-less state" rule, every organization that is its own
    billing root (see ``resolve_billing_root``) has exactly one ``Subscription``.
    A reseller child never gets one of its own — it pools against its root's.
    """

    @inject
    def __init__(
        self,
        payment_service: Annotated["PaymentService | None", Provide["payment_service"]] = None,
        audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
        payment_provider_resolver: Annotated[
            "PaymentProviderResolver | None", Provide["payment_provider_resolver"]
        ] = None,
    ) -> None:
        """``payment_service`` drives the provider round-trips the plan-change
        and add-on purchase flows need (creating/updating the provider-side plan,
        attaching or moving a subscription onto it). Injected via DI, like every
        other cross-service dependency in this codebase (``OrganizationService``'s
        constructor is the model for this) — deliberately **not** the other
        direction: ``PaymentService`` does not depend on ``SubscriptionService``,
        which would make the two circular. The webhook views orchestrate calling
        into both instead (see ``PaymentsViewSet``).

        ``audit_service`` records the billing business writes that need an audit
        trail — currently just ``set_payment_provider``'s staff repoint.

        ``payment_provider_resolver`` is the single home of the pin -> default
        provider rule (see ``payments.services.payment_provider_resolver``).
        ``create_subscription_for_organization`` stamps its result onto the new
        ``Subscription.payment_provider`` -- that column is the sole input to
        every later existing-row provider resolution for the subscription (Rule A),
        so hardcoding it here would make the organization's pin inert on the whole
        subscription path.

        All three default to ``None`` so every existing bare ``SubscriptionService()``
        call across the codebase and test suite keeps working — ``@inject``
        resolves ``Provide["payment_service"]`` / ``Provide["audit_service"]`` /
        ``Provide["payment_provider_resolver"]``
        from the wired container automatically once Django has started
        (``payments`` is in ``INTERNAL_INSTALLED_APPS``, which
        ``DICoreConfig.ready()`` wires), the same pattern ``CalendarService.__init__``
        uses.
        """
        self.payment_service = payment_service
        self.audit_service = audit_service
        self.payment_provider_resolver = payment_provider_resolver

    def _require_payment_service(self) -> "PaymentService":
        if self.payment_service is None:
            raise RuntimeError(
                "SubscriptionService.payment_service is not set -- construct via "
                "the DI container (or pass payment_service=...) before driving "
                "the provider."
            )
        return self.payment_service

    def _require_payment_provider_resolver(self) -> "PaymentProviderResolver":
        if self.payment_provider_resolver is None:
            raise RuntimeError(
                "SubscriptionService.payment_provider_resolver is not set -- "
                "construct via the DI container (or pass "
                "payment_provider_resolver=...) before resolving an "
                "organization's payment provider."
            )
        return self.payment_provider_resolver

    def _require_audit_service(self) -> "AuditService":
        if self.audit_service is None:
            raise RuntimeError(
                "SubscriptionService.audit_service is not set -- construct via "
                "the DI container (or pass audit_service=...) before writing an "
                "audited business change."
            )
        return self.audit_service

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
            plan at rollout, which acts as the "no feature flag" rollout switch).
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
        # Rule B (new row): resolve from the organization -- its
        # `BillingProfile.payment_provider` pin when set, `DEFAULT_PAYMENT_PROVIDER`
        # otherwise -- through the one resolver that owns that rule.
        #
        # This column is *not* a placeholder even though the subscription created
        # here starts on a $0 plan that never touches a gateway: `Subscription` is
        # a `OneToOneField` on organization, so this is the only row the
        # organization will ever have, and it is the row every later paid operation
        # (`process_subscription`, `change_subscription_plan`, `cancel_subscription`,
        # `_ensure_provider_plan`) resolves its adapter from under Rule A. It is
        # also what `PaymentsViewSet._apply_subscription_payment_side_effects`
        # hands to `record_payment_method`, i.e. what gets written into the
        # organization's write-once pin on its first confirmed subscription charge.
        # A hardcoded value here would send a Stripe-pinned organization's card
        # token to MercadoPago and then permanently pin it there.
        provider = self._require_payment_provider_resolver().resolve_for_organization(organization)

        with transaction.atomic():
            subscription, created = Subscription.objects.get_or_create(
                organization=organization,
                defaults={
                    "plan": plan,
                    "billing_state": BillingState.FREE,
                    "billing_interval": BillingInterval.MONTHLY,
                    "current_period_start": now,
                    "current_period_end": period_end,
                    "payment_provider": provider,
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
        idempotency_key: str = "",
    ) -> Subscription:
        """Move ``subscription`` toward ``plan`` — the single entry point behind
        ``POST /billing/subscription/change-plan/``.

        Upgrade vs. downgrade is decided **once**, from ``_plan_price`` compared
        against ``subscription``'s current plan/interval, and that one decision
        is what both the provider is charged against and what capacity is
        eventually granted from — this avoids the recurring "two checks that
        must agree" failure shape by having only one.

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
          for the cycle-close sweep to apply later.

        A request for the plan/interval ``subscription`` is *already* fully
        settled on (no pending change either) is a no-op.

        **Serialized under a row lock.** The subscription row is re-read
        ``SELECT ... FOR UPDATE`` and the settled / already-in-flight checks are
        re-evaluated under that lock, so two concurrent first-upgrade requests
        cannot both observe a blank ``external_id`` and each drive the provider:
        the second blocks until the first commits, then sees its result and
        no-ops (``already_settled``) rather than charging again. ``idempotency_key``
        (required by ``ChangePlanRequestSerializer``) is additionally forwarded to
        the provider as its own idempotency key, so even a crash *after* the
        provider call but *before* the request commits — which rolls back
        ``external_id`` and would otherwise re-drive the provider on retry —
        cannot produce a second subscription/charge.

        :raises UnconfirmedPlanChangeError: a *different* plan change was already
            initiated and its charge is still awaiting webhook confirmation
            (see ``Subscription.plan_change_pending_confirmation``). Re-requesting
            the same plan/interval is a no-op, not an error.
        """
        assert_plan_is_complete(plan)
        with transaction.atomic():
            # Re-read under a row lock so the checks below run against committed
            # state, not the possibly-stale instance the caller handed in. Under
            # `ATOMIC_REQUESTS` the lock is held for the rest of the request
            # (including the provider round trip in `_initiate_upgrade`), which is
            # exactly what serializes concurrent first-upgrades.
            subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
            already_settled = (
                subscription.plan_id == plan.pk
                and subscription.billing_interval == billing_interval
                and subscription.pending_plan_id is None
            )
            if already_settled:
                return subscription
            if subscription.plan_change_pending_confirmation:
                raise UnconfirmedPlanChangeError(subscription.organization_id)

            current_price = self._plan_price(subscription.plan, subscription.billing_interval)
            new_price = self._plan_price(plan, billing_interval)
            if new_price > current_price:
                return self._initiate_upgrade(
                    subscription, plan, billing_interval, payment_token, idempotency_key
                )
            return self._schedule_downgrade(subscription, plan, billing_interval)

    def _initiate_upgrade(
        self,
        subscription: Subscription,
        plan: BillingPlan,
        billing_interval: str,
        payment_token: str,
        idempotency_key: str = "",
    ) -> Subscription:
        # Checked *before* any write: a subscription with no provider-side
        # instrument yet needs a token to attach one, and that is knowable
        # up front, with nothing to unwind if it is missing. (Everything past
        # this point does write, and depends on the caller's transaction --
        # `ATOMIC_REQUESTS` when called from a request, or the `request_plan_change`
        # lock -- to unwind atomically on any *later* failure, same as every
        # other provider round trip in this codebase.)
        if not subscription.external_id and not payment_token:
            raise PaymentTokenRequiredError(subscription.organization_id)

        # An upgrade supersedes any downgrade previously scheduled, and marks
        # itself as awaiting confirmation so a *second*, different upgrade cannot
        # be initiated before this one's charge confirms (which would make the
        # confirming webhook grant the later plan's capacity rather than the plan
        # this charge paid for). Cleared in `confirm_plan_change`.
        subscription.pending_plan = None
        subscription.pending_billing_interval = ""
        subscription.pending_plan_effective_at = None
        subscription.plan = plan
        subscription.billing_interval = billing_interval
        subscription.plan_change_pending_confirmation = True
        subscription.save(
            update_fields=[
                "plan",
                "billing_interval",
                "pending_plan",
                "pending_billing_interval",
                "pending_plan_effective_at",
                "plan_change_pending_confirmation",
            ]
        )
        # NOTE deliberately *not* called here: `_sync_limits`/`_sync_entitlements`
        # are what grant capacity, and this method must not grant it
        # synchronously. `subscription.plan` alone grants nothing --
        # `EntitlementService` reads `SubscriptionPlanLimit`, not this FK.

        # Rule A (existing-row resolution) only protects provider-side state
        # that already exists. A subscription with a blank `external_id` has
        # none -- nothing has ever been created at the provider its
        # `payment_provider` column happens to name -- and this is exactly the
        # row every *first* upgrade runs against, just below. Re-resolving here
        # (pin -> default, via the same `PaymentProviderResolver` Rule B
        # already uses) and restamping is what makes a staff repoint via
        # `set_payment_provider`, or a `DEFAULT_PAYMENT_PROVIDER` change, reach
        # a subscription that was stamped before any `BillingProfile` pin
        # existed -- `create_subscription_for_organization` runs from the
        # `Organization` post-save signal, before a `BillingProfile` can exist.
        # Once `external_id` is non-empty, the row carries live provider state
        # and must not move -- Rule A applies unchanged from here on.
        if not subscription.external_id:
            resolved_provider = self._require_payment_provider_resolver().resolve_for_organization(
                subscription.organization
            )
            if resolved_provider != subscription.payment_provider:
                subscription.payment_provider = resolved_provider
                subscription.save(update_fields=["payment_provider"])

        payment_service = self._require_payment_service()
        created_plan = self._ensure_provider_plan(subscription, plan, billing_interval)
        if not subscription.external_id:
            payment_service.process_subscription(
                subscription, payment_token, idempotency_key=idempotency_key
            )
        else:
            payment_service.change_subscription_plan(
                subscription, created_plan, idempotency_key=idempotency_key
            )
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
        validate a caching scheme against in this environment. Correct, if not
        maximally efficient — a follow-up could add a
        provider-keyed external id to the catalog plan itself.

        ``provider`` is resolved from ``subscription.payment_provider`` --
        `subscription` already exists, so this is an existing-row operation, the
        same rule ``process_subscription``/``change_subscription_plan``/
        ``cancel_subscription`` follow on ``PaymentService``.
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
            ),
            provider=subscription.payment_provider,
        )
        subscription.plan_external_id = created.external_id
        subscription.save(update_fields=["plan_external_id"])
        return created

    def retry_failed_charge(self, subscription: Subscription, idempotency_key: str) -> Subscription:
        """Ask the provider to collect the outstanding balance behind
        ``subscription``'s failed charge -- the automatic dunning ladder's own
        retry (``DunningService._retry_charge_and_notify``, driven by
        ``payments/tasks.py::process_dunning``).

        **Drives ``pay_outstanding_invoice``, not ``change_subscription_plan``.**
        This method used to reuse ``_initiate_upgrade``'s
        ``_ensure_provider_plan`` + ``change_subscription_plan`` pair --
        exactly the operation a live Stripe probe proved collects **$0.00**
        against a real past-due invoice (a same-amount price move prorates to
        zero; see ``BaseSubscriptionAdapter.pay_outstanding_invoice``'s
        docstring for the numbers). The user-facing ``retry_payment`` endpoint
        was fixed first; this method was deliberately left alone until its own
        fix landed here. ``payment_token`` is passed as ``""``: the
        ladder is re-driving whatever instrument is *already on file*, it has
        no new token to attach (see ``BaseSubscriptionAdapter
        .pay_outstanding_invoice``'s docstring for the two-callers contract).

        ``idempotency_key`` reaches the provider's own idempotency header (see
        ``BaseSubscriptionAdapter.pay_outstanding_invoice``), so a Celery task
        redelivery of the same logical dunning attempt (``CELERY_TASK_ACKS_LATE``)
        cannot double-charge.

        Writes nothing about the outcome locally -- success or a further failure
        arrives later through the subscription-payment webhook, exactly like
        every other provider-driven charge in this service.

        A subscription that never attached a payment method (blank
        ``external_id``) has nothing to retry against; returned unchanged rather
        than driving a pointless provider round trip. **Deliberately unlike
        ``retry_payment``**, which raises ``SubscriptionNotAttachedError`` for
        the same condition -- that method is a synchronous user request that
        must not report success having silently done nothing; this one is a
        background beat tick nobody is waiting on, and raising out of it would
        poison the Celery task for a legitimate "nothing attached yet" case.

        **MercadoPago fallback, explicitly temporary, and pinned to
        MercadoPago.** MercadoPago's adapter raises ``CollectionNotSupportedError``
        from ``pay_outstanding_invoice`` -- it has no verified "collect the
        outstanding balance" primitive (see that adapter's docstring).
        Catching it here and falling back to the
        ``_ensure_provider_plan`` + ``change_subscription_plan`` path this
        method used before ``pay_outstanding_invoice`` existed keeps
        MercadoPago's ladder byte-identical **in provider calls, arguments,
        and idempotency key** to that earlier behavior -- not quite byte-identical
        in every respect: an MP subscription whose organization has no
        ``BillingProfile`` now fails one call earlier and without side
        effects (``PaymentService.pay_outstanding_invoice`` raises
        ``MissingBillingProfileError`` from its own ``_serialize_subscription``
        before ever reaching the adapter), where previously
        ``_ensure_provider_plan`` ran first and minted an orphan provider-side
        plan object before ``change_subscription_plan`` hit the same missing
        profile -- a strictly better failure order, not a regression, since
        that error is not caught here and propagates (now to the Celery
        task's own best-effort guard). The fallback exists rather than
        breaking every MercadoPago dunning tick on a guess about a primitive
        nobody has verified. Retire it once
        ``MercadoPagoSubscriptionAdapter.pay_outstanding_invoice``'s own
        docstring probe recipe is run against a real MercadoPago account and
        its refusal is replaced with a verified collection call.

        Re-raised, rather than falling back, for every **other** provider:
        the fallback is a deliberate, temporary concession to MercadoPago's
        specific unverified state, not a generic "this provider raised
        something odd" catch-all. A Stripe subscription (or any future
        provider) is never expected to raise ``CollectionNotSupportedError``
        today -- if one somehow did, silently routing it into
        ``change_subscription_plan`` would drive exactly the operation a live
        Stripe probe proved collects **$0.00** (see this method's own opening
        paragraph), with only an ``INFO`` log to notice by. Re-raising makes
        that combination fail loudly instead.

        **A tick with nothing owed must not raise either -- but it is not a
        routine no-op for a GRACE subscription.** ``NoOutstandingBalanceError``
        means the provider reports nothing outstanding right now even though
        this subscription is in an active payment-failure episode -- e.g. the
        balance was resolved through another channel between two ticks, or a
        genuine bookkeeping mismatch between this system and the provider.
        Swallowed here for the same operational reason as the blank-
        ``external_id`` case above (a background beat must not raise on a
        state it cannot fix by raising), but the caller
        (``DunningService._retry_charge_and_notify``) still stamps
        ``last_dunning_attempt_at`` and still sends that rung's "your payment
        failed, update your card" (or final-warning) email regardless of this
        swallow -- so a subscription that hits this on every tick is silently
        walked to RESTRICTED at grace expiry while the provider says it owes
        nothing. Logged at ``WARNING``, not ``INFO``: this is an
        inconsistency worth someone's attention, not routine "nothing to do".
        Reconciling the provider's and this system's view of the balance is a
        separate decision and deliberately out of scope here.

        **Nor must a declined (or unattemptable) charge.** ``ChargeDeclinedError``
        means the provider either attempted the charge and the card on file was
        declined, or refused to attempt it at all (e.g. no default payment
        method on file) -- see that exception's own docstring for the full
        translation. Either is the *common* dunning-tick outcome, since a
        dead or missing card is why the subscription is in dunning at all.
        Left uncaught, this reached the Celery task
        (``payments.tasks.process_dunning_for_subscription``) unhandled: per
        that task's own docstring, a raising task "is redelivered and fails
        identically forever, turning a benign race into a permanent stream of
        alerts" -- exactly what a still-dead card would do on every
        subsequent tick. Logged and swallowed here, same as the two outcomes
        above -- **not** because the provider's own webhook will perform some
        transition on this decline (a ladder retry never reaches the
        webhook-driven ``enter_grace`` edge: the subscription here is already
        GRACE/RESTRICTED, and ``DunningService.enter_grace`` no-ops for both
        -- see that method's docstring), but because the tick's own
        bookkeeping (``last_dunning_attempt_at`` and the ladder's reminder
        email, both written by ``_retry_charge_and_notify``) must not be
        rolled back by an expected decline. The webhook, when it arrives,
        still records the ``Payment``/``PaymentStatusUpdate`` rows for this
        failed charge -- it just performs no ``billing_state`` transition of
        its own here.
        """
        if not subscription.external_id:
            logger.warning(
                "retry_failed_charge: Subscription %s has no external_id -- nothing "
                "attached at the provider to retry.",
                subscription.pk,
            )
            return subscription
        payment_service = self._require_payment_service()
        needs_mercadopago_fallback = False
        try:
            payment_service.pay_outstanding_invoice(
                subscription, payment_token="", idempotency_key=idempotency_key
            )
        except CollectionNotSupportedError:
            if subscription.payment_provider != PaymentProviders.MERCADOPAGO:
                # See this method's docstring for why the fallback below is
                # pinned to MercadoPago and every other provider re-raises.
                raise
            logger.info(
                "retry_failed_charge: Subscription %s's provider has no verified "
                "pay_outstanding_invoice primitive -- falling back to the "
                "older change_subscription_plan path. See "
                "MercadoPagoSubscriptionAdapter.pay_outstanding_invoice's "
                "docstring for what would retire this fallback.",
                subscription.pk,
            )
            needs_mercadopago_fallback = True
        except NoOutstandingBalanceError:
            logger.warning(
                "retry_failed_charge: Subscription %s has no outstanding balance "
                "at the provider right now, despite being in an active dunning "
                "episode -- the ladder's own bookkeeping (last_dunning_attempt_at, "
                "the reminder email) still proceeds regardless. See this method's "
                "docstring: this is an inconsistency, not a routine no-op.",
                subscription.pk,
            )
        except ChargeDeclinedError as e:
            logger.info(
                "retry_failed_charge: Subscription %s's charge was declined by "
                "the provider (%s) -- an expected dunning-tick outcome, not an "
                "error. See this method's docstring for why nothing further is "
                "written here.",
                subscription.pk,
                e.provider_message,
            )
            if e.invoices_paid:
                logger.warning(
                    "retry_failed_charge: Subscription %s partially collected -- %d "
                    "outstanding invoice(s) were paid before the provider declined a "
                    "later one (%s). This is a partial, not a pure, decline.",
                    subscription.pk,
                    e.invoices_paid,
                    e.provider_message,
                )
        # Run outside the `except` block above (rather than inside it) so a
        # failure in the fallback itself surfaces as its own clean traceback
        # rather than "During handling of the above exception..." burying the
        # real error under the CollectionNotSupportedError that triggered it.
        if needs_mercadopago_fallback:
            created_plan = self._ensure_provider_plan(
                subscription, subscription.plan, subscription.billing_interval
            )
            payment_service.change_subscription_plan(
                subscription, created_plan, idempotency_key=idempotency_key
            )
        return subscription

    def retry_payment(
        self, subscription: Subscription, payment_token: str, idempotency_key: str
    ) -> Subscription:
        """Grace recovery: attach a *new* payment instrument to ``subscription``
        and collect the outstanding balance against it -- the single entry
        point behind ``POST /billing/subscription/retry-payment/``.

        This is **not** ``retry_failed_charge`` (nor, transitively, the
        ``_ensure_provider_plan`` + ``change_subscription_plan`` pair
        ``retry_failed_charge`` drives) -- it used to be, and that was a
        defect, not a design choice.
        ``change_subscription_plan`` moves a subscriber onto a plan and only
        charges a *proration* as a side effect of that move; it was never a
        "collect the missed payment" primitive, and a Stripe test-mode probe
        driving the real adapter methods with a Test Clock proved it out on a
        genuine renewal failure:

        - $49 renewal invoice: ``open`` before retry, still ``open`` after.
        - Collected: $49.00 before retry, **$0.00** from the retry itself.
        - Stripe subscription status: ``past_due`` before retry, ``active`` after
          (a false recovery -- see below).

        The mechanism: ``_ensure_provider_plan`` mints a *fresh* Stripe Price at
        the *same* amount every call (see its own docstring for why), so
        ``Subscription.modify(proration_behavior="always_invoice")`` produced
        offsetting proration line items (``-42.47`` / ``+42.47``) that net to
        zero. Stripe raised a **$0.00 invoice**, finalized it, marked it paid,
        and flipped the subscription to ``active`` -- while the real past-due
        invoice sat untouched, and that $0.00 invoice's ``invoice.paid`` event
        was itself in ``RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES``, so it would
        have reached ``resolve_payment_success`` and reported a false recovery
        had the zero-amount guard (``PaymentsViewSet
        ._apply_subscription_payment_side_effects``) not also been added.
        **Do not restore the call to ``retry_failed_charge``/
        ``change_subscription_plan`` here** -- that is what silently collected
        nothing while reporting success.

        This method now attaches ``payment_token`` first
        (``PaymentService.update_subscription_payment_token`` ->
        ``BaseSubscriptionAdapter.update_subscription_payment_token``), *then*
        drives ``PaymentService.pay_outstanding_invoice`` ->
        ``BaseSubscriptionAdapter.pay_outstanding_invoice`` -- the provider's
        actual "collect the specific outstanding balance now" primitive (Stripe:
        locate the subscription's ``open`` invoice and ``Invoice.pay`` it; see
        that method's docstring for the full contract and MercadoPago's explicit,
        unverified refusal) -- against the newly attached instrument. Order
        matters, since attaching after charging would charge the dead instrument
        one more time.

        **Serialized by a row lock; deduplicated by the caller's idempotency
        key -- not by this method.** These are two separate claims, and only
        the first is this method's job. The subscription row is re-read
        ``SELECT ... FOR UPDATE`` inside ``transaction.atomic()``, exactly like
        ``request_plan_change``, so two concurrent calls for the same
        subscription cannot interleave mid-flight -- the second blocks until
        the first commits. That lock buys ordering, not dedup: an earlier
        version of this method additionally stamped ``last_dunning_attempt_at``
        under the lock and refused a second call landing in the same
        ``retry_attempt_ordinal`` bucket as the dunning ladder's own throttle.
        That gate was removed -- ``last_dunning_attempt_at`` is also stamped by
        the dunning ladder (``DunningService._retry_charge_and_notify``), and
        ``MIN_DUNNING_RETRY_INTERVAL`` is 20 hours, so gating this endpoint on
        that field meant: the ladder ticks on the payer's dead card, stamps the
        bucket, the payer gets the "update your card" notification, submits a
        new card five minutes later -- and is refused with 409 for up to 20
        hours, on the one endpoint that exists to let them fix exactly this.
        Worse, if that new card is itself declined, the payer cannot try a
        *different* card for up to 20 hours either. The field cannot tell "a
        duplicate submission" apart from "the ladder ran" or "a different card
        was already tried", so gating on it blocked the cases that must work.
        Do not restore that gate.

        Deduplication is instead the caller's ``idempotency_key`` -- namespaced
        below into ``retry-payment-{subscription.pk}-{idempotency_key}`` and
        forwarded to the provider as ``x-idempotency-key``. A repeat submission
        of the *same* user intent (a double-click, a client retrying a slow
        response without regenerating its key) carries the *same* client key,
        so the provider collapses it into a single charge -- this method drives
        the attach + charge call again, but the second charge never lands. A
        *different* client key is a deliberately different attempt and is
        deliberately allowed to reach the provider: it is indistinguishable
        from "my first new card was declined, here is another one", and must
        not be refused.

        **Idempotency key is namespaced** ``retry-payment-{subscription.pk}-{idempotency_key}``,
        structurally distinct from the dunning ladder's own
        ``dunning-retry-{pk}-{ordinal}`` (see ``DunningService._retry_charge_and_notify``).
        This is the load-bearing detail of grace recovery: a payer retrying with
        a *new* card must never be deduplicated by the provider against the
        *scheduled* dunning attempt that just failed on the *old* card -- if the
        two shared a bucket, the provider would treat the user's new-card charge
        as a repeat of the already-failed old-card one and swallow it, and every
        surface (user, API, dashboard) would report success while no money
        moved.

        Writes nothing about the *outcome* locally, exactly like
        ``retry_failed_charge`` -- success arrives later through the
        subscription-payment webhook (``DunningService.resolve_payment_success``
        -> ``ACTIVE``), never synchronously from this call. The 200 this drives
        from the view does **not** mean "you are now active".

        :raises RetryPaymentNotApplicableError: ``subscription.billing_state``
            is not ``GRACE`` or ``RESTRICTED``; or the episode is
            downgrade-originated (``is_downgrade_grace``) rather than a failed
            charge -- see below. There is no failed charge to retry right now.
        :raises SubscriptionNotAttachedError: ``subscription.external_id`` is
            blank -- there is no provider-side instrument to attach a new token
            to or a balance to collect against. Unlike ``retry_failed_charge``'s
            existing dunning-ladder caller (which logs and returns unchanged --
            fine for a background beat tick nobody is waiting on), a user-facing
            request must not report success having silently done nothing.

        Not every GRACE/RESTRICTED episode means "a payment failed" --
        ``_schedule_downgrade`` also puts a subscription into GRACE, with
        ``pending_plan`` set, when an org is over its *new, lower* limits, with
        no charge ever having failed. ``is_downgrade_grace`` (module-level in
        ``dunning_service``, shared with ``DunningService._process_grace`` -- see
        its docstring; the two call sites must not drift apart) is checked and
        refused before anything is driven: there is no failed charge behind a
        downgrade-originated episode, so there is nothing for
        ``pay_outstanding_invoice`` to legitimately collect either -- refusing
        here up front with the specific, actionable ``retry_payment_not_applicable``
        gives a clearer signal than letting the request through only to bounce
        off ``NoOutstandingBalanceError`` after already attaching a new payment
        instrument for no reason.
        """
        with transaction.atomic():
            # Re-read under a row lock, same discipline as `request_plan_change`
            # -- see this method's docstring for what the lock does and does
            # not buy: it serializes concurrent calls, it does not deduplicate
            # them.
            subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
            if subscription.billing_state not in (BillingState.GRACE, BillingState.RESTRICTED):
                raise RetryPaymentNotApplicableError(subscription.organization_id)
            if is_downgrade_grace(subscription):
                raise RetryPaymentNotApplicableError(subscription.organization_id)
            if not subscription.external_id:
                raise SubscriptionNotAttachedError(subscription.organization_id)

            payment_service = self._require_payment_service()
            payment_service.update_subscription_payment_token(subscription, payment_token)
            namespaced_idempotency_key = retry_payment_idempotency_key(
                subscription.pk, idempotency_key
            )
            # `pay_outstanding_invoice`,
            # never `retry_failed_charge`/`change_subscription_plan` -- see this
            # method's docstring for the probe evidence of why that collected
            # $0.00 against a real past-due invoice.
            payment_service.pay_outstanding_invoice(
                subscription, payment_token, namespaced_idempotency_key
            )
        return subscription

    def _schedule_downgrade(
        self, subscription: Subscription, plan: BillingPlan, billing_interval: str
    ) -> Subscription:
        """No cash refund: schedule ``plan`` to take over at the next period
        boundary while applying its (lower, or equal-price) limits immediately.

        ``grace_period_ends_at`` is stamped so the org has a window to reduce
        usage before anything past the ordinary "no new creates over the
        ceiling" enforcement applies -- nothing here evicts or deletes existing
        over-count resources; ``check_limit`` never has.

        **Also drives ``billing_state`` into GRACE** (fixes a dead edge where a
        downgrade left the state ACTIVE/FREE): before this, ``grace_period_ends_at``
        was stamped here but ``billing_state`` stayed ACTIVE/FREE, so
        ``process_dunning``'s GRACE/RESTRICTED sweep (``payments/tasks.py``) never
        looked at this row and the stamped deadline never expired -- a downgrade
        that left an organization over its new limits could sit indefinitely with
        a "grace window" nothing was ever going to close. Routing this write
        through ``transition_billing_state`` like every other ``billing_state``
        change puts it on the one path the sweep already watches:
        ``DunningService`` tells a downgrade-originated grace episode apart from a
        payment-failure one (``is_downgrade_grace``) only to skip
        the charge-retry ladder -- there is no charge to retry for a downgrade --
        and to resolve it against the just-applied (lower) limits rather than the
        catalog ``free`` plan at expiry (``DunningService._expire_downgrade_grace``).

        ``transition_billing_state`` is idempotent on an already-GRACE
        subscription (e.g. downgrading a second time before the first grace
        window resolved) and legal from both ACTIVE and FREE (see
        ``LEGAL_BILLING_STATE_TRANSITIONS``). A RESTRICTED or CANCELLED
        subscription has no GRACE edge on the diagram from those states --
        rather than raise and abort an otherwise-valid downgrade request (the
        limits below must still apply), the illegal-transition case is caught
        and logged; ``billing_state`` is left exactly as it was, matching this
        method's behavior before this change. Escalating *out of* RESTRICTED is
        not this method's job.
        """
        assert_plan_is_complete(plan)
        with transaction.atomic():
            subscription.pending_plan = plan
            subscription.pending_billing_interval = billing_interval
            subscription.pending_plan_effective_at = subscription.current_period_end
            grace_days = plan.grace_period_days
            if grace_days is None:
                grace_days = settings.BILLING_DEFAULT_GRACE_PERIOD_DAYS
            subscription.grace_period_ends_at = timezone.now() + datetime.timedelta(days=grace_days)
            subscription.save(
                update_fields=[
                    "pending_plan",
                    "pending_billing_interval",
                    "pending_plan_effective_at",
                    "grace_period_ends_at",
                ]
            )
            try:
                transition_billing_state(subscription, BillingState.GRACE)
            except IllegalBillingStateTransitionError:
                logger.warning(
                    "_schedule_downgrade: Subscription %s requested a downgrade while "
                    "billing_state=%s, which has no GRACE edge on the billing lifecycle "
                    "diagram. grace_period_ends_at/pending_plan are stamped above "
                    "regardless; billing_state is left unchanged.",
                    subscription.pk,
                    subscription.billing_state,
                )
            self._sync_limits(subscription, plan)
            self._sync_entitlements(subscription, plan)
        return subscription

    def confirm_plan_change(self, subscription: Subscription) -> Subscription:
        """Grant the capacity for the plan ``subscription``'s latest charge paid
        for, once that charge is confirmed ``APPROVED`` by the provider.

        Called from the subscription-payment webhook path
        (``PaymentsViewSet.subscription_payment_update``) — never synchronously
        from the request that initiates an upgrade.

        Two cases, keyed off whether a *scheduled downgrade* is currently in its
        grace window (``pending_plan`` set, ``pending_plan_effective_at`` still in
        the future):

        - **No pending future downgrade** (an upgrade confirmation, or a routine
          renewal): re-copy against ``subscription.plan`` via ``change_plan`` —
          the exact field ``_initiate_upgrade`` set at initiation time.
        - **Downgrade in its grace window**: ``_schedule_downgrade`` already
          applied the *lower* pending plan's limits immediately while leaving
          ``subscription.plan`` on the still-paid higher plan. A subscription
          payment landing now must **not** restore the higher plan's limits, so
          sync from the pending (lower) plan instead. This is the fix for a
          redelivered APPROVED webhook silently lifting the ceiling back up
          mid-downgrade; ``subscription.plan`` stays on the paid plan until the
          cycle-close boundary sweep flips it.

        Idempotent / safe to call on every approved subscription payment (not
        only the first one after an upgrade) — both sync paths are bulk upserts —
        and clears ``plan_change_pending_confirmation`` so a further plan change
        is allowed again.

        The ``billing_state -> ACTIVE`` write goes through
        ``billing_state_machine.transition_billing_state`` — the same validator
        ``DunningService`` uses for every other transition — rather
        than writing the field directly, so this and the dunning ladder can never
        define "which transitions are legal" two different ways. In practice
        this is a same-state no-op on every call that lands here: a webhook's
        ``PaymentsViewSet`` handler calls ``DunningService.resolve_payment_success``
        first, which already moves ``GRACE``/``RESTRICTED`` subscriptions to
        ``ACTIVE`` before this runs. ``FREE -> ACTIVE`` (a first-ever upgrade
        confirming) and ``ACTIVE -> ACTIVE`` (a routine renewal) are both legal
        edges on the diagram and are what this call actually drives. A stray
        approved payment for an already-``CANCELLED`` subscription is not on the
        diagram at all (cancellation has no automatic reactivation edge) — logged
        and left alone rather than raised, since a webhook handler must not 500
        on a real, if unusual, provider delivery.
        """
        pending_plan = subscription.pending_plan
        if pending_plan is not None and self._pending_downgrade_is_future(subscription):
            self._sync_limits(subscription, pending_plan)
            self._sync_entitlements(subscription, pending_plan)
        else:
            self.change_plan(subscription, subscription.plan)

        if subscription.plan_change_pending_confirmation:
            subscription.plan_change_pending_confirmation = False
            subscription.save(update_fields=["plan_change_pending_confirmation"])

        try:
            transition_billing_state(subscription, BillingState.ACTIVE)
        except IllegalBillingStateTransitionError:
            logger.warning(
                "confirm_plan_change: Subscription %s received an approved payment "
                "while billing_state=%s, which has no ACTIVE edge on the billing "
                "lifecycle diagram (e.g. a stray webhook for a cancelled subscription). "
                "Plan/limit sync above still applied; billing_state left unchanged.",
                subscription.pk,
                subscription.billing_state,
            )
        return subscription

    def _pending_downgrade_is_future(self, subscription: Subscription) -> bool:
        """True while a scheduled downgrade is still within its grace window --
        ``pending_plan`` set and ``pending_plan_effective_at`` not yet reached.
        The single predicate ``confirm_plan_change`` uses to decide it must sync
        the *lower* pending plan's limits rather than restore ``subscription.plan``'s."""
        return (
            subscription.pending_plan_id is not None
            and subscription.pending_plan_effective_at is not None
            and subscription.pending_plan_effective_at > timezone.now()
        )

    def cancel_subscription(self, subscription: Subscription) -> Subscription:
        """Cancel ``subscription``. Runs the provider-side cancellation
        (best-effort skipped when the org never attached a payment method) and
        moves ``billing_state`` to ``CANCELLED`` immediately.

        The move goes through ``transition_billing_state`` like every other
        ``billing_state`` write, not a raw field assignment, so it is validated
        against ``LEGAL_BILLING_STATE_TRANSITIONS`` (which carries the
        ``FREE``/``GRACE``/``RESTRICTED`` -> ``CANCELLED`` edges this action
        needs, beyond the diagram's single ``ACTIVE -> CANCELLED``). Any dunning
        bookkeeping (``grace_period_ends_at``/``last_dunning_attempt_at``) is
        cleared in the same write so a cancelled row never carries a stale grace
        deadline the dunning sweep would otherwise ignore anyway.

        The spec's full "runs to the end of the paid cycle, then reverts to
        FREE" lifecycle is handled by the cycle-close sweep (the
        ``CANCELLED -> FREE`` edge) -- this method only exposes the immediate
        cancel action the endpoint needs.
        """
        payment_service = self._require_payment_service()
        if subscription.external_id:
            payment_service.cancel_subscription(subscription)
        with transaction.atomic():
            transition_billing_state(subscription, BillingState.CANCELLED)
            subscription.grace_period_ends_at = None
            subscription.last_dunning_attempt_at = None
            subscription.save(update_fields=["grace_period_ends_at", "last_dunning_attempt_at"])
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
        failed -- this is the fail-closed-for-money rule: when in doubt
        whether a prior attempt already charged, do not charge again. The
        ``get_or_create`` below is the single decision both "was this already
        purchased" and "was this already charged" hang off, on purpose -- a
        second, independently-derived answer to either question is exactly the
        "two checks that must agree" defect shape to avoid.

        The ``get_or_create`` dedup alone is **not** durable against a crash: the
        row only commits when the surrounding request transaction commits, and
        under ``ATOMIC_REQUESTS`` that is *after* the provider call below, so a
        crash between the charge and the commit rolls the row back and a retry
        would ``create=True`` again. ``idempotency_key`` is therefore also
        forwarded to the provider as its own idempotency key (see
        ``PaymentService.create_payment`` / ``BasePaymentAdapter.process``), so
        the provider itself refuses the second charge even when the local dedup
        row did not survive -- that is what makes "at most once" hold across a
        rollback or process restart, not merely within one committed transaction.

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
            idempotency_key=idempotency_key,
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
        stay in effect, and there is no cycle-close sweep yet to
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
        source of truth, replacing the earlier ``billing_state`` proxy.
        Called only from the webhook path, once a charge against the
        instrument is confirmed ``APPROVED`` -- never synchronously from a
        request that merely attempts to attach one.

        ``external_id`` is whatever the provider returned for the confirmed
        charge (a payment's or a subscription's external id) -- there is no
        separate "tokenize a card" step in this codebase's provider adapters,
        so a confirmed charge is the strongest signal available that the
        instrument behind it is real and chargeable. A blank id means there is
        nothing to record (should not happen for a confirmed charge; logged and
        skipped rather than writing a meaningless row).

        Also pins ``organization``'s ``BillingProfile.payment_provider`` to
        ``provider``, in the same transaction as the ``PaymentMethod``
        ``get_or_create`` -- but only the first time: an already-pinned profile
        is left untouched. An organization that somehow gets a confirmed
        instrument at a *second*, different provider keeps its original pin --
        the discrepancy is logged at ``warning`` so it surfaces rather than
        silently repointing future charges.

        The pin write is a single conditional ``UPDATE ... WHERE payment_provider
        = ''``, not a read-then-write. Two concurrent calls for the same
        organization at *different* providers, each inside its own
        ``transaction.atomic()``, cannot both observe an empty pin in Python and
        both issue an unconditional ``save()`` -- only one row-matching ``UPDATE``
        can win at the database. The loser's zero-affected-rows result is what
        drives the "already pinned" warning below, so the discrepancy still
        surfaces even under real concurrency, not just when calls happen to run
        sequentially.
        """
        if not external_id:
            logger.warning(
                "record_payment_method called with no external_id for organization %s "
                "provider %s; nothing recorded.",
                organization.pk,
                provider,
            )
            return None
        with transaction.atomic():
            payment_method, _created = PaymentMethod.objects.get_or_create(
                organization=organization,
                provider=provider,
                external_id=external_id,
                defaults={"is_active": True},
            )
            if not payment_method.is_active:
                payment_method.is_active = True
                payment_method.save(update_fields=["is_active"])

            try:
                billing_profile = organization.billing_profile
            except BillingProfile.DoesNotExist:
                logger.warning(
                    "record_payment_method confirmed a payment method for organization "
                    "%s at provider %s, but the organization has no BillingProfile to "
                    "pin; nothing pinned.",
                    organization.pk,
                    provider,
                )
            else:
                # Conditional UPDATE, not a Python-level "is it empty" branch --
                # see the docstring above. Only a row that is *still* unpinned at
                # the moment the UPDATE runs matches the WHERE clause, so exactly
                # one of two concurrent callers can ever win the pin.
                pinned = BillingProfile.objects.filter(
                    organization=organization, payment_provider=""
                ).update(payment_provider=provider)
                if not pinned:
                    billing_profile.refresh_from_db(fields=["payment_provider"])
                    if billing_profile.payment_provider != provider:
                        logger.warning(
                            "Organization %s confirmed a payment method at provider %s but "
                            "is already pinned to %s; leaving the existing pin in place.",
                            organization.pk,
                            provider,
                            billing_profile.payment_provider,
                        )
        return payment_method

    def set_payment_provider(
        self, organization: Organization, provider: str, actor: "User | None" = None
    ) -> BillingProfile:
        """Staff repoint lever: pin ``organization``'s ``BillingProfile`` to
        ``provider``, overwriting whatever it was pinned to before (including a
        never-pinned, empty profile).

        Unlike ``record_payment_method``'s write-once pin, this always writes --
        it is the explicit escape hatch for moving an organization's *future*
        charges onto a different provider. Callers are Django admin (see
        ``payments.admin.BillingProfileAdmin``) or an operator running this by
        hand; there is no end-user-facing API surface for it -- staff repoints
        are deliberately kept off the public API.

        ``provider=""`` is a legitimate un-pin, not an error: an empty string is
        what the admin's ``<select>`` submits when a staff member clears the
        field, and a staff repoint (including back to "unset") is treated as a
        deliberate action, not a slug to validate against the provider
        registry. Any other value must still name a real, configured provider.

        Deliberately carries **no active-subscription guard**: this succeeds
        even when the organization holds a live ``Subscription`` at the old
        provider. That is a knowingly accepted tradeoff, not an oversight --
        the lever exists precisely for the migrate-a-customer-off-a-provider
        case, and a guard would block it in exactly that scenario. Unwinding
        the stranded provider-side subscription this leaves behind is the
        operator's manual responsibility; the audit entry below records the
        pre-repoint provider so that stranded state stays traceable.

        :param actor: The staff member driving this repoint, when called from a
            request context (e.g. ``BillingProfileAdmin.save_model``). Resolved
            to a ``MEMBERSHIP`` actor via ``AuditService.actor_from_user`` so the
            audit entry names who repointed the pin, not just what changed.
            Defaults to ``None``, which records a ``SYSTEM`` actor -- unchanged
            behavior for every caller that has no request-scoped user (an
            operator running this by hand, or a script).
        Validates **registry membership only**, deliberately: it does not require
        the deployment to already hold that provider's outbound credential
        (``PaymentService.get_configured_payment_adapter``). Repointing an
        organization onto a provider whose secret is not in this environment yet
        is a legitimate staff action -- the ordering "flip the pin, then add the
        key" is normal, the pin only governs *future* charges, and refusing it
        here would surface as an uncaught 500 in the Django admin (see
        ``payments.admin.BillingProfileAdmin.save_model``, which catches nothing).
        The credential is asserted later, at the charge call sites that actually
        need it.

        :raises UnknownPaymentProviderError: when a non-empty ``provider`` is not
            a member of ``PaymentProviders``, or is absent from the payment
            adapter registry (``PaymentService.get_payment_adapter``) -- either
            way, there is no adapter this deployment could ever drive that
            provider with. The only exception this method raises for a bad slug.
        :raises MissingBillingProfileError: when ``organization`` has no
            ``BillingProfile`` to pin.
        """
        if provider:
            if provider not in PaymentProviders.values:
                raise UnknownPaymentProviderError(provider)
            # Registry membership only -- see the docstring above. Raises
            # `UnknownPaymentProviderError` for a slug with no registered adapter
            # and nothing else.
            self._require_payment_service().get_payment_adapter(provider)

        try:
            billing_profile = organization.billing_profile
        except BillingProfile.DoesNotExist as e:
            raise MissingBillingProfileError from e

        previous_provider = billing_profile.payment_provider
        billing_profile.payment_provider = provider
        billing_profile.save(update_fields=["payment_provider"])

        audit_service = self._require_audit_service()
        actor_snapshot = (
            audit_service.actor_from_user(actor, organization.pk)
            if actor is not None
            else audit_service.system_actor()
        )
        audit_service.record(
            organization_id=organization.pk,
            action=AuditAction.UPDATE,
            actor=actor_snapshot,
            subject=audit_service.subject_from_instance(billing_profile),
            diff={"payment_provider": {"old": previous_provider, "new": provider}},
        )
        return billing_profile

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
