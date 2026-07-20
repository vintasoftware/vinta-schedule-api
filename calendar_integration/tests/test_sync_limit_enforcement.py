"""Phase 6b integration: the bulk resource-calendar import writer.

The plan's Review models note for this phase calls the bulk sync writers "the
single most likely place for an unmetered path to survive" objective 1. These
tests exercise ``CalendarSyncService._execute_organization_calendar_resources_import``
directly -- the same seam identified there -- proving:

* headroom is checked *before* the bulk write, not per-row after it;
* an org with headroom for N rooms importing more than N gets exactly N, plus a
  recorded partial-import warning, rather than the whole sync failing;
* re-running the import against already-imported rooms does not double-count
  (a re-sync is an update, not a create, and consumes no headroom);
* an org on the ``unlimited`` plan imports everything, unchanged.
"""

import datetime
from unittest.mock import MagicMock

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarType
from calendar_integration.models import Calendar, CalendarOrganizationResourcesImport
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import CalendarResourceData
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService
from users.models import User


class FakeHost:
    """Minimal SyncServiceHost double -- records ``request_calendar_sync`` calls.

    Mirrors ``calendar_integration/tests/services/test_calendar_sync_service.py``'s
    ``FakeHost``, trimmed to what this module exercises.
    """

    def __init__(self) -> None:
        self.request_calendar_sync_calls: list[Calendar] = []

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self, *args, **kwargs
    ) -> None:
        pass

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None:
        pass

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source=None,
    ):
        self.request_calendar_sync_calls.append(calendar)
        return None

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        import_workflow_state=None,
        bypass_limits: bool = False,
    ):
        return []


def _organization_with_resource_calendar_limit(limit_value: int | None) -> Organization:
    """A standalone organization with a ``resource_calendars`` ceiling.

    ``limit_value=None`` builds an ``unlimited``-shaped subscription (NULL ceiling),
    matching the plan's "unlimited plan is the switch" rollout rule.
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
        resource_key=LimitedResource.RESOURCE_CALENDARS,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )
    return organization


def _make_resources(count: int, prefix: str = "room") -> list[CalendarResourceData]:
    return [
        CalendarResourceData(
            name=f"Room {i}",
            description="",
            provider="google",
            external_id=f"{prefix}_{i}",
            email=f"{prefix}_{i}@example.com",
            capacity=4,
        )
        for i in range(count)
    ]


def _make_service(
    organization: Organization, resources: list[CalendarResourceData]
) -> tuple[CalendarSyncService, FakeHost]:
    # get_or_create: this helper is called more than once per organization in the
    # re-run tests below, and the user identity is irrelevant to what is under test.
    user, _created = User.objects.get_or_create(
        email=f"sync-{organization.pk}@example.com",
        defaults={"password": "pass"},  # noqa: S106
    )
    fake_adapter = MagicMock()
    fake_adapter.provider = "google"
    fake_adapter.get_available_calendar_resources.return_value = resources

    context = CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
        entitlement_service=EntitlementService(),
    )
    host = FakeHost()
    return CalendarSyncService(context=context, calendar_cache={}, host=host), host


@pytest.mark.django_db
class TestPartialResourceCalendarImport:
    def test_headroom_for_two_importing_ten_gets_exactly_two_and_a_recorded_warning(self):
        organization = _organization_with_resource_calendar_limit(2)
        resources = _make_resources(10)
        service, host = _make_service(organization, resources)
        import_state = baker.make(
            CalendarOrganizationResourcesImport,
            organization=organization,
        )
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        result = service._execute_organization_calendar_resources_import(
            start, end, import_workflow_state=import_state
        )

        # All 10 discovered resources are returned (what the provider reported)...
        assert len(list(result)) == 10
        # ...but only 2 were actually persisted as RESOURCE calendars.
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 2
        )
        assert len(host.request_calendar_sync_calls) == 2

        import_state.refresh_from_db()
        assert import_state.error_message
        assert "2 of 10" in import_state.error_message

    def test_no_headroom_imports_zero_and_does_not_raise_to_the_caller(self):
        organization = _organization_with_resource_calendar_limit(0)
        resources = _make_resources(5)
        service, host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        # Must not raise -- a sync into an org with no headroom creates zero
        # calendars and reports back to the caller rather than blowing up the sync.
        result = service._execute_organization_calendar_resources_import(start, end)

        assert len(list(result)) == 5
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 0
        )
        assert host.request_calendar_sync_calls == []

    def test_rerunning_the_import_is_not_double_counted(self):
        """A re-sync of already-imported rooms is an update, not a create, and must
        not consume headroom again -- the ceiling stays at exactly what was first
        imported, run after run."""
        organization = _organization_with_resource_calendar_limit(2)
        resources = _make_resources(10)
        service, _host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        service._execute_organization_calendar_resources_import(start, end)
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 2
        )

        # Re-run against the identical 10-resource discovery (the 2 already-imported
        # rooms plus the 8 that were skipped the first time).
        service2, _host2 = _make_service(organization, resources)
        service2._execute_organization_calendar_resources_import(start, end)

        # Still exactly 2 -- the re-sync updated the 2 existing rows and could not
        # afford any of the remaining 8 new ones (still no headroom), and critically
        # did NOT count the 2 already-imported rooms as new demand.
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 2
        )

    def test_rerunning_an_exact_match_import_consumes_no_headroom(self):
        """When every discovered resource is already imported, the re-run must skip
        the headroom check entirely (no new_resources -> no check_limit call) and
        resync all of them."""
        organization = _organization_with_resource_calendar_limit(2)
        resources = _make_resources(2)
        service, _host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        service._execute_organization_calendar_resources_import(start, end)
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 2
        )

        service2, host2 = _make_service(organization, resources)
        service2._execute_organization_calendar_resources_import(start, end)

        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 2
        )
        # Both already-imported rooms were re-synced (not silently dropped).
        assert len(host2.request_calendar_sync_calls) == 2

    def test_bypass_limits_imports_everything_regardless_of_headroom(self):
        organization = _organization_with_resource_calendar_limit(1)
        resources = _make_resources(4)
        service, host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        service._execute_organization_calendar_resources_import(start, end, bypass_limits=True)

        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 4
        )
        assert len(host.request_calendar_sync_calls) == 4


@pytest.mark.django_db
class TestUnlimitedPlanFullSyncIsUnchanged:
    """The plan's own rollout switch: an org on ``unlimited`` must import
    everything a full sync discovers, with no change in behavior."""

    def test_unlimited_org_imports_all_discovered_resources(self):
        organization = _organization_with_resource_calendar_limit(None)
        resources = _make_resources(25)
        service, host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        result = service._execute_organization_calendar_resources_import(start, end)

        assert len(list(result)) == 25
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 25
        )
        assert len(host.request_calendar_sync_calls) == 25
