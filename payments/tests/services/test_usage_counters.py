"""Per-counter semantics for ``EntitlementService.get_current_usage``.

``test_every_limited_resource_has_a_counter`` proves each ``LimitedResource``
member is *registered*. It says nothing about whether the registered counter counts
the right rows — and for the two counters whose semantics were not obvious
(``availability_windows`` and ``public_api_system_users``), getting that wrong is an
over-report, and an over-report is a lockout *below* real usage.

The availability tests deliberately drive the **real** ``AvailabilityService``
rather than hand-building ``AvailableTime`` rows: the whole defect was that editing
a recurring window silently *inserts* rows, which a hand-built fixture would never
reproduce.

``TestUsageBreakdown`` at the bottom of this file is the Phase 1 regression gate
for the billing usage summary & ledger plan: every ``UsageCounter`` now returns a
per-organization ``dict[int, int]`` instead of a scalar, and that class is what
proves the grouping arithmetic behind both ``get_usage_breakdown`` and
``get_current_usage`` is correct, for every ``LimitedResource`` member, over a
pool with contributions from several organizations. It pins each resource's
breakdown to hard-coded literals rather than comparing the breakdown to the
total — see that class's docstring for why the latter would be vacuous.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.utils import timezone

import pytest
from model_bakery import baker

from audit.services import AuditService
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
)
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.recurrence_manager import RecurrenceManager
from calendar_integration.tests.services.test_availability_service import FakeHost
from payments.billing_constants import LimitedResource
from payments.exceptions import InapplicableInvitationExclusionError
from payments.models import MeteredOccurrence, Subscription
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import current_billing_period_start
from public_api.models import SystemUser
from tenancy.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from users.models import Profile, User
from webhooks.models import WebhookConfiguration


@pytest.fixture
def entitlement_service() -> EntitlementService:
    return EntitlementService()


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Usage Counter Org")


@pytest.fixture
def user(db: Any, organization: Organization) -> User:
    account = User.objects.create_user(email="usage_counters@example.com", password="pass")
    Profile.objects.create(user=account)
    OrganizationMembership.objects.create(
        user=account, organization=organization, role=OrganizationRole.ADMIN
    )
    return account


@pytest.fixture
def audit_service() -> AuditService:
    from di_core.containers import container

    assert container is not None
    return container.audit_service()


@pytest.fixture
def managed_calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Usage Counter Calendar",
        external_id="usage_counter_cal",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
        manage_available_windows=True,
    )


@pytest.fixture
def availability_service(
    organization: Organization, user: User, audit_service: AuditService
) -> AvailabilityService:
    context = CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
        audit_service=audit_service,
    )
    return AvailabilityService(
        context=context,
        recurrence_manager=RecurrenceManager(),
        host=FakeHost(organization=organization),
    )


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
class TestAvailabilityWindowCounter:
    def test_a_plain_window_counts_once(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_a_recurring_window_counts_once_regardless_of_its_occurrences(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """Occurrences are computed in Postgres, not stored, so this is the baseline
        the modified-occurrence case below is measured against."""
        availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_editing_one_occurrence_does_not_add_a_window(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """``create_recurring_available_time_exception`` implements "edit this one
        occurrence" by calling ``create_available_time`` — i.e. by **inserting a
        second row**. Counting every ``AvailableTime`` row therefore reported 2 for
        one window the user created. An organization on a limit of 5 that created 3
        recurring windows and edited 3 occurrences would read as 6 and be blocked
        from creating its 4th, which is a lockout *below* its real usage.
        """
        parent = availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_available_time_exception(
            parent_available_time=parent,
            exception_date=datetime.date(2025, 7, 3),
            modified_start_time=_utc(2025, 7, 3, 14),
            modified_end_time=_utc(2025, 7, 3, 16),
            is_cancelled=False,
        )

        # The extra row genuinely exists -- this is not a test that passes because
        # the edit did nothing.
        assert AvailableTime.objects.filter(organization_id=organization.pk).count() > 1, (
            "Expected the modified occurrence to have inserted a derived row."
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        ), (
            "A window whose occurrence was edited must still count as one window. "
            "The counter is counting recurrence-derived rows."
        )

    def test_cancelling_one_occurrence_does_not_add_a_window(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        parent = availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_available_time_exception(
            parent_available_time=parent,
            exception_date=datetime.date(2025, 7, 3),
            is_cancelled=True,
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_splitting_a_series_does_not_add_a_window(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """A bulk modification splits the series and inserts a continuation row,
        linked by ``bulk_modification_parent``. Still one window to the user."""
        parent = availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_available_time_bulk_modification(
            parent_available_time=parent,
            modification_start_date=_utc(2025, 7, 5, 10),
            modified_start_time_offset=datetime.timedelta(hours=4),
            modified_end_time_offset=datetime.timedelta(hours=4),
        )

        assert AvailableTime.objects.filter(organization_id=organization.pk).count() > 1, (
            "Expected the bulk modification to have inserted a continuation row."
        )
        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_two_independent_windows_count_twice(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """The exclusion must not swallow genuinely separate windows."""
        for hour in (10, 14):
            availability_service.create_available_time(
                calendar=managed_calendar,
                start_time=_utc(2025, 7, 1, hour),
                end_time=_utc(2025, 7, 1, hour + 1),
                timezone="UTC",
            )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 2
        )


@pytest.mark.django_db
class TestBlockedTimeCounter:
    """Phase 2c: blocked time is metered on the same ``availability_windows``
    counter as availability windows, base and group-scoped rows alike.

    Mirrors ``TestAvailabilityWindowCounter`` exactly -- the two models share
    ``RecurringMixin``, so the same recurrence-derived-row defect and the same
    ``only_user_authored`` fix apply to both.
    """

    def test_a_plain_block_counts_once(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_a_recurring_block_counts_once_regardless_of_its_occurrences(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_editing_one_occurrence_does_not_inflate_the_block_count(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """``create_recurring_blocked_time_exception`` implements "edit this one
        occurrence" by *inserting a second row*, exactly like its availability-window
        counterpart. Counting every ``BlockedTime`` row would over-report."""
        parent = availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_blocked_time_exception(
            parent_blocked_time=parent,
            exception_date=datetime.date(2025, 7, 3),
            modified_start_time=_utc(2025, 7, 3, 14),
            modified_end_time=_utc(2025, 7, 3, 16),
            is_cancelled=False,
        )

        # The extra row genuinely exists -- this is not a test that passes because
        # the edit did nothing.
        assert BlockedTime.objects.filter(organization_id=organization.pk).count() > 1, (
            "Expected the modified occurrence to have inserted a derived row."
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        ), (
            "A block whose occurrence was edited must still count as one block. "
            "The counter is counting recurrence-derived rows."
        )

    def test_cancelling_one_occurrence_does_not_inflate_the_block_count(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        parent = availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_blocked_time_exception(
            parent_blocked_time=parent,
            exception_date=datetime.date(2025, 7, 3),
            is_cancelled=True,
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_splitting_a_series_does_not_inflate_the_block_count(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """A bulk modification splits the series and inserts a continuation row,
        linked by ``bulk_modification_parent``. Still one block to the user."""
        parent = availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_blocked_time_bulk_modification(
            parent_blocked_time=parent,
            modification_start_date=_utc(2025, 7, 5, 10),
            modified_start_time_offset=datetime.timedelta(hours=4),
            modified_end_time_offset=datetime.timedelta(hours=4),
        )

        assert BlockedTime.objects.filter(organization_id=organization.pk).count() > 1, (
            "Expected the bulk modification to have inserted a continuation row."
        )
        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_two_independent_blocks_count_twice(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        for hour in (10, 14):
            availability_service.create_blocked_time(
                calendar=managed_calendar,
                start_time=_utc(2025, 7, 1, hour),
                end_time=_utc(2025, 7, 1, hour + 1),
                timezone="UTC",
            )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 2
        )

    def test_group_scoped_blocks_count_alongside_base_blocks(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """``BlockedTime.objects`` (the default manager) excludes group-scoped rows
        by design (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 0). The counter must
        read through ``unscoped()`` or a group-scoped block would bypass metering
        entirely -- the spec's rule is "every time window is metered" regardless of
        scope."""
        availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
        )
        group = baker.make(CalendarGroup, organization=organization)
        slot = baker.make(CalendarGroupSlot, organization=organization, group=group)
        baker.make(
            BlockedTime,
            organization=organization,
            calendar=managed_calendar,
            timezone="UTC",
            group_slot=slot,
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 2
        )

    def test_availability_windows_and_blocked_time_count_together(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """The one shared ``availability_windows`` ceiling covers both models -- one
        rule for every authored time window, positive or negative (Phase 2c)."""
        availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
        )
        availability_service.create_blocked_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 2, 10),
            end_time=_utc(2025, 7, 2, 12),
            timezone="UTC",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 2
        )


@pytest.mark.django_db
class TestPublicApiSystemUserCounter:
    def _make_system_user(self, organization, suffix, **kwargs):
        return SystemUser.objects.create(
            organization=organization,
            integration_name=f"integration-{suffix}",
            long_lived_token_hash=f"hash-{suffix}",
            **kwargs,
        )

    def test_counts_only_live_system_users(self, entitlement_service, organization: Organization):
        """Both off-switches free capacity. ``is_active=False`` is a revoked token
        and ``deleted_at`` is the soft delete; a token in either state can no longer
        authenticate, so charging for it would make revoking one pointless."""
        self._make_system_user(organization, "live", is_active=True, deleted_at=None)
        self._make_system_user(organization, "revoked", is_active=False, deleted_at=None)
        self._make_system_user(organization, "deleted", is_active=True, deleted_at=timezone.now())

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
            )
            == 1
        )

    def test_another_organizations_system_users_do_not_leak_in(
        self, entitlement_service, organization: Organization
    ):
        other = Organization.objects.create(name="Someone Else")
        self._make_system_user(organization, "mine")
        self._make_system_user(other, "theirs")

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
            )
            == 1
        )

    def test_an_organizationless_system_user_is_invisible(
        self, entitlement_service, organization: Organization
    ):
        """``SystemUser.organization`` is nullable. Such a token belongs to no
        billing root, so it consumes nobody's capacity — correct for pooling, but it
        does mean it is entirely unmetered. Pinned here so that whoever makes the
        column non-nullable has to revisit this deliberately."""
        self._make_system_user(None, "orphan")

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
            )
            == 0
        )


@pytest.fixture
def pooled_subtree(db: Any) -> tuple[Organization, Organization, Organization]:
    """A billing root with two direct children, none of which hold their own
    ``Subscription`` — ``root``'s auto-provisioned ``Subscription`` (see
    ``conftest.provision_default_subscription``) is the one every resource in this
    pool checks against.
    """
    root = Organization.objects.create(name="Breakdown Root", can_invite_organizations=True)
    child_a = Organization.objects.create(name="Breakdown Child A", parent=root)
    child_b = Organization.objects.create(name="Breakdown Child B", parent=root)
    return root, child_a, child_b


@pytest.mark.django_db
class TestUsageBreakdown:
    """The regression gate for Phase 1 of the billing usage summary & ledger plan.

    ``UsageCounter`` was widened from ``Callable[[UsageContext], int]`` to
    ``Callable[[UsageContext], dict[int, int]]`` so the per-organization
    attribution the usage-summary read surface needs can be derived from the
    *same* counters enforcement already uses, instead of a second, parallel
    definition of "what counts as usage" that could eventually disagree with the
    first.

    ``test_breakdown_sums_to_the_total_for_every_limited_resource`` pins each
    resource's breakdown to hard-coded literals (``EXPECTED_BREAKDOWNS`` in that
    test), not to a value computed from the code under test. Asserting
    ``sum(breakdown.values()) == total`` instead would be vacuous:
    ``get_current_usage`` is *defined* as ``sum(get_usage_breakdown(...).values())``
    (see ``EntitlementService._count_usage``), so both sides are the same
    computation and can never disagree, no matter how ``_group_counts_by_organization``
    is broken. A bug that drops every group but the first would shrink both sides
    together and the assertion would stay green. Pinning to a literal is the only
    way this test can fail when the grouping arithmetic is wrong — do not
    "simplify" it back to a breakdown-vs-total comparison.
    """

    def test_breakdown_sums_to_the_total_for_every_limited_resource(
        self,
        entitlement_service: EntitlementService,
        pooled_subtree: tuple[Organization, Organization, Organization],
    ):
        root, child_a, child_b = pooled_subtree
        subscription = Subscription.objects.get(organization=root)

        # organization_members: memberships on two organizations, a pending
        # invitation on the third. child_a already carries >1 row, so this
        # resource clears the "multi-row in one organization, across multiple
        # organizations" bar (see the bundle_calendars/webhook_subscriptions/
        # public_api_system_users comments below for why that bar matters).
        baker.make(OrganizationMembership, organization=root, is_active=True, _quantity=1)
        baker.make(OrganizationMembership, organization=child_a, is_active=True, _quantity=2)
        baker.make(
            OrganizationInvitation,
            organization=child_b,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # resource_calendars: root and child_a each end up with two RESOURCE
        # calendars once the availability/blocked-time calendars further down are
        # counted, so this resource also clears the multi-row/multi-organization
        # bar without a dedicated fixture of its own.
        baker.make(
            Calendar,
            organization=root,
            calendar_type=CalendarType.RESOURCE,
            external_id="breakdown-resource-root",
        )
        baker.make(
            Calendar,
            organization=child_a,
            calendar_type=CalendarType.RESOURCE,
            external_id="breakdown-resource-child-a",
        )

        # bundle_calendars: a single row on a single organization cannot tell a
        # grouping bug from correct behavior -- nothing else in the repo asserts
        # this resource at >=2 rows in one organization across >=2 organizations
        # (reviewer finding, BLOCKER 2). root gets two more bundle calendars so
        # one organization has >1 row and the resource still spans >1
        # organization.
        baker.make(
            Calendar,
            organization=child_b,
            calendar_type=CalendarType.BUNDLE,
            external_id="breakdown-bundle-child-b",
        )
        baker.make(
            Calendar,
            organization=root,
            calendar_type=CalendarType.BUNDLE,
            external_id="breakdown-bundle-root-1",
        )
        baker.make(
            Calendar,
            organization=root,
            calendar_type=CalendarType.BUNDLE,
            external_id="breakdown-bundle-root-2",
        )

        # calendar_groups
        baker.make(CalendarGroup, organization=root)
        baker.make(CalendarGroup, organization=child_b, _quantity=2)

        # availability_windows: AvailableTime and BlockedTime, base rows.
        # child_a gets both an AvailableTime and a BlockedTime so the two source
        # breakdowns actually share an organization key -- otherwise
        # ``_merge_breakdowns`` degrading to last-write-wins (``merged[org] =
        # count`` instead of ``+=``) would go uncaught, because no organization
        # would ever appear on both sides to expose the difference.
        available_calendar = baker.make(
            Calendar,
            organization=child_a,
            calendar_type=CalendarType.RESOURCE,
            external_id="breakdown-availability-child-a",
        )
        baker.make(AvailableTime, organization=child_a, calendar=available_calendar, timezone="UTC")
        baker.make(AvailableTime, organization=child_a, calendar=available_calendar, timezone="UTC")
        baker.make(BlockedTime, organization=child_a, calendar=available_calendar, timezone="UTC")
        blocked_calendar = baker.make(
            Calendar,
            organization=root,
            calendar_type=CalendarType.RESOURCE,
            external_id="breakdown-blocked-root",
        )
        baker.make(BlockedTime, organization=root, calendar=blocked_calendar, timezone="UTC")

        # webhook_subscriptions: same "single row, single organization" gap as
        # bundle_calendars (BLOCKER 2). child_a gets two so one organization has
        # >1 row and the resource still spans >1 organization.
        baker.make(WebhookConfiguration, organization=child_b, deleted_at=None)
        baker.make(WebhookConfiguration, organization=child_a, deleted_at=None, _quantity=2)

        # public_api_system_users: same gap again (BLOCKER 2). child_b gets two
        # so one organization has >1 row and the resource still spans >1
        # organization.
        SystemUser.objects.create(
            organization=child_a,
            integration_name="breakdown-integration",
            long_lived_token_hash="breakdown-hash",
        )
        SystemUser.objects.create(
            organization=child_b,
            integration_name="breakdown-integration-child-b-1",
            long_lived_token_hash="breakdown-hash-child-b-1",
        )
        SystemUser.objects.create(
            organization=child_b,
            integration_name="breakdown-integration-child-b-2",
            long_lived_token_hash="breakdown-hash-child-b-2",
        )

        # event_occurrences: same subscription, two organizations in the pool.
        # root gets two occurrences so one organization has >1 row.
        # event_id is a soft reference with no FK constraint; the per-organization
        # ranges (root: 1-2, child_b: 3) are chosen only to satisfy the unique
        # constraint on (organization, event_id, occurrence_start).
        period_start = current_billing_period_start(subscription)
        MeteredOccurrence.objects.create(
            organization=root,
            subscription=subscription,
            event_id=1,
            occurrence_start=period_start + datetime.timedelta(hours=1),
            billing_period_start=period_start,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )
        MeteredOccurrence.objects.create(
            organization=root,
            subscription=subscription,
            event_id=2,
            occurrence_start=period_start + datetime.timedelta(hours=2),
            billing_period_start=period_start,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )
        MeteredOccurrence.objects.create(
            organization=child_b,
            subscription=subscription,
            event_id=3,
            occurrence_start=period_start + datetime.timedelta(hours=3),
            billing_period_start=period_start,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )

        # Pinned to literals, deliberately -- see the class docstring for why
        # comparing the breakdown to the total instead would be vacuous. Every
        # ``LimitedResource`` member must have an entry: the loop below fails
        # loudly (``KeyError``) for a newly added resource with no pinned
        # expectation yet, rather than silently skipping it.
        expected_breakdowns: dict[str, dict[int, int]] = {
            LimitedResource.ORGANIZATION_MEMBERS: {root.pk: 1, child_a.pk: 2, child_b.pk: 1},
            LimitedResource.RESOURCE_CALENDARS: {root.pk: 2, child_a.pk: 2},
            LimitedResource.CALENDAR_GROUPS: {root.pk: 1, child_b.pk: 2},
            LimitedResource.BUNDLE_CALENDARS: {child_b.pk: 1, root.pk: 2},
            LimitedResource.AVAILABILITY_WINDOWS: {child_a.pk: 3, root.pk: 1},
            LimitedResource.WEBHOOK_SUBSCRIPTIONS: {child_b.pk: 1, child_a.pk: 2},
            LimitedResource.PUBLIC_API_SYSTEM_USERS: {child_a.pk: 1, child_b.pk: 2},
            LimitedResource.EVENT_OCCURRENCES: {root.pk: 2, child_b.pk: 1},
        }

        for resource_key in LimitedResource:
            expected = expected_breakdowns[resource_key]
            breakdown = entitlement_service.get_usage_breakdown(root, resource_key)
            total = entitlement_service.get_current_usage(root, resource_key)

            assert breakdown == expected, (
                f"{resource_key}: expected breakdown {expected}, got {breakdown}."
            )
            assert total == sum(expected.values()), (
                f"{resource_key}: get_current_usage reported {total}, expected "
                f"{sum(expected.values())} (sum of the pinned breakdown)."
            )

    def test_a_non_contributing_organization_is_absent_not_present_with_zero(
        self,
        entitlement_service: EntitlementService,
        pooled_subtree: tuple[Organization, Organization, Organization],
    ):
        root, child_a, child_b = pooled_subtree
        baker.make(CalendarGroup, organization=root)

        breakdown = entitlement_service.get_usage_breakdown(root, LimitedResource.CALENDAR_GROUPS)

        assert breakdown == {root.pk: 1}
        assert child_a.pk not in breakdown
        assert child_b.pk not in breakdown

    def test_exclude_invitation_id_seat_exclusion_still_applies_to_the_breakdown(
        self,
        entitlement_service: EntitlementService,
        pooled_subtree: tuple[Organization, Organization, Organization],
    ):
        """The accept-path net-zero rule (``UsageContext.exclude_invitation_id``)
        must survive the widening exactly: excluding the invitation being accepted
        removes it from the breakdown, not just from the summed total."""
        root, child_a, _child_b = pooled_subtree
        baker.make(OrganizationMembership, organization=child_a, is_active=True)
        invitation = baker.make(
            OrganizationInvitation,
            organization=child_a,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        breakdown_with_invitation = entitlement_service.get_usage_breakdown(
            root, LimitedResource.ORGANIZATION_MEMBERS
        )
        assert breakdown_with_invitation == {child_a.pk: 2}

        breakdown_excluding_invitation = entitlement_service.get_usage_breakdown(
            root,
            LimitedResource.ORGANIZATION_MEMBERS,
            exclude_invitation_id=invitation.pk,
        )
        assert breakdown_excluding_invitation == {child_a.pk: 1}

    def test_exclude_invitation_id_raises_for_any_other_resource_key(
        self,
        entitlement_service: EntitlementService,
        pooled_subtree: tuple[Organization, Organization, Organization],
    ):
        root, child_a, _child_b = pooled_subtree
        invitation = baker.make(
            OrganizationInvitation,
            organization=child_a,
            accepted_at=None,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        with pytest.raises(InapplicableInvitationExclusionError):
            entitlement_service.get_usage_breakdown(
                root,
                LimitedResource.CALENDAR_GROUPS,
                exclude_invitation_id=invitation.pk,
            )
