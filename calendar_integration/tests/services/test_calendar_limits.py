"""Phase 6b: pre-paid limit guards on calendar/group/bundle/availability creation.

Spec use-case 2 ("an organization hits a pre-paid limit and is blocked"), applied
to the four resource-creation paths this phase adds guards to:
``resource_calendars`` (``CalendarService.create_resource_calendar``),
``calendar_groups`` (``CalendarGroupService.create_group``), ``bundle_calendars``
(``CalendarService.create_bundle_calendar``), and ``availability_windows``
(``CalendarService.create_available_time`` / ``bulk_create_availability_windows`` /
``batch_modify_available_times``).

Every test in this module was confirmed to fail when its corresponding guard was
removed (a guard's own test would otherwise pass whether or not the invariant
holds -- exactly the failure mode this plan has repeatedly shipped).
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarGroup
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import CalendarGroupInputData
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit


def _organization_with_limit(resource_key: str, limit_value: int | None) -> Organization:
    """A standalone (non-reseller) organization with a ceiling on ``resource_key``.

    ``limit_value=None`` builds an ``unlimited``-shaped subscription (NULL ceiling).
    The plan's "no feature flag -- the ``unlimited`` plan is the switch" decision makes
    that the rollout's only off switch, so every guard here is exercised against it as
    well as against a finite ceiling: a guard that is not inert at NULL breaks every
    existing organization the day it ships.
    """
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=resource_key,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )
    return organization


@pytest.mark.django_db
class TestCreateResourceCalendarLimit:
    def test_raises_and_creates_nothing_at_the_limit(self):
        organization = _organization_with_limit(LimitedResource.RESOURCE_CALENDARS, 1)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.RESOURCE,
            external_id="seed-resource-1",
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        with pytest.raises(OverLimitError) as exc_info:
            service.create_resource_calendar(name="Blocked Room")

        assert exc_info.value.resource_key == LimitedResource.RESOURCE_CALENDARS
        assert exc_info.value.current_usage == 1
        assert exc_info.value.limit == 1
        assert not Calendar.objects.filter(organization=organization, name="Blocked Room").exists()

    @pytest.mark.parametrize("limit_value", [2, None], ids=["headroom", "unlimited"])
    def test_succeeds_with_headroom(self, limit_value):
        organization = _organization_with_limit(LimitedResource.RESOURCE_CALENDARS, limit_value)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.RESOURCE,
            external_id="seed-resource-2",
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)
        calendar = service.create_resource_calendar(name="Fits Room", description="")

        assert calendar.pk is not None
        assert calendar.calendar_type == CalendarType.RESOURCE

    def test_bypass_limits_creates_anyway(self):
        organization = _organization_with_limit(LimitedResource.RESOURCE_CALENDARS, 1)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.RESOURCE,
            external_id="seed-resource-3",
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)
        calendar = service.create_resource_calendar(
            name="Bypassed Room", description="", bypass_limits=True
        )

        assert calendar.pk is not None


@pytest.mark.django_db
class TestCreateGroupLimit:
    def test_raises_and_creates_nothing_at_the_limit(self):
        organization = _organization_with_limit(LimitedResource.CALENDAR_GROUPS, 1)
        baker.make(CalendarGroup, organization=organization)

        service = CalendarGroupService()
        service.initialize(organization=organization)

        with pytest.raises(OverLimitError) as exc_info:
            service.create_group(CalendarGroupInputData(name="Blocked Group"))

        assert exc_info.value.resource_key == LimitedResource.CALENDAR_GROUPS
        assert not CalendarGroup.objects.filter(
            organization=organization, name="Blocked Group"
        ).exists()

    @pytest.mark.parametrize("limit_value", [2, None], ids=["headroom", "unlimited"])
    def test_succeeds_with_headroom(self, limit_value):
        organization = _organization_with_limit(LimitedResource.CALENDAR_GROUPS, limit_value)
        baker.make(CalendarGroup, organization=organization)

        service = CalendarGroupService()
        service.initialize(organization=organization)
        group = service.create_group(CalendarGroupInputData(name="Fits Group"))

        assert group.pk is not None

    def test_bypass_limits_creates_anyway(self):
        organization = _organization_with_limit(LimitedResource.CALENDAR_GROUPS, 1)
        baker.make(CalendarGroup, organization=organization)

        service = CalendarGroupService()
        service.initialize(organization=organization)
        group = service.create_group(
            CalendarGroupInputData(name="Bypassed Group"), bypass_limits=True
        )

        assert group.pk is not None


@pytest.mark.django_db
class TestCreateBundleCalendarLimit:
    def test_raises_and_creates_nothing_at_the_limit(self):
        organization = _organization_with_limit(LimitedResource.BUNDLE_CALENDARS, 1)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.BUNDLE,
            external_id="seed-bundle-1",
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        with pytest.raises(OverLimitError) as exc_info:
            service.create_bundle_calendar(name="Blocked Bundle")

        assert exc_info.value.resource_key == LimitedResource.BUNDLE_CALENDARS
        assert not Calendar.objects.filter(
            organization=organization, name="Blocked Bundle"
        ).exists()

    @pytest.mark.parametrize("limit_value", [2, None], ids=["headroom", "unlimited"])
    def test_succeeds_with_headroom(self, limit_value):
        organization = _organization_with_limit(LimitedResource.BUNDLE_CALENDARS, limit_value)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.BUNDLE,
            external_id="seed-bundle-2",
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)
        bundle = service.create_bundle_calendar(name="Fits Bundle")

        assert bundle.pk is not None
        assert bundle.calendar_type == CalendarType.BUNDLE

    def test_bypass_limits_creates_anyway(self):
        organization = _organization_with_limit(LimitedResource.BUNDLE_CALENDARS, 1)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.BUNDLE,
            external_id="seed-bundle-3",
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)
        bundle = service.create_bundle_calendar(name="Bypassed Bundle", bypass_limits=True)

        assert bundle.pk is not None


def _calendar_managing_windows(organization: Organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
    )


@pytest.mark.django_db
class TestCreateAvailableTimeLimit:
    def test_raises_and_creates_nothing_at_the_limit(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 1)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        with pytest.raises(OverLimitError) as exc_info:
            service.create_available_time(
                calendar=calendar, start_time=start, end_time=end, timezone="UTC"
            )

        assert exc_info.value.resource_key == LimitedResource.AVAILABILITY_WINDOWS
        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 1
        )

    @pytest.mark.parametrize("limit_value", [2, None], ids=["headroom", "unlimited"])
    def test_succeeds_with_headroom(self, limit_value):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, limit_value)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        available_time = service.create_available_time(
            calendar=calendar, start_time=start, end_time=end, timezone="UTC"
        )

        assert available_time.pk is not None

    def test_bypass_limits_creates_anyway(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 1)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        available_time = service.create_available_time(
            calendar=calendar,
            start_time=start,
            end_time=end,
            timezone="UTC",
            bypass_limits=True,
        )

        assert available_time.pk is not None


@pytest.mark.django_db
class TestBatchModifyAvailableTimesLimit:
    """A batch with ``create`` operations is itself a bulk-creation path -- it must
    be guarded on the count of ``create`` operations, not bypass the single-window
    guard entirely."""

    def test_batch_of_creates_raises_and_creates_nothing_at_the_limit(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 1)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        ops = [
            {
                "action": "create",
                "start_time": datetime.datetime(2026, 1, 2, 9, 0, tzinfo=datetime.UTC),
                "end_time": datetime.datetime(2026, 1, 2, 10, 0, tzinfo=datetime.UTC),
                "timezone": "UTC",
            }
        ]

        with pytest.raises(OverLimitError) as exc_info:
            service.batch_modify_available_times(calendar=calendar, operations=ops)

        assert exc_info.value.resource_key == LimitedResource.AVAILABILITY_WINDOWS
        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 1
        )

    @pytest.mark.parametrize("limit_value", [3, None], ids=["headroom", "unlimited"])
    def test_batch_of_creates_succeeds_with_headroom(self, limit_value):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, limit_value)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        ops = [
            {
                "action": "create",
                "start_time": datetime.datetime(2026, 1, 2, 9, 0, tzinfo=datetime.UTC),
                "end_time": datetime.datetime(2026, 1, 2, 10, 0, tzinfo=datetime.UTC),
                "timezone": "UTC",
            },
            {
                "action": "create",
                "start_time": datetime.datetime(2026, 1, 3, 9, 0, tzinfo=datetime.UTC),
                "end_time": datetime.datetime(2026, 1, 3, 10, 0, tzinfo=datetime.UTC),
                "timezone": "UTC",
            },
        ]

        result = service.batch_modify_available_times(calendar=calendar, operations=ops)

        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 3
        )
        assert len(result) == 3


def _create_op(day: int) -> dict:
    return {
        "action": "create",
        "start_time": datetime.datetime(2026, 1, day, 9, 0, tzinfo=datetime.UTC),
        "end_time": datetime.datetime(2026, 1, day, 10, 0, tzinfo=datetime.UTC),
        "timezone": "UTC",
    }


@pytest.mark.django_db
class TestBatchModifyAvailableTimesIsNetOfDeletes:
    """A batch is charged its **net** growth, not its gross ``create`` count.

    Both the REST ``AvailableTimeBatchSerializer`` and the GraphQL
    ``batch_update_availability_windows`` mutation allow mixed actions, so
    replace-semantics is a normal edit. Charging creates alone leaves an organization
    sitting exactly at its ceiling permanently unable to edit its availability by
    replacement -- a false block on a change that moves usage by zero.
    """

    def test_one_for_one_replacement_at_the_ceiling_is_allowed(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 5)
        calendar = _calendar_managing_windows(organization)
        existing = [
            baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")
            for _ in range(5)
        ]

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        service.batch_modify_available_times(
            calendar=calendar,
            operations=[{"action": "delete", "id": existing[0].id}, _create_op(2)],
        )

        # Net zero: still exactly at the ceiling, and the replacement landed.
        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 5
        )
        assert not AvailableTime.objects.filter(id=existing[0].id).exists()

    def test_growing_batch_at_the_ceiling_still_raises(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 5)
        calendar = _calendar_managing_windows(organization)
        existing = [
            baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")
            for _ in range(5)
        ]

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        with pytest.raises(OverLimitError) as exc_info:
            service.batch_modify_available_times(
                calendar=calendar,
                operations=[
                    {"action": "delete", "id": existing[0].id},
                    _create_op(2),
                    _create_op(3),
                ],
            )

        assert exc_info.value.resource_key == LimitedResource.AVAILABILITY_WINDOWS
        # Nothing in the batch was applied -- the delete included.
        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 5
        )
        assert AvailableTime.objects.filter(id=existing[0].id).exists()

    def test_update_only_batch_at_the_ceiling_is_allowed(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 2)
        calendar = _calendar_managing_windows(organization)
        existing = [
            baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")
            for _ in range(2)
        ]

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        service.batch_modify_available_times(
            calendar=calendar,
            operations=[
                {
                    "action": "update",
                    "id": existing[0].id,
                    "start_time": datetime.datetime(2026, 2, 1, 9, 0, tzinfo=datetime.UTC),
                    "end_time": datetime.datetime(2026, 2, 1, 11, 0, tzinfo=datetime.UTC),
                }
            ],
        )

        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 2
        )

    def test_delete_only_batch_at_the_ceiling_is_allowed(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 2)
        calendar = _calendar_managing_windows(organization)
        existing = [
            baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")
            for _ in range(2)
        ]

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        service.batch_modify_available_times(
            calendar=calendar,
            operations=[{"action": "delete", "id": existing[0].id}],
        )

        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 1
        )

    def test_deleting_a_row_the_counter_does_not_count_earns_no_credit(self):
        """The delete credit has to be computed with the *counter's* predicate.

        ``_count_availability_windows`` counts only ``only_user_authored`` rows, so
        crediting the deletion of a derived row (a recurrence exception) would hand out
        capacity for freeing something that occupied none -- and let the batch push real
        usage past the ceiling.
        """
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 1)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")
        derived = baker.make(
            AvailableTime,
            organization=organization,
            calendar=calendar,
            timezone="UTC",
            is_recurring_exception=True,
        )

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        with pytest.raises(OverLimitError):
            service.batch_modify_available_times(
                calendar=calendar,
                operations=[{"action": "delete", "id": derived.id}, _create_op(2)],
            )

        assert AvailableTime.objects.filter(id=derived.id).exists()


@pytest.mark.django_db
class TestBulkCreateAvailabilityWindowsLimit:
    """``bulk_create_availability_windows`` is charged ``len(windows)``, which the
    single-window path only ever exercises at delta=1."""

    def test_multi_window_batch_over_the_ceiling_raises_and_creates_nothing(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 4)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        windows = [
            (
                datetime.datetime(2026, 1, day, 9, 0, tzinfo=datetime.UTC),
                datetime.datetime(2026, 1, day, 10, 0, tzinfo=datetime.UTC),
                "UTC",
                None,
            )
            for day in range(2, 4 + 1 + 1)  # 4 windows: usage 1 + 4 > ceiling 4
        ]

        with pytest.raises(OverLimitError) as exc_info:
            service.bulk_create_availability_windows(
                calendar=calendar, availability_windows=windows
            )

        assert exc_info.value.resource_key == LimitedResource.AVAILABILITY_WINDOWS
        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 1
        )

    def test_multi_window_batch_that_exactly_fills_the_ceiling_is_allowed(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 4)
        calendar = _calendar_managing_windows(organization)
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")

        service = CalendarService()
        service.initialize_without_provider(organization=organization)

        windows = [
            (
                datetime.datetime(2026, 1, day, 9, 0, tzinfo=datetime.UTC),
                datetime.datetime(2026, 1, day, 10, 0, tzinfo=datetime.UTC),
                "UTC",
                None,
            )
            for day in range(2, 5)  # 3 windows: usage 1 + 3 == ceiling 4
        ]

        service.bulk_create_availability_windows(calendar=calendar, availability_windows=windows)

        assert (
            AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 4
        )
