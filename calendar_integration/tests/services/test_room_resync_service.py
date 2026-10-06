"""Unit tests for ``RoomResyncService``, against an in-memory provider directory.

The notifier is a ``MagicMock`` with ``RoomSyncNotifier``'s spec, so the
assertions are on the full list of notices the resync asked for. The audit
service is the real one with ``record`` patched, for the same reason. The
acceptance test at the bottom uses the real notifier instead, and counts the
emails it queues.
"""

import datetime
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, call

from django.utils import timezone

import pytest
from vinta_billing.constants import LimitKind
from vinta_billing.models import Subscription, SubscriptionPlanLimit
from vinta_billing.services.entitlement_service import EntitlementService

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    ResourceSyncOperation,
    ResourceSyncStatus,
    RSVPStatus,
)
from calendar_integration.factories import create_resource_location, create_resource_provider_link
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    RecurrenceRule,
    ResourceAllocation,
    ResourceCalendarProviderLink,
    ResourceLocation,
)
from calendar_integration.services.room_resync_service import RoomResyncResult, RoomResyncService
from calendar_integration.services.room_sync_notifier import RoomSyncNotifier
from calendar_integration.signals import resource_room_archived, resource_room_synced
from calendar_integration.tests.room_resync_fakes import (
    FakeAdapterResolver,
    FakeRoomDirectory,
    make_location,
    make_room,
)
from common.organization_context import organization_context
from organizations.models import Organization
from organizations.tests.helpers import make_admin_membership
from payments.seams.resource_keys import RESOURCE_CALENDARS
from payments.seams.scopes import scope_for
from users.factories import UserFactory


GOOGLE = CalendarProvider.GOOGLE


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Resync Org")


@pytest.fixture(autouse=True)
def _bound(organization: Organization) -> Iterator[None]:
    with organization_context(organization):
        yield


@pytest.fixture
def directory() -> FakeRoomDirectory:
    return FakeRoomDirectory(provider=GOOGLE, locations=[make_location()])


@pytest.fixture
def resolver(organization: Organization, directory: FakeRoomDirectory) -> FakeAdapterResolver:
    resolver = FakeAdapterResolver()
    resolver.enable(organization, directory)
    return resolver


@pytest.fixture
def notifier() -> MagicMock:
    return MagicMock(spec=RoomSyncNotifier)


@pytest.fixture
def audit_service(di_container: Any, monkeypatch: pytest.MonkeyPatch) -> OrganizationAuditService:
    service = di_container.audit_service()
    monkeypatch.setattr(service, "record", MagicMock())
    return service


@pytest.fixture
def service(
    resolver: FakeAdapterResolver, notifier: MagicMock, audit_service: OrganizationAuditService
) -> RoomResyncService:
    return RoomResyncService(
        resource_directory_adapter_resolver=resolver,
        room_sync_notifier=notifier,
        audit_service=audit_service,
        entitlement_service=EntitlementService(),
    )


@pytest.fixture
def signals() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Every ``resource_room_synced`` / ``resource_room_archived`` send, in order."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def on_synced(sender: Any, **kwargs: Any) -> None:
        sent.append(
            ("synced", {key: kwargs[key] for key in ("calendar_id", "provider", "created")})
        )

    def on_archived(sender: Any, **kwargs: Any) -> None:
        sent.append(("archived", {key: kwargs[key] for key in ("calendar_id", "provider")}))

    resource_room_synced.connect(on_synced, dispatch_uid="test_room_resync_synced")
    resource_room_archived.connect(on_archived, dispatch_uid="test_room_resync_archived")
    yield sent
    resource_room_synced.disconnect(dispatch_uid="test_room_resync_synced")
    resource_room_archived.disconnect(dispatch_uid="test_room_resync_archived")


@pytest.fixture
def resync(
    service: RoomResyncService,
    organization: Organization,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> Callable[[], RoomResyncResult | None]:
    """Run one resync for the Google directory, executing its on-commit callbacks."""

    def _run() -> RoomResyncResult | None:
        with django_capture_on_commit_callbacks(execute=True):
            return service.resync(organization, GOOGLE)

    return _run


def _room_calendar(
    organization: Organization,
    external_id: str,
    name: str,
    *,
    capacity: int | None = 8,
    calendar_type: str = CalendarType.RESOURCE,
    visibility: str = CalendarVisibility.ACTIVE,
) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name=name,
        external_id=external_id,
        email=f"{external_id}@resource.example.com",
        provider=GOOGLE,
        calendar_type=calendar_type,
        capacity=capacity,
        visibility=visibility,
    )


def _synced_room(
    organization: Organization,
    external_id: str,
    name: str,
    *,
    sync_status: str = ResourceSyncStatus.SYNCED,
    pending_fields: dict[str, Any] | None = None,
    location: ResourceLocation | None = None,
    **link_kwargs: Any,
) -> ResourceCalendarProviderLink:
    """A room calendar plus a link whose snapshot is ``make_room(external_id, name)``.

    The link points at ``location``, or at the ``building-1`` location when one exists.
    """
    calendar = _room_calendar(organization, external_id, name)
    if location is None:
        location = ResourceLocation.objects.filter(external_building_id="building-1").first()
    return create_resource_provider_link(
        calendar=calendar,
        sync_status=sync_status,
        location=location,
        provider_snapshot=make_room(external_id, name).synced_values(),
        pending_fields=pending_fields,
        **link_kwargs,
    )


def _event(
    organization: Organization,
    calendar: Calendar,
    *,
    starts_in: datetime.timedelta,
    title: str = "Planning",
    recurrence_rule: RecurrenceRule | None = None,
) -> CalendarEvent:
    start = (timezone.now() + starts_in).replace(tzinfo=None, microsecond=0)
    return CalendarEvent.objects.create(
        organization=organization,
        calendar=calendar,
        title=title,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(hours=1),
        timezone="UTC",
        recurrence_rule=recurrence_rule,
    )


def _reload(link: ResourceCalendarProviderLink) -> ResourceCalendarProviderLink:
    return ResourceCalendarProviderLink.objects.select_related("calendar", "location").get(
        id=link.id
    )


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


class TestGating:
    def test_not_write_enabled_returns_none_without_calling_the_provider(
        self,
        service: RoomResyncService,
        organization: Organization,
        directory: FakeRoomDirectory,
    ) -> None:
        directory.rooms = [make_room("room-1", "Huddle 1")]

        assert service.resync(organization, CalendarProvider.MICROSOFT) is None
        assert directory.list_calls == 0
        assert not Calendar.objects.exists()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


class TestLocations:
    def test_creates_every_listed_location(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        directory.locations = [
            make_location("building-1", "1", building_name="HQ"),
            make_location("building-1", "2", building_name="HQ"),
            make_location("building-2", "", building_name="Annex", floor_name=""),
        ]

        result = resync()

        assert result is not None
        assert result.locations_created == 3
        rows = ResourceLocation.objects.order_by("external_building_id", "external_floor_id")
        assert [
            (
                row.organization_id,
                row.provider,
                row.external_building_id,
                row.external_floor_id,
                row.building_name,
                row.floor_name,
                row.is_active,
            )
            for row in rows
        ] == [
            (organization.id, GOOGLE, "building-1", "1", "HQ", "1", True),
            (organization.id, GOOGLE, "building-1", "2", "HQ", "2", True),
            (organization.id, GOOGLE, "building-2", "", "Annex", "", True),
        ]

    def test_renamed_location_is_updated_in_place(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        location = create_resource_location(organization=organization, building_name="Old name")
        directory.locations = [make_location(building_name="New name")]

        result = resync()

        assert result is not None
        assert (result.locations_created, result.locations_updated) == (0, 1)
        location.refresh_from_db()
        assert location.building_name == "New name"

    def test_unseen_locations_are_deleted_unless_a_room_points_at_them(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        referenced = create_resource_location(
            organization=organization, external_building_id="gone-referenced"
        )
        create_resource_location(organization=organization, external_building_id="gone-free")
        # A room that stays put in a location the provider stopped listing.
        link = _synced_room(organization, "room-1", "Huddle 1", location=referenced)
        link.provider_snapshot = {
            **link.provider_snapshot,
            "location_ref": {"external_building_id": "gone-referenced", "external_floor_id": "1"},
        }
        link.save(update_fields=["provider_snapshot"])
        directory.rooms = [make_room("room-1", "Huddle 1", building="gone-referenced")]
        directory.locations = [make_location("building-1")]

        result = resync()

        assert result is not None
        assert (result.locations_deleted, result.locations_deactivated) == (1, 1)
        assert sorted(
            ResourceLocation.objects.values_list("external_building_id", "is_active")
        ) == [("building-1", True), ("gone-referenced", False)]
        assert _reload(link).location == referenced

    def test_location_seen_again_is_reactivated(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        location = create_resource_location(organization=organization, is_active=False)
        directory.locations = [make_location()]

        resync()

        location.refresh_from_db()
        assert location.is_active is True


# ---------------------------------------------------------------------------
# (a) linking existing calendars and (b) importing new rooms
# ---------------------------------------------------------------------------


class TestNewRooms:
    def test_a_links_an_existing_unlinked_resource_calendar(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        signals: list[tuple[str, dict[str, Any]]],
    ) -> None:
        calendar = _room_calendar(organization, "room-1", "Stale name", capacity=2)
        provider_room = make_room("room-1", "Huddle 1", capacity=6, description="By the window")
        directory.rooms = [provider_room]

        result = resync()

        assert result is not None
        assert result.linked_calendar_ids == [calendar.id]
        assert result.imported_calendar_ids == []
        link = ResourceCalendarProviderLink.objects.select_related("location").get(
            calendar=calendar
        )
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.provider == GOOGLE
        assert link.provider_snapshot == provider_room.synced_values()
        assert link.pending_fields == {}
        assert link.location is not None
        assert link.location.location_ref == {
            "external_building_id": "building-1",
            "external_floor_id": "1",
        }
        calendar.refresh_from_db()
        assert (calendar.name, calendar.capacity, calendar.description) == (
            "Huddle 1",
            6,
            "By the window",
        )
        assert Calendar.objects.count() == 1
        assert signals == [
            ("synced", {"calendar_id": calendar.id, "provider": GOOGLE, "created": False})
        ]

    def test_b_imports_an_unknown_room(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        signals: list[tuple[str, dict[str, Any]]],
    ) -> None:
        provider_room = make_room("room-new", "Focus 2", capacity=3, building=None)
        directory.rooms = [provider_room]

        result = resync()

        calendar = Calendar.objects.get(external_id="room-new")
        assert result is not None
        assert result.imported_calendar_ids == [calendar.id]
        assert (
            calendar.organization_id,
            calendar.provider,
            calendar.calendar_type,
            calendar.name,
            calendar.capacity,
            calendar.email,
            calendar.visibility,
        ) == (
            organization.id,
            GOOGLE,
            CalendarType.RESOURCE,
            "Focus 2",
            3,
            "room-new@resource.example.com",
            CalendarVisibility.ACTIVE,
        )
        link = ResourceCalendarProviderLink.objects.get(calendar=calendar)
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.provider_snapshot == provider_room.synced_values()
        assert link.location is None
        assert link.last_synced_at is not None
        assert signals == [
            ("synced", {"calendar_id": calendar.id, "provider": GOOGLE, "created": False})
        ]

    def test_b_promotes_a_live_calendar_of_another_type_with_the_rooms_id(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        calendar = _room_calendar(
            organization, "room-1", "Shared calendar", calendar_type=CalendarType.PERSONAL
        )
        directory.rooms = [make_room("room-1", "Huddle 1")]

        result = resync()

        assert result is not None
        assert result.imported_calendar_ids == [calendar.id]
        calendar.refresh_from_db()
        assert (calendar.calendar_type, calendar.name) == (CalendarType.RESOURCE, "Huddle 1")

    def test_a_calendar_disabled_in_vinta_is_neither_linked_nor_revived(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        calendar = _room_calendar(
            organization, "room-1", "Huddle 1", visibility=CalendarVisibility.INACTIVE
        )
        directory.rooms = [make_room("room-1", "Huddle 1")]

        result = resync()

        assert result is not None
        assert (result.linked_calendar_ids, result.imported_calendar_ids) == ([], [])
        assert not ResourceCalendarProviderLink.objects.exists()
        calendar.refresh_from_db()
        assert calendar.visibility == CalendarVisibility.INACTIVE

    @pytest.mark.parametrize(
        ("external_id", "tags"),
        [
            pytest.param("vinta-{key}", [], id="google-resource-id"),
            pytest.param("AAMk-provider-id", ["vinta-link-{key}"], id="microsoft-tag"),
        ],
    )
    def test_a_room_vinta_created_for_a_pending_link_is_not_imported_again(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        external_id: str,
        tags: list[str],
    ) -> None:
        # The push created the room on the provider but has not committed the
        # provider's id onto the calendar yet.
        pending_calendar = _room_calendar(organization, "", "Huddle 1")
        link = create_resource_provider_link(
            calendar=pending_calendar, sync_status=ResourceSyncStatus.PENDING_CREATION
        )
        key = link.provisional_key
        provider_room = make_room(external_id.format(key=key), "Huddle 1")
        provider_room.provider_payload = {"tags": [tag.format(key=key) for tag in tags]}
        directory.rooms = [provider_room]

        result = resync()

        assert result is not None
        assert (result.linked_calendar_ids, result.imported_calendar_ids) == ([], [])
        assert list(Calendar.objects.values_list("id", flat=True)) == [pending_calendar.id]
        assert _reload(link).sync_status == ResourceSyncStatus.PENDING_CREATION

    def test_partial_import_at_the_plan_limit(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        # Every organization is provisioned a subscription; cap its rooms at two.
        SubscriptionPlanLimit.objects.update_or_create(
            subscription=Subscription.objects.get(scope=scope_for(organization)),
            resource_key=RESOURCE_CALENDARS,
            defaults={"limit_value": 2, "kind": LimitKind.PREPAID},
        )
        existing = _room_calendar(organization, "room-0", "Room 0")
        directory.rooms = [
            make_room("room-0", "Room 0"),
            make_room("room-1", "Room 1"),
            make_room("room-2", "Room 2"),
            make_room("room-3", "Room 3"),
        ]

        result = resync()

        assert result is not None
        # room-0 is already counted, so linking it is free; one slot is left.
        assert result.linked_calendar_ids == [existing.id]
        imported = Calendar.objects.get(id__in=result.imported_calendar_ids)
        assert imported.external_id == "room-1"
        assert result.import_warning is not None
        assert "Imported 1 of 3" in result.import_warning
        assert sorted(Calendar.objects.values_list("external_id", flat=True)) == [
            "room-0",
            "room-1",
        ]
        assert ResourceCalendarProviderLink.objects.count() == 2


# ---------------------------------------------------------------------------
# (c) provider-side changes
# ---------------------------------------------------------------------------


class TestProviderChanges:
    def test_rename_overwrites_calendar_and_snapshot(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
        audit_service: Any,
    ) -> None:
        create_resource_location(organization=organization)
        link = _synced_room(organization, "room-1", "Huddle 1")
        directory.rooms = [make_room("room-1", "Huddle One")]

        result = resync()

        assert result is not None
        assert result.updated_calendar_ids == [link.calendar.id]
        link = _reload(link)
        assert link.calendar.name == "Huddle One"
        assert link.provider_snapshot == make_room("room-1", "Huddle One").synced_values()
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert notifier.mock_calls == []
        audit_service.record.assert_not_called()

    def test_provider_change_overrides_a_pending_edit_to_the_same_field_and_keeps_the_others(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
        audit_service: Any,
    ) -> None:
        create_resource_location(organization=organization)
        link = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"name": "Vinta name", "capacity": 12},
        )
        # Vinta's own edits are already on the calendar while they wait to be pushed.
        Calendar.objects.filter(id=link.calendar.id).update(name="Vinta name", capacity=12)
        directory.rooms = [make_room("room-1", "Provider name")]

        resync()

        link = _reload(link)
        assert (link.calendar.name, link.calendar.capacity) == ("Provider name", 12)
        assert link.pending_fields == {"capacity": 12}
        assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
        assert link.provider_snapshot["name"] == "Provider name"
        assert notifier.mock_calls == [call.notify_edit_discarded(link.calendar.id, ["name"])]
        audit_service.record.assert_called_once()
        kwargs = audit_service.record.call_args.kwargs
        assert kwargs["action"] == AuditAction.EXTERNAL_CHANGE_ROOM_EDIT_DISCARDED
        assert kwargs["diff"] == {"name": {"old": "Vinta name", "new": "Provider name"}}
        assert kwargs["subject"].subject_id == str(link.calendar.id)

    def test_dropping_the_last_pending_edit_settles_the_link(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
    ) -> None:
        link = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
            last_error="Provider rejected the capacity",
            attempt_count=7,
            retry_deadline=timezone.now(),
            pending_fields={"capacity": 999},
        )
        directory.rooms = [make_room("room-1", "Huddle 1", capacity=20)]

        resync()

        link = _reload(link)
        assert (
            link.sync_status,
            link.failed_operation,
            link.last_error,
            link.attempt_count,
            link.retry_deadline,
            link.pending_fields,
        ) == (ResourceSyncStatus.SYNCED, "", "", 0, None, {})
        assert link.calendar.capacity == 20
        assert notifier.mock_calls == [call.notify_edit_discarded(link.calendar.id, ["capacity"])]

    def test_pending_edit_matching_the_provider_value_is_dropped_silently(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
        audit_service: Any,
    ) -> None:
        link = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"name": "Same name"},
        )
        directory.rooms = [make_room("room-1", "Same name")]

        resync()

        link = _reload(link)
        assert (link.sync_status, link.pending_fields) == (ResourceSyncStatus.SYNCED, {})
        assert notifier.mock_calls == []
        audit_service.record.assert_not_called()

    def test_pending_edit_to_a_field_the_provider_did_not_change_survives(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
    ) -> None:
        link = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"name": "Vinta name"},
        )
        Calendar.objects.filter(id=link.calendar.id).update(name="Vinta name")
        directory.rooms = [make_room("room-1", "Huddle 1")]

        result = resync()

        assert result is not None
        assert result.updated_calendar_ids == []
        link = _reload(link)
        assert (link.calendar.name, link.pending_fields, link.sync_status) == (
            "Vinta name",
            {"name": "Vinta name"},
            ResourceSyncStatus.PENDING_UPDATE,
        )
        assert notifier.mock_calls == []

    def test_provider_move_repoints_the_link_location(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        create_resource_location(organization=organization)
        link = _synced_room(organization, "room-1", "Huddle 1")
        directory.locations = [make_location(), make_location("building-2", "3")]
        directory.rooms = [make_room("room-1", "Huddle 1", building="building-2", floor="3")]

        resync()

        link = _reload(link)
        assert link.location is not None
        assert link.location.location_ref == {
            "external_building_id": "building-2",
            "external_floor_id": "3",
        }
        assert link.provider_snapshot["location_ref"] == link.location.location_ref

    def test_provider_without_a_description_never_clears_it(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        link = _synced_room(organization, "room-1", "Huddle 1")
        Calendar.objects.filter(id=link.calendar.id).update(description="Kept")
        directory.rooms = [make_room("room-1", "Huddle 1", description=None)]

        result = resync()

        assert result is not None
        assert result.updated_calendar_ids == []
        assert _reload(link).calendar.description == "Kept"


# ---------------------------------------------------------------------------
# (d) provider-side deletions
# ---------------------------------------------------------------------------


class TestProviderDeletions:
    def test_deleted_room_with_a_future_booking_is_archived_and_flagged(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
        signals: list[tuple[str, dict[str, Any]]],
    ) -> None:
        link = _synced_room(organization, "room-1", "Huddle 1")
        _event(organization, link.calendar, starts_in=datetime.timedelta(days=3))
        _event(organization, link.calendar, starts_in=-datetime.timedelta(days=3))
        directory.rooms = [make_room("room-other", "Other")]

        result = resync()

        assert result is not None
        assert result.archived_calendar_ids == [link.calendar.id]
        link = _reload(link)
        assert link.sync_status == ResourceSyncStatus.ARCHIVED
        assert link.archived_at is not None
        assert link.flagged_bookings_at == link.archived_at
        assert link.calendar.visibility == CalendarVisibility.INACTIVE
        assert link.is_bookable is False
        assert notifier.mock_calls == [call.notify_bookings_flagged(link.calendar.id, 1)]
        assert ("archived", {"calendar_id": link.calendar.id, "provider": GOOGLE}) in signals

    def test_deleted_room_with_only_past_bookings_is_archived_without_a_flag(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
    ) -> None:
        link = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"name": "x"},
        )
        _event(organization, link.calendar, starts_in=-datetime.timedelta(days=3))
        directory.rooms = [make_room("room-other", "Other")]

        resync()

        link = _reload(link)
        assert (link.sync_status, link.flagged_bookings_at) == (ResourceSyncStatus.ARCHIVED, None)
        assert notifier.mock_calls == []

    def test_bookings_through_allocations_and_open_series_are_flagged(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
    ) -> None:
        link = _synced_room(organization, "room-1", "Huddle 1")
        personal = Calendar.objects.create(
            organization=organization, name="Ana", provider=GOOGLE, external_id="ana"
        )
        allocated = _event(organization, personal, starts_in=datetime.timedelta(days=2))
        ResourceAllocation.objects.create(
            organization=organization,
            event=allocated,
            calendar=link.calendar,
            status=RSVPStatus.ACCEPTED,
        )
        declined = _event(organization, personal, starts_in=datetime.timedelta(days=2))
        ResourceAllocation.objects.create(
            organization=organization,
            event=declined,
            calendar=link.calendar,
            status=RSVPStatus.DECLINED,
        )
        weekly = RecurrenceRule.objects.create(organization=organization, frequency="WEEKLY")
        _event(
            organization,
            link.calendar,
            starts_in=-datetime.timedelta(days=30),
            recurrence_rule=weekly,
        )
        directory.rooms = [make_room("room-other", "Other")]

        resync()

        assert notifier.mock_calls == [call.notify_bookings_flagged(link.calendar.id, 2)]

    def test_an_empty_listing_archives_nothing(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        link = _synced_room(organization, "room-1", "Huddle 1")
        directory.rooms = []

        result = resync()

        assert result is not None
        assert result.archived_calendar_ids == []
        assert _reload(link).sync_status == ResourceSyncStatus.SYNCED


# ---------------------------------------------------------------------------
# Skipped statuses
# ---------------------------------------------------------------------------


class TestSkippedStatuses:
    @pytest.mark.parametrize(
        ("sync_status", "failed_operation"),
        [
            (ResourceSyncStatus.PENDING_CREATION, ""),
            (ResourceSyncStatus.PENDING_DELETION, ""),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.CREATE),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.DELETE),
            (ResourceSyncStatus.ARCHIVED, ""),
        ],
    )
    def test_link_is_untouched_whether_changed_or_gone_on_the_provider(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
        notifier: MagicMock,
        sync_status: str,
        failed_operation: str,
    ) -> None:
        renamed = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=sync_status,
            failed_operation=failed_operation,
        )
        gone = _synced_room(
            organization,
            "room-2",
            "Huddle 2",
            sync_status=sync_status,
            failed_operation=failed_operation,
        )
        directory.rooms = [make_room("room-1", "Renamed")]
        before = [
            (link.sync_status, link.provider_snapshot, link.calendar.name, link.calendar.visibility)
            for link in (_reload(renamed), _reload(gone))
        ]

        result = resync()

        assert result is not None
        assert (result.updated_calendar_ids, result.archived_calendar_ids) == ([], [])
        assert result.imported_calendar_ids == []
        after = [
            (link.sync_status, link.provider_snapshot, link.calendar.name, link.calendar.visibility)
            for link in (_reload(renamed), _reload(gone))
        ]
        assert after == before
        assert notifier.mock_calls == []

    def test_failed_update_takes_provider_changes_but_is_not_archived(
        self,
        resync: Callable[[], RoomResyncResult | None],
        directory: FakeRoomDirectory,
        organization: Organization,
    ) -> None:
        renamed = _synced_room(
            organization,
            "room-1",
            "Huddle 1",
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )
        gone = _synced_room(
            organization,
            "room-2",
            "Huddle 2",
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )
        directory.rooms = [make_room("room-1", "Renamed")]

        resync()

        assert _reload(renamed).calendar.name == "Renamed"
        assert _reload(gone).sync_status == ResourceSyncStatus.SYNC_FAILED


# ---------------------------------------------------------------------------
# Spec acceptance scenario 6
# ---------------------------------------------------------------------------


def test_acceptance_rename_then_delete_with_a_booking_is_flagged(
    organization: Organization,
    directory: FakeRoomDirectory,
    resolver: FakeAdapterResolver,
    audit_service: OrganizationAuditService,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """Room X renamed and room Y deleted on the provider, Y with one future booking.

    One resync renames X, archives Y with ``flagged_bookings_at`` set, and queues
    one admin email per notice type. A second resync writes nothing.
    """
    make_admin_membership(
        user=UserFactory().create_user(email="room-admin@example.com"),
        organization=organization,
        is_active=True,
    )
    notification_service = MagicMock()
    service = RoomResyncService(
        resource_directory_adapter_resolver=resolver,
        room_sync_notifier=RoomSyncNotifier(notification_service=notification_service),
        audit_service=audit_service,
        entitlement_service=EntitlementService(),
    )
    create_resource_location(organization=organization)
    room_x = _synced_room(
        organization,
        "room-x",
        "Huddle 1",
        sync_status=ResourceSyncStatus.PENDING_UPDATE,
        pending_fields={"name": "Huddle Uno"},
    )
    room_y = _synced_room(organization, "room-y", "Huddle 2")
    _event(organization, room_y.calendar, starts_in=datetime.timedelta(days=5))
    directory.rooms = [make_room("room-x", "Huddle One")]

    with django_capture_on_commit_callbacks(execute=True):
        service.resync(organization, GOOGLE)

    room_x, room_y = _reload(room_x), _reload(room_y)
    assert room_x.calendar.name == "Huddle One"
    assert room_x.sync_status == ResourceSyncStatus.SYNCED
    assert room_y.sync_status == ResourceSyncStatus.ARCHIVED
    assert room_y.flagged_bookings_at is not None
    assert room_y.calendar.visibility == CalendarVisibility.INACTIVE
    templates = sorted(
        email.kwargs["body_template"]
        for email in notification_service.create_notification.call_args_list
    )
    assert templates == [
        "calendar_integration/emails/room_bookings_flagged.body.html",
        "calendar_integration/emails/room_edit_discarded.body.html",
    ]

    notification_service.reset_mock()
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        second = service.resync(organization, GOOGLE)

    assert second == RoomResyncResult()
    assert callbacks == []
    notification_service.create_notification.assert_not_called()
    assert _reload(room_x).modified == room_x.modified
    assert _reload(room_y).modified == room_y.modified
