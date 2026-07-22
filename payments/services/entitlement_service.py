"""Effective limits, pooled usage counting, and entitlement lookups.

This is the engine every enforcement call site uses. Three rules matter and are
easy to break by accident:

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
from dataclasses import dataclass

from django.db.models import Sum

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from payments.billing_constants import (
    BillingState,
    LimitedResource,
    LimitKind,
    LimitRemedy,
)
from payments.exceptions import InapplicableInvitationExclusionError, OverLimitError
from payments.models import MeteredOccurrence, PaymentMethod, Subscription
from payments.services.billing_dataclasses import EffectiveLimit, LimitCheckResult
from payments.services.subscription_service import (
    current_billing_period_start,
    is_billing_root,
    resolve_billing_root,
)
from public_api.models import SystemUser
from webhooks.models import WebhookConfiguration


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageContext:
    """Everything a usage counter is allowed to depend on.

    A single parameter object rather than a widening positional signature: most
    counters need only ``organization_ids``, and the two that need more
    (``event_occurrences`` wants the billing period, ``organization_members``
    wants the accept-path exclusion) should not force every other counter to grow
    a parameter it ignores.
    """

    organization_ids: Sequence[int]
    subscription: Subscription | None = None
    exclude_invitation_id: int | None = None
    """The invitation currently being accepted or resent, if any.

    Accepting an invitation is **net zero** on seat usage: the pending invitation
    stops being pending and becomes the membership it was already holding a seat
    for. Counting it on both sides would make the accept fail its own
    ``check_limit(delta=1)`` at exactly the ceiling, so an organization could
    never fill its last seat — it could invite up to the limit and then be unable
    to let anybody in. Resending is the same shape: it reuses the still-pending
    row rather than creating a new one, so excluding it makes the resend net-zero
    too.
    """


UsageCounter = Callable[["UsageContext"], int]


def _count_organization_members(context: UsageContext) -> int:
    """Seats in use: active memberships plus still-open invitations.

    Pending invitations count toward the ceiling deliberately — without that, an
    organization could hold unlimited outstanding invitations and blow past its
    seat limit the moment they are accepted. Expired and already-accepted
    invitations do not count: an expired one can never become a seat, and an
    accepted one is already counted as its membership.
    """
    members = OrganizationMembership.objects.occupying_a_seat(context.organization_ids).count()
    pending_invitations = OrganizationInvitation.objects.pending(
        context.organization_ids, exclude_id=context.exclude_invitation_id
    ).count()
    return members + pending_invitations


def _count_resource_calendars(context: UsageContext) -> int:
    """Resource/room calendars, excluding soft-deleted ones."""
    return (
        Calendar.objects.live_of_type(CalendarType.RESOURCE)
        .filter(organization_id__in=context.organization_ids)
        .count()
    )


def _count_bundle_calendars(context: UsageContext) -> int:
    """Bundle calendars, excluding soft-deleted ones."""
    return (
        Calendar.objects.live_of_type(CalendarType.BUNDLE)
        .filter(organization_id__in=context.organization_ids)
        .count()
    )


def _count_calendar_groups(context: UsageContext) -> int:
    return CalendarGroup.objects.filter(organization_id__in=context.organization_ids).count()


def _count_availability_windows(context: UsageContext) -> int:
    """Availability windows the organization actually authored.

    Not every ``AvailableTime`` row is a window somebody created: editing one
    occurrence of a recurring window, or splitting a series, *inserts* extra rows
    (see ``AvailableTimeQuerySet.only_user_authored`` for the full list and the one
    residual gap). Counting those would over-report — an organization with a limit
    of 5 that created 3 recurring windows and edited 3 occurrences would read as 6
    and be blocked below its real usage, which the rollout's "nobody is blocked as
    a consequence of the rollout itself" rule forbids.
    """
    return (
        AvailableTime.objects.only_user_authored()
        .filter(organization_id__in=context.organization_ids)
        .count()
    )


def _count_webhook_subscriptions(context: UsageContext) -> int:
    """Webhook configurations, excluding soft-deleted ones (``deleted_at`` set)."""
    return (
        WebhookConfiguration.objects.live()
        .filter(organization_id__in=context.organization_ids)
        .count()
    )


def _count_public_api_system_users(context: UsageContext) -> int:
    """Active, non-soft-deleted public-API system users.

    ``SystemUser.organization`` is nullable, so a system user with no organization
    is invisible to this counter and consumes nobody's capacity. That is correct
    for pooling (it belongs to no billing root) but does mean an org-less token is
    entirely unmetered; whoever makes ``organization`` non-nullable should revisit
    this.
    """
    return SystemUser.objects.live().filter(organization_id__in=context.organization_ids).count()


def _count_event_occurrences(context: UsageContext) -> int:
    """Metered event occurrences in the subscription's current billing period.

    Occurrences of a recurring series are computed, never stored, so this counts
    the ``MeteredOccurrence`` rows ``MeteringService`` wrote — **not** a second,
    independent expansion of the calendar. There is deliberately only one place
    that decides an occurrence happened; a counter that re-derived it would be a
    second opinion, and the two would eventually disagree about a customer's bill.

    Reads back through ``MeteredOccurrenceQuerySet.for_billing_period``, the same
    method the meter's own allowance arithmetic uses, so "in this period" means one
    thing. A subscription-less pool (a broken invariant, warned about elsewhere)
    reports zero: this resource is post-paid, so under-reporting cannot block
    anybody.

    The period comes from ``current_billing_period_start`` — derived from
    ``timezone.now()`` — and **not** from ``Subscription.current_period_start``.
    Reading the column directly is the bug this replaced: the meter stamps
    ``billing_period_start`` by resolving each occurrence's own start time, and
    nothing advances the stored column (cycle close is not implemented yet), so once
    the stored period elapsed the meter wrote one period while this counter asked for
    an earlier one and got zero permanently. Both sides now go through
    ``resolve_billing_period_start``.
    """
    subscription = context.subscription
    if subscription is None:
        return 0
    return (
        MeteredOccurrence.objects.for_billing_period(
            subscription.pk, current_billing_period_start(subscription)
        )
        .for_organizations(context.organization_ids)
        .count()
    )


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


def _reject_inapplicable_invitation_exclusion(resource_key: str, has_exclusion: bool) -> None:
    """An invitation exclusion (eager id or lazy resolver) is read by exactly one
    usage counter.

    Every other counter takes the ``UsageContext`` and ignores the field, so
    passing one with any other ``resource_key`` is a no-op that *looks* like a
    seat exclusion took place. Raising is the only way that mistake is visible;
    logging would leave the caller with a wrong answer it believes.
    """
    if has_exclusion and resource_key != LimitedResource.ORGANIZATION_MEMBERS:
        raise InapplicableInvitationExclusionError(resource_key)


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
        root = resolve_billing_root(organization)
        return self._effective_limit_for_subscription(
            self._get_subscription_for_root(root),
            resource_key,
            root.pk,
            asked_for_organization_pk=organization.pk,
        )

    def _effective_limit_for_subscription(
        self,
        subscription: Subscription | None,
        resource_key: str,
        root_pk: int | None = None,
        asked_for_organization_pk: int | None = None,
    ) -> EffectiveLimit:
        """``get_effective_limit`` given an already-resolved subscription.

        Split out so ``check_limit`` can resolve the billing root and its
        subscription **once** and reuse both, instead of re-walking the ``parent``
        chain (one query per level) and re-fetching the subscription for the
        ceiling lookup, the usage count, and the remedy.

        :param root_pk: The **billing root**'s pk — always the root, never the
            organization that was asked about, so the warning below means one thing
            regardless of which entry point produced it. The subscription that is
            missing belongs to the root; logging a child's pk there would send
            whoever reads it looking for a subscription that was never supposed to
            exist.
        :param asked_for_organization_pk: The organization the caller actually asked
            about, when it differs from the root. Context only.
        """
        if subscription is None:
            logger.warning(
                "No subscription resolved for billing root %s (resource %s, asked for "
                "organization %s); treating the limit as unlimited. Every billing root is "
                "expected to hold exactly one Subscription — this indicates a broken "
                "invariant, not a normal state.",
                root_pk,
                resource_key,
                asked_for_organization_pk if asked_for_organization_pk is not None else root_pk,
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

        # NOTE: no period/expiry filter. `is_active` is the only check, so a
        # one-time (`is_recurring=False`) add-on raises the ceiling forever rather
        # than for the period it was bought for. Deactivating it is currently a
        # manual act. This belongs with the add-on purchase work that introduces
        # one-time purchases in the first place; handling expiry here would invent
        # a semantic with no spec.
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

    def get_current_usage(
        self,
        organization: Organization,
        resource_key: str,
        exclude_invitation_id: int | None = None,
    ) -> int:
        """Point-in-time usage of ``resource_key``, summed across the whole pooled
        subtree that ``organization`` belongs to.

        The subtree is every organization that resolves to the same billing root:
        the root itself plus all descendants, stopping at any nested billing root
        (which pays for its own subtree separately).

        :param exclude_invitation_id: See ``UsageContext.exclude_invitation_id`` —
            the accept-invitation path is net zero and must not double-count. Only
            meaningful for ``organization_members``; passing it with another
            ``resource_key`` raises rather than being silently ignored.
        """
        _reject_inapplicable_invitation_exclusion(resource_key, exclude_invitation_id is not None)
        root = resolve_billing_root(organization)
        return self._count_usage(
            root,
            resource_key,
            self._get_subscription_for_root(root),
            exclude_invitation_id=exclude_invitation_id,
        )

    def _count_usage(
        self,
        root: Organization,
        resource_key: str,
        subscription: Subscription | None,
        exclude_invitation_id: int | None = None,
    ) -> int:
        """``get_current_usage`` given an already-resolved root and subscription."""
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
        return counter(
            UsageContext(
                organization_ids=self._get_pooled_organization_ids(root),
                subscription=subscription,
                exclude_invitation_id=exclude_invitation_id,
            )
        )

    @staticmethod
    def _lock_billing_root_row(root: Organization) -> None:
        """Take ``SELECT ... FOR UPDATE`` on ``root``'s ``Subscription`` row.

        Discards the returned row: the point is the row lock, and every subsequent
        read in the caller's transaction goes through the same connection.
        """
        Subscription.objects.select_for_update().filter(organization=root).first()

    def lock_billing_root(self, organization: Organization) -> None:
        """Acquire the guard lock for ``organization`` *before* computing a delta.

        ``check_limit(lock=True)`` locks and counts in one call, which is all a
        single-row create needs. A bulk writer that must first *read* the database to
        work out how many rows it is about to create (e.g. the room-import writer
        splitting discovered resources into "already counted" and "new") has to take
        the lock before that read, or it computes its delta from a snapshot a
        concurrent writer may already have invalidated.

        Re-locking the same row later in the same transaction — which
        ``check_limit(lock=True)`` will do — is a no-op, so the two compose. Held
        until the caller's transaction commits; requires an open transaction, exactly
        like ``check_limit(lock=True)``.
        """
        self._lock_billing_root_row(resolve_billing_root(organization))

    def is_billing_root_restricted(self, organization: Organization) -> bool:
        """The single check for "must this organization's writes be blocked and
        its calendar sync paused?" -- ``True`` only when the *billing root*'s
        ``Subscription.billing_state`` is ``RESTRICTED``.

        Resolved at the billing root, like every other check in this service, so a
        reseller child answers exactly the question its root would -- the reseller
        cascade (``resolve_billing_root`` already routes children to the root) is
        automatic from that alone; nothing about the cascade needs reimplementing
        anywhere else.

        This is the **one** semantic definition of "restricted" both halves of
        the restriction behavior consult: the write block (every explicit
        ``check_not_restricted`` call site on an update/delete path, which routes
        through here) and every
        calendar-sync-pause site (``calendar_integration.tasks.calendar_sync_tasks``,
        the ``request_*`` methods on ``CalendarSyncService``, and
        ``CalendarWebhookService``'s webhook-triggered sync). Two *independently
        derived* answers to "is this org restricted" is exactly the recurring
        two-predicates defect; the definition here is the only one.

        Two hot-path guards -- ``check_limit`` and ``check_postpaid_allowance``
        below -- do **not** call this method; they inline the identical
        ``subscription.billing_state == BillingState.RESTRICTED`` test on the
        ``root`` / ``Subscription`` they have *already* resolved once, purely to
        avoid re-walking the ``parent`` chain and re-fetching the subscription on
        the two hottest create paths in the product. That is a deliberate copy of
        the same test against the same resolved state -- not an independently
        derived predicate -- so it cannot disagree with this one; each such site
        carries a comment pointing back here.

        **``GRACE`` is not restricted.** Only ``RESTRICTED`` blocks -- a ``GRACE``
        organization stays fully writable and its sync keeps running; escalation is
        the dunning ladder (``DunningService``), never a write/sync block. Do not
        widen this to any other ``BillingState``.

        A missing subscription reads as **not restricted** (``False``), never
        restricted -- ``billing_state`` only exists on a real row, and an
        organization with no billing set up at all (a broken invariant, not a
        restricted one) must not be caught by this check; that would conflate "we
        don't know" with "we know, and the answer is blocked", which the fail-open
        convention the rest of this service follows forbids.
        """
        root = resolve_billing_root(organization)
        subscription = self._get_subscription_for_root(root)
        return subscription is not None and subscription.billing_state == BillingState.RESTRICTED

    def check_not_restricted(self, organization: Organization) -> None:
        """Raise ``OverLimitError`` (``remedy=resolve_billing``) when
        ``organization``'s billing root is ``RESTRICTED``; otherwise a no-op.

        The entry point every guarded create/update/delete method that does not
        already route through ``check_limit`` / ``check_postpaid_allowance``
        (which fold ``is_billing_root_restricted`` in directly, see their
        docstrings) calls before writing an ``OrganizationModel`` row on a guarded
        resource. See ``is_billing_root_restricted`` for what "restricted" means
        and why it is defined exactly once.
        """
        if self.is_billing_root_restricted(organization):
            raise OverLimitError.from_restricted_organization()

    def check_limit(
        self,
        organization: Organization,
        resource_key: str,
        delta: int = 1,
        lock: bool = False,
        exclude_invitation_id: int | None = None,
        exclude_invitation_id_resolver: Callable[[], int | None] | None = None,
    ) -> LimitCheckResult:
        """Would creating ``delta`` more of ``resource_key`` stay within the ceiling?

        Resolves the billing root and its ``Subscription`` **once** and threads both
        through the ceiling lookup, the usage count, and the remedy. Doing it per
        step re-walks the ``parent`` chain (a query per level) and re-fetches the
        subscription several times on what is a guarded create path.

        On the unlimited path usage is **not counted at all** — the answer cannot
        depend on it, and every organization is on the ``unlimited`` plan for the
        whole rollout, so counting there would make every guarded create pay for a
        value nobody reads. ``LimitCheckResult.current_usage`` is ``None`` in that
        case, not ``0``: reporting a number nobody measured would be a lie a caller
        could act on.

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

            Correctness depends on the connection running at **READ COMMITTED**
            (PostgreSQL's default, and this project's). The second transaction
            blocks on the locked row and, on acquiring it, re-reads the resource
            tables and sees the first one's committed insert. Under REPEATABLE READ
            it would instead see its original snapshot — the same pre-write count
            the lock exists to prevent — and both callers would be allowed. If the
            project ever raises the isolation level, this guard has to be
            revisited, not just retested.
        :param exclude_invitation_id: See ``UsageContext.exclude_invitation_id``. Two
            legitimate callers pass this: ``check_seat_limit_for_invitation_accept``
            (the accept path — prefer that named entry point, a call a reviewer can
            see, over passing this kwarg directly) and ``invite_user_to_organization``'s
            resend branch, which excludes the still-pending invitation being reused so
            a resend at the exact ceiling is net-zero rather than a false block. Only
            meaningful for ``organization_members``; passing it with any other
            ``resource_key`` raises, since it would otherwise be silently ignored.
        :param exclude_invitation_id_resolver: Lazy alternative to ``exclude_invitation_id``
            for a caller whose exclusion itself requires a query (e.g. resolving the
            still-pending invitation a resend is reusing). Called at most once, and only
            after the ceiling is known to be finite, so an ``unlimited`` organization never
            pays for that query. Mutually exclusive with ``exclude_invitation_id``; same
            ``organization_members``-only restriction.
        """
        _reject_inapplicable_invitation_exclusion(
            resource_key,
            exclude_invitation_id is not None or exclude_invitation_id_resolver is not None,
        )
        root = resolve_billing_root(organization)
        if lock:
            self._lock_billing_root_row(root)

        subscription = self._get_subscription_for_root(root)
        # RESTRICTED blocks every write outright, independent of the
        # numeric ceiling below -- an organization whose plan carries no ceiling at
        # all (``unlimited``, every organization's actual plan for this whole
        # rollout) could otherwise create freely while RESTRICTED, since the
        # ``is_unlimited`` branch below never even looks at ``billing_state``.
        # This is the identical test ``is_billing_root_restricted`` performs,
        # inlined here against the ``root`` / ``subscription`` already resolved
        # above so this hot create path does not re-walk the ``parent`` chain and
        # re-fetch the subscription just to re-ask the same question -- see that
        # method's docstring for why the copy is deliberate and cannot diverge.
        # ``current_usage``/``ceiling`` are ``0``/``0`` sentinels here -- this
        # block is not about capacity, so there is no meaningful count to report;
        # ``remedy`` is always ``resolve_billing``, which supersedes whatever
        # ``_resolve_remedy_for`` would otherwise have picked (below, unreached
        # for a RESTRICTED subscription now that this short-circuit exists).
        if subscription is not None and subscription.billing_state == BillingState.RESTRICTED:
            return LimitCheckResult(
                allowed=False,
                resource_key=resource_key,
                current_usage=0,
                ceiling=0,
                remedy=LimitRemedy.RESOLVE_BILLING,
            )

        effective_limit = self._effective_limit_for_subscription(
            subscription, resource_key, root.pk, asked_for_organization_pk=organization.pk
        )
        if effective_limit.is_unlimited:
            return LimitCheckResult(
                allowed=True,
                resource_key=resource_key,
                current_usage=None,
                ceiling=None,
            )

        # Narrowed by the ``is_unlimited`` return above: limit_value is not None here.
        ceiling = effective_limit.limit_value or 0
        if exclude_invitation_id_resolver is not None:
            exclude_invitation_id = exclude_invitation_id_resolver()
        current_usage = self._count_usage(
            root, resource_key, subscription, exclude_invitation_id=exclude_invitation_id
        )
        allowed = current_usage + delta <= ceiling
        return LimitCheckResult(
            allowed=allowed,
            resource_key=resource_key,
            current_usage=current_usage,
            ceiling=ceiling,
            remedy=(None if allowed else self._resolve_remedy_for(subscription, effective_limit)),
        )

    def check_seat_limit_for_invitation_accept(
        self, invitation: OrganizationInvitation, lock: bool = True
    ) -> LimitCheckResult:
        """May ``invitation`` be accepted without exceeding the seat ceiling?

        The accept path's own entry point, rather than "``check_limit`` plus the
        right kwarg". Accepting is **net zero** on seats — the pending invitation
        stops being pending and becomes the membership it was already holding a
        seat for — so it must be excluded from the pending count or the accept
        fails its own check at exactly the ceiling, and an organization can never
        fill its last seat.

        Getting that wrong via ``check_limit(..., exclude_invitation_id=...)`` is a
        *missing kwarg*: invisible in review, ungreppable, and silent (a permanent
        lockout rather than an error). Getting it wrong here is a missing call.

        ``lock`` defaults to ``True`` — unlike ``check_limit`` — because this is
        only ever called immediately before the accept writes, which is exactly the
        situation the row lock exists for. See ``check_limit`` for the transaction
        and isolation-level requirements that come with it.
        """
        return self.check_limit(
            invitation.organization,
            LimitedResource.ORGANIZATION_MEMBERS,
            delta=1,
            lock=lock,
            exclude_invitation_id=invitation.pk,
        )

    def has_payment_method(self, organization: Organization) -> bool:
        """Does the billing root have a chargeable payment method on file, right now?

        Resolved at the billing root, like every other check in this service, so a
        reseller child asks the same question its root would answer.

        **Queries the real record** (``PaymentMethod``, ``is_active=True``). This
        used to be answered from a ``Subscription.billing_state`` allow-list proxy,
        because no payment-method record existed yet. Now
        ``SubscriptionService.record_payment_method`` writes a real record from the
        webhook path once a charge against an instrument is confirmed, and this
        method reads that record instead of inferring from billing states: once an
        instrument is actually persisted, ``billing_state`` stops being evidence of
        whether one is on file at all. An organization can be ``ACTIVE`` from a past
        cycle with no *current* instrument (e.g. after an admin removed it), or hold
        a valid card on file while ``GRACE`` — a failed charge moves
        ``ACTIVE -> GRACE`` but says nothing about whether the card itself is still
        attached, and a ``GRACE`` organization stays fully operational (only
        ``RESTRICTED`` blocks writes). Under the old proxy ``GRACE`` had to read
        ``False`` categorically, even for an organization whose card is fine and
        whose *next* retry will succeed; the real record answers that case correctly
        instead of by state-based inference.

        A missing subscription's organization has no billing root ``PaymentMethod``
        row either, so this still reads ``False`` for it — nothing to charge.
        Note that on the postpaid path this rarely decides anything — a
        subscription-less pool resolves to an unlimited ceiling and returns before
        this is ever consulted (see ``check_postpaid_allowance``).
        """
        root = resolve_billing_root(organization)
        return self._has_payment_method_for_organization_id(root.pk)

    @classmethod
    def _has_payment_method_for_subscription(cls, subscription: Subscription | None) -> bool:
        if subscription is None:
            return False
        return cls._has_payment_method_for_organization_id(subscription.organization_id)

    @staticmethod
    def _has_payment_method_for_organization_id(organization_id: int) -> bool:
        return PaymentMethod.objects.filter(
            organization_id=organization_id, is_active=True
        ).exists()

    def check_postpaid_allowance(
        self,
        organization: Organization,
        delta: int = 1,
        lock: bool = False,
        delta_resolver: Callable[[Subscription], int] | None = None,
    ) -> LimitCheckResult:
        """Would creating ``delta`` more ``event_occurrences`` need a payment method
        this organization does not have?

        The only postpaid ``LimitedResource`` member, so unlike ``check_limit`` this
        never takes a ``resource_key`` — there is only one to ask about.

        Unlike a prepaid ceiling, the allowance is not a hard cap. An organization
        **with** a payment method is let straight through even past it — the
        excess accrues as overage (billed at ``PlanLimit.overage_unit_price`` when
        ``MeteringService`` later meters it; this method never writes, it only
        decides whether creation may proceed). An organization **without** one is
        blocked the moment ``delta`` would take it to or past the allowance,
        because there is nothing to charge the overage to. This matches the rule:
        an organization with a payment method accrues past its included allowance
        and is never interrupted; one without a payment method is blocked at the
        allowance.

        On the unlimited path (``limit_value is None``), usage is not counted at
        all and ``current_usage``/``ceiling`` are ``None`` — identical to
        ``check_limit``'s unlimited branch, and for the same reason: every
        organization is on the ``unlimited`` plan for this whole rollout, so this
        method can never block anybody today. See the tests for that inertness
        guarantee on every guarded path.

        **Exception to all of the above: a ``RESTRICTED`` billing root
        blocks unconditionally**, before the unlimited check, before counting
        usage, and regardless of whether a payment method is on file — a
        ``RESTRICTED`` organization may not create more events even if it could
        technically pay for them; the only way out is resolving the restriction
        (``remedy=resolve_billing``), not adding a card. See
        ``is_billing_root_restricted``.

        ``delta`` must be in the same unit ``current_usage`` is measured in: the
        number of ``MeteredOccurrence`` rows this creation will eventually cause —
        **occurrences, not masters**. For a one-off event those coincide (1). For a
        *recurring* master they do not: ``MeteringService`` expands the master's rule
        and writes one row per occurrence, so a daily series costs ~30 a month, not 1.
        A caller creating a recurring master must therefore pass ``delta_resolver``
        rather than a hand-counted ``delta``.

        The other established value is the bundle fan-out's
        ``1 + n_internal_children`` (a bundle booking is billed as the primary
        calendar's event plus one more per ``CalendarProvider.INTERNAL`` child, never
        per member calendar). A caller that invents its own number here reproduces
        the "two checks that must agree" defect — derive it from the same
        provider/parent checks the meter and the fan-out writer use, never recompute
        it independently.

        :param delta_resolver: Lazy alternative to ``delta`` for a caller whose unit
            count is itself a query — specifically, expanding a just-created recurring
            master through ``MeteringService.occurrence_starts_of`` (the meter's own
            expansion, so the guard and the meter cannot disagree). Receives the
            resolved billing-root ``Subscription`` so it can bound its window with
            ``resolve_billing_period``. Called at most once, and **only after the
            ceiling is known to be finite**, so an ``unlimited`` organization — i.e.
            every organization for this whole rollout — never pays for the expansion.
            Takes precedence over ``delta`` when both are given.
        :param lock: Same contract as ``check_limit``'s ``lock`` — ``SELECT ... FOR
            UPDATE`` on the billing root's ``Subscription`` row before counting, so
            two racing creates at the allowance boundary serialize on one row.
            Requires an open transaction; see ``check_limit`` for the full isolation-
            level discussion.

            **Taken only once a finite ceiling is known to exist**, unlike
            ``check_limit``, which locks before resolving anything. That ordering
            difference is deliberate and load-bearing. Every event-creation path
            passes ``lock=True``, ``create_event`` is ``@transaction.atomic`` with an
            external provider round-trip inside it, and every organization is on
            ``unlimited`` — so locking first would put an organization-wide row lock
            on the hottest write path in the product, held across a network call, in
            service of a NULL ceiling that cannot block anybody. Two users booking
            different calendars of the same organization would serialize.

            Nothing is lost by locking later: the ceiling is not the racing quantity.
            ``_count_usage`` — the read the lock actually exists to serialize — still
            runs after the lock is acquired, and under READ COMMITTED it therefore
            still sees a racing transaction's committed inserts.
        """
        root = resolve_billing_root(organization)
        subscription = self._get_subscription_for_root(root)
        # RESTRICTED blocks outright, ahead of the unlimited check and
        # the payment-method check both. The identical test
        # ``is_billing_root_restricted`` performs, inlined here against the already
        # resolved ``root`` / ``subscription`` so this hot event-creation path does
        # not re-walk the ``parent`` chain and re-fetch the subscription to re-ask
        # the same question -- see that method's docstring. Sentinel 0/0
        # usage/ceiling, same convention as ``check_limit``'s restricted short-circuit.
        if subscription is not None and subscription.billing_state == BillingState.RESTRICTED:
            return LimitCheckResult(
                allowed=False,
                resource_key=LimitedResource.EVENT_OCCURRENCES,
                current_usage=0,
                ceiling=0,
                remedy=LimitRemedy.RESOLVE_BILLING,
            )

        effective_limit = self._effective_limit_for_subscription(
            subscription,
            LimitedResource.EVENT_OCCURRENCES,
            root.pk,
            asked_for_organization_pk=organization.pk,
        )
        if effective_limit.is_unlimited:
            return LimitCheckResult(
                allowed=True,
                resource_key=LimitedResource.EVENT_OCCURRENCES,
                current_usage=None,
                ceiling=None,
            )

        if lock:
            self._lock_billing_root_row(root)

        # Narrowed by the ``is_unlimited`` return above: limit_value is not None here.
        ceiling = effective_limit.limit_value or 0
        if delta_resolver is not None and subscription is not None:
            delta = delta_resolver(subscription)
        current_usage = self._count_usage(root, LimitedResource.EVENT_OCCURRENCES, subscription)
        within_allowance = current_usage + delta <= ceiling
        if within_allowance or self._has_payment_method_for_subscription(subscription):
            return LimitCheckResult(
                allowed=True,
                resource_key=LimitedResource.EVENT_OCCURRENCES,
                current_usage=current_usage,
                ceiling=ceiling,
            )
        # The only way to reach here is ``has_payment_method`` being False -- no
        # active ``PaymentMethod`` row on file for the billing root. The remedy is
        # always "go get a payment method", never ``_resolve_remedy_for``'s
        # billing-first branch, even for ``GRACE``: from this guard's point of view
        # there is nothing chargeable on file, and attaching a working instrument is
        # what both resolves the dunning and lifts this block. ``RESTRICTED`` never
        # reaches this branch at all -- it is short-circuited above, unconditionally,
        # before payment-method is ever consulted.
        return LimitCheckResult(
            allowed=False,
            resource_key=LimitedResource.EVENT_OCCURRENCES,
            current_usage=current_usage,
            ceiling=ceiling,
            remedy=LimitRemedy.ADD_PAYMENT_METHOD,
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

    def _resolve_remedy_for(
        self, subscription: Subscription | None, effective_limit: EffectiveLimit
    ) -> str:
        """Pick the ``LimitRemedy`` that will actually unblock this caller.

        An organization in grace (or, defensively, restricted) has a payment
        problem in front of any capacity problem, so it is pointed at billing
        first. Otherwise a pre-paid ceiling is liftable by buying capacity, while
        a post-paid allowance is not — only a bigger plan raises it.

        Takes the already-resolved ``subscription`` rather than re-fetching it: this
        runs on the blocked branch of ``check_limit``, which has one in hand.

        The ``RESTRICTED`` half of the ``in (...)`` below is unreachable in
        practice: ``check_limit`` short-circuits a ``RESTRICTED``
        subscription unconditionally, before this is ever called (see
        ``is_billing_root_restricted``). Left in rather than narrowed to
        ``GRACE`` alone — both source the same remedy, and removing it would make
        this function's correctness depend on exactly where its one caller happens
        to short-circuit, which is a coincidence worth not encoding twice.
        """
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

    def get_pooled_organization_ids(self, organization: Organization) -> list[int]:
        """Every organization whose usage pools with ``organization``'s.

        Public entry point onto the same subtree walk every usage counter runs on,
        for callers that need the pool itself rather than a count —
        ``MeteringService`` sweeps calendar events across exactly this set, and it
        must be the *same* set the ``event_occurrences`` counter later reads back,
        or the meter and the counter would be looking at different organizations.
        """
        return self._get_pooled_organization_ids(resolve_billing_root(organization))

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
