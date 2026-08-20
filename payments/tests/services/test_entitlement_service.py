"""``EntitlementService`` — effective limits, usage counting, and entitlements.

The key behavior under test is the fail-open rule: a NULL ``limit_value``
*and* a missing ``SubscriptionPlanLimit`` row both mean **unlimited**, never zero.
A missing seed row locking an organization out of a resource it could use
yesterday is the failure mode this whole feature is most likely to produce, and
these tests are what stop it.
"""

import datetime
from decimal import Decimal

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest
from model_bakery import baker
from vinta_billing.constants import BillingState, LimitKind, LimitRemedy
from vinta_billing.exceptions import InapplicableUsageExtraError
from vinta_billing.models import (
    BillingPlan,
    Subscription,
    SubscriptionAddOn,
    SubscriptionEntitlement,
    SubscriptionPlanLimit,
)
from vinta_billing.registry import resources
from vinta_billing.services.entitlement_service import EntitlementService

from calendar_integration.constants import CalendarType, CalendarVisibility
from calendar_integration.models import Calendar, CalendarGroup
from organizations.models import Organization, OrganizationInvitation, OrganizationMembership
from payments.seams.resource_keys import (
    BUNDLE_CALENDARS,
    CALENDAR_GROUPS,
    EVENT_OCCURRENCES,
    ORGANIZATION_MEMBERS,
    PARTNER_API,
    RESOURCE_CALENDARS,
    RESOURCE_KEYS,
    WEBHOOK_SUBSCRIPTIONS,
    WHITE_LABEL_BRANDING,
)
from payments.seams.resources import EXCLUDE_INVITATION_ID
from payments.seams.seats import check_seat_limit_for_invitation_accept
from webhooks.models import WebhookConfiguration


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


@pytest.fixture
def service():
    return EntitlementService()


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def subscription(organization):
    now = timezone.now()
    return baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )


def make_limit(subscription, resource_key, limit_value, kind=LimitKind.PREPAID, **kwargs):
    return baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=resource_key,
        limit_value=limit_value,
        kind=kind,
        **kwargs,
    )


def make_add_on(subscription, resource_key, quantity, is_active=True):
    return baker.make(
        SubscriptionAddOn,
        subscription=subscription,
        resource_key=resource_key,
        quantity=quantity,
        is_recurring=True,
        is_active=is_active,
    )


@pytest.mark.django_db
class TestGetEffectiveLimit:
    """The generic shape of ``get_effective_limit`` -- the subscription's own
    row and the NULL-is-unlimited rule -- is now the package's own to prove
    (``vinta_billing`` ``tests/test_entitlement_engine
    .py::TestEffectiveLimit``/``TestAddOns``). What survives here is what
    those generic tests do not pin: the add-on aggregation's cross-resource
    isolation and multi-row summing (the package's own add-on tests cover
    only one active row on one resource each), the unlimited branch skipping
    the add-on aggregate entirely, and the kind/overage-price passthrough
    this project's postpaid resources depend on.
    """

    def test_adds_active_add_on_quantity_to_the_plan_limit(
        self, service, organization, subscription
    ):
        make_limit(subscription, ORGANIZATION_MEMBERS, 5)
        make_add_on(subscription, ORGANIZATION_MEMBERS, 3)
        make_add_on(subscription, ORGANIZATION_MEMBERS, 2)

        result = service.get_effective_limit(organization, ORGANIZATION_MEMBERS)

        assert result.limit_value == 10

    def test_ignores_add_ons_for_a_different_resource(self, service, organization, subscription):
        make_limit(subscription, ORGANIZATION_MEMBERS, 5)
        make_add_on(subscription, RESOURCE_CALENDARS, 3)

        result = service.get_effective_limit(organization, ORGANIZATION_MEMBERS)

        assert result.limit_value == 5

    def test_unlimited_plan_limit_never_runs_the_add_on_aggregate(
        self, service, organization, subscription
    ):
        """The unlimited branch must skip the add-on ``Sum`` query entirely rather
        than run it and add to ``None`` -- the ordering detail the delegation onto
        ``effective_limit_from_resolved`` must preserve.
        """
        make_limit(subscription, ORGANIZATION_MEMBERS, None)
        make_add_on(subscription, ORGANIZATION_MEMBERS, 3)

        with CaptureQueriesContext(connection) as captured:
            result = service.get_effective_limit(organization, ORGANIZATION_MEMBERS)

        assert result.limit_value is None
        assert not [
            query
            for query in captured.captured_queries
            if "vinta_billing_subscriptionaddon" in query["sql"]
        ], (
            "The unlimited branch ran the add-on aggregate query. Queries seen: "
            f"{[query['sql'] for query in captured.captured_queries]}"
        )

    def test_carries_the_kind_and_overage_price_through(self, service, organization, subscription):
        make_limit(
            subscription,
            EVENT_OCCURRENCES,
            50,
            kind=LimitKind.POSTPAID,
            overage_unit_price=Decimal("0.0500"),
        )

        result = service.get_effective_limit(organization, EVENT_OCCURRENCES)

        assert result.kind == LimitKind.POSTPAID
        assert result.overage_unit_price == Decimal("0.0500")


@pytest.mark.django_db
class TestUsageCounters:
    def test_every_limited_resource_has_a_counter(self):
        """A new ``LimitedResource`` member without a counter would silently report
        zero usage forever — i.e. an unenforceable limit that looks enforced.

        The closed ``resource_key -> counter`` dict this used to read became the
        open ``vinta_billing.registry.resources``, which
        ``payments/seams/resources.py`` populates. Same invariant, new home.
        """
        assert set(resources.keys()) == set(RESOURCE_KEYS)

    def test_counts_active_memberships_and_pending_invitations(
        self, service, organization, subscription
    ):
        baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=2)
        baker.make(OrganizationMembership, organization=organization, is_active=False)
        baker.make(
            OrganizationInvitation,
            organization=organization,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        usage = service.get_current_usage(organization, ORGANIZATION_MEMBERS)

        assert usage == 3

    def test_expired_and_accepted_invitations_do_not_count(
        self, service, organization, subscription
    ):
        """An expired invitation can never become a seat; an accepted one is already
        counted as its membership. Counting either would over-report."""
        baker.make(
            OrganizationInvitation,
            organization=organization,
            accepted_at=None,
            expires_at=timezone.now() - datetime.timedelta(days=1),
        )
        baker.make(
            OrganizationInvitation,
            organization=organization,
            accepted_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        usage = service.get_current_usage(organization, ORGANIZATION_MEMBERS)

        assert usage == 0

    def test_resource_calendar_counter_excludes_other_types_and_soft_deletes(
        self, service, organization, subscription
    ):
        # `external_id` is unique per (provider, organization), so each calendar
        # needs a distinct one rather than baker's shared blank default.
        for index, (calendar_type, visibility) in enumerate(
            [
                (CalendarType.RESOURCE, CalendarVisibility.ACTIVE),
                (CalendarType.RESOURCE, CalendarVisibility.UNLISTED),
                (CalendarType.RESOURCE, CalendarVisibility.INACTIVE),
                (CalendarType.BUNDLE, CalendarVisibility.ACTIVE),
            ]
        ):
            baker.make(
                Calendar,
                organization=organization,
                calendar_type=calendar_type,
                visibility=visibility,
                external_id=f"external-{index}",
            )

        assert service.get_current_usage(organization, RESOURCE_CALENDARS) == 2
        assert service.get_current_usage(organization, BUNDLE_CALENDARS) == 1

    def test_calendar_group_and_webhook_counters(self, service, organization, subscription):
        baker.make(CalendarGroup, organization=organization, _quantity=2)
        baker.make(WebhookConfiguration, organization=organization, deleted_at=None)
        baker.make(WebhookConfiguration, organization=organization, deleted_at=timezone.now())

        assert service.get_current_usage(organization, CALENDAR_GROUPS) == 2
        assert service.get_current_usage(organization, WEBHOOK_SUBSCRIPTIONS) == 1

    def test_usage_is_scoped_to_the_organization(self, service, organization, subscription):
        """A sibling organization's rows must never leak into this one's count."""
        other = baker.make(Organization, parent=None, can_invite_organizations=False)
        baker.make(CalendarGroup, organization=other, _quantity=3)
        baker.make(CalendarGroup, organization=organization)

        assert service.get_current_usage(organization, CALENDAR_GROUPS) == 1


@pytest.mark.django_db
class TestCheckLimit:
    """The generic allow/block shape -- under the ceiling, exactly at it, past
    it, a ``delta`` bigger than one, unlimited never blocking, an add-on
    lifting a blocked check -- is now the package's own to prove
    (``vinta_billing`` ``tests/test_entitlement_engine.py::TestCheckLimit``/
    ``TestAddOns``). What survives here is what those generic tests do not
    reach: this project's billing-state remedy routing, and the row-lock /
    query-count guarantees ``check_limit`` makes on top of the generic engine
    behaviour.
    """

    def test_postpaid_resource_recommends_a_plan_upgrade(self, service, organization, subscription):
        """Extra capacity is not purchasable for a post-paid allowance, so pointing
        the user at an add-on would be a dead end."""
        make_limit(subscription, EVENT_OCCURRENCES, 0, kind=LimitKind.POSTPAID)

        result = service.check_limit(organization, EVENT_OCCURRENCES)

        assert result.allowed is False
        assert result.remedy == LimitRemedy.UPGRADE_PLAN

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_unpaid_organization_is_pointed_at_billing_first(
        self, service, organization, subscription, billing_state
    ):
        subscription.billing_state = billing_state
        subscription.save(update_fields=["billing_state"])
        make_limit(subscription, CALENDAR_GROUPS, 1)
        baker.make(CalendarGroup, organization=organization)

        result = service.check_limit(organization, CALENDAR_GROUPS)

        assert result.allowed is False
        assert result.remedy == LimitRemedy.RESOLVE_BILLING

    def test_lock_takes_a_row_lock_on_the_subscription(self, service, organization, subscription):
        """``lock=True`` must issue ``SELECT ... FOR UPDATE`` against the
        *subscription* table, not merely run without error.

        This assertion used to be vacuous: it requested ``django_assert_num_queries``
        and never used it, and its only check (``allowed is True``) passed
        identically if ``lock=True`` were ignored outright. The whole concurrency
        guarantee rests on this one statement being emitted against the right row,
        so the SQL itself is what gets asserted.
        """
        make_limit(subscription, CALENDAR_GROUPS, 3)

        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            result = service.check_limit(organization, CALENDAR_GROUPS, lock=True)

        assert result.allowed is True
        locking_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"] and "vinta_billing_subscription" in query["sql"]
        ]
        assert locking_queries, (
            "check_limit(lock=True) issued no SELECT ... FOR UPDATE against "
            "vinta_billing_subscription. Queries seen: "
            f"{[query['sql'] for query in captured.captured_queries]}"
        )

    def test_no_lock_takes_no_row_lock(self, service, organization, subscription):
        """The negative half — otherwise the assertion above could pass on a lock
        somebody took unconditionally."""
        make_limit(subscription, CALENDAR_GROUPS, 3)

        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            service.check_limit(organization, CALENDAR_GROUPS, lock=False)

        assert not [query for query in captured.captured_queries if "FOR UPDATE" in query["sql"]]

    def test_unlimited_does_not_count_usage_at_all(self, service, organization, subscription):
        """Every organization is on the ``unlimited`` plan for the whole rollout, so
        every guarded create runs this path. Counting usage there buys nothing — the
        answer cannot depend on it — and the required test pins "no change in
        behavior **or query count**" for an unlimited org.

        Asserted as "no query touched the counted tables", which is what actually
        matters, rather than a brittle absolute query number.
        """
        make_limit(subscription, CALENDAR_GROUPS, None)
        baker.make(CalendarGroup, organization=organization, _quantity=3)

        with CaptureQueriesContext(connection) as captured:
            result = service.check_limit(organization, CALENDAR_GROUPS)

        assert result.allowed is True
        assert result.ceiling is None
        assert result.current_usage is None, (
            "Usage must be reported as 'not measured', not as a fabricated 0."
        )
        assert not [
            query
            for query in captured.captured_queries
            if "calendar_integration_calendargroup" in query["sql"]
        ], "The unlimited path counted usage nobody reads."

    def test_check_limit_resolves_the_billing_root_and_subscription_once(
        self, service, subscription
    ):
        """``resolve_billing_root`` walks ``parent`` with one query per level and used
        to run three times per ``check_limit``, with the subscription re-fetched
        twice more on top. On a three-level tree that is ~9 avoidable queries on a
        guarded create path.

        Pinned as an upper bound on subscription reads rather than an exact total,
        so unrelated query changes elsewhere do not make this test brittle.
        """
        root = subscription.organization
        mid = baker.make(Organization, parent=root, can_invite_organizations=False)
        leaf = baker.make(Organization, parent=mid, can_invite_organizations=False)
        make_limit(subscription, CALENDAR_GROUPS, 3)

        with CaptureQueriesContext(connection) as captured:
            service.check_limit(leaf, CALENDAR_GROUPS)

        subscription_reads = [
            query
            for query in captured.captured_queries
            if 'FROM "vinta_billing_subscription"' in query["sql"]
        ]
        assert len(subscription_reads) == 1, (
            f"Expected the subscription to be fetched once, got {len(subscription_reads)}: "
            f"{[query['sql'] for query in subscription_reads]}"
        )


@pytest.mark.django_db
class TestSeatCountingOnTheAcceptPath:
    """Accepting an invitation is net zero.

    ``_count_organization_members`` counts pending invitations *and* active
    memberships, which is right for the invite path (an outstanding invitation is a
    reserved seat) and wrong for the accept path: the invitation being accepted is
    already counted, so ``check_limit(delta=1)`` at the ceiling would reject a
    change that does not move the total. An organization could invite up to its
    limit and then never let the last person in.
    """

    def _make_pending_invitation(self, organization):
        return baker.make(
            OrganizationInvitation,
            organization=organization,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

    def test_the_invitation_being_accepted_is_excluded_from_usage(
        self, service, organization, subscription
    ):
        baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=4)
        invitation = self._make_pending_invitation(organization)

        assert service.get_current_usage(organization, ORGANIZATION_MEMBERS) == 5
        assert (
            service.get_current_usage(
                organization,
                ORGANIZATION_MEMBERS,
                usage_extra={EXCLUDE_INVITATION_ID: invitation.pk},
            )
            == 4
        )

    def test_an_organization_at_its_ceiling_can_still_accept_its_last_invitation(
        self, service, organization, subscription
    ):
        """The concrete lockout: seat limit 5, four members plus one pending invite.
        Without the exclusion the accept sees 5 + 1 > 5 and is refused, so the org
        can never reach its own ceiling."""
        make_limit(subscription, ORGANIZATION_MEMBERS, 5)
        baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=4)
        invitation = self._make_pending_invitation(organization)

        assert not service.check_limit(organization, ORGANIZATION_MEMBERS).allowed, (
            "A sixth *new* invite must still be blocked."
        )

        result = service.check_limit(
            organization,
            ORGANIZATION_MEMBERS,
            usage_extra={EXCLUDE_INVITATION_ID: invitation.pk},
        )

        assert result.allowed is True
        assert result.current_usage == 4

    def test_the_named_accept_entry_point_applies_the_exclusion(
        self, service, organization, subscription
    ):
        """``exclude_invitation_id`` is a kwarg six other call sites must *not* pass
        and the accept path must never forget — and forgetting it is a silent
        permanent lockout, not an error. ``check_seat_limit_for_invitation_accept``
        turns that into a missing *call*, which a reviewer can see.
        """
        make_limit(subscription, ORGANIZATION_MEMBERS, 5)
        baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=4)
        invitation = self._make_pending_invitation(organization)

        result = check_seat_limit_for_invitation_accept(service, invitation, lock=False)

        assert result.allowed is True
        assert result.current_usage == 4
        assert result.resource_key == ORGANIZATION_MEMBERS

    def test_the_named_accept_entry_point_still_blocks_a_genuinely_full_organization(
        self, service, organization, subscription
    ):
        """Net zero is not a bypass: with the ceiling already filled by memberships
        alone, the accept is still refused."""
        make_limit(subscription, ORGANIZATION_MEMBERS, 5)
        baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=5)
        invitation = self._make_pending_invitation(organization)

        result = check_seat_limit_for_invitation_accept(service, invitation, lock=False)

        assert result.allowed is False
        assert result.current_usage == 5

    def test_excluding_an_invitation_on_a_non_seat_resource_raises(
        self, service, organization, subscription
    ):
        """Only ``_count_organization_members`` reads ``exclude_invitation_id``, so
        passing it for any other resource would otherwise be a silent no-op that
        reads as an exclusion that never happened.

        The guard used to be the host's own ``InapplicableInvitationExclusionError``,
        raised from a host ``EntitlementService``. It is
        ``vinta_billing.exceptions.InapplicableUsageExtraError`` now, raised by the
        engine off what ``payments.seams.resources`` *declared*: the seven
        non-seat resources register ``usage_extra_keys=frozenset()``, and an empty
        declaration is what makes a key aimed at the wrong resource raise rather
        than be forwarded and ignored. Undeclared (``None``, the package default)
        would leave the guard off, which is what makes this test worth keeping.
        """
        invitation = self._make_pending_invitation(organization)

        with pytest.raises(InapplicableUsageExtraError):
            service.check_limit(
                organization,
                RESOURCE_CALENDARS,
                usage_extra={EXCLUDE_INVITATION_ID: invitation.pk},
            )

        with pytest.raises(InapplicableUsageExtraError):
            service.get_current_usage(
                organization,
                RESOURCE_CALENDARS,
                usage_extra={EXCLUDE_INVITATION_ID: invitation.pk},
            )

    def test_the_exclusion_does_not_hide_other_pending_invitations(
        self, service, organization, subscription
    ):
        """It must exclude exactly one invitation, not all of them."""
        make_limit(subscription, ORGANIZATION_MEMBERS, 5)
        baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=3)
        accepted = self._make_pending_invitation(organization)
        self._make_pending_invitation(organization)

        result = service.check_limit(
            organization,
            ORGANIZATION_MEMBERS,
            usage_extra={EXCLUDE_INVITATION_ID: accepted.pk},
        )

        assert result.current_usage == 4
        assert result.allowed is True


@pytest.mark.django_db
class TestHasEntitlement:
    def test_enabled_entitlement_is_granted(self, service, organization, subscription):
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=PARTNER_API,
            is_enabled=True,
        )

        assert service.has_entitlement(organization, PARTNER_API) is True

    def test_disabled_entitlement_is_denied(self, service, organization, subscription):
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=PARTNER_API,
            is_enabled=False,
        )

        assert service.has_entitlement(organization, PARTNER_API) is False

    def test_missing_entitlement_row_is_denied(self, service, organization, subscription):
        """Deliberately the opposite of the limits fail-open rule.

        ``SubscriptionService._sync_entitlements`` *deletes* rows for entitlements
        the current plan does not carry, so absence is exactly how a revoked grant
        is represented. Failing open here would hand every paid feature to every
        organization whose plan omits it.
        """
        assert service.has_entitlement(organization, WHITE_LABEL_BRANDING) is False

    def test_missing_subscription_denies(self, service, organization):
        """A missing subscription denies, and deliberately does **not** mirror
        ``get_effective_limit``'s fail-open on the same condition.

        The two are only superficially the same "we don't know". For a numeric
        ceiling, not knowing resolves to ``limit_value=None`` -- unlimited --
        which is what the rollout actually seeds, so failing open there changes
        nothing. For a boolean gate, not knowing would resolve to *granted*, which
        hands out paid features the ``free`` plan does not carry. Same phrase,
        opposite consequence.

        Rollout safety does not depend on this branch either: migration
        ``0009_backfill_unlimited_subscriptions`` plus
        ``is_default_for_new_organizations`` put every billing root on
        ``unlimited``, whose ``_sync_entitlements`` writes every entitlement
        enabled. This branch only fires once that invariant is already broken --
        e.g. ops deleting a ``Subscription`` to re-provision an organization -- and
        failing open would grant the *most* access to exactly the organizations
        whose billing state is corrupt. A loud, recoverable 402 is the better
        failure.
        """
        assert service.has_entitlement(organization, PARTNER_API) is False
