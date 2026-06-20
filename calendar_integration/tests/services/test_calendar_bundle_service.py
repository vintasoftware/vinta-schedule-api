"""Direct unit tests for ``CalendarBundleService``.

These construct the bundle sub-service directly from a shared
:class:`CalendarServiceContext` (built by initializing a facade, then reused —
the perf guardrail) and exercise:

- ``create_bundle_calendar`` / ``update_bundle_calendar`` invariants (primary
  calendar selection, child reconciliation).
- ``create_bundle_event`` / ``update_bundle_event`` / ``delete_bundle_event``
  fan-out paths (with a stubbed host for event CRUD).
- ``_get_primary_calendar`` and ``_collect_bundle_attendees`` helpers.

The exhaustive bundle-behavior coverage stays in ``test_calendar_service.py``
against the facade; these assert the extracted service behaves identically in
isolation and that the facade forwards to it.
"""

import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.calendar_bundle_service import CalendarBundleService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    CalendarEventInputData,
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Bundle Service Org", should_sync_rooms=False)


@pytest.fixture
def child_calendar_internal(organization, db):
    """An internal (no provider) child calendar."""
    return Calendar.objects.create(
        name="Internal Child Calendar",
        external_id="bundle-internal-child-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


@pytest.fixture
def child_calendar_google(organization, db):
    """A Google child calendar — used as the designated primary."""
    return Calendar.objects.create(
        name="Google Child Calendar",
        external_id="bundle-google-child-1",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


@pytest.fixture
def bundle_calendar(organization, child_calendar_internal, child_calendar_google, db):
    """Bundle calendar with Google as primary and Internal as a child."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    return service.create_bundle_calendar(
        name="Test Bundle Calendar",
        description="A test bundle calendar",
        child_calendars=[child_calendar_internal, child_calendar_google],
        primary_calendar=child_calendar_google,
    )


@pytest.fixture
def empty_bundle_calendar(organization, db):
    """Bundle calendar with no children — used for error-path tests."""
    return Calendar.objects.create(
        name="Empty Bundle Calendar",
        description="A bundle calendar with no children",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.BUNDLE,
        organization=organization,
    )


@pytest.fixture
def bundle_event_data():
    """Minimal valid CalendarEventInputData for bundle event tests."""
    return CalendarEventInputData(
        title="Bundle Meeting",
        description="A meeting created through bundle calendar",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


@pytest.fixture
def initialized_facade(organization):
    """An initialized (no-provider) facade — source of the shared context."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    return service


@pytest.fixture
def bundle_service(initialized_facade):
    """``CalendarBundleService`` wired from the facade's shared context.

    The facade is supplied as the ``host`` so availability, event CRUD, and the
    shared write-adapter / permission helpers all route through the facade,
    exactly the wiring the facade uses internally.
    """
    return CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )


# ---------------------------------------------------------------------------
# Helper: build a real CalendarEvent that looks like a bundle primary event
# ---------------------------------------------------------------------------


def _make_primary_event(
    organization: Organization,
    calendar: Calendar,
    bundle_calendar: Calendar,
    external_id: str = "primary-event-1",
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        title="Original Title",
        description="Original description",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        calendar=calendar,
        organization=organization,
        external_id=external_id,
        is_bundle_primary=True,
        bundle_calendar=bundle_calendar,
    )


# ===========================================================================
# create_bundle_calendar
# ===========================================================================


@pytest.mark.django_db
def test_create_bundle_calendar_no_children(bundle_service, organization):
    """Creating a bundle calendar without children succeeds."""
    cal = bundle_service.create_bundle_calendar(
        name="Solo Bundle",
        description="No children",
    )

    assert cal.name == "Solo Bundle"
    assert cal.calendar_type == CalendarType.BUNDLE
    assert cal.provider == CalendarProvider.INTERNAL
    assert cal.organization == organization
    assert cal.bundle_relationships.count() == 0


@pytest.mark.django_db
def test_create_bundle_calendar_with_primary(
    bundle_service, organization, child_calendar_internal, child_calendar_google
):
    """Creating a bundle calendar designates exactly one child as primary."""
    cal = bundle_service.create_bundle_calendar(
        name="Bundle With Primary",
        child_calendars=[child_calendar_internal, child_calendar_google],
        primary_calendar=child_calendar_google,
    )

    assert cal.bundle_children.count() == 2

    primary_rel = cal.bundle_relationships.filter(is_primary=True).first()
    assert primary_rel is not None
    assert primary_rel.child_calendar == child_calendar_google

    non_primary = cal.bundle_relationships.filter(is_primary=False)
    assert non_primary.count() == 1
    assert non_primary.first().child_calendar == child_calendar_internal


@pytest.mark.django_db
def test_create_bundle_calendar_primary_not_in_children_raises(
    bundle_service, organization, child_calendar_internal, child_calendar_google
):
    """Primary calendar must be in the children list."""
    other = Calendar.objects.create(
        name="Unrelated",
        external_id="unrelated-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )
    with pytest.raises(ValueError, match="Primary calendar must be one of the child calendars"):
        bundle_service.create_bundle_calendar(
            name="Bad Bundle",
            child_calendars=[child_calendar_internal, child_calendar_google],
            primary_calendar=other,
        )


@pytest.mark.django_db
def test_create_bundle_calendar_cross_org_child_raises(
    bundle_service, organization, child_calendar_internal
):
    """Children from a different organization must be rejected."""
    other_org = Organization.objects.create(name="Other Org")
    other_cal = Calendar.objects.create(
        name="Other Calendar",
        external_id="other-org-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=other_org,
    )
    with pytest.raises(
        ValueError, match="All child calendars must belong to the same organization"
    ):
        bundle_service.create_bundle_calendar(
            name="Bad Bundle",
            child_calendars=[child_calendar_internal, other_cal],
        )


@pytest.mark.django_db
def test_create_bundle_calendar_bundle_child_raises(
    bundle_service, organization, child_calendar_internal
):
    """A bundle calendar must not itself be a child of another bundle."""
    # Create a bundle calendar with a distinct external_id to avoid unique constraint
    # with any other INTERNAL/empty-external-id calendar in the fixture set.
    existing_bundle = Calendar.objects.create(
        name="Existing Bundle Child",
        external_id="existing-bundle-for-child-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.BUNDLE,
        organization=organization,
    )
    with pytest.raises(
        ValueError,
        match="Child calendars of a bundle must not be bundle calendars themselves",
    ):
        bundle_service.create_bundle_calendar(
            name="Nested Bundle",
            child_calendars=[child_calendar_internal, existing_bundle],
        )


@pytest.mark.django_db
def test_create_bundle_calendar_creates_ownership_for_user(organization, child_calendar_internal):
    """When a User initializes the facade, a CalendarOwnership row is created."""
    user = User.objects.create_user(email="bundle-owner@example.com", password="pw")
    Profile.objects.create(user=user)
    # The owning user must be a member: the ownership PROTECT FK references
    # OrganizationMembership(user_id, organization_id).
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)

    facade = CalendarService()
    facade.initialize_without_provider(user_or_token=user, organization=organization)
    service = CalendarBundleService(
        context=facade._build_context_snapshot(),
        host=facade,
    )

    cal = service.create_bundle_calendar(
        name="Owned Bundle",
        child_calendars=[child_calendar_internal],
    )

    assert cal.ownerships.filter(membership_user_id=user.id).exists()


# ===========================================================================
# update_bundle_calendar
# ===========================================================================


@pytest.mark.django_db
def test_update_bundle_calendar_adds_new_child(
    bundle_service,
    organization,
    bundle_calendar,
    child_calendar_internal,
    child_calendar_google,
):
    """Passing a new child adds a relationship row."""
    new_child = Calendar.objects.create(
        name="New Internal Child",
        external_id="new-internal-2",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )

    result = bundle_service.update_bundle_calendar(
        bundle_calendar=bundle_calendar,
        child_calendars=[child_calendar_internal, child_calendar_google, new_child],
        primary_calendar=child_calendar_google,
    )

    assert result.bundle_children.count() == 3
    assert new_child in result.bundle_children.all()


@pytest.mark.django_db
def test_update_bundle_calendar_removes_dropped_child(
    bundle_service,
    organization,
    bundle_calendar,
    child_calendar_google,
):
    """Omitting an existing child removes its relationship row."""
    result = bundle_service.update_bundle_calendar(
        bundle_calendar=bundle_calendar,
        child_calendars=[child_calendar_google],
        primary_calendar=child_calendar_google,
    )

    assert result.bundle_children.count() == 1
    assert child_calendar_google in result.bundle_children.all()


@pytest.mark.django_db
def test_update_bundle_calendar_changes_primary(
    bundle_service,
    organization,
    bundle_calendar,
    child_calendar_internal,
    child_calendar_google,
):
    """The primary designation moves from Google → Internal."""
    result = bundle_service.update_bundle_calendar(
        bundle_calendar=bundle_calendar,
        child_calendars=[child_calendar_internal, child_calendar_google],
        primary_calendar=child_calendar_internal,
    )

    primary_rel = ChildrenCalendarRelationship.objects.filter(
        bundle_calendar=result,
        organization=organization,
        is_primary=True,
    ).first()
    assert primary_rel is not None
    assert primary_rel.child_calendar == child_calendar_internal


@pytest.mark.django_db
def test_update_bundle_calendar_non_bundle_raises(
    bundle_service, organization, child_calendar_internal
):
    """Passing a non-BUNDLE calendar raises ValueError."""
    with pytest.raises(ValueError, match="Calendar is not a bundle"):
        bundle_service.update_bundle_calendar(
            bundle_calendar=child_calendar_internal,
            child_calendars=[child_calendar_internal],
        )


@pytest.mark.django_db
def test_update_bundle_calendar_primary_not_in_children_raises(
    bundle_service, organization, bundle_calendar, child_calendar_internal, child_calendar_google
):
    """Primary calendar must be in the new children set."""
    other = Calendar.objects.create(
        name="Outside",
        external_id="outside-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )
    with pytest.raises(ValueError, match="Primary calendar must be one of the child calendars"):
        bundle_service.update_bundle_calendar(
            bundle_calendar=bundle_calendar,
            child_calendars=[child_calendar_internal, child_calendar_google],
            primary_calendar=other,
        )


# ===========================================================================
# create_bundle_event (fan-out)
# ===========================================================================


@pytest.mark.django_db
def test_create_bundle_event_non_bundle_calendar_raises(
    bundle_service, child_calendar_internal, bundle_event_data
):
    """create_bundle_event raises when the calendar is not a BUNDLE."""
    with pytest.raises(ValueError, match="Calendar must be a bundle calendar"):
        bundle_service.create_bundle_event(child_calendar_internal, bundle_event_data)


@pytest.mark.django_db
def test_create_bundle_event_no_children_raises(
    bundle_service, empty_bundle_calendar, bundle_event_data
):
    """create_bundle_event raises when the bundle has no children."""
    with pytest.raises(ValueError, match="Bundle calendar has no child calendars"):
        bundle_service.create_bundle_event(empty_bundle_calendar, bundle_event_data)


@pytest.mark.django_db
def test_create_bundle_event_no_availability_raises(
    initialized_facade,
    bundle_calendar,
    child_calendar_internal,
    bundle_event_data,
):
    """create_bundle_event raises when any child has no availability."""
    service = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )
    with patch.object(
        initialized_facade,
        "get_availability_windows_in_range",
        return_value=[],
    ):
        with pytest.raises(ValueError, match="No availability in child calendar"):
            service.create_bundle_event(bundle_calendar, bundle_event_data)


@pytest.mark.django_db
def test_create_bundle_event_uses_designated_primary(
    organization,
    initialized_facade,
    bundle_calendar,
    child_calendar_google,
    child_calendar_internal,
    bundle_event_data,
):
    """The primary CalendarEvent is created in the designated primary calendar."""
    service = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )

    availability_window = [
        AvailableTimeWindow(
            start_time=bundle_event_data.start_time,
            end_time=bundle_event_data.end_time,
        )
    ]

    created_calls: list[tuple[int, CalendarEventInputData]] = []

    def fake_create_event(calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        """Return a minimal CalendarEvent for the requested calendar."""
        cal = Calendar.objects.get(id=calendar_id, organization=organization)
        evt = CalendarEvent(
            id=len(created_calls) + 1,
            title=event_data.title,
            calendar=cal,
            organization=organization,
            start_time_tz_unaware=event_data.start_time,
            end_time_tz_unaware=event_data.end_time,
            timezone="UTC",
            external_id=f"fake-{len(created_calls)}",
        )
        evt.save()
        created_calls.append((calendar_id, event_data))
        return evt

    with patch.object(
        initialized_facade, "get_availability_windows_in_range", return_value=availability_window
    ):
        with patch.object(initialized_facade, "create_event", side_effect=fake_create_event):
            result = service.create_bundle_event(bundle_calendar, bundle_event_data)

    # First call must target the designated primary calendar (Google)
    assert created_calls[0][0] == child_calendar_google.id
    # Result is the primary event — marked as such by create_bundle_event
    result.refresh_from_db()
    assert result.is_bundle_primary is True
    assert result.bundle_calendar == bundle_calendar


@pytest.mark.django_db
def test_create_bundle_event_creates_blocked_time_for_provider_children(
    organization,
    initialized_facade,
    bundle_event_data,
    child_calendar_google,
):
    """Non-primary PROVIDER children get a BlockedTime instead of a CalendarEvent."""
    other_google = Calendar.objects.create(
        name="Google Child 2",
        external_id="google-child-2-bsvc",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )

    service_facade = CalendarService()
    service_facade.initialize_without_provider(organization=organization)
    bc = service_facade.create_bundle_calendar(
        name="Bundle With Two Google Children",
        child_calendars=[child_calendar_google, other_google],
        primary_calendar=child_calendar_google,
    )

    bundle_svc = CalendarBundleService(
        context=service_facade._build_context_snapshot(),
        host=service_facade,
    )

    availability_window = [
        AvailableTimeWindow(
            start_time=bundle_event_data.start_time,
            end_time=bundle_event_data.end_time,
        )
    ]

    def fake_create_event(calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        cal = Calendar.objects.get(id=calendar_id, organization=organization)
        evt = CalendarEvent(
            title=event_data.title,
            calendar=cal,
            organization=organization,
            start_time_tz_unaware=event_data.start_time,
            end_time_tz_unaware=event_data.end_time,
            timezone="UTC",
            external_id=f"fake-bk-{calendar_id}",
        )
        evt.save()
        return evt

    bt_count_before = BlockedTime.objects.filter(organization=organization).count()

    with patch.object(
        service_facade, "get_availability_windows_in_range", return_value=availability_window
    ):
        with patch.object(service_facade, "create_event", side_effect=fake_create_event):
            bundle_svc.create_bundle_event(bc, bundle_event_data)

    # A BlockedTime must have been created for other_google
    bt_count_after = BlockedTime.objects.filter(organization=organization).count()
    assert bt_count_after == bt_count_before + 1

    bt = BlockedTime.objects.filter(
        organization=organization, bundle_primary_event__isnull=False
    ).last()
    assert bt is not None
    assert bt.calendar == other_google


# ===========================================================================
# update_bundle_event (fan-out)
# ===========================================================================


@pytest.mark.django_db
def test_update_bundle_event_non_primary_raises(bundle_service, organization):
    """update_bundle_event raises when the event is not a bundle primary."""
    cal = Calendar.objects.create(
        name="Some Calendar",
        external_id="some-up-1",
        organization=organization,
    )
    non_primary = CalendarEvent.objects.create(
        title="Non-primary Event",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        calendar=cal,
        organization=organization,
        is_bundle_primary=False,
        external_id="non-primary-up-1",
    )

    event_data = CalendarEventInputData(
        title="Updated",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    with pytest.raises(ValueError, match="Event must be a bundle primary event"):
        bundle_service.update_bundle_event(non_primary, event_data)


@pytest.mark.django_db
def test_update_bundle_event_delegates_to_host_and_updates_blocked_times(
    initialized_facade,
    organization,
    bundle_calendar,
):
    """update_bundle_event calls host.update_event for each representation and
    updates BlockedTime rows in-place."""
    primary_cal = Calendar.objects.create(
        name="Primary Calendar",
        external_id="primary-up-1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    primary_event = _make_primary_event(organization, primary_cal, bundle_calendar, "pev-up-1")

    repr_cal = Calendar.objects.create(
        name="Internal Repr Calendar",
        external_id="repr-up-1",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
    )
    CalendarEvent.objects.create(
        title="[Bundle] Original Title",
        description="Bundle event from ...",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        calendar=repr_cal,
        organization=organization,
        external_id="repr-ev-up-1",
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    blocked_cal = Calendar.objects.create(
        name="Blocked Calendar",
        external_id="blocked-up-1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    blocked = BlockedTime.objects.create(
        calendar=blocked_cal,
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Bundle event: Original Title",
        organization=organization,
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    svc = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )

    updated_data = CalendarEventInputData(
        title="Updated Title",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    with patch.object(initialized_facade, "update_event", return_value=primary_event) as mock_up:
        svc.update_bundle_event(primary_event, updated_data)

    # host.update_event called once for primary + once for the representation
    assert mock_up.call_count == 2

    # BlockedTime updated in-place
    blocked.refresh_from_db()
    assert blocked.reason == "Bundle event: Updated Title"


# ===========================================================================
# delete_bundle_event (fan-out)
# ===========================================================================


@pytest.mark.django_db
def test_delete_bundle_event_non_primary_raises(bundle_service, organization):
    """delete_bundle_event raises when the event is not a bundle primary."""
    cal = Calendar.objects.create(
        name="Del Calendar",
        external_id="del-cal-1",
        organization=organization,
    )
    non_primary = CalendarEvent.objects.create(
        title="Non-primary",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        calendar=cal,
        organization=organization,
        is_bundle_primary=False,
        external_id="non-primary-del-1",
    )

    with pytest.raises(ValueError, match="Event must be a bundle primary event"):
        bundle_service.delete_bundle_event(non_primary)


@pytest.mark.django_db
def test_delete_bundle_event_deletes_representations_and_blocked_times(
    initialized_facade,
    organization,
    bundle_calendar,
):
    """delete_bundle_event deletes all representation events and blocked times,
    then the primary event itself via the host."""
    primary_cal = Calendar.objects.create(
        name="Primary Calendar",
        external_id="primary-del-1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    primary_event = _make_primary_event(organization, primary_cal, bundle_calendar, "pev-del-1")

    repr_cal = Calendar.objects.create(
        name="Repr Calendar",
        external_id="repr-del-1",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
    )
    CalendarEvent.objects.create(
        title="[Bundle] Original Title",
        description="Bundle event from ...",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        calendar=repr_cal,
        organization=organization,
        external_id="repr-ev-del-1",
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    blocked_cal = Calendar.objects.create(
        name="Blocked Calendar",
        external_id="blocked-del-1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    blocked = BlockedTime.objects.create(
        calendar=blocked_cal,
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Bundle event: Original Title",
        organization=organization,
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    svc = CalendarBundleService(
        context=initialized_facade._build_context_snapshot(),
        host=initialized_facade,
    )

    deleted_ids: list[tuple[int, int]] = []

    def fake_delete(calendar_id: int, event_id: int, delete_series: bool = False) -> None:
        deleted_ids.append((calendar_id, event_id))
        CalendarEvent.objects.filter(id=event_id, organization=organization).delete()

    with patch.object(initialized_facade, "delete_event", side_effect=fake_delete):
        svc.delete_bundle_event(primary_event)

    # host.delete_event called for representation + primary
    assert len(deleted_ids) == 2

    # The blocked time was deleted directly (not via host.delete_event)
    assert not BlockedTime.objects.filter(id=blocked.id).exists()


# ===========================================================================
# Facade delegation tests
# ===========================================================================


@pytest.mark.django_db
def test_facade_create_bundle_calendar_delegates_to_bundle_service(organization):
    """Facade.create_bundle_calendar must forward to CalendarBundleService."""
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)

    with patch.object(
        CalendarBundleService, "create_bundle_calendar", return_value=Mock(spec=Calendar)
    ) as mock_method:
        facade.create_bundle_calendar(name="Delegated Bundle")
        mock_method.assert_called_once()


@pytest.mark.django_db
def test_facade_update_bundle_calendar_delegates_to_bundle_service(organization, bundle_calendar):
    """Facade.update_bundle_calendar must forward to CalendarBundleService."""
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)

    with patch.object(
        CalendarBundleService, "update_bundle_calendar", return_value=bundle_calendar
    ) as mock_method:
        facade.update_bundle_calendar(bundle_calendar=bundle_calendar, child_calendars=[])
        mock_method.assert_called_once()


@pytest.mark.django_db
def test_facade_create_bundle_event_delegates_to_bundle_service(
    organization, bundle_calendar, bundle_event_data
):
    """Facade._create_bundle_event must forward to CalendarBundleService."""
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)

    mock_event = Mock(spec=CalendarEvent)

    with patch.object(
        CalendarBundleService, "create_bundle_event", return_value=mock_event
    ) as mock_method:
        result = facade._create_bundle_event(bundle_calendar, bundle_event_data)
        mock_method.assert_called_once_with(bundle_calendar, bundle_event_data)
        assert result is mock_event


@pytest.mark.django_db
def test_facade_update_bundle_event_delegates_to_bundle_service(
    organization, bundle_calendar, bundle_event_data
):
    """Facade._update_bundle_event must forward to CalendarBundleService."""
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)

    cal = Calendar.objects.create(
        name="Del Cal", external_id="del-cal-d", organization=organization
    )
    mock_event = MagicMock(spec=CalendarEvent)
    mock_event.is_bundle_primary = True
    mock_event.bundle_calendar = bundle_calendar
    mock_event.calendar = cal
    mock_event.id = 999

    with patch.object(
        CalendarBundleService, "update_bundle_event", return_value=mock_event
    ) as mock_method:
        result = facade._update_bundle_event(mock_event, bundle_event_data)
        mock_method.assert_called_once_with(mock_event, bundle_event_data)
        assert result is mock_event


@pytest.mark.django_db
def test_facade_delete_bundle_event_delegates_to_bundle_service(
    organization, bundle_calendar, bundle_event_data
):
    """Facade._delete_bundle_event must forward to CalendarBundleService."""
    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)

    mock_event = MagicMock(spec=CalendarEvent)
    mock_event.is_bundle_primary = True

    with patch.object(
        CalendarBundleService, "delete_bundle_event", return_value=None
    ) as mock_method:
        facade._delete_bundle_event(mock_event)
        mock_method.assert_called_once_with(mock_event)


# ===========================================================================
# _get_primary_calendar helper
# ===========================================================================


@pytest.mark.django_db
def test_get_primary_calendar_returns_designated(
    bundle_service, organization, bundle_calendar, child_calendar_google
):
    """_get_primary_calendar returns the designated primary child."""
    primary = bundle_service._get_primary_calendar(bundle_calendar)
    assert primary == child_calendar_google


@pytest.mark.django_db
def test_get_primary_calendar_no_primary_raises(
    bundle_service, organization, empty_bundle_calendar
):
    """_get_primary_calendar raises when no relationship is marked primary."""
    with pytest.raises(
        ValueError, match="Bundle calendar has no designated primary child calendar"
    ):
        bundle_service._get_primary_calendar(empty_bundle_calendar)


# ===========================================================================
# _collect_bundle_attendees helper
# ===========================================================================


@pytest.mark.django_db
def test_collect_bundle_attendees_includes_calendar_owners(
    organization,
    child_calendar_internal,
    child_calendar_google,
    bundle_event_data,
):
    """_collect_bundle_attendees adds owners of child calendars as attendees."""
    user = User.objects.create_user(email="attendee@example.com", password="pw")
    Profile.objects.create(user=user)

    from calendar_integration.models import CalendarOwnership
    from organizations.models import OrganizationMembership

    OrganizationMembership.objects.create(user=user, organization=organization)
    CalendarOwnership.objects.create(
        organization=organization,
        calendar=child_calendar_internal,
        membership_user_id=user.id,
        is_default=False,
    )

    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)
    svc = CalendarBundleService(
        context=facade._build_context_snapshot(),
        host=facade,
    )

    attendees = svc._collect_bundle_attendees(
        [child_calendar_internal, child_calendar_google],
        bundle_event_data,
    )

    user_ids = {a.user_id for a in attendees}
    assert user.id in user_ids


@pytest.mark.django_db
def test_collect_bundle_attendees_deduplicates(
    organization,
    child_calendar_internal,
    bundle_event_data,
):
    """If a user is both an explicit attendee and a calendar owner, they appear once."""
    user = User.objects.create_user(email="dedup@example.com", password="pw")
    Profile.objects.create(user=user)

    from calendar_integration.models import CalendarOwnership
    from calendar_integration.services.dataclasses import EventAttendanceInputData
    from organizations.models import OrganizationMembership

    OrganizationMembership.objects.create(user=user, organization=organization)
    CalendarOwnership.objects.create(
        organization=organization,
        calendar=child_calendar_internal,
        membership_user_id=user.id,
        is_default=False,
    )

    event_data_with_attendee = CalendarEventInputData(
        title="Event",
        description="",
        start_time=bundle_event_data.start_time,
        end_time=bundle_event_data.end_time,
        timezone="UTC",
        attendances=[EventAttendanceInputData(user_id=user.id)],
        external_attendances=[],
        resource_allocations=[],
    )

    facade = CalendarService()
    facade.initialize_without_provider(organization=organization)
    svc = CalendarBundleService(
        context=facade._build_context_snapshot(),
        host=facade,
    )

    attendees = svc._collect_bundle_attendees(
        [child_calendar_internal],
        event_data_with_attendee,
    )

    user_ids = [a.user_id for a in attendees]
    assert user_ids.count(user.id) == 1
