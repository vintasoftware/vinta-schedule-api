"""The room bookability guard on ``CalendarEventService.create_event`` / ``update_event``.

A room whose provider link is not bookable (pending creation, failed on create,
pending deletion, failed on delete, archived) cannot be added to an event. A room
with no link, a manual room or any room in an organization with the
``resource_calendar_provider_sync`` flag off, books exactly as before.
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import RoomNotBookableError
from calendar_integration.factories import create_resource_provider_link
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarOwnership,
    ResourceAllocation,
    ResourceCalendarProviderLink,
)
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
    ResourceAllocationInputData,
)
from organizations.models import Organization, OrganizationMembership
from public_api.services import PublicAPIAuthService
from users.models import Profile, User


START = datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC)
END = START + datetime.timedelta(hours=1)


@pytest.fixture
def mock_google_adapter():
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter.create_event.return_value = _adapter_output("provider-event")
        mock_adapter.update_event.return_value = _adapter_output("provider-event")
        yield mock_adapter


def _adapter_output(external_id: str) -> CalendarEventAdapterOutputData:
    return CalendarEventAdapterOutputData(
        calendar_external_id="guard_cal",
        external_id=external_id,
        title="Meeting",
        description="",
        start_time=START,
        end_time=END,
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
    )


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Room Guard Org", should_sync_rooms=False)


@pytest.fixture
def user(db):
    user = User.objects.create_user(email="room-guard@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return user


@pytest.fixture
def calendar(organization):
    return Calendar.objects.create(
        name="Organizer Calendar",
        external_id="guard_cal",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


def _owner_token(user, organization, **target) -> None:
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)
    token = CalendarManagementToken.objects.create(
        membership_user_id=user.id,
        token_hash=f"guard_token_{next(iter(target.values())).id}",
        organization=organization,
        **target,
    )
    token.permissions.all().delete()
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(permission=permission, organization_id=organization.id)


@pytest.fixture
def event_service(user, organization, calendar, mock_google_adapter):
    social_account = SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid="guard-1"
    )
    SocialToken.objects.create(
        account=social_account,
        token="access",
        token_secret="refresh",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    _owner_token(user, organization, calendar=calendar)
    facade = CalendarService()
    facade.authenticate(account=user, organization=organization)
    return CalendarEventService(
        context=facade._context,
        recurrence_manager=facade._recurrence_manager,
        calendar_cache=facade._calendar_cache,
        host=facade,
    )


def _room(organization, name: str, provider: str = CalendarProvider.GOOGLE) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name=name,
        email=f"{name.lower().replace(' ', '-')}@resource.example.com",
        external_id=f"ext-{name}",
        provider=provider,
        calendar_type=CalendarType.RESOURCE,
    )


def _event_input(*rooms: Calendar) -> CalendarEventInputData:
    return CalendarEventInputData(
        title="Meeting",
        description="",
        start_time=START,
        end_time=END,
        timezone="UTC",
        resource_allocations=[ResourceAllocationInputData(resource_id=room.id) for room in rooms],
    )


def _allocated_room_ids(organization, event: CalendarEvent) -> set[int]:
    return set(
        ResourceAllocation.objects.filter_by_organization(organization.id)
        .filter(event=event)
        .values_list("calendar_fk_id", flat=True)
    )


@pytest.mark.django_db
class TestCreateEvent:
    @pytest.mark.parametrize(
        ("sync_status", "failed_operation", "message"),
        [
            (ResourceSyncStatus.PENDING_CREATION, "", "pending creation"),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.CREATE, "sync failed on create"),
            (ResourceSyncStatus.PENDING_DELETION, "", "pending deletion"),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.DELETE, "sync failed on delete"),
            (ResourceSyncStatus.ARCHIVED, "", "archived"),
        ],
    )
    def test_room_that_is_not_bookable_is_rejected(
        self,
        event_service,
        organization,
        calendar,
        mock_google_adapter,
        sync_status,
        failed_operation,
        message,
    ):
        room = _room(organization, "Room A")
        create_resource_provider_link(
            calendar=room, sync_status=sync_status, failed_operation=failed_operation
        )

        with pytest.raises(RoomNotBookableError, match=rf"Room A is not bookable: {message}\."):
            event_service.create_event(calendar.id, _event_input(room))

        mock_google_adapter.create_event.assert_not_called()
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    @pytest.mark.parametrize(
        ("sync_status", "failed_operation"),
        [
            (ResourceSyncStatus.SYNCED, ""),
            (ResourceSyncStatus.PENDING_UPDATE, ""),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.UPDATE),
        ],
    )
    def test_bookable_room_is_allocated(
        self, event_service, organization, calendar, sync_status, failed_operation
    ):
        room = _room(organization, "Room A")
        create_resource_provider_link(
            calendar=room, sync_status=sync_status, failed_operation=failed_operation
        )

        event = event_service.create_event(calendar.id, _event_input(room))

        assert _allocated_room_ids(organization, event) == {room.id}

    def test_rooms_without_a_link_book_as_before(self, event_service, organization, calendar):
        manual = _room(organization, "Manual", provider=CalendarProvider.INTERNAL)
        imported = _room(organization, "Imported")
        assert not ResourceCalendarProviderLink.objects.filter_by_organization(
            organization.id
        ).exists()

        event = event_service.create_event(calendar.id, _event_input(manual, imported))

        assert _allocated_room_ids(organization, event) == {manual.id, imported.id}


@pytest.fixture
def org_wide_facade(organization, di_container):
    """A facade acting as an org-wide public-API token, which may update any event.

    ``update_event`` called by a ``User`` with rooms fails before the guard, in the
    permission diff (``calendar_service_utils.py`` reads ``.calendar`` off a
    ``Calendar``), so the update cases go through the public-API path.
    """
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="room_guard_update", organization=organization
    )
    facade = di_container.calendar_service()
    facade.initialize_without_provider(user_or_token=system_user, organization=organization)
    return facade


@pytest.fixture
def internal_event(organization):
    internal_calendar = Calendar.objects.create(
        name="Internal Calendar", external_id="guard_internal_cal", organization=organization
    )
    return CalendarEvent.objects.create(
        organization=organization,
        calendar=internal_calendar,
        title="Meeting",
        start_time_tz_unaware=START.replace(tzinfo=None),
        end_time_tz_unaware=END.replace(tzinfo=None),
        timezone="UTC",
    )


@pytest.mark.django_db
class TestUpdateEvent:
    def test_adding_a_room_that_is_not_bookable_is_rejected(
        self, org_wide_facade, organization, internal_event
    ):
        archived = _room(organization, "Archived")
        create_resource_provider_link(calendar=archived, sync_status=ResourceSyncStatus.ARCHIVED)

        with pytest.raises(RoomNotBookableError, match=r"Archived is not bookable: archived\."):
            org_wide_facade.update_event(
                internal_event.calendar_fk_id, internal_event.id, _event_input(archived)
            )

        assert _allocated_room_ids(organization, internal_event) == set()

    def test_room_the_event_already_holds_is_not_checked(
        self, org_wide_facade, organization, internal_event
    ):
        room = _room(organization, "Room A")
        create_resource_provider_link(calendar=room, sync_status=ResourceSyncStatus.ARCHIVED)
        ResourceAllocation.objects.create(
            organization=organization, event=internal_event, calendar=room
        )

        updated = org_wide_facade.update_event(
            internal_event.calendar_fk_id, internal_event.id, _event_input(room)
        )

        assert _allocated_room_ids(organization, updated) == {room.id}


@pytest.fixture
def owner_scoped_facade(organization, user, di_container, internal_event):
    """A facade acting as a public-API token scoped to the owner of the internal calendar.

    The one actor that can both update and create events on that calendar today: an
    org-wide token may not create events, and a ``User`` update with rooms fails in
    the permission diff (see ``org_wide_facade``).
    """
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=user, organization=organization, defaults={"is_active": True}
    )
    CalendarOwnership.objects.create(
        calendar=internal_event.calendar, membership_user_id=user.id, organization=organization
    )
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="room_guard_split",
        organization=organization,
        scoped_to_membership=membership,
    )
    facade = di_container.calendar_service()
    facade.initialize_without_provider(user_or_token=system_user, organization=organization)
    return facade


def _archive(room: Calendar) -> None:
    """Archive the room after it was booked, as a provider-side deletion does."""
    link = ResourceCalendarProviderLink.objects.filter_by_organization(room.organization_id).get(
        calendar=room
    )
    link.sync_status = ResourceSyncStatus.ARCHIVED
    link.save()


@pytest.mark.django_db
class TestCarriedOverRooms:
    """Writes that copy an existing booking's rooms do not book those rooms anew."""

    def test_transfer_keeps_a_room_archived_since_booking(
        self, event_service, organization, calendar, user, mock_google_adapter
    ):
        mock_google_adapter.create_event.side_effect = [
            _adapter_output("original-event"),
            _adapter_output("transferred-event"),
        ]
        mock_google_adapter.get_event.return_value = _adapter_output("original-event")
        room = _room(organization, "Room A")
        create_resource_provider_link(calendar=room)
        original = event_service.create_event(calendar.id, _event_input(room))
        _owner_token(user, organization, event_fk=original)
        target = Calendar.objects.create(
            name="Target Calendar",
            external_id="guard_target_cal",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        )
        _owner_token(user, organization, calendar=target)
        _archive(room)

        moved = event_service.transfer_event(original, target)

        assert moved.calendar == target
        assert _allocated_room_ids(organization, moved) == {room.id}

    def test_editing_a_series_from_a_date_keeps_a_room_archived_since_booking(
        self, owner_scoped_facade, organization, internal_event
    ):
        room = _room(organization, "Room A")
        create_resource_provider_link(calendar=room)
        series = owner_scoped_facade.create_event(
            internal_event.calendar_fk_id,
            CalendarEventInputData(
                title="Daily",
                description="",
                # An hour after ``internal_event``, which would otherwise take the slot.
                start_time=END,
                end_time=END + datetime.timedelta(hours=1),
                timezone="UTC",
                recurrence_rule="RRULE:FREQ=DAILY;COUNT=5",
                resource_allocations=[ResourceAllocationInputData(resource_id=room.id)],
            ),
        )
        _archive(room)

        continuation = owner_scoped_facade.modify_recurring_event_from_date(
            parent_event=series,
            modification_start_date=END + datetime.timedelta(days=2),
            modified_title="Renamed",
        )

        assert continuation is not None
        assert continuation.title == "Renamed"
        assert _allocated_room_ids(organization, continuation) == {room.id}
        assert _allocated_room_ids(organization, series) == {room.id}
