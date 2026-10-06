"""Unit tests for the room provider sync schema and contracts.

Covers the four new models (``ResourceLocation``, ``ResourceCalendarProviderLink``,
``ResourceCalendarCreateRequest``, ``MicrosoftOrganizationConnection``), the link's
pure helpers, the queryset methods the push engine and resync rely on, the room
dataclasses, the ``ResourceDirectoryError`` family, the two room signals, and a fake
implementation of the adapter protocols that mypy checks against them.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Collection
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import (
    ResourceDirectoryError,
    ResourceDirectoryInvalidInputError,
    ResourceDirectoryNotFoundError,
    ResourceDirectoryNotWriteEnabledError,
    ResourceDirectoryPermissionError,
)
from calendar_integration.factories import (
    create_microsoft_organization_connection,
    create_resource_location,
    create_resource_provider_link,
)
from calendar_integration.models import (
    Calendar,
    GoogleCalendarServiceAccount,
    MicrosoftOrganizationConnection,
    ResourceCalendarCreateRequest,
    ResourceCalendarProviderLink,
    ResourceLocation,
)
from calendar_integration.services.dataclasses import (
    BusyWindow,
    ResourceLocationData,
    ResourceLocationRef,
    RoomDirectoryData,
    RoomWriteData,
)
from calendar_integration.services.protocols.resource_directory_adapter import (
    ResourceDirectoryAdapter,
    ResourceDirectoryAdapterResolver,
)
from calendar_integration.signals import resource_room_archived, resource_room_synced
from common.organization_context import organization_context
from organizations.models import Organization


LOCATION_REF = {"external_building_id": "b-1", "external_floor_id": "2"}
SNAPSHOT = {
    "name": "Huddle",
    "description": "Small room",
    "capacity": 4,
    "location_ref": LOCATION_REF,
}


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def other_organization(db) -> Organization:
    return baker.make(Organization)


def _make_room(organization: Organization, provider: str = CalendarProvider.GOOGLE) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        provider=provider,
        external_id=f"room-{uuid.uuid4()}",
    )


@pytest.fixture
def room(organization: Organization) -> Calendar:
    return _make_room(organization)


def _link(**kwargs: Any) -> ResourceCalendarProviderLink:
    """An unsaved link, for the pure helpers."""
    return ResourceCalendarProviderLink(**kwargs)


class TestFieldsChangedByProvider:
    def test_no_change_returns_empty_set(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))

        assert link.fields_changed_by_provider(dict(SNAPSHOT)) == set()

    def test_single_field_change(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))

        changed = link.fields_changed_by_provider({**SNAPSHOT, "name": "Huddle One"})

        assert changed == {"name"}

    def test_location_change(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))
        moved = {"external_building_id": "b-1", "external_floor_id": "3"}

        changed = link.fields_changed_by_provider({**SNAPSHOT, "location_ref": moved})

        assert changed == {"location_ref"}

    def test_location_removed_counts_as_change(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))

        changed = link.fields_changed_by_provider({**SNAPSHOT, "location_ref": None})

        assert changed == {"location_ref"}

    def test_field_the_provider_does_not_report_is_never_changed(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))
        without_description = {k: v for k, v in SNAPSHOT.items() if k != "description"}

        assert link.fields_changed_by_provider(without_description) == set()

    def test_unknown_keys_are_ignored(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))

        assert link.fields_changed_by_provider({**SNAPSHOT, "color": "blue"}) == set()

    def test_empty_snapshot_counts_every_non_null_reported_field(self):
        link = _link(provider_snapshot={})

        changed = link.fields_changed_by_provider({**SNAPSHOT, "capacity": None})

        assert changed == {"name", "description", "location_ref"}

    def test_matches_room_directory_synced_values(self):
        link = _link(provider_snapshot=dict(SNAPSHOT))
        room = RoomDirectoryData(
            external_id="ext-1",
            email="huddle@example.com",
            name="Huddle",
            description="Small room",
            capacity=6,
            location_ref=ResourceLocationRef("b-1", "2"),
        )

        assert link.fields_changed_by_provider(room.synced_values()) == {"capacity"}


class TestIsBookable:
    @pytest.mark.parametrize(
        ("sync_status", "failed_operation", "expected"),
        [
            (ResourceSyncStatus.PENDING_CREATION, "", False),
            (ResourceSyncStatus.SYNCED, "", True),
            (ResourceSyncStatus.PENDING_UPDATE, "", True),
            (ResourceSyncStatus.PENDING_DELETION, "", False),
            (ResourceSyncStatus.ARCHIVED, "", False),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.CREATE, False),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.UPDATE, True),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.DELETE, False),
        ],
    )
    def test_is_bookable(self, sync_status: str, failed_operation: str, expected: bool):
        link = _link(sync_status=sync_status, failed_operation=failed_operation)

        assert link.is_bookable is expected

    def test_every_status_is_covered(self):
        # Guards the parametrization above against a status added later.
        assert set(ResourceSyncStatus.values) == {
            "pending_creation",
            "synced",
            "pending_update",
            "pending_deletion",
            "sync_failed",
            "archived",
        }


class TestMarkPushed:
    def test_clears_fields_whose_pushed_value_still_matches(self):
        link = _link(
            provider_snapshot=dict(SNAPSHOT),
            pending_fields={"name": "Huddle One", "capacity": 8},
        )

        cleared = link.mark_pushed({"name": "Huddle One", "capacity": 8})

        assert cleared == {"name", "capacity"}
        assert link.pending_fields == {}
        assert link.provider_snapshot == {**SNAPSHOT, "name": "Huddle One", "capacity": 8}

    def test_keeps_a_field_edited_again_while_the_push_was_in_flight(self):
        link = _link(
            provider_snapshot=dict(SNAPSHOT),
            pending_fields={"name": "Huddle Two", "capacity": 8},
        )

        cleared = link.mark_pushed({"name": "Huddle One", "capacity": 8})

        assert cleared == {"capacity"}
        assert link.pending_fields == {"name": "Huddle Two"}

    def test_keeps_pending_fields_that_were_not_pushed(self):
        link = _link(
            provider_snapshot=dict(SNAPSHOT),
            pending_fields={"name": "Huddle One", "description": "New"},
        )

        cleared = link.mark_pushed({"name": "Huddle One"})

        assert cleared == {"name"}
        assert link.pending_fields == {"description": "New"}

    def test_snapshot_takes_the_provider_values_when_given(self):
        link = _link(provider_snapshot=dict(SNAPSHOT), pending_fields={"name": "huddle one"})

        link.mark_pushed({"name": "huddle one"}, {**SNAPSHOT, "name": "Huddle One"})

        assert link.pending_fields == {}
        assert link.provider_snapshot == {**SNAPSHOT, "name": "Huddle One"}

    def test_snapshot_keeps_unpushed_fields_the_provider_changed(self):
        # An IT admin changed capacity on the provider; Vinta Schedule pushed only a
        # rename. Calendar still holds the old capacity, so the snapshot must too,
        # or the next resync would see no capacity change and never import it.
        link = _link(provider_snapshot=dict(SNAPSHOT), pending_fields={"name": "Huddle One"})
        provider_room = {**SNAPSHOT, "name": "Huddle One", "capacity": 20}

        link.mark_pushed({"name": "Huddle One"}, provider_room)

        assert link.provider_snapshot == {**SNAPSHOT, "name": "Huddle One"}
        assert link.fields_changed_by_provider(provider_room) == {"capacity"}

    def test_pushed_field_the_provider_does_not_report_takes_the_pushed_value(self):
        link = _link(provider_snapshot=dict(SNAPSHOT), pending_fields={"description": "New"})
        without_description = {k: v for k, v in SNAPSHOT.items() if k != "description"}

        link.mark_pushed({"description": "New"}, without_description)

        assert link.provider_snapshot == {**SNAPSHOT, "description": "New"}

    def test_snapshot_ignores_unknown_keys(self):
        link = _link(provider_snapshot={}, pending_fields={"name": "Huddle", "color": "blue"})

        link.mark_pushed({"name": "Huddle", "color": "blue"}, {"name": "Huddle", "color": "red"})

        assert link.provider_snapshot == {"name": "Huddle"}

    def test_sets_last_synced_at(self):
        link = _link(provider_snapshot={}, pending_fields={})
        before = timezone.now()

        link.mark_pushed({})

        assert link.last_synced_at is not None
        assert link.last_synced_at >= before

    def test_does_not_change_status_or_retry_state(self):
        deadline = timezone.now() + datetime.timedelta(hours=6)
        link = _link(
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            attempt_count=3,
            retry_deadline=deadline,
            provider_snapshot={},
            pending_fields={"name": "x"},
        )

        link.mark_pushed({"name": "x"})

        assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
        assert link.attempt_count == 3
        assert link.retry_deadline == deadline


@pytest.mark.django_db
class TestFactoriesRoundTrip:
    def test_resource_location(self, organization: Organization):
        location = create_resource_location(
            organization=organization,
            provider=CalendarProvider.MICROSOFT,
            external_building_id="bld",
            building_name="HQ",
            external_floor_id="flr",
            floor_name="Floor 3",
        )

        location.refresh_from_db()
        assert location.organization == organization
        assert location.provider == CalendarProvider.MICROSOFT
        assert location.is_active is True
        assert location.location_ref == {"external_building_id": "bld", "external_floor_id": "flr"}
        assert str(location) == "HQ / Floor 3"

    def test_resource_provider_link(self, organization: Organization, room: Calendar):
        location = create_resource_location(organization=organization)

        link = create_resource_provider_link(
            calendar=room,
            location=location,
            provider_snapshot=dict(SNAPSHOT),
            pending_fields={"name": "New"},
        )

        link.refresh_from_db()
        assert link.organization == organization
        assert link.calendar == room
        assert link.provider == CalendarProvider.GOOGLE
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.location == location
        assert link.provider_snapshot == SNAPSHOT
        assert link.pending_fields == {"name": "New"}
        assert isinstance(link.provisional_key, uuid.UUID)
        assert link.attempt_count == 0
        assert link.failed_operation == ""
        # The reverse one-to-one accessor the rest of the plan reads.
        assert Calendar.objects.filter_by_organization(organization).get(
            pk=room.pk
        ).provider_link == (link)

    def test_microsoft_organization_connection(self, organization: Organization):
        connection = create_microsoft_organization_connection(
            organization=organization, tenant_id="tenant-1"
        )

        connection.refresh_from_db()
        assert connection.organization == organization
        assert connection.tenant_id == "tenant-1"
        assert connection.write_enabled is False
        assert connection.consented_at is not None

    def test_create_request(self, organization: Organization, room: Calendar):
        expires_at = timezone.now() + datetime.timedelta(hours=24)

        request = ResourceCalendarCreateRequest.objects.create(
            organization=organization,
            idempotency_key="K1",
            request_fingerprint="a" * 64,
            calendar=room,
            expires_at=expires_at,
        )

        request.refresh_from_db()
        assert request.calendar == room
        assert request.expires_at == expires_at

    def test_google_service_account_write_fields_default_off(self, organization: Organization):
        account = GoogleCalendarServiceAccount.objects.create(
            organization=organization,
            email="service@example.com",
            admin_email="admin@example.com",
            private_key_id="test_key_id",
            private_key="test_private_key",
        )

        account.refresh_from_db()
        assert account.write_enabled is False
        assert account.write_verified_at is None


@pytest.mark.django_db
class TestUniquenessConstraints:
    def test_one_link_per_calendar(self, room: Calendar):
        create_resource_provider_link(calendar=room)

        with pytest.raises(IntegrityError), transaction.atomic():
            create_resource_provider_link(calendar=room)

    def test_provisional_key_is_unique(self, organization: Organization, room: Calendar):
        link = create_resource_provider_link(calendar=room)
        other_room = _make_room(organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            create_resource_provider_link(calendar=other_room, provisional_key=link.provisional_key)

    def test_location_unique_per_provider_building_and_floor(self, organization: Organization):
        create_resource_location(organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            create_resource_location(organization=organization)

    def test_same_location_allowed_on_another_provider_floor_or_org(
        self, organization: Organization, other_organization: Organization
    ):
        create_resource_location(organization=organization)

        create_resource_location(organization=organization, provider=CalendarProvider.MICROSOFT)
        create_resource_location(organization=organization, external_floor_id="2")
        create_resource_location(organization=other_organization)

        assert ResourceLocation.objects.filter_by_organization(organization).count() == 3
        assert ResourceLocation.objects.filter_by_organization(other_organization).count() == 1

    def test_idempotency_key_unique_per_organization(
        self, organization: Organization, other_organization: Organization, room: Calendar
    ):
        expires_at = timezone.now() + datetime.timedelta(hours=24)
        ResourceCalendarCreateRequest.objects.create(
            organization=organization,
            idempotency_key="K1",
            request_fingerprint="a" * 64,
            calendar=room,
            expires_at=expires_at,
        )
        ResourceCalendarCreateRequest.objects.create(
            organization=other_organization,
            idempotency_key="K1",
            request_fingerprint="a" * 64,
            calendar=_make_room(other_organization),
            expires_at=expires_at,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            ResourceCalendarCreateRequest.objects.create(
                organization=organization,
                idempotency_key="K1",
                request_fingerprint="b" * 64,
                calendar=_make_room(organization),
                expires_at=expires_at,
            )

    def test_one_microsoft_connection_per_organization(self, organization: Organization):
        create_microsoft_organization_connection(organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            create_microsoft_organization_connection(organization=organization)

    def test_location_referenced_by_a_link_cannot_be_deleted(
        self, organization: Organization, room: Calendar
    ):
        location = create_resource_location(organization=organization)
        create_resource_provider_link(calendar=room, location=location)

        with pytest.raises(ProtectedError):
            ResourceLocation.original_manager.filter(pk=location.pk).delete()


@pytest.mark.django_db
class TestOrganizationScoping:
    def test_link_in_org_a_is_invisible_from_org_b(
        self, organization: Organization, other_organization: Organization, room: Calendar
    ):
        link = create_resource_provider_link(calendar=room)

        with organization_context(other_organization):
            assert list(ResourceCalendarProviderLink.objects.all()) == []
            with pytest.raises(ResourceCalendarProviderLink.DoesNotExist):
                ResourceCalendarProviderLink.objects.get(pk=link.pk)
        with organization_context(organization):
            assert list(ResourceCalendarProviderLink.objects.all()) == [link]

    def test_location_and_connection_are_scoped(
        self, organization: Organization, other_organization: Organization
    ):
        location = create_resource_location(organization=organization)
        connection = create_microsoft_organization_connection(organization=organization)
        expires_at = timezone.now() + datetime.timedelta(hours=1)
        request = ResourceCalendarCreateRequest.objects.create(
            organization=organization,
            idempotency_key="K1",
            request_fingerprint="a" * 64,
            calendar=_make_room(organization),
            expires_at=expires_at,
        )

        with organization_context(other_organization):
            assert not ResourceLocation.objects.exists()
            assert not MicrosoftOrganizationConnection.objects.exists()
            assert not ResourceCalendarCreateRequest.objects.exists()
        with organization_context(organization):
            assert list(ResourceLocation.objects.all()) == [location]
            assert list(MicrosoftOrganizationConnection.objects.all()) == [connection]
            assert list(ResourceCalendarCreateRequest.objects.all()) == [request]


@pytest.mark.django_db
class TestLinkQuerySet:
    def _links_by_status(
        self, organization: Organization
    ) -> dict[str, ResourceCalendarProviderLink]:
        links = {}
        for status in ResourceSyncStatus.values:
            links[status] = create_resource_provider_link(
                calendar=_make_room(organization), sync_status=status
            )
        return links

    def test_due_for_push(self, organization: Organization):
        links = self._links_by_status(organization)

        with organization_context(organization):
            due = set(ResourceCalendarProviderLink.objects.due_for_push())

        assert due == {
            links[ResourceSyncStatus.PENDING_CREATION],
            links[ResourceSyncStatus.PENDING_UPDATE],
            links[ResourceSyncStatus.PENDING_DELETION],
        }

    def test_on_provider_matches_is_bookable_for_every_state(self, organization: Organization):
        links = [
            create_resource_provider_link(
                calendar=_make_room(organization),
                sync_status=sync_status,
                failed_operation=failed_operation,
            )
            for sync_status in ResourceSyncStatus.values
            for failed_operation in ["", *ResourceSyncOperation.values]
        ]

        with organization_context(organization):
            on_provider = set(ResourceCalendarProviderLink.objects.on_provider())

        assert on_provider == {link for link in links if link.is_bookable}
        assert {(link.sync_status, link.failed_operation) for link in on_provider} == {
            (ResourceSyncStatus.SYNCED, ""),
            (ResourceSyncStatus.SYNCED, ResourceSyncOperation.CREATE),
            (ResourceSyncStatus.SYNCED, ResourceSyncOperation.UPDATE),
            (ResourceSyncStatus.SYNCED, ResourceSyncOperation.DELETE),
            (ResourceSyncStatus.PENDING_UPDATE, ""),
            (ResourceSyncStatus.PENDING_UPDATE, ResourceSyncOperation.CREATE),
            (ResourceSyncStatus.PENDING_UPDATE, ResourceSyncOperation.UPDATE),
            (ResourceSyncStatus.PENDING_UPDATE, ResourceSyncOperation.DELETE),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.UPDATE),
        }

    def test_for_resync(self, organization: Organization):
        links = self._links_by_status(organization)
        failed_update = create_resource_provider_link(
            calendar=_make_room(organization),
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )
        create_resource_provider_link(
            calendar=_make_room(organization),
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.DELETE,
        )
        create_resource_provider_link(
            calendar=_make_room(organization, provider=CalendarProvider.MICROSOFT),
            sync_status=ResourceSyncStatus.SYNCED,
        )

        with organization_context(organization):
            resync = set(ResourceCalendarProviderLink.objects.for_resync(CalendarProvider.GOOGLE))

        assert resync == {
            links[ResourceSyncStatus.SYNCED],
            links[ResourceSyncStatus.PENDING_UPDATE],
            failed_update,
        }

    def test_with_flagged_bookings(self, organization: Organization):
        flagged = create_resource_provider_link(
            calendar=_make_room(organization),
            sync_status=ResourceSyncStatus.ARCHIVED,
            flagged_bookings_at=timezone.now(),
        )
        create_resource_provider_link(
            calendar=_make_room(organization), sync_status=ResourceSyncStatus.ARCHIVED
        )

        with organization_context(organization):
            assert list(ResourceCalendarProviderLink.objects.with_flagged_bookings()) == [flagged]

    @pytest.mark.parametrize("skip_locked", [False, True])
    def test_locked_for_update_returns_the_link_inside_a_transaction(
        self, organization: Organization, room: Calendar, skip_locked: bool
    ):
        link = create_resource_provider_link(calendar=room)
        create_resource_provider_link(calendar=_make_room(organization))

        with organization_context(organization), transaction.atomic():
            locked = list(
                ResourceCalendarProviderLink.objects.locked_for_update(
                    link.pk, skip_locked=skip_locked
                )
            )

        assert locked == [link]

    def test_locked_for_update_is_organization_scoped(
        self, other_organization: Organization, room: Calendar
    ):
        link = create_resource_provider_link(calendar=room)

        with organization_context(other_organization), transaction.atomic():
            locked = list(ResourceCalendarProviderLink.objects.locked_for_update(link.pk))

        assert locked == []


@pytest.mark.django_db
class TestLocationAndCreateRequestQuerySets:
    def test_active_and_for_provider(self, organization: Organization):
        google_active = create_resource_location(organization=organization)
        create_resource_location(organization=organization, external_floor_id="2", is_active=False)
        create_resource_location(organization=organization, provider=CalendarProvider.MICROSOFT)

        with organization_context(organization):
            found = list(ResourceLocation.objects.active().for_provider(CalendarProvider.GOOGLE))

        assert found == [google_active]

    def test_expired_and_live(self, organization: Organization):
        now = timezone.now()
        expired = ResourceCalendarCreateRequest.objects.create(
            organization=organization,
            idempotency_key="old",
            request_fingerprint="a" * 64,
            calendar=_make_room(organization),
            expires_at=now - datetime.timedelta(minutes=1),
        )
        live = ResourceCalendarCreateRequest.objects.create(
            organization=organization,
            idempotency_key="new",
            request_fingerprint="a" * 64,
            calendar=_make_room(organization),
            expires_at=now + datetime.timedelta(hours=1),
        )

        with organization_context(organization):
            assert list(ResourceCalendarCreateRequest.objects.expired(now)) == [expired]
            assert list(ResourceCalendarCreateRequest.objects.live(now)) == [live]


class TestRoomDataclasses:
    def test_location_ref_round_trip(self):
        ref = ResourceLocationRef("b-1", "2")

        assert ref.to_dict() == LOCATION_REF
        assert ResourceLocationRef.from_dict(ref.to_dict()) == ref
        assert ResourceLocationRef.from_dict(None) is None
        assert ResourceLocationRef.from_dict({"external_building_id": "b"}) == (
            ResourceLocationRef("b", "")
        )

    def test_location_data_ref(self):
        data = ResourceLocationData("b-1", "HQ", "2", "Floor 2")

        assert data.ref == ResourceLocationRef("b-1", "2")

    def test_synced_values_omits_a_description_the_provider_does_not_carry(self):
        room = RoomDirectoryData(
            external_id="ext-1",
            email="huddle@example.com",
            name="Huddle",
            description=None,
            capacity=None,
            location_ref=None,
        )

        assert room.synced_values() == {"name": "Huddle", "capacity": None, "location_ref": None}

    def test_synced_values_includes_every_field(self):
        room = RoomDirectoryData(
            external_id="ext-1",
            email="huddle@example.com",
            name="Huddle",
            description="Small room",
            capacity=4,
            location_ref=ResourceLocationRef("b-1", "2"),
        )

        assert room.synced_values() == SNAPSHOT

    def test_room_write_data_from_synced_values(self):
        key = uuid.uuid4()

        assert RoomWriteData.from_synced_values(SNAPSHOT, key) == RoomWriteData(
            name="Huddle",
            description="Small room",
            capacity=4,
            location_ref=ResourceLocationRef("b-1", "2"),
            provisional_key=key,
        )
        assert RoomWriteData.from_synced_values({"name": "Bare"}, key) == RoomWriteData(
            name="Bare", description="", capacity=None, location_ref=None, provisional_key=key
        )


class TestResourceDirectoryErrors:
    @pytest.mark.parametrize(
        ("error_class", "is_transient"),
        [
            (ResourceDirectoryError, True),
            (ResourceDirectoryInvalidInputError, False),
            (ResourceDirectoryPermissionError, True),
            (ResourceDirectoryNotFoundError, False),
            (ResourceDirectoryNotWriteEnabledError, True),
        ],
    )
    def test_default_transience(self, error_class: type[ResourceDirectoryError], is_transient):
        error = error_class()

        assert error.is_transient is is_transient
        assert isinstance(error, ResourceDirectoryError)
        assert str(error) == error_class.default_message

    def test_transience_and_message_can_be_overridden(self):
        error = ResourceDirectoryError("quota exceeded for the day", is_transient=False)

        assert error.is_transient is False
        assert str(error) == "quota exceeded for the day"


@pytest.mark.django_db
class TestRoomSignals:
    # Needs the database: the real Google event-sync receiver also hears the send.
    def test_signals_deliver_their_keyword_arguments(self) -> None:
        received: list[tuple[str, dict[str, Any]]] = []

        def on_synced(sender: Any, **kwargs: Any) -> None:
            received.append(("synced", kwargs))

        def on_archived(sender: Any, **kwargs: Any) -> None:
            received.append(("archived", kwargs))

        resource_room_synced.connect(on_synced, weak=False)
        resource_room_archived.connect(on_archived, weak=False)
        try:
            resource_room_synced.send(
                sender=ResourceCalendarProviderLink,
                calendar_id=1,
                organization_id=7,
                provider=CalendarProvider.GOOGLE,
                created=True,
            )
            resource_room_archived.send(
                sender=ResourceCalendarProviderLink,
                calendar_id=2,
                organization_id=7,
                provider="microsoft",
            )
        finally:
            resource_room_synced.disconnect(on_synced)
            resource_room_archived.disconnect(on_archived)

        assert [
            (name, {k: v for k, v in kw.items() if k != "signal"}) for name, kw in received
        ] == [
            (
                "synced",
                {
                    "calendar_id": 1,
                    "organization_id": 7,
                    "provider": CalendarProvider.GOOGLE,
                    "created": True,
                },
            ),
            ("archived", {"calendar_id": 2, "organization_id": 7, "provider": "microsoft"}),
        ]


class FakeResourceDirectoryAdapter:
    """An in-memory room directory. Assigned to the protocol type below, so mypy
    fails this file if the protocol and a structural implementation drift apart."""

    provider: str = CalendarProvider.GOOGLE

    def __init__(self) -> None:
        self.rooms: dict[str, RoomDirectoryData] = {}
        self.busy: list[BusyWindow] = []

    def list_locations(self) -> list[ResourceLocationData]:
        return [ResourceLocationData("b-1", "HQ", "2", "Floor 2")]

    def list_rooms(self) -> list[RoomDirectoryData]:
        return list(self.rooms.values())

    def create_room(self, data: RoomWriteData) -> RoomDirectoryData:
        external_id = f"vinta-{data.provisional_key}"
        if external_id not in self.rooms:
            self.rooms[external_id] = RoomDirectoryData(
                external_id=external_id,
                email=f"{external_id}@resource.example.com",
                name=data.name,
                description=data.description,
                capacity=data.capacity,
                location_ref=data.location_ref,
            )
        return self.rooms[external_id]

    def update_room(
        self, external_id: str, data: RoomWriteData, fields: Collection[str]
    ) -> RoomDirectoryData:
        if external_id not in self.rooms:
            raise ResourceDirectoryNotFoundError()
        room = self.rooms[external_id]
        for field in fields:
            setattr(room, field, getattr(data, field))
        return room

    def delete_room(self, external_id: str) -> None:
        if self.rooms.pop(external_id, None) is None:
            raise ResourceDirectoryNotFoundError()

    def get_free_busy(
        self, room_email: str, start: datetime.datetime, end: datetime.datetime
    ) -> list[BusyWindow]:
        return [window for window in self.busy if window.start < end and window.end > start]


class FakeResourceDirectoryAdapterResolver:
    def __init__(self, adapter: ResourceDirectoryAdapter, write_enabled: bool = True) -> None:
        self.adapter = adapter
        self.write_enabled = write_enabled

    def adapter_for(self, organization: Organization, provider: str) -> ResourceDirectoryAdapter:
        if not self.is_write_enabled(organization, provider):
            raise ResourceDirectoryNotWriteEnabledError()
        return self.adapter

    def is_write_enabled(self, organization: Organization, provider: str) -> bool:
        return self.write_enabled and provider == self.adapter.provider


class TestAdapterProtocols:
    def test_fake_adapter_satisfies_the_protocol(self) -> None:
        adapter: ResourceDirectoryAdapter = FakeResourceDirectoryAdapter()
        write = RoomWriteData.from_synced_values(SNAPSHOT, uuid.uuid4())

        created = adapter.create_room(write)
        replayed = adapter.create_room(write)
        updated = adapter.update_room(
            created.external_id,
            RoomWriteData.from_synced_values(
                {**SNAPSHOT, "name": "Renamed"}, write.provisional_key
            ),
            ["name"],
        )

        assert replayed is created
        assert adapter.list_rooms() == [updated]
        assert updated.name == "Renamed"
        adapter.delete_room(created.external_id)
        assert adapter.list_rooms() == []
        with pytest.raises(ResourceDirectoryNotFoundError):
            adapter.delete_room(created.external_id)

    def test_fake_resolver_satisfies_the_protocol(self) -> None:
        organization = Organization(name="Unsaved")
        adapter = FakeResourceDirectoryAdapter()
        resolver: ResourceDirectoryAdapterResolver = FakeResourceDirectoryAdapterResolver(adapter)
        disabled: ResourceDirectoryAdapterResolver = FakeResourceDirectoryAdapterResolver(
            adapter, write_enabled=False
        )

        assert resolver.adapter_for(organization, CalendarProvider.GOOGLE) is adapter
        assert resolver.is_write_enabled(organization, CalendarProvider.MICROSOFT) is False
        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            disabled.adapter_for(organization, CalendarProvider.GOOGLE)
