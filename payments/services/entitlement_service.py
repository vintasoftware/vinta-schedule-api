"""Effective limits, pooled usage counting, and entitlement lookups.

This is the engine every enforcement phase calls. Three rules are load-bearing
and easy to break by accident:

1. **NULL is unlimited, never zero.** A ``SubscriptionPlanLimit.limit_value`` of
   ``None`` means no ceiling. So does the *absence* of a row for a resource. Both
   fail open — a missing seed row must never lock an organization out of
   something it could do yesterday.
2. **Usage pools at the billing root.** A reseller child holds no
   ``Subscription``; its usage counts against its root's ceiling together with
   every other organization in the subtree. The subtree stops at any nested
   billing root, which pays for its own subtree (see
   ``payments.services.subscription_service.is_billing_root`` — the single
   definition of that predicate, deliberately not restated here).
3. **Counting and checking must be inseparable under concurrency.**
   ``check_limit(..., lock=True)`` takes ``SELECT ... FOR UPDATE`` on the *root*
   ``Subscription`` row before counting, so two racing creates for the last unit
   of capacity serialize on one row and exactly one sees room.
"""

import logging
from collections.abc import Callable, Sequence

from django.db.models import Sum
from django.utils import timezone

from calendar_integration.constants import CalendarType, CalendarVisibility
from calendar_integration.models import AvailableTime, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from payments.billing_constants import (
    BillingState,
    LimitedResource,
    LimitKind,
    LimitRemedy,
)
from payments.models import Subscription
from payments.services.billing_dataclasses import EffectiveLimit, LimitCheckResult
from payments.services.subscription_service import is_billing_root, resolve_billing_root
from public_api.models import SystemUser
from webhooks.models import WebhookConfiguration


logger = logging.getLogger(__name__)


UsageCounter = Callable[[Sequence[int], Subscription | None], int]


def _count_organization_members(organization_ids: Sequence[int], _: Subscription | None) -> int:
    """Seats in use: active memberships plus still-open invitations.

    Pending invitations count toward the ceiling deliberately — without that, an
    organization could hold unlimited outstanding invitations and blow past its
    seat limit the moment they are accepted. Expired and already-accepted
    invitations do not count: an expired one can never become a seat, and an
    accepted one is already counted as its membership.
    """
    now = timezone.now()
    members = OrganizationMembership.objects.filter(
        organization_id__in=organization_ids, is_active=True
    ).count()
    pending_invitations = OrganizationInvitation.objects.filter(
        organization_id__in=organization_ids,
        accepted_at__isnull=True,
        expires_at__gt=now,
    ).count()
    return members + pending_invitations


def _count_resource_calendars(organization_ids: Sequence[int], _: Subscription | None) -> int:
    """Resource/room calendars, excluding soft-deleted ones.

    ``CalendarVisibility.INACTIVE`` is the soft-delete state (``DELETE
    /calendars/{id}/`` sets it rather than removing the row), so counting it would
    make deleting a room fail to free capacity.
    """
    return (
        Calendar.objects.filter(
            organization_id__in=organization_ids,
            calendar_type=CalendarType.RESOURCE,
        )
        .exclude(visibility=CalendarVisibility.INACTIVE)
        .count()
    )


def _count_bundle_calendars(organization_ids: Sequence[int], _: Subscription | None) -> int:
    """Bundle calendars, excluding soft-deleted ones (see ``_count_resource_calendars``)."""
    return (
        Calendar.objects.filter(
            organization_id__in=organization_ids,
            calendar_type=CalendarType.BUNDLE,
        )
        .exclude(visibility=CalendarVisibility.INACTIVE)
        .count()
    )


def _count_calendar_groups(organization_ids: Sequence[int], _: Subscription | None) -> int:
    return CalendarGroup.objects.filter(organization_id__in=organization_ids).count()


def _count_availability_windows(organization_ids: Sequence[int], _: Subscription | None) -> int:
    return AvailableTime.objects.filter(organization_id__in=organization_ids).count()


def _count_webhook_subscriptions(organization_ids: Sequence[int], _: Subscription | None) -> int:
    """Webhook configurations, excluding soft-deleted ones (``deleted_at`` set)."""
    return WebhookConfiguration.objects.filter(
        organization_id__in=organization_ids, deleted_at__isnull=True
    ).count()


def _count_public_api_system_users(organization_ids: Sequence[int], _: Subscription | None) -> int:
    """Active, non-soft-deleted public-API system users."""
    return SystemUser.objects.filter(
        organization_id__in=organization_ids, is_active=True, deleted_at__isnull=True
    ).count()


def _count_event_occurrences(
    organization_ids: Sequence[int], subscription: Subscription | None
) -> int:
    """Metered event occurrences in the subscription's current billing period.

    Always ``0`` for now: occurrences are computed rather than stored, and the
    ``MeteredOccurrence`` table that records billed ones is introduced by the
    metering phase. Returning ``0`` (rather than omitting the counter) keeps
    ``USAGE_COUNTERS`` total over ``LimitedResource`` — ``get_current_usage`` is
    then a lookup that cannot ``KeyError`` on a resource somebody forgot to
    register, and ``test_every_limited_resource_has_a_counter`` fails loudly the
    day a new member is added without one.

    ``event_occurrences`` is post-paid, so a zero here under-reports rather than
    blocking anyone in the meantime.
    """
    del organization_ids, subscription
    return 0


USAGE_COUNTERS: dict[str, UsageCounter] = {
    LimitedResource.ORGANIZATION_MEMBERS: _count_organization_members,
    LimitedResource.RESOURCE_CALENDARS: _count_resource_calendars,
    LimitedResource.CALENDAR_GROUPS: _count_calendar_groups,
    LimitedResource.BUNDLE_CALENDARS: _count_bundle_calendars,
    LimitedResource.AVAILABILITY_WINDOWS: _count_availability_windows,
    LimitedResource.WEBHOOK_SUBSCRIPTIONS: _count_webhook_subscriptions,
    LimitedResource.PUBLIC_API_SYSTEM_USERS: _count_public_api_system_users,
    LimitedResource.EVENT_OCCURRENCES: _count_event_occurrences,
}


class EntitlementService:
    """Answers "what is the ceiling?", "how much is in use?", and "may I create one
    more?" for any organization and limited resource.

    Stateless; injected via ``di_core.containers``. Read-only — nothing here
    writes, so it is safe to call from inside a caller's transaction (and
    ``check_limit(lock=True)`` requires exactly that).
    """

    def get_effective_limit(self, organization: Organization, resource_key: str) -> EffectiveLimit:
        """Resolve ``organization``'s ceiling for ``resource_key``.

        The value is the billing root's ``SubscriptionPlanLimit.limit_value`` plus
        the quantity of every active ``SubscriptionAddOn`` on the same resource.

        Fails open in all three "we don't know" cases — no subscription, no limit
        row for this resource, or a NULL ``limit_value`` — by returning
        ``limit_value=None`` (unlimited). Treating any of them as zero would turn a
        data gap into a total lockout, which the rollout explicitly forbids.
        """
        subscription = self._get_root_subscription(organization)
        if subscription is None:
            logger.warning(
                "No subscription resolved for organization %s (resource %s); treating the "
                "limit as unlimited. Every billing root is expected to hold exactly one "
                "Subscription — this indicates a broken invariant, not a normal state.",
                organization.pk,
                resource_key,
            )
            return EffectiveLimit(
                resource_key=resource_key, limit_value=None, kind=None, overage_unit_price=None
            )

        limit = subscription.limits.filter(resource_key=resource_key).first()
        if limit is None:
            logger.debug(
                "Subscription %s has no SubscriptionPlanLimit row for %s; treating it as "
                "unlimited (fail-open).",
                subscription.pk,
                resource_key,
            )
            return EffectiveLimit(
                resource_key=resource_key, limit_value=None, kind=None, overage_unit_price=None
            )

        if limit.limit_value is None:
            # Unlimited plus any amount of purchased capacity is still unlimited;
            # skip the add-on aggregate entirely rather than adding to NULL.
            return EffectiveLimit(
                resource_key=resource_key,
                limit_value=None,
                kind=limit.kind,
                overage_unit_price=limit.overage_unit_price,
            )

        add_on_quantity = (
            subscription.add_ons.filter(resource_key=resource_key, is_active=True).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )
        return EffectiveLimit(
            resource_key=resource_key,
            limit_value=limit.limit_value + add_on_quantity,
            kind=limit.kind,
            overage_unit_price=limit.overage_unit_price,
        )

    def get_current_usage(self, organization: Organization, resource_key: str) -> int:
        """Point-in-time usage of ``resource_key``, summed across the whole pooled
        subtree that ``organization`` belongs to.

        The subtree is every organization that resolves to the same billing root:
        the root itself plus all descendants, stopping at any nested billing root
        (which pays for its own subtree separately).
        """
        root = resolve_billing_root(organization)
        organization_ids = self._get_pooled_organization_ids(root)
        subscription = self._get_subscription_for_root(root)
        counter = USAGE_COUNTERS.get(resource_key)
        if counter is None:
            # Unreachable while USAGE_COUNTERS covers LimitedResource (asserted by
            # test_every_limited_resource_has_a_counter). Fail open on an unknown
            # key rather than raising mid-request.
            logger.warning(
                "No usage counter registered for resource %s; reporting zero usage.",
                resource_key,
            )
            return 0
        return counter(organization_ids, subscription)

    def check_limit(
        self,
        organization: Organization,
        resource_key: str,
        delta: int = 1,
        lock: bool = False,
    ) -> LimitCheckResult:
        """Would creating ``delta`` more of ``resource_key`` stay within the ceiling?

        :param lock: When ``True``, take ``SELECT ... FOR UPDATE`` on the billing
            root's ``Subscription`` row *before* counting, so concurrent checks for
            the last unit of capacity serialize on that one row instead of both
            reading the same pre-write count and both succeeding. The lock is held
            until the caller's transaction commits, which means the caller must
            perform the actual create inside that same transaction for the
            serialization to be worth anything. Scoped to the subscription row
            rather than the resource table to keep contention off hot paths.

            Requires an open transaction. ``ATOMIC_REQUESTS = True`` satisfies this
            for anything called from a request; Celery tasks and management
            commands must open their own ``transaction.atomic`` block.
        """
        root = resolve_billing_root(organization)
        if lock:
            # Discard the returned row: the point is the row lock, and
            # get_effective_limit below re-reads through the same transaction.
            Subscription.objects.select_for_update().filter(organization=root).first()

        effective_limit = self.get_effective_limit(root, resource_key)
        if effective_limit.is_unlimited:
            return LimitCheckResult(
                allowed=True,
                resource_key=resource_key,
                current_usage=self.get_current_usage(root, resource_key),
                ceiling=None,
            )

        # Narrowed by the ``is_unlimited`` return above: limit_value is not None here.
        ceiling = effective_limit.limit_value or 0
        current_usage = self.get_current_usage(root, resource_key)
        allowed = current_usage + delta <= ceiling
        return LimitCheckResult(
            allowed=allowed,
            resource_key=resource_key,
            current_usage=current_usage,
            ceiling=ceiling,
            remedy=None if allowed else self._resolve_remedy(root, effective_limit),
        )

    def has_entitlement(self, organization: Organization, entitlement_key: str) -> bool:
        """Is the boolean feature gate ``entitlement_key`` granted to ``organization``?

        Resolved at the billing root, like limits. **Unlike limits, this fails
        closed**: an absent ``SubscriptionEntitlement`` row means "not granted",
        not "granted". The asymmetry is deliberate —
        ``SubscriptionService._sync_entitlements`` *deletes* rows for entitlements
        the current plan does not carry, so absence is how a revoked grant is
        represented. Failing open here would hand every feature to every
        organization whose plan omits it, whereas failing open on a limit only
        risks under-charging.
        """
        subscription = self._get_root_subscription(organization)
        if subscription is None:
            logger.warning(
                "No subscription resolved for organization %s; denying entitlement %s. "
                "Every billing root is expected to hold exactly one Subscription.",
                organization.pk,
                entitlement_key,
            )
            return False
        entitlement = subscription.entitlements.filter(entitlement_key=entitlement_key).first()
        return entitlement is not None and entitlement.is_enabled

    def _resolve_remedy(self, root: Organization, effective_limit: EffectiveLimit) -> str:
        """Pick the ``LimitRemedy`` that will actually unblock this caller.

        An organization in grace or restricted has a payment problem in front of
        any capacity problem, so it is pointed at billing first. Otherwise a
        pre-paid ceiling is liftable by buying capacity, while a post-paid
        allowance is not — only a bigger plan raises it.
        """
        subscription = self._get_subscription_for_root(root)
        if subscription is not None and subscription.billing_state in (
            BillingState.GRACE,
            BillingState.RESTRICTED,
        ):
            return LimitRemedy.RESOLVE_BILLING
        if effective_limit.kind == LimitKind.POSTPAID:
            return LimitRemedy.UPGRADE_PLAN
        return LimitRemedy.PURCHASE_ADD_ON

    def _get_root_subscription(self, organization: Organization) -> Subscription | None:
        return self._get_subscription_for_root(resolve_billing_root(organization))

    def _get_subscription_for_root(self, root: Organization) -> Subscription | None:
        """Fetch ``root``'s subscription without raising when it is missing.

        ``Subscription.organization`` is a ``OneToOneField``, so the reverse
        accessor raises ``RelatedObjectDoesNotExist`` rather than returning
        ``None``; every caller here wants the ``None``.
        """
        return Subscription.objects.filter(organization=root).first()

    def _get_pooled_organization_ids(self, root: Organization) -> list[int]:
        """Every organization whose usage counts against ``root``'s ceiling.

        ``root`` plus all descendants, not descending past a nested billing root —
        a child with ``can_invite_organizations=True`` is its own billing root and
        pays for its own subtree, so folding its usage in here would double-count
        it and charge the ancestor for capacity it did not sell.

        Breadth-first with a ``seen`` set. ``parent`` is user-mutable (Django
        admin), and while a cycle is normally unreachable by *descent* from a
        well-formed root — a cycle member's parent is another cycle member, so it
        is nobody else's child — it becomes reachable as soon as a cycle member is
        itself a billing root (e.g. ``can_invite_organizations=True`` with its
        parent pointing back into the cycle). The ``seen`` set is what makes that
        case terminate instead of looping forever.
        """
        seen = {root.pk}
        frontier = [root.pk]
        while frontier:
            children = Organization.objects.filter(parent_id__in=frontier).exclude(pk__in=seen)
            next_frontier = []
            for child in children:
                if is_billing_root(child):
                    # Nested reseller: its own root, pays for its own subtree.
                    continue
                seen.add(child.pk)
                next_frontier.append(child.pk)
            frontier = next_frontier
        return sorted(seen)
