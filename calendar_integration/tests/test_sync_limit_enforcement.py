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
* an upsert that **promotes** an existing non-RESOURCE calendar into the counted
  set consumes headroom, because the writer forces ``calendar_type=RESOURCE`` on
  whatever row it matches -- the split between "free" and "chargeable" is "will
  this write raise the usage counter?", asserted here as a property over every
  (type, visibility) combination rather than a handful of examples;
* a truncated import is distinguishable from a clean one (terminal status
  ``PARTIAL``, with a bounded warning);
* an org on the ``unlimited`` plan imports everything, unchanged.
"""

import datetime
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker

from calendar_integration.constants import (
    CalendarOrganizationResourceImportStatus,
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
)
from calendar_integration.models import Calendar, CalendarOrganizationResourcesImport
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import CalendarResourceData
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService
from users.models import Profile, User


class FakeHost:
    """Minimal SyncServiceHost double -- records ``request_calendar_sync`` calls.

    Mirrors ``calendar_integration/tests/services/test_calendar_sync_service.py``'s
    ``FakeHost``, trimmed to what this module exercises.
    """

    def __init__(self) -> None:
        self.request_calendar_sync_calls: list[Calendar] = []
        self._service: CalendarSyncService | None = None

    def bind(self, service: CalendarSyncService) -> None:
        """Route the host's executor hook back to the real service under test.

        ``import_organization_calendar_resources`` reaches the executor *through* the
        host, so a host that stubs it out turns a "full sync" test into a test of
        nothing. Binding lets the outer flow (status transitions, terminal status) be
        driven end to end against the real writer.
        """
        self._service = service

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
        if self._service is None:
            return []
        return self._service._execute_organization_calendar_resources_import(
            start_time,
            end_time,
            import_workflow_state=import_workflow_state,
            bypass_limits=bypass_limits,
        )


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
    service = CalendarSyncService(context=context, calendar_cache={}, host=host)
    host.bind(service)
    return service, host


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

        # The return value is what was imported, not what was discovered.
        assert len(list(result)) == 2
        # Exactly 2 were persisted as RESOURCE calendars.
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

        assert len(list(result)) == 0
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
class TestPromotingAnExistingCalendarConsumesHeadroom:
    """Regression: the split between "already imported" and "new" must be
    "will this write raise ``_count_resource_calendars``?", not "does *a* Calendar row
    exist for this org + external_id?".

    The writer keys ``update_or_create`` on ``(organization, external_id)`` and forces
    ``calendar_type=RESOURCE`` in its ``defaults``, so matching a PERSONAL row --
    which ``import_account_calendars`` creates on the very same key, and which for
    Microsoft shares an entire id space with the rooms listing -- **promotes** that row
    into the counted set. Classifying it as "already imported" consumed no headroom
    while raising usage, i.e. unbounded unmetered resource calendars.
    """

    def test_promoting_personal_calendars_consumes_headroom(self):
        organization = _organization_with_resource_calendar_limit(2)
        resources = _make_resources(5)
        # Three of the five rooms already exist as PERSONAL calendars, exactly as
        # import_account_calendars would have left them.
        for i in range(3):
            baker.make(
                Calendar,
                organization=organization,
                external_id=f"room_{i}",
                calendar_type=CalendarType.PERSONAL,
            )
        service, host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        service._execute_organization_calendar_resources_import(start, end)

        # Usage started at 0 with a ceiling of 2, and all five discovered rooms are
        # chargeable (three promotions plus two creates), so exactly two may land.
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 2
        )
        assert len(host.request_calendar_sync_calls) == 2

    def test_promotion_only_import_at_the_ceiling_promotes_nothing(self):
        """The early-return path: when every discovered room already has a row, the
        old split found no "new" resources at all and returned before ``check_limit``
        was ever called -- while the write loop retyped every one of them."""
        organization = _organization_with_resource_calendar_limit(0)
        resources = _make_resources(3)
        for i in range(3):
            baker.make(
                Calendar,
                organization=organization,
                external_id=f"room_{i}",
                calendar_type=CalendarType.PERSONAL,
            )
        service, host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        service._execute_organization_calendar_resources_import(start, end)

        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 0
        )
        # The rows are untouched, still PERSONAL.
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.PERSONAL
            ).count()
            == 3
        )
        assert host.request_calendar_sync_calls == []

    def test_soft_deleted_rows_are_free_because_the_upsert_leaves_them_soft_deleted(self):
        """The other side of the same rule: a row the write cannot bring *into*
        ``live_of_type`` must not be charged either.

        ``update_or_create``'s defaults do not touch ``visibility``, so retyping a
        soft-deleted row leaves it soft-deleted and outside the counter. Charging it
        would be a false block on a write that moves usage by zero.
        """
        organization = _organization_with_resource_calendar_limit(1)
        resources = _make_resources(3)
        for i in range(2):
            baker.make(
                Calendar,
                organization=organization,
                external_id=f"room_{i}",
                calendar_type=CalendarType.PERSONAL,
                visibility=CalendarVisibility.INACTIVE,
            )
        service, _host = _make_service(organization, resources)
        start = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC)

        service._execute_organization_calendar_resources_import(start, end)

        # room_2 is the only chargeable resource and it fits in the ceiling of 1;
        # the two soft-deleted rows are retyped but stay uncounted.
        assert (
            Calendar.objects.live_of_type(CalendarType.RESOURCE)
            .filter(organization=organization)
            .count()
            == 1
        )
        assert (
            Calendar.objects.filter(
                organization=organization, external_id="room_2", calendar_type=CalendarType.RESOURCE
            ).count()
            == 1
        )


@pytest.mark.django_db
class TestSplitPredicateMatchesTheUsageCounter:
    """The split predicate and the usage counter's predicate must agree for every
    row shape, in both directions.

    Two predicates that disagree is the actual defect behind the promotion bypass, so
    this asserts the property rather than a handful of examples: for each
    (``calendar_type``, ``visibility``) combination, "the queryset says this row is
    free" must equal "the retyping upsert does not change what ``live_of_type``
    counts".
    """

    @pytest.mark.parametrize("calendar_type", list(CalendarType))
    @pytest.mark.parametrize("visibility", list(CalendarVisibility))
    def test_free_iff_the_retype_does_not_move_the_counter(self, calendar_type, visibility):
        organization = _organization_with_resource_calendar_limit(None)
        probe = baker.make(
            Calendar,
            organization=organization,
            external_id="probe",
            calendar_type=calendar_type,
            visibility=visibility,
        )

        def counted() -> int:
            return (
                Calendar.objects.filter_by_organization(organization.id)
                .live_of_type(CalendarType.RESOURCE)
                .count()
            )

        says_free = "probe" in Calendar.objects.filter_by_organization(
            organization.id
        ).external_ids_not_newly_counted_as_type(["probe"], CalendarType.RESOURCE)

        before = counted()
        # What the writer's ``update_or_create`` defaults do to a matched row: force
        # the type, leave ``visibility`` alone.
        probe.calendar_type = CalendarType.RESOURCE
        probe.save(update_fields=["calendar_type"])

        assert says_free is (counted() == before)


@pytest.mark.django_db
class TestPartialImportIsDistinguishableFromSuccess:
    """``SUCCESS`` plus an advisory string on an error column is not a contract a
    consumer can read; a truncated import gets its own terminal status."""

    def test_capped_import_ends_partial_with_a_bounded_warning(self):
        organization = _organization_with_resource_calendar_limit(2)
        resources = _make_resources(40)
        service, _host = _make_service(organization, resources)
        import_state = baker.make(
            CalendarOrganizationResourcesImport,
            organization=organization,
            start_time=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC),
        )

        service.import_organization_calendar_resources(import_state)

        import_state.refresh_from_db()
        assert import_state.status == CalendarOrganizationResourceImportStatus.PARTIAL
        assert "2 of 40" in import_state.error_message
        # The 38 skipped ids are elided rather than concatenated into the column.
        assert "and 18 more" in import_state.error_message
        assert "room_39" not in import_state.error_message

    def test_full_import_ends_success_with_no_warning(self):
        organization = _organization_with_resource_calendar_limit(10)
        resources = _make_resources(3)
        service, _host = _make_service(organization, resources)
        import_state = baker.make(
            CalendarOrganizationResourcesImport,
            organization=organization,
            start_time=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC),
        )

        service.import_organization_calendar_resources(import_state)

        import_state.refresh_from_db()
        assert import_state.status == CalendarOrganizationResourceImportStatus.SUCCESS
        assert import_state.error_message == ""

    def test_duplicate_external_id_in_one_discovery_is_not_double_charged(self):
        """A provider that lists the same ``external_id`` twice in a single discovery
        must not have that resource charged twice: 3 unique resources need exactly 3
        units of headroom, not 4, even though the raw discovery has 4 entries. Before
        the fix, ``chargeable_resources`` counted the duplicate as a second unit,
        crowded out a genuinely-new resource from the capped slice, and produced a
        false partial-import warning for an organization that had headroom for
        everything it actually needed."""
        organization = _organization_with_resource_calendar_limit(3)
        unique_resources = _make_resources(3)
        # The duplicate is listed first, so a naive cap (no dedup) burns a headroom
        # slot on it before reaching the third, genuinely-new resource.
        resources_with_duplicate = [unique_resources[0], *unique_resources]
        service, _host = _make_service(organization, resources_with_duplicate)
        import_state = baker.make(
            CalendarOrganizationResourcesImport,
            organization=organization,
            start_time=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC),
        )

        service.import_organization_calendar_resources(import_state)

        import_state.refresh_from_db()
        assert import_state.status == CalendarOrganizationResourceImportStatus.SUCCESS
        assert import_state.error_message == ""
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 3
        )


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

    def test_unlimited_org_full_sync_ends_success_with_no_warning(self):
        """Driven through ``import_organization_calendar_resources`` -- the real entry
        point, terminal status included -- rather than poking the executor directly."""
        organization = _organization_with_resource_calendar_limit(None)
        resources = _make_resources(25)
        service, host = _make_service(organization, resources)
        import_state = baker.make(
            CalendarOrganizationResourcesImport,
            organization=organization,
            start_time=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2026, 1, 1, 17, 0, tzinfo=datetime.UTC),
        )

        service.import_organization_calendar_resources(import_state)

        import_state.refresh_from_db()
        assert import_state.status == CalendarOrganizationResourceImportStatus.SUCCESS
        assert import_state.error_message == ""
        assert (
            Calendar.objects.filter(
                organization=organization, calendar_type=CalendarType.RESOURCE
            ).count()
            == 25
        )
        assert len(host.request_calendar_sync_calls) == 25


@pytest.mark.django_db
class TestSyncPathIsWiredThroughDI:
    """The other tests here hand-build the context, so they would pass even if the
    container had never been wired. This one builds the facade the way production
    does and asserts the sync sub-service really receives an entitlement service."""

    def test_authenticated_facade_gives_the_sync_service_an_entitlement_service(self):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        user = User.objects.create_user(email="di-sync@example.com", password="pw")  # noqa: S106
        Profile.objects.create(user=user)
        account = SocialAccount.objects.create(
            user=user, provider=CalendarProvider.GOOGLE, uid="di-12345"
        )
        SocialToken.objects.create(
            account=account,
            token="access",  # noqa: S106
            token_secret="refresh",  # noqa: S106
            expires_at=timezone.now() + datetime.timedelta(hours=1),
        )

        with patch(
            "calendar_integration.services.calendar_adapters."
            "google_calendar_adapter.GoogleCalendarAdapter"
        ) as adapter_class:
            adapter = MagicMock()
            adapter.provider = CalendarProvider.GOOGLE
            del adapter.resolve_expression
            del adapter.get_source_expressions
            adapter_class.return_value = adapter
            adapter_class.from_service_account.return_value = adapter

            service = CalendarService()
            service.authenticate(account=user, organization=organization)

            sync_service = service._get_sync_service()

        assert isinstance(sync_service._context.entitlement_service, EntitlementService)
