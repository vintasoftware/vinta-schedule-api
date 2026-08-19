"""Registers the host's limited resources and entitlements against
``vinta_billing.registry``.

The package ships no opinion about what it is billing for -- a "resource
calendar" or a "webhook subscription" is entirely this project's vocabulary.
Registration is what tells the engine that vocabulary exists, and each
resource's ``counter`` is how the engine learns to count it.

This module is the definition site. The keys and labels registered below are
literals, not read off ``payments.billing_constants.LimitedResource`` /
``Entitlement`` -- those enums are what this registry replaces, and they have
no equivalent in ``vinta_billing``. Copying their string values here once,
rather than importing and iterating them, is what lets this module keep
working once Phase 6 deletes ``billing_constants.py``.
``payments/tests/seams/test_resources.py`` cross-checks the two sides against
each other for as long as the enums still exist.

Every counter here is the corresponding ``_count_*`` function that used to
live on ``payments.services.entitlement_service``, rewritten against
``vinta_billing.counting.UsageContext`` / ``count_by_organization`` /
``merge_breakdowns`` in place of the module-private helpers that used to do
the same job. The counting logic itself -- which rows count, which are
excluded, why two tables get merged instead of concatenated -- is unchanged;
only the plumbing it is built on moved.

Registration already happens from process start without any help:
``di_core``'s DI wiring (``DICoreConfig.ready()``) imports every submodule
under ``payments``, including this one, before ``PaymentsConfig.ready()``
runs. ``PaymentsConfig.ready()`` imports this module anyway, as a deliberate
order-independent guarantee rather than as the fix for a gap -- see that
method for why leaning on DI wiring to import a registry is coupling worth
not having.
"""

from __future__ import annotations

from typing import cast

from django.utils.translation import gettext as _

from vinta_billing.constants import LimitKind, LimitRemedy
from vinta_billing.counting import UsageContext, count_by_organization, merge_breakdowns
from vinta_billing.models import MeteredOccurrence, Subscription
from vinta_billing.registry import entitlements, resources
from vinta_billing.services.subscription_service import current_billing_period_start

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarGroup
from organizations.models import OrganizationInvitation, OrganizationMembership
from public_api.models import SystemUser
from webhooks.models import WebhookConfiguration


#: The registered resource keys, as symbols. The strings themselves are the
#: definition -- they are what the ``PlanLimit`` / ``BillingPeriodResourceUsage``
#: rows already hold and what the API already returns -- but a call site should
#: still say what it means rather than repeat a literal that nothing would flag
#: if it were mistyped. This module is where they live: it is the registration
#: site, so a name here cannot drift from a key that exists.
ORGANIZATION_MEMBERS = "organization_members"
RESOURCE_CALENDARS = "resource_calendars"
CALENDAR_GROUPS = "calendar_groups"
BUNDLE_CALENDARS = "bundle_calendars"
AVAILABILITY_WINDOWS = "availability_windows"
WEBHOOK_SUBSCRIPTIONS = "webhook_subscriptions"
PUBLIC_API_SYSTEM_USERS = "public_api_system_users"
EVENT_OCCURRENCES = "event_occurrences"

#: The one ``usage_extra`` key any counter here reads -- see
#: :func:`_count_organization_members` and ``payments.seams.seats``.
EXCLUDE_INVITATION_ID = "exclude_invitation_id"

#: Declared on every resource whose counter reads *no* per-call data, which is
#: seven of the eight. An empty declaration is not the same as no declaration:
#: ``usage_extra_keys=None`` (the package default, and what a 0.3.0-era
#: registration says) turns the check off entirely, while ``frozenset()`` says
#: "this counter reads nothing", which is exactly what makes a key meant for
#: ``organization_members`` visible when it is aimed here by mistake. Without
#: it, ``check_limit(usage_extra={"exclude_invitation_id": ...})`` against, say,
#: ``resource_calendars`` returns a count computed as though nothing had been
#: excluded -- an answer with nothing in it to say the exclusion did not happen.
#: That is the guard the host used to spell as
#: ``InapplicableInvitationExclusionError``; it is
#: ``vinta_billing.exceptions.InapplicableUsageExtraError`` now.
READS_NO_USAGE_EXTRA: frozenset[str] = frozenset()


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


#: Every resource here is capped up front, and a purchase of more capacity is
#: the only remedy. Mirrors ``EntitlementService._resolve_remedy_for``'s
#: non-grace branch: the GRACE/RESTRICTED override that function also applies
#: is a per-call decision the engine's own ``check_limit`` makes, not
#: something a static per-resource registration can express.
resources.register(
    ORGANIZATION_MEMBERS,
    label=_("Organization members"),
    kind=LimitKind.PREPAID,
    counter=_count_organization_members,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=frozenset({EXCLUDE_INVITATION_ID}),
)
resources.register(
    RESOURCE_CALENDARS,
    label=_("Resource calendars"),
    kind=LimitKind.PREPAID,
    counter=_count_resource_calendars,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)
resources.register(
    CALENDAR_GROUPS,
    label=_("Calendar groups"),
    kind=LimitKind.PREPAID,
    counter=_count_calendar_groups,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)
resources.register(
    BUNDLE_CALENDARS,
    label=_("Bundle calendars"),
    kind=LimitKind.PREPAID,
    counter=_count_bundle_calendars,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)
resources.register(
    AVAILABILITY_WINDOWS,
    label=_("Availability windows"),
    kind=LimitKind.PREPAID,
    counter=_count_availability_windows,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)
resources.register(
    WEBHOOK_SUBSCRIPTIONS,
    label=_("Webhook subscriptions"),
    kind=LimitKind.PREPAID,
    counter=_count_webhook_subscriptions,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)
resources.register(
    PUBLIC_API_SYSTEM_USERS,
    label=_("Public API system users"),
    kind=LimitKind.PREPAID,
    counter=_count_public_api_system_users,
    remedy=LimitRemedy.PURCHASE_ADD_ON,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)
#: The one postpaid resource: metered and billed after the fact, so its
#: remedy is a bigger plan rather than more capacity. Mirrors the seed
#: migration's own ``POSTPAID_RESOURCES``
#: (``payments/migrations/0007_seed_billing_plans.py``), which this
#: registration must not silently drift from.
resources.register(
    EVENT_OCCURRENCES,
    label=_("Event occurrences"),
    kind=LimitKind.POSTPAID,
    counter=_count_event_occurrences,
    remedy=LimitRemedy.UPGRADE_PLAN,
    usage_extra_keys=READS_NO_USAGE_EXTRA,
)

entitlements.register("external_calendar_google", label=_("Google Calendar sync"))
entitlements.register("external_calendar_microsoft", label=_("Microsoft Calendar sync"))
entitlements.register("partner_api", label=_("Partner / public API access"))
entitlements.register("white_label_branding", label=_("White-label branding"))
entitlements.register("advanced_scheduling", label=_("Advanced scheduling"))
