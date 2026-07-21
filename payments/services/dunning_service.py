"""Grace, dunning, and the restricted transition (Phase 10).

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

Two things load-bearing enough to repeat here (see their docstrings for the
full reasoning):

- **Never touches ``PaymentMethod``.** A failed charge says nothing about
  whether the card is still attached -- ``EntitlementService.has_payment_method``
  must keep reading ``True`` for a GRACE organization with a card on file, so it
  keeps accruing postpaid usage; the dunning ladder, not the postpaid guard, is
  what escalates it.
- **Clears ``plan_change_pending_confirmation``** whenever it moves a
  subscription into GRACE, so a first-upgrade whose initial charge fails does
  not leave the organization stuck unable to request a different plan (Phase 9
  set the flag; a failed charge never reaches the APPROVED webhook branch that
  would otherwise clear it).
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

#: A dunning retry younger than this is considered "already handled today" --
#: ``process_dunning``'s per-subscription idempotency gate (see
#: ``Subscription.last_dunning_attempt_at``). A little under 24h rather than a
#: strict calendar-day comparison, so this needs no timezone-of-day reasoning to
#: be correct: whatever wall-clock time the beat schedule fires at, back-to-back
#: runs of the *same* day never re-fire, and a run the next day always does.
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
        clears ``plan_change_pending_confirmation`` (Constraint 2 -- see module
        docstring) in the same transition, so a failed first-upgrade charge does
        not leave the organization stuck.

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
            grace_days = subscription.plan.grace_period_days
            if grace_days is None:
                grace_days = settings.BILLING_DEFAULT_GRACE_PERIOD_DAYS
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

        with transaction.atomic():
            transition_billing_state(subscription, BillingState.ACTIVE)
            subscription.grace_period_ends_at = None
            subscription.last_dunning_attempt_at = None
            subscription.save(update_fields=["grace_period_ends_at", "last_dunning_attempt_at"])
        return subscription

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
            # No charge to retry -- Phase 11 write-blocks RESTRICTED, so there is
            # nothing left to do here except notice a manual fix (e.g. deleting
            # resources) already brought usage back under the free plan.
            self.check_free_fallback(subscription)

    def _process_grace(self, subscription: Subscription) -> None:
        """One tick's worth of GRACE handling, in priority order.

        The free-fallback and expiry checks run on **every** tick, regardless
        of ``MIN_DUNNING_RETRY_INTERVAL`` -- only the charge-retry-and-notify
        step is throttled to roughly once a day. Getting this ordering backwards
        (throttling the *whole* method, expiry check included) is exactly the
        defect the beat schedule's own comment (``celerybeat_schedule.py``)
        warns against: an hourly beat exists specifically so a subscription
        whose ``grace_period_ends_at`` elapses moves to RESTRICTED within the
        hour, not up to ``MIN_DUNNING_RETRY_INTERVAL`` late because the most
        recent retry happened to land close to the deadline.
        """
        now = timezone.now()
        if self.check_free_fallback(subscription):
            return
        if (
            subscription.grace_period_ends_at is not None
            and subscription.grace_period_ends_at <= now
        ):
            self.expire_grace(subscription)
            return
        last_attempt = subscription.last_dunning_attempt_at
        if last_attempt is not None and (now - last_attempt) < MIN_DUNNING_RETRY_INTERVAL:
            return
        self._retry_charge_and_notify(subscription, now)

    def _retry_charge_and_notify(self, subscription: Subscription, now: datetime.datetime) -> None:
        """Retry the failed charge and send that rung of the ladder's email.

        ``idempotency_key`` is derived from ``(subscription, calendar date)`` --
        stable across a ``CELERY_TASK_ACKS_LATE`` redelivery of the same logical
        attempt (so the provider itself refuses a second charge for it, per
        Phase 9's provider-idempotency plumbing), but distinct from the previous
        and next day's attempt.
        """
        idempotency_key = f"dunning-retry-{subscription.pk}-{now:%Y-%m-%d}"
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
        """GRACE -> RESTRICTED once the grace window has elapsed unresolved.

        Idempotent: a no-op for any state other than GRACE.
        """
        if subscription.billing_state != BillingState.GRACE:
            return subscription

        with transaction.atomic():
            transition_billing_state(subscription, BillingState.RESTRICTED)
            transaction.on_commit(lambda: self._notify_restricted(subscription))
        return subscription

    def check_free_fallback(self, subscription: Subscription) -> bool:
        """GRACE|RESTRICTED -> FREE once current usage fits under the catalog's
        ``free`` plan's ceilings on every ``LimitedResource``.

        Returns ``True`` when the fallback happened. Deliberately leaves
        ``Subscription.plan`` untouched -- this is a gate on ``billing_state``
        only, the same way ``SubscriptionService._schedule_downgrade`` already
        lets ``billing_state`` and ``plan`` disagree for the length of a
        downgrade's grace window (see ``pending_plan``). Whether the org's
        nominal ``plan``/billing should also snap to free is a product decision
        this phase does not make -- see the phase report's open questions.

        Resolved by ``slug`` (``FREE_PLAN_SLUG``), **not**
        ``is_default_for_new_organizations`` -- that flag currently marks the
        rollout's ``unlimited`` kill-switch plan (Phase 3), whose every
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

        with transaction.atomic():
            transition_billing_state(subscription, BillingState.FREE)
            subscription.grace_period_ends_at = None
            subscription.last_dunning_attempt_at = None
            subscription.save(update_fields=["grace_period_ends_at", "last_dunning_attempt_at"])
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
        """ACTIVE -> CANCELLED -- the diagram's only cancellation edge, validated.

        Not currently wired to ``SubscriptionService.cancel_subscription``
        (Phase 9's existing cancellation action, which predates this validator
        and -- by design, see that method's docstring -- allows cancelling from
        any ``billing_state``, including ``FREE``). Kept here, tested against
        the diagram, as the correct entry point for a future caller; rewiring
        the live endpoint is a product decision about what "cancel" should mean
        for an organization with nothing paid to cancel, which this phase does
        not make. See the phase report's open questions.
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
