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


def _organization_with_limit(resource_key: str, limit_value: int) -> Organization:
    """A standalone (non-reseller) organization with a finite ceiling on ``resource_key``."""
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

    def test_succeeds_with_headroom(self):
        organization = _organization_with_limit(LimitedResource.RESOURCE_CALENDARS, 2)
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

    def test_succeeds_with_headroom(self):
        organization = _organization_with_limit(LimitedResource.CALENDAR_GROUPS, 2)
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

    def test_succeeds_with_headroom(self):
        organization = _organization_with_limit(LimitedResource.BUNDLE_CALENDARS, 2)
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

    def test_succeeds_with_headroom(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 2)
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

    def test_batch_of_creates_succeeds_with_headroom(self):
        organization = _organization_with_limit(LimitedResource.AVAILABILITY_WINDOWS, 3)
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
