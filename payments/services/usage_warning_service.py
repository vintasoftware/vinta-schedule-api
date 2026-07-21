"""Proactive approaching-limit / limit-reached warnings (Phase 12).

``GET /billing/usage/`` (``payments/billing_views.py``) is the *pull* side of
"an organization can see where it stands"; this is the *push* side -- "it is
warned before it is blocked rather than by being blocked" (spec Use-case 8).

Both sides -- and the enforcement guards themselves
(``EntitlementService.check_limit`` / ``check_postpaid_allowance``) -- read
the *same* ceiling: ``EntitlementService.get_effective_limit`` /
``get_current_usage``. There is deliberately no second "how close is this
organization to its limit" computation in this module; ``_ratio`` below is
the *only* place "approaching" is defined, so an org can never be told it has
headroom a guard then denies, or vice versa.
"""

import datetime
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import transaction

from vintasend.constants import NotificationTypes
from vintasend.services.notification_service import NotificationContextDict

from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingState, LimitedResource, LimitWarningLevel
from payments.models import LimitWarningNotification, Subscription
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import current_billing_period_start


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationService


logger = logging.getLogger(__name__)

#: Usage/limit ratio at which an organization is warned it is *approaching* its
#: effective limit, before it is ever blocked -- the plan's stated default
#: ("crosses a threshold (default 80%)"). The *only* definition of
#: "approaching" in this codebase: ``UsageWarningService`` is the sole reader,
#: and it resolves the ceiling from ``EntitlementService.get_effective_limit``
#: -- the identical method ``check_limit``/``check_postpaid_allowance`` resolve
#: their own ceiling from -- so "approaching 80% of X" and "blocked at X" can
#: never disagree about what X is.
APPROACHING_LIMIT_THRESHOLD = Decimal("0.8")


class UsageWarningService:
    """Stateless; injected via ``di_core.containers``.

    ``check_subscription`` is the beat task's
    (``payments.tasks.check_approaching_limits_for_subscription``) single entry
    point -- mirrors ``DunningService.process_subscription``'s shape of "one
    beat tick's worth of work for one subscription".
    """

    def __init__(
        self,
        entitlement_service: EntitlementService,
        notification_service: "NotificationService | None" = None,
    ) -> None:
        self.entitlement_service = entitlement_service
        self.notification_service = notification_service

    def check_subscription(self, subscription: Subscription) -> None:
        """Check every ``LimitedResource`` on ``subscription``'s organization
        against its effective limit and send an approaching-limit or
        limit-reached in-app notification -- at most once per resource per
        level per billing cycle (see ``LimitWarningNotification``).

        A no-op for a subscription whose billing root is already ``RESTRICTED``
        or ``CANCELLED``: a ``RESTRICTED`` organization already knows it is
        blocked (Phase 11's own notification), and warning it that it is
        "approaching" a limit it is already over adds nothing; ``CANCELLED``
        is running out the clock to ``FREE``, not accruing toward anything
        that will block it. ``subscription.billing_state`` is read directly
        here rather than through ``EntitlementService.is_billing_root_restricted``
        -- a ``Subscription`` only ever exists on its own billing root (see
        ``resolve_billing_root``/``is_billing_root``), so the two reads are
        the identical fact; this mirrors the same deliberate inline copy
        ``check_limit`` documents on its own ``RESTRICTED`` short-circuit.

        Best-effort per resource: a failure checking or notifying about one
        resource is logged and does not stop the remaining resources on this
        subscription from being checked. Because the beat entry point fans out
        one Celery task per subscription
        (``payments.tasks.check_approaching_limits``), a failure here already
        cannot affect any *other* subscription's sweep.
        """
        if subscription.billing_state in (BillingState.RESTRICTED, BillingState.CANCELLED):
            return

        organization = subscription.organization
        billing_period_start = current_billing_period_start(subscription)
        for resource_key in LimitedResource.values:
            try:
                self._check_resource(subscription, organization, resource_key, billing_period_start)
            except Exception:
                logger.exception(
                    "Approaching-limit check failed for subscription %s, resource %s; "
                    "continuing with the remaining resources.",
                    subscription.pk,
                    resource_key,
                )

    def _check_resource(
        self,
        subscription: Subscription,
        organization: Organization,
        resource_key: str,
        billing_period_start: datetime.datetime,
    ) -> None:
        effective_limit = self.entitlement_service.get_effective_limit(organization, resource_key)
        if effective_limit.is_unlimited:
            return  # No ceiling -- nothing to approach. limit_value is None here.

        # Narrowed by the `is_unlimited` return above: `limit_value` is not None
        # here. `or 0` is a type-narrowing idiom, not a semantic fallback --
        # mirrors `EntitlementService.check_limit`'s identical `ceiling =
        # effective_limit.limit_value or 0`.
        limit_value = effective_limit.limit_value or 0
        current_usage = self.entitlement_service.get_current_usage(organization, resource_key)
        level = self._level_for(current_usage, limit_value)
        if level is None:
            return

        # Claim the marker and send inside the same transaction: if `_notify`
        # raises, the rollback un-creates the row, so a transient send
        # failure is retried on the next beat tick within the same cycle
        # rather than being permanently debounced by a marker for a
        # notification that never actually went out. The claim still closes
        # out the same-warning race between two concurrent beat ticks the
        # unique constraint always has: a concurrent `mark_if_new` blocks on
        # the pending row until this transaction commits (success) or rolls
        # back (failure, in which case the other tick's claim then goes
        # through and it -- not this one -- sends).
        with transaction.atomic():
            is_new = LimitWarningNotification.objects.mark_if_new(
                subscription_id=subscription.pk,
                resource_key=resource_key,
                billing_period_start=billing_period_start,
                level=level,
            )
            if not is_new:
                return
            self._notify(subscription, resource_key, level, current_usage, limit_value)

    @staticmethod
    def _level_for(current_usage: int, limit_value: int) -> str | None:
        """The single definition of "approaching" vs. "reached" -- see the
        module docstring for why there is only ever one."""
        ratio = UsageWarningService._ratio(current_usage, limit_value)
        if ratio >= 1:
            return LimitWarningLevel.REACHED
        if ratio >= APPROACHING_LIMIT_THRESHOLD:
            return LimitWarningLevel.APPROACHING
        return None

    @staticmethod
    def _ratio(current_usage: int, limit_value: int) -> Decimal:
        """``current_usage / limit_value``, handling the one degenerate case a
        ``limit_value=0`` ("not included", per ``BillingPlan.clean``) forces:
        dividing by zero. Any usage at all against a zero ceiling already *is*
        the ceiling (ratio 1, i.e. ``REACHED``); zero usage against a zero
        ceiling has nothing to warn about (ratio 0) -- a resource an
        organization has never touched should never generate a notification.
        """
        if limit_value <= 0:
            return Decimal(1) if current_usage > 0 else Decimal(0)
        return Decimal(current_usage) / Decimal(limit_value)

    def _notify(
        self,
        subscription: Subscription,
        resource_key: str,
        level: str,
        current_usage: int,
        limit_value: int,
    ) -> None:
        if self.notification_service is None:
            return
        organization = subscription.organization
        context_kwargs = NotificationContextDict(
            {
                "organization_name": organization.name,
                "resource_key": resource_key,
                "current_usage": current_usage,
                "limit_value": limit_value,
            }
        )
        if level == LimitWarningLevel.APPROACHING:
            title = "You're approaching a plan limit"
            body_template = "payments/in_app/approaching_limit.body.txt"
            context_name = "approaching_limit_context"
        else:
            title = "You've reached a plan limit"
            body_template = "payments/in_app/limit_reached.body.txt"
            context_name = "limit_reached_context"

        for user_id in self._recipient_user_ids(subscription):
            self.notification_service.create_notification(
                user_id=user_id,
                notification_type=NotificationTypes.IN_APP.value,
                title=title,
                body_template=body_template,
                context_name=context_name,
                context_kwargs=context_kwargs,
            )

    def _recipient_user_ids(self, subscription: Subscription) -> list[int]:
        """Admins and billing owners of ``subscription.organization`` --
        mirrors ``DunningService._recipient_user_ids``. Resolved on the
        subscription's own organization (the billing root), never the pooled
        subtree -- one commercial relationship per reseller tree."""
        return list(
            OrganizationMembership.objects.billing_recipients(subscription.organization_id)
            .values_list("user_id", flat=True)
            .distinct()
        )
