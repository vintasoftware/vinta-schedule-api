"""Grace, dunning, and the restricted transition.

Orchestrates every ``BillingState`` transition that follows a payment outcome --
entering GRACE on a failed charge, retrying across the grace window with
escalating notification, expiring into RESTRICTED, and recovering to ACTIVE or
FREE -- on top of the single validator in ``billing_state_machine.py``.

**Every caller that drives one of these transitions goes through this
service** (the webhook handlers in ``payments/views.py``, the beat task in
``payments/tasks.py``) rather than writing ``Subscription.billing_state``
directly -- see ``billing_state_machine.LEGAL_BILLING_STATE_TRANSITIONS`` for
why that matters: the set of transitions the diagram permits and the set the
code can actually perform must be the same set, defined once.

Two things important enough to repeat here (see their docstrings for the
full reasoning):

- **Never touches ``PaymentMethod``.** A failed charge says nothing about
  whether the card is still attached -- ``EntitlementService.has_payment_method``
  must keep reading ``True`` for a GRACE organization with a card on file, so it
  keeps accruing postpaid usage; the dunning ladder, not the postpaid guard, is
  what escalates it.
- **Clears ``plan_change_pending_confirmation``** whenever it moves a
  subscription into GRACE, so a first-upgrade whose initial charge fails does
  not leave the organization stuck unable to request a different plan (the flag
  was set when the upgrade was initiated; a failed charge never reaches the
  APPROVED webhook branch that would otherwise clear it).
"""

import datetime
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from vintasend.constants import NotificationTypes
from vintasend.services.notification_service import NotificationContextDict

from organizations.models import OrganizationMembership
from payments.billing_constants import BillingState
from payments.constants import PaymentStatuses
from payments.models import BillingPlan, Subscription
from payments.services.billing_state_machine import transition_billing_state


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationService

    from payments.services.entitlement_service import EntitlementService
    from payments.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)

#: The width of one dunning **retry bucket**: the grace window is sliced into
#: consecutive intervals of this length, counting from the moment grace began,
#: and at most one real retry charge is attempted per bucket. This single
#: quantity is what both the retry throttle *and* the charge's idempotency key
#: derive from (``_retry_attempt_ordinal``), so the gate that decides *whether*
#: to retry and the key that decides *which* charge it is can never disagree --
#: the earlier design quantized those two decisions independently (a 20h
#: rolling gate vs. a calendar-day key) and, for a first attempt landing between
#: 00:00 and 03:59 UTC, opened the gate a second time inside the *same* calendar
#: day, so the second charge reached the provider with the first attempt's key
#: and was silently deduplicated -- a genuine collection attempt dropped. Two
#: attempts in different buckets now always get distinct keys; a
#: ``CELERY_TASK_ACKS_LATE`` redelivery of the *same* attempt (same bucket)
#: reuses one. A little under 24h so the ladder retries roughly daily.
MIN_DUNNING_RETRY_INTERVAL = datetime.timedelta(hours=20)

#: Below this much time left before ``grace_period_ends_at``, the reminder
#: escalates to ``"final_warning"``. See ``dunning_reminder_context``.
FINAL_WARNING_WINDOW = datetime.timedelta(days=1)

#: ``PaymentStatuses`` that count as "the recurring charge failed" for the
#: subscription-payment webhook (``PaymentsViewSet.subscription_payment_update``)
#: to move a subscription into GRACE. Deliberately narrower than "anything that
#: isn't APPROVED":
#:
#: - ``PENDING``/``IN_PROCESS``/``IN_MEDIATION`` are not failures yet -- the
#:   provider has not finished deciding.
#: - ``CANCELLED`` is the payer backing out of a charge attempt, not the
#:   provider declining one; conflating the two would move a subscription into
#:   GRACE on an action nobody except the payer initiated.
#: - ``CHARGED_BACK`` is a reversal *after* a charge already succeeded -- a
#:   dispute/fraud flow with its own remediation, not something a dunning retry
#:   can fix by trying the same charge again.
#: - ``REFUNDED``/``PARTIALLY_REFUNDED`` are post-success outcomes for the same
#:   reason.
FAILED_SUBSCRIPTION_PAYMENT_STATUSES: frozenset[str] = frozenset(
    {
        PaymentStatuses.REJECTED,
        PaymentStatuses.REJECTED_BY_BANK,
        PaymentStatuses.EXPIRED,
        PaymentStatuses.ERROR,
    }
)

#: Slug of the catalog's free-tier ``BillingPlan``, seeded by
#: ``payments.migrations.0007_seed_billing_plans``. Mirrors that migration's own
#: ``FREE_PLAN_SLUG`` constant (kept as a separate literal rather than imported --
#: importing a slug constant out of a migration module ties this service to that
#: module's continued existence, same reasoning as every other seeded-slug
#: reference in this codebase, e.g. ``payments.migrations.0009``'s own
#: ``UNLIMITED_PLAN_SLUG``).
FREE_PLAN_SLUG = "free"


class DunningService:
    """Stateless; injected via ``di_core.containers``.

    Unlike ``SubscriptionService``/``EntitlementService``, this does not use
    ``@inject``-resolved defaults -- ``di_core.containers.AppContainer`` always
    constructs it with explicit ``subscription_service``/``entitlement_service``/
    ``notification_service`` arguments (mirroring
    ``ExternalEventChangeRequestService``'s registration), and every test builds
    it explicitly too, so there is no bare ``DunningService()`` call site that
    needs automatic resolution from the wired container.
    """

    def __init__(
        self,
        subscription_service: "SubscriptionService",
        entitlement_service: "EntitlementService",
        notification_service: "NotificationService | None" = None,
    ) -> None:
        self.subscription_service = subscription_service
        self.entitlement_service = entitlement_service
        self.notification_service = notification_service

    # ------------------------------------------------------------------
    # Webhook-driven transitions
    # ------------------------------------------------------------------

    def enter_grace(self, subscription: Subscription) -> Subscription:
        """ACTIVE|FREE -> GRACE on a failed recurring charge.

        Idempotent and tolerant rather than strict: called from the
        subscription-payment webhook whenever a charge for ``subscription``
        comes back failed, which can legitimately happen while the subscription
        is already GRACE (a later retry also fails), RESTRICTED (further along
        the ladder already), or CANCELLED (dunning no longer applies) -- none of
        those are on the diagram as a *source* for this edge, so rather than
        raise on a normal provider redelivery timing race, this treats all three
        as a no-op and returns unchanged.

        Stamps ``grace_period_ends_at`` from ``BillingPlan.grace_period_days``,
        falling back to the ``BILLING_DEFAULT_GRACE_PERIOD_DAYS`` setting, and
        clears ``plan_change_pending_confirmation`` (see module docstring) in
        the same transition, so a failed first-upgrade charge does not leave the
        organization stuck.

        Does **not** touch ``PaymentMethod`` -- see module docstring.
        """
        if subscription.billing_state in (
            BillingState.GRACE,
            BillingState.RESTRICTED,
            BillingState.CANCELLED,
        ):
            return subscription

        with transaction.atomic():
            transition_billing_state(subscription, BillingState.GRACE)
            grace_days = self._grace_period_days(subscription)
            subscription.grace_period_ends_at = timezone.now() + datetime.timedelta(days=grace_days)
            subscription.last_dunning_attempt_at = None
            update_fields = ["grace_period_ends_at", "last_dunning_attempt_at"]
            if subscription.plan_change_pending_confirmation:
                subscription.plan_change_pending_confirmation = False
                update_fields.append("plan_change_pending_confirmation")
            subscription.save(update_fields=update_fields)
            transaction.on_commit(lambda: self._notify_entered_grace(subscription))
        return subscription

    def resolve_payment_success(self, subscription: Subscription) -> Subscription:
        """GRACE|RESTRICTED -> ACTIVE on a confirmed charge.

        Called from the subscription-payment webhook *before*
        ``SubscriptionService.confirm_plan_change`` -- once this has moved
        ``billing_state`` to ``ACTIVE``, ``confirm_plan_change``'s own
        (idempotent) transition call is a same-state no-op, so the two never
        disagree about which write actually happened.

        No-op for any other state (``ACTIVE``, ``FREE``, ``CANCELLED``) -- there
        is no dunning bookkeeping to clear, and ``confirm_plan_change`` owns the
        ``FREE -> ACTIVE`` first-upgrade-confirmation transition on its own.
        """
        if subscription.billing_state not in (BillingState.GRACE, BillingState.RESTRICTED):
            return subscription

        was_restricted = subscription.billing_state == BillingState.RESTRICTED
        with transaction.atomic():
            transition_billing_state(subscription, BillingState.ACTIVE)
            subscription.grace_period_ends_at = None
            subscription.last_dunning_attempt_at = None
            subscription.save(update_fields=["grace_period_ends_at", "last_dunning_attempt_at"])
            if was_restricted:
                self._trigger_resync_after_recovery(subscription)
        return subscription

    def _trigger_resync_after_recovery(self, subscription: Subscription) -> None:
        """Queue a resync of every calendar this billing root's pooled subtree
        owns, once ``subscription`` has just left ``RESTRICTED`` for a live state.

        Callers pass this **only** when the *prior* state was ``RESTRICTED`` --
        sync was never paused for a ``GRACE`` organization (only ``RESTRICTED``
        write-blocks and sync-pauses; see
        ``EntitlementService.is_billing_root_restricted``), so leaving ``GRACE``
        has nothing to reconcile.

        Fanned out per pooled organization (``EntitlementService
        .get_pooled_organization_ids``) -- the exact set every usage counter and
        the sync-pause guard itself resolve against -- not per calendar directly;
        each fanned-out task (``resync_organization_calendars_task``) resolves
        its own organization's calendars. Deferred import: ``calendar_integration
        .tasks`` is not imported at module level, mirroring the same
        avoid-a-module-level-cross-app-import convention
        ``CalendarSyncService.request_calendar_sync`` already uses for its own
        Celery task import.

        Called from inside the caller's ``transaction.atomic()`` block, wrapped
        in ``transaction.on_commit`` here so the fan-out cannot race the
        transaction that actually moved ``billing_state`` off RESTRICTED --
        queuing before commit would let a worker pick up the resync and find the
        subscription still (as far as an uncommitted read is concerned)
        RESTRICTED.
        """
        from calendar_integration.tasks.calendar_sync_tasks import (
            resync_organization_calendars_task,
        )

        organization_ids = self.entitlement_service.get_pooled_organization_ids(
            subscription.organization
        )
        transaction.on_commit(
            lambda: [
                resync_organization_calendars_task.delay(organization_id=organization_id)
                for organization_id in organization_ids
            ]
        )

    # ------------------------------------------------------------------
    # process_dunning-driven transitions
    # ------------------------------------------------------------------

    def process_subscription(self, subscription: Subscription) -> None:
        """One ``process_dunning`` beat tick for one subscription.

        The single dispatch point the beat task calls through -- see module
        docstring. Re-reads nothing: the caller (``payments.tasks.
        process_dunning_for_subscription``) already fetched ``subscription``
        fresh from the fan-out.
        """
        if subscription.billing_state == BillingState.GRACE:
            self._process_grace(subscription)
        elif subscription.billing_state == BillingState.RESTRICTED:
            # No charge to retry -- RESTRICTED is write-blocked, so there is
            # nothing left to do here except notice a manual fix (e.g. deleting
            # resources) already brought usage back under the free plan.
            self.check_free_fallback(subscription)

    def _process_grace(self, subscription: Subscription) -> None:
        """One tick's worth of GRACE handling, in priority order.

        The expiry check runs on **every** tick, regardless of
        ``MIN_DUNNING_RETRY_INTERVAL`` -- only the charge-retry-and-notify step
        is throttled to roughly once a day. Getting this ordering backwards
        (throttling the *whole* method, expiry check included) is exactly the
        defect the beat schedule's own comment (``celerybeat_schedule.py``)
        warns against: an hourly beat exists specifically so a subscription
        whose ``grace_period_ends_at`` elapses moves out of GRACE within the
        hour, not up to ``MIN_DUNNING_RETRY_INTERVAL`` late because the most
        recent retry happened to land close to the deadline.

        Free-fallback is deliberately **not** checked here: an org that entered
        GRACE from a *payment failure* but happens to already sit under the
        free-plan ceilings still owes money on a card that is on file, so it
        gets the full retry ladder across the grace window; ``expire_grace``
        decides FREE vs. RESTRICTED only once the window is unresolved (the
        diagram's ``Grace -> Free`` edge is the downgrade-under-limit path, not
        the payment-failure path -- a distinction a GRACE subscription carries
        no reason field to make mid-window, so the only safe place to collapse
        it is at expiry).

        A **downgrade-originated** grace episode (``_is_downgrade_grace``)
        skips the charge-retry-and-notify step entirely, every tick,
        not only past expiry: unlike a payment-failure grace there is no failed
        charge to retry -- ``SubscriptionService._schedule_downgrade`` already
        applied the lower ceiling immediately, and this window exists solely to
        give the organization time to reduce usage (or upgrade back) before
        RESTRICTED. It is still evaluated for expiry on every tick, identically
        to a payment-failure grace.

        The retry throttle and the charge's idempotency key are two views of
        the same ``_retry_attempt_ordinal`` -- the tick fires a real charge only
        when ``now`` falls in a *later* retry bucket than the last attempt did,
        and the charge carries that bucket's number as its key -- so the gate
        and the key can never disagree about whether this is a new attempt.
        """
        now = timezone.now()
        if (
            subscription.grace_period_ends_at is not None
            and subscription.grace_period_ends_at <= now
        ):
            self.expire_grace(subscription)
            return
        if self._is_downgrade_grace(subscription):
            return
        current_ordinal = self._retry_attempt_ordinal(subscription, now)
        last_attempt = subscription.last_dunning_attempt_at
        if (
            last_attempt is not None
            and self._retry_attempt_ordinal(subscription, last_attempt) >= current_ordinal
        ):
            return
        self._retry_charge_and_notify(subscription, now, current_ordinal)

    def _retry_attempt_ordinal(self, subscription: Subscription, at: datetime.datetime) -> int:
        """Which retry bucket ``at`` falls in, counting from the start of this
        grace episode in ``MIN_DUNNING_RETRY_INTERVAL`` steps.

        The episode's anchor is ``grace_period_ends_at`` (stamped once when
        grace begins, cleared only when the subscription leaves GRACE, so it is
        fixed for the whole episode) minus the plan's grace window -- i.e. the
        moment grace began. Bucket ``floor((at - grace_start) / interval)`` is
        the single shared quantity the throttle gate and the idempotency key
        both read: two attempts in different buckets are two distinct charges
        with two distinct keys; a redelivery of the same attempt lands in the
        same bucket and reuses its key.
        """
        grace_ends_at = subscription.grace_period_ends_at
        if grace_ends_at is None:
            return 0
        grace_start = grace_ends_at - datetime.timedelta(days=self._grace_period_days(subscription))
        return (at - grace_start) // MIN_DUNNING_RETRY_INTERVAL

    def _grace_period_days(self, subscription: Subscription) -> int:
        grace_days = subscription.plan.grace_period_days
        if grace_days is None:
            grace_days = settings.BILLING_DEFAULT_GRACE_PERIOD_DAYS
        return grace_days

    def _retry_charge_and_notify(
        self, subscription: Subscription, now: datetime.datetime, attempt_ordinal: int
    ) -> None:
        """Retry the failed charge and send that rung of the ladder's email.

        ``idempotency_key`` is derived from ``(subscription, attempt_ordinal)``
        -- the retry bucket ``now`` falls in (``_retry_attempt_ordinal``). It is
        stable across a ``CELERY_TASK_ACKS_LATE`` redelivery of the same logical
        attempt (a redelivery lands in the same bucket, so the provider itself
        refuses a second charge for it, through the provider's own idempotency
        key) and distinct from the previous and next bucket's attempt, so a
        genuinely new retry is never mistaken for a redelivery of the last one.
        """
        idempotency_key = f"dunning-retry-{subscription.pk}-{attempt_ordinal}"
        urgency = self._ladder_urgency(subscription, now)
        with transaction.atomic():
            subscription.last_dunning_attempt_at = now
            subscription.save(update_fields=["last_dunning_attempt_at"])
            self.subscription_service.retry_failed_charge(subscription, idempotency_key)
            transaction.on_commit(lambda: self._notify_reminder(subscription, urgency))

    def _ladder_urgency(self, subscription: Subscription, now: datetime.datetime) -> str:
        ends_at = subscription.grace_period_ends_at
        if ends_at is not None and ends_at - now <= FINAL_WARNING_WINDOW:
            return "final_warning"
        return "reminder"

    def expire_grace(self, subscription: Subscription) -> Subscription:
        """Resolve a grace window that elapsed unpaid: GRACE -> FREE if current
        usage now fits under the free plan's ceilings, otherwise GRACE ->
        RESTRICTED.

        Free-fallback is evaluated **here, at expiry**, not on every GRACE tick:
        letting it run mid-window would abandon collecting from a payment-failure
        org the instant its usage happened to fit under free limits, skipping the
        entire retry ladder on a customer with a card on file who owes money. By
        the time the window has elapsed unresolved, the ladder has run its full
        course, so collapsing to the cheaper of the two terminal states is the
        right call.

        A **downgrade-originated** grace episode (``_is_downgrade_grace``)
        resolves differently: ``_expire_downgrade_grace`` checks against the
        limits ``SubscriptionService._schedule_downgrade`` already applied (the
        pending, lower plan's), not the catalog ``free`` plan -- the org may have
        downgraded to any paid tier, not necessarily ``free`` -- and restores
        ACTIVE rather than FREE when resolved, since the org remains a paying
        subscriber of its still-active (pre-boundary) plan.

        Idempotent: a no-op for any state other than GRACE.
        """
        if subscription.billing_state != BillingState.GRACE:
            return subscription

        if self._is_downgrade_grace(subscription):
            return self._expire_downgrade_grace(subscription)

        if self.check_free_fallback(subscription):
            return subscription

        with transaction.atomic():
            transition_billing_state(subscription, BillingState.RESTRICTED)
            transaction.on_commit(lambda: self._notify_restricted(subscription))
        return subscription

    def _expire_downgrade_grace(self, subscription: Subscription) -> Subscription:
        """Resolve a downgrade-originated grace window that elapsed
        with the organization still over its new, lower limits: GRACE -> ACTIVE
        if usage now fits, otherwise GRACE -> RESTRICTED.

        Checked against ``_fits_under_current_limits`` -- the limits
        ``_schedule_downgrade`` already synced onto ``subscription`` immediately
        when the downgrade was requested -- rather than
        ``check_free_fallback``'s catalog ``free`` plan: an organization
        downgrading from, say, ``pro`` to a mid-tier paid plan is never going to
        "fit under free" as its resolution condition, and checking the wrong
        plan would restrict organizations that already did exactly what the
        downgrade asked of them.

        ACTIVE, not FREE, on the resolved branch: unlike a payment-failure grace
        (which only ever reaches FREE by fitting under the *catalog's* free
        ceilings), an organization here remains a paying subscriber of its
        still-active, pre-boundary ``subscription.plan`` -- the downgrade itself
        has not taken effect yet (that is the cycle-close sweep). Both
        ``(GRACE, ACTIVE)`` and ``(GRACE, RESTRICTED)`` are already legal edges
        on the diagram; no new edge is needed for this branch.
        """
        if self._fits_under_current_limits(subscription):
            with transaction.atomic():
                transition_billing_state(subscription, BillingState.ACTIVE)
                subscription.grace_period_ends_at = None
                subscription.last_dunning_attempt_at = None
                subscription.save(update_fields=["grace_period_ends_at", "last_dunning_attempt_at"])
            return subscription

        with transaction.atomic():
            transition_billing_state(subscription, BillingState.RESTRICTED)
            transaction.on_commit(lambda: self._notify_restricted(subscription))
        return subscription

    @staticmethod
    def _is_downgrade_grace(subscription: Subscription) -> bool:
        """True when this GRACE episode originated from a scheduled downgrade
        (``SubscriptionService._schedule_downgrade``) rather than a failed
        recurring charge (``enter_grace``).

        Inferred from ``pending_plan_id`` being set -- only ``_schedule_downgrade``
        stamps it; ``enter_grace`` never touches it. It is cleared once a later
        upgrade supersedes the scheduled downgrade (``_initiate_upgrade`` clears
        ``pending_plan``) or, once cycle close ships, once the downgrade is
        applied at the boundary.

        **Known limitation**, accepted as out of scope here: an
        organization with a downgrade already scheduled whose *currently active*
        (still higher, pre-boundary) plan then also fails a renewal charge reads
        as a downgrade-grace here too, and the genuinely failed charge does not
        get retried. Disambiguating the two reasons unambiguously would need a
        dedicated reason field on ``Subscription`` -- a larger schema change than
        the dead-edge gap this method exists to close warrants. The compound case
        is rare (a renewal charge landing inside a downgrade's typically
        much-shorter grace window) and, either way, the organization still lands
        on a state the sweep inspects and can expire -- it no longer sits
        forever on an unswept row, which is the gap this method closes.
        """
        return subscription.pending_plan_id is not None

    def _fits_under_current_limits(self, subscription: Subscription) -> bool:
        """Does current usage already fit under ``subscription``'s **currently
        synced** ``SubscriptionPlanLimit`` ceilings (including active add-ons)?

        Unlike ``_fits_under_plan`` (checked against a specific catalog
        ``BillingPlan`` -- the ``free`` tier, for a payment-failure grace's
        fallback), this reads back the *effective* limit
        (``EntitlementService.get_effective_limit``, which folds in add-ons) for
        whatever resource keys ``subscription.limits`` carries right now. For a
        downgrade-originated grace episode those rows are exactly the *lower*
        (pending) plan's, already synced by ``_schedule_downgrade`` the moment
        the downgrade was requested -- so "fits" here means the overage that
        triggered this grace episode has actually been resolved (by deleting
        resources, buying an add-on, etc.), without assuming the organization
        downgraded to the catalog's ``free`` tier specifically, which it may not
        have.
        """
        organization = subscription.organization
        for resource_key in subscription.limits.values_list("resource_key", flat=True):
            effective_limit = self.entitlement_service.get_effective_limit(
                organization, resource_key
            )
            if effective_limit.limit_value is None:
                continue
            usage = self.entitlement_service.get_current_usage(organization, resource_key)
            if usage > effective_limit.limit_value:
                return False
        return True

    def check_free_fallback(self, subscription: Subscription) -> bool:
        """GRACE|RESTRICTED -> FREE once current usage fits under the catalog's
        ``free`` plan's ceilings on every ``LimitedResource``.

        Returns ``True`` when the fallback happened. Deliberately leaves
        ``Subscription.plan`` untouched -- this is a gate on ``billing_state``
        only, the same way ``SubscriptionService._schedule_downgrade`` already
        lets ``billing_state`` and ``plan`` disagree for the length of a
        downgrade's grace window (see ``pending_plan``). Whether the org's
        nominal ``plan``/billing should also snap to free is a product decision
        left open here.

        Resolved by ``slug`` (``FREE_PLAN_SLUG``), **not**
        ``is_default_for_new_organizations`` -- that flag currently marks the
        rollout's ``unlimited`` kill-switch plan, whose every
        ``PlanLimit.limit_value`` is ``NULL``. Every ceiling check below skips a
        ``NULL`` limit (it means "no ceiling"), so resolving "the free plan" via
        the default-for-new-organizations flag would make *every* usage trivially
        "fit" and short-circuit the entire dunning ladder on its first tick, for
        the whole length of the rollout. The catalog's actual ``free`` tier (real,
        finite limits) is what this check means by "free limits".
        """
        if subscription.billing_state not in (BillingState.GRACE, BillingState.RESTRICTED):
            return False

        free_plan = self._free_plan()
        if free_plan is None or not self._fits_under_plan(subscription, free_plan):
            return False

        was_restricted = subscription.billing_state == BillingState.RESTRICTED
        with transaction.atomic():
            transition_billing_state(subscription, BillingState.FREE)
            subscription.grace_period_ends_at = None
            subscription.last_dunning_attempt_at = None
            subscription.save(update_fields=["grace_period_ends_at", "last_dunning_attempt_at"])
            if was_restricted:
                self._trigger_resync_after_recovery(subscription)
        return True

    def _free_plan(self) -> BillingPlan | None:
        return BillingPlan.objects.filter(slug=FREE_PLAN_SLUG, is_active=True).first()

    def _fits_under_plan(self, subscription: Subscription, plan: BillingPlan) -> bool:
        organization = subscription.organization
        for plan_limit in plan.limits.all():
            if plan_limit.limit_value is None:
                continue
            usage = self.entitlement_service.get_current_usage(
                organization, plan_limit.resource_key
            )
            if usage > plan_limit.limit_value:
                return False
        return True

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self, subscription: Subscription) -> Subscription:
        """-> CANCELLED, validated against the diagram's cancellation edges.

        A thin validated transition kept as an alternative entry point. The live
        cancel action (``SubscriptionService.cancel_subscription``) routes its
        own ``billing_state`` write through ``transition_billing_state`` too and
        additionally clears the dunning bookkeeping, so both paths agree on which
        source states may cancel (``ACTIVE``/``FREE``/``GRACE``/``RESTRICTED`` --
        see ``LEGAL_BILLING_STATE_TRANSITIONS``).
        """
        transition_billing_state(subscription, BillingState.CANCELLED)
        return subscription

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _recipient_user_ids(self, subscription: Subscription) -> list[int]:
        """Admins and billing owners of ``subscription.organization`` -- see
        ``OrganizationMembershipQuerySet.billing_recipients``. Resolved on the
        subscription's own organization (the billing root), never the pooled
        subtree -- one commercial relationship per reseller tree."""
        return list(
            OrganizationMembership.objects.billing_recipients(subscription.organization_id)
            .values_list("user_id", flat=True)
            .distinct()
        )

    def _notify_entered_grace(self, subscription: Subscription) -> None:
        if self.notification_service is None:
            return
        organization = subscription.organization
        context_kwargs = NotificationContextDict(
            {
                "organization_name": organization.name,
                "grace_period_ends_at": self._format_dt(subscription.grace_period_ends_at),
            }
        )
        for user_id in self._recipient_user_ids(subscription):
            self.notification_service.create_notification(
                user_id=user_id,
                notification_type=NotificationTypes.IN_APP.value,
                title="Your payment could not be processed",
                body_template="payments/in_app/entered_grace.body.txt",
                context_name="dunning_entered_grace_context",
                context_kwargs=context_kwargs,
            )
            self.notification_service.create_notification(
                user_id=user_id,
                notification_type=NotificationTypes.EMAIL.value,
                title="Your payment could not be processed",
                body_template="payments/emails/dunning_payment_failed.body.html",
                context_name="dunning_entered_grace_context",
                context_kwargs=context_kwargs,
                subject_template="payments/emails/dunning_payment_failed.subject.txt",
                preheader_template="payments/emails/dunning_payment_failed.pre_header.txt",
            )

    def _notify_reminder(self, subscription: Subscription, urgency: str) -> None:
        if self.notification_service is None:
            return
        organization = subscription.organization
        context_kwargs = NotificationContextDict(
            {
                "organization_name": organization.name,
                "grace_period_ends_at": self._format_dt(subscription.grace_period_ends_at),
                "urgency": urgency,
            }
        )
        for user_id in self._recipient_user_ids(subscription):
            self.notification_service.create_notification(
                user_id=user_id,
                notification_type=NotificationTypes.EMAIL.value,
                title="We're still unable to process your payment",
                body_template="payments/emails/dunning_reminder.body.html",
                context_name="dunning_reminder_context",
                context_kwargs=context_kwargs,
                subject_template="payments/emails/dunning_reminder.subject.txt",
                preheader_template="payments/emails/dunning_reminder.pre_header.txt",
            )

    def _notify_restricted(self, subscription: Subscription) -> None:
        if self.notification_service is None:
            return
        organization = subscription.organization
        context_kwargs = NotificationContextDict({"organization_name": organization.name})
        for user_id in self._recipient_user_ids(subscription):
            self.notification_service.create_notification(
                user_id=user_id,
                notification_type=NotificationTypes.EMAIL.value,
                title="Your account has been restricted",
                body_template="payments/emails/dunning_restricted.body.html",
                context_name="dunning_restricted_context",
                context_kwargs=context_kwargs,
                subject_template="payments/emails/dunning_restricted.subject.txt",
                preheader_template="payments/emails/dunning_restricted.pre_header.txt",
            )

    @staticmethod
    def _format_dt(value: datetime.datetime | None) -> str:
        return value.isoformat() if value is not None else ""
