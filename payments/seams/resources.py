"""Registers the host's limited resources and entitlements against
``vinta_billing.registry``.

The package ships no opinion about what it is billing for -- a "resource
calendar" or a "webhook subscription" is entirely this project's vocabulary.
Registration is what tells the engine that vocabulary exists, and each
resource's ``counter`` is how the engine learns to count it.

Every counter here is the corresponding ``_count_*`` function that used to
live on ``payments.services.entitlement_service``, rewritten against
``vinta_billing.counting.UsageContext`` / ``count_by_organization`` /
``merge_breakdowns`` in place of the module-private helpers that used to do
the same job. The counting logic itself -- which rows count, which are
excluded, why two tables get merged instead of concatenated -- is unchanged;
only the plumbing it is built on moved.

Registered from ``PaymentsConfig.ready()`` once ``vinta_billing`` joins
``INSTALLED_APPS`` (Phase 1). Importing this module is what runs the
``resources.register(...)`` / ``entitlements.register(...)`` calls at the
bottom -- there is no other entry point.
"""

from __future__ import annotations

from typing import cast

from vinta_billing.constants import LimitKind, LimitRemedy
from vinta_billing.counting import UsageContext, count_by_organization, merge_breakdowns
from vinta_billing.registry import UsageCounter, entitlements, resources

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarGroup
from organizations.models import OrganizationInvitation, OrganizationMembership
from payments.billing_constants import Entitlement, LimitedResource
from payments.models import MeteredOccurrence, Subscription
from payments.services.subscription_service import current_billing_period_start
from public_api.models import SystemUser
from webhooks.models import WebhookConfiguration


def _count_organization_members(context: UsageContext) -> dict[int, int]:
    """Seats in use per organization: active memberships plus still-open invitations.

    Pending invitations count toward the ceiling deliberately -- without that, an
    organization could hold unlimited outstanding invitations and blow past its
    seat limit the moment they are accepted. Expired and already-accepted
    invitations do not count: an expired one can never become a seat, and an
    accepted one is already counted as its membership.

    Memberships and pending invitations are grouped separately, then merged
    key-wise (``merge_breakdowns``) rather than concatenated, so an organization
    holding both kinds of seat is not double-keyed in the result.

    ``exclude_invitation_id`` travels through ``UsageContext.extra`` -- the
    engine never reads that field itself, it only forwards whatever a caller
    passed as ``check_limit(..., usage_extra=...)``. See
    ``UsageContext.get`` for the read.
    """
    members = count_by_organization(
        OrganizationMembership.objects.occupying_a_seat(context.organization_ids)
    )
    pending_invitations = count_by_organization(
        OrganizationInvitation.objects.pending(
            context.organization_ids, exclude_id=context.get("exclude_invitation_id")
        )
    )
    return merge_breakdowns(members, pending_invitations)


def _count_resource_calendars(context: UsageContext) -> dict[int, int]:
    """Resource/room calendars per organization, excluding soft-deleted ones.

    ``unscoped()`` for the reason given on every pooled counter below: a usage
    count spans a subscription's whole reseller subtree
    (``context.organization_ids``), which no single-organization binding can
    express. The tenant boundary is ``organization_ids`` itself, resolved from
    the billing root by ``EntitlementService`` before this counter ever runs,
    and applied on the next line.
    """
    return count_by_organization(
        Calendar.objects.unscoped()
        .live_of_type(CalendarType.RESOURCE)
        .filter(organization_id__in=context.organization_ids)
    )


def _count_bundle_calendars(context: UsageContext) -> dict[int, int]:
    """Bundle calendars per organization, excluding soft-deleted ones.

    ``unscoped()``: see :func:`_count_resource_calendars`.
    """
    return count_by_organization(
        Calendar.objects.unscoped()
        .live_of_type(CalendarType.BUNDLE)
        .filter(organization_id__in=context.organization_ids)
    )


def _count_calendar_groups(context: UsageContext) -> dict[int, int]:
    """Calendar groups per organization.

    ``unscoped()``: see :func:`_count_resource_calendars`.
    """
    return count_by_organization(
        CalendarGroup.objects.unscoped().filter(organization_id__in=context.organization_ids)
    )


def _count_availability_windows(context: UsageContext) -> dict[int, int]:
    """Every time window the organization actually authored, per organization --
    availability windows and blocked time alike, positive or negative.

    Not every ``AvailableTime``/``BlockedTime`` row is a window somebody created:
    editing one occurrence of a recurring window, or splitting a series, *inserts*
    extra rows (see ``AvailableTimeQuerySet.only_user_authored`` /
    ``BlockedTimeQuerySet.only_user_authored`` for the full list and the one
    residual gap each carries). Counting those would over-report -- an organization
    with a limit of 5 that created 3 recurring windows and edited 3 occurrences
    would read as 6 and be blocked below its real usage, which the rollout's
    "nobody is blocked as a consequence of the rollout itself" rule forbids.

    Reads through ``unscoped()`` on both models: the default manager on each
    excludes group-scoped rows (``group_slot`` set) by design, so counting
    through it would under-report and let group-scoped windows and blocks bypass
    the plan limit entirely. The spec's metering rule is "every time window an
    organization authors is metered" regardless of scope or sign, so base and
    group-scoped rows of both models are counted together here. Blocked time is
    metered here alongside availability windows deliberately, not incidentally:
    it is a billing-rule change applied to every organization at once, not a
    side effect of adding group scoping.

    The two models are grouped separately, then merged key-wise
    (``merge_breakdowns``): an organization that authored both availability
    windows and blocked time must have its counts added, not one shadowing the
    other.
    """
    organization_filter = {"organization_id__in": context.organization_ids}
    availability_windows = count_by_organization(
        AvailableTime.objects.unscoped().only_user_authored().filter(**organization_filter)
    )
    blocked_times = count_by_organization(
        BlockedTime.objects.unscoped().only_user_authored().filter(**organization_filter)
    )
    return merge_breakdowns(availability_windows, blocked_times)


def _count_webhook_subscriptions(context: UsageContext) -> dict[int, int]:
    """Webhook configurations per organization, excluding soft-deleted ones
    (``deleted_at`` set)."""
    # ``unscoped()`` first, like the ``calendar_integration`` counters above: a
    # usage context spans a billing root's whole pooled reseller subtree, so this
    # is a deliberate cross-organization read that no single bound organization
    # covers. ``organization_id__in`` is the scope.
    return count_by_organization(
        WebhookConfiguration.objects.unscoped()
        .live()
        .filter(organization_id__in=context.organization_ids)
    )


def _count_public_api_system_users(context: UsageContext) -> dict[int, int]:
    """Active, non-soft-deleted public-API system users, per organization.

    ``SystemUser.organization`` is nullable, so a system user with no organization
    is invisible to this counter and consumes nobody's capacity. That is correct
    for pooling (it belongs to no billing root) but does mean an org-less token is
    entirely unmetered; whoever makes ``organization`` non-nullable should revisit
    this.
    """
    # ``unscoped()`` for the pooled-subtree reason given in
    # ``_count_webhook_subscriptions``.
    return count_by_organization(
        SystemUser.objects.unscoped().live().filter(organization_id__in=context.organization_ids)
    )


def _count_event_occurrences(context: UsageContext) -> dict[int, int]:
    """Metered event occurrences in the subscription's current billing period, per
    organization.

    Occurrences of a recurring series are computed, never stored, so this counts
    the ``MeteredOccurrence`` rows ``MeteringService`` wrote -- **not** a second,
    independent expansion of the calendar. There is deliberately only one place
    that decides an occurrence happened; a counter that re-derived it would be a
    second opinion, and the two would eventually disagree about a customer's bill.

    Reads back through ``MeteredOccurrenceQuerySet.for_billing_period``, the same
    method the meter's own allowance arithmetic uses, so "in this period" means one
    thing. A subscription-less pool (a broken invariant, warned about elsewhere)
    reports an empty breakdown: this resource is post-paid, so under-reporting
    cannot block anybody.

    The period comes from ``current_billing_period_start`` -- derived from
    ``timezone.now()`` -- and **not** from ``Subscription.current_period_start``.
    Reading the column directly is the bug this replaced: the meter stamps
    ``billing_period_start`` by resolving each occurrence's own start time, and
    nothing advances the stored column (cycle close is not implemented yet), so once
    the stored period elapsed the meter wrote one period while this counter asked for
    an earlier one and got zero permanently. Both sides now go through
    ``resolve_billing_period_start``.

    Grouped over the **existing** ``for_billing_period(...).for_organizations(...)``
    queryset -- never a second, independently filtered query -- so the period and
    pool this counter groups by are provably the same ones the scalar count used to
    read.

    ``context.subscription`` is typed against ``vinta_billing.models.Subscription``
    by the generic ``UsageContext`` dataclass, but at every call site that actually
    reaches this counter it is the host's own ``payments.models.Subscription`` --
    the one ``EntitlementService`` resolved for the billing root. The ``cast``
    below documents that rather than changing behaviour.
    """
    subscription = cast("Subscription | None", context.subscription)
    if subscription is None:
        return {}
    return count_by_organization(
        MeteredOccurrence.objects.for_billing_period(
            subscription.pk, current_billing_period_start(subscription)
        ).for_organizations(context.organization_ids)
    )


#: One counter per ``LimitedResource`` member, keyed by its string value so the
#: registration loop below can look each one up without repeating the key.
_COUNTERS: dict[str, UsageCounter] = {
    LimitedResource.ORGANIZATION_MEMBERS: _count_organization_members,
    LimitedResource.RESOURCE_CALENDARS: _count_resource_calendars,
    LimitedResource.CALENDAR_GROUPS: _count_calendar_groups,
    LimitedResource.BUNDLE_CALENDARS: _count_bundle_calendars,
    LimitedResource.AVAILABILITY_WINDOWS: _count_availability_windows,
    LimitedResource.WEBHOOK_SUBSCRIPTIONS: _count_webhook_subscriptions,
    LimitedResource.PUBLIC_API_SYSTEM_USERS: _count_public_api_system_users,
    LimitedResource.EVENT_OCCURRENCES: _count_event_occurrences,
}

#: Only ``event_occurrences`` is metered and billed after the fact; every other
#: resource is capped up front. Kept as a set, mirroring the seed migration's own
#: ``POSTPAID_RESOURCES`` (``payments/migrations/0007_seed_billing_plans.py``),
#: which this registration must not silently drift from.
_POSTPAID_RESOURCES = {LimitedResource.EVENT_OCCURRENCES}

for _member in LimitedResource:
    resources.register(
        _member.value,
        # ``TextChoices.label`` is already the translated string the old
        # ``TextChoices`` class rendered -- byte-identical by construction,
        # since this reads the same class rather than a hand-copied literal.
        label=_member.label,
        kind=(LimitKind.POSTPAID if _member in _POSTPAID_RESOURCES else LimitKind.PREPAID),
        counter=_COUNTERS[_member.value],
        # Mirrors ``EntitlementService._resolve_remedy_for``'s non-grace branch:
        # a post-paid ceiling is only liftable by a bigger plan, a pre-paid one
        # by buying capacity. The GRACE/RESTRICTED override that function also
        # applies is a per-call decision the engine's own ``check_limit`` makes,
        # not something a static per-resource registration can express.
        remedy=(
            LimitRemedy.UPGRADE_PLAN
            if _member in _POSTPAID_RESOURCES
            else LimitRemedy.PURCHASE_ADD_ON
        ),
    )

for _entitlement in Entitlement:
    entitlements.register(_entitlement.value, label=_entitlement.label)
