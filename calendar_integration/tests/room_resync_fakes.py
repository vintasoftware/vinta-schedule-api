"""In-memory stand-ins for a provider room directory, for the room resync tests.

``FakeRoomDirectory`` implements ``ResourceDirectoryAdapter`` over two plain
lists a test edits between runs, the way a provider admin would edit the
directory. ``FakeAdapterResolver`` hands it out for the organizations and
providers a test marks write-enabled.
"""

import datetime
from collections.abc import Collection
from dataclasses import dataclass, field

from calendar_integration.exceptions import ResourceDirectoryNotWriteEnabledError
from calendar_integration.services.dataclasses import (
    BusyWindow,
    ResourceLocationData,
    ResourceLocationRef,
    RoomDirectoryData,
    RoomWriteData,
)
from organizations.models import Organization


@dataclass
class FakeRoomDirectory:
    """A provider room directory held in memory. The resync only reads it."""

    provider: str
    locations: list[ResourceLocationData] = field(default_factory=list)
    rooms: list[RoomDirectoryData] = field(default_factory=list)
    list_calls: int = 0

    def list_locations(self) -> list[ResourceLocationData]:
        """The locations as the test left them."""
        return list(self.locations)

    def list_rooms(self) -> list[RoomDirectoryData]:
        """The rooms as the test left them."""
        self.list_calls += 1
        return list(self.rooms)

    def room(self, external_id: str) -> RoomDirectoryData:
        """The listed room ``external_id``, for a test to edit in place."""
        return next(room for room in self.rooms if room.external_id == external_id)

    def remove_room(self, external_id: str) -> None:
        """Delete the room ``external_id`` from the directory."""
        self.rooms = [room for room in self.rooms if room.external_id != external_id]

    def create_room(self, data: RoomWriteData) -> RoomDirectoryData:
        """Not used by the resync."""
        raise NotImplementedError

    def update_room(
        self, external_id: str, data: RoomWriteData, fields: Collection[str]
    ) -> RoomDirectoryData:
        """Not used by the resync."""
        raise NotImplementedError

    def delete_room(self, external_id: str) -> None:
        """Not used by the resync."""
        raise NotImplementedError

    def get_free_busy(
        self, room_email: str, start: datetime.datetime, end: datetime.datetime
    ) -> list[BusyWindow]:
        """Not used by the resync."""
        raise NotImplementedError


@dataclass
class FakeAdapterResolver:
    """Resolves ``FakeRoomDirectory`` instances for write-enabled ``(organization, provider)`` pairs."""

    directories: dict[tuple[int, str], FakeRoomDirectory] = field(default_factory=dict)

    def enable(self, organization: Organization, directory: FakeRoomDirectory) -> None:
        """Make ``organization`` write-enabled on ``directory.provider``, served by ``directory``."""
        self.directories[(organization.id, directory.provider)] = directory

    def adapter_for(self, organization: Organization, provider: str) -> FakeRoomDirectory:
        """The directory for the pair, or the not-write-enabled error the real resolver raises."""
        directory = self.directories.get((organization.id, provider))
        if directory is None:
            raise ResourceDirectoryNotWriteEnabledError()
        return directory

    def is_write_enabled(self, organization: Organization, provider: str) -> bool:
        """Whether a directory was registered for the pair."""
        return (organization.id, provider) in self.directories


def make_room(
    external_id: str,
    name: str,
    *,
    capacity: int | None = 8,
    description: str | None = "",
    building: str | None = "building-1",
    floor: str = "1",
    email: str | None = None,
) -> RoomDirectoryData:
    """A provider room. ``building=None`` gives a room with no location."""
    return RoomDirectoryData(
        external_id=external_id,
        email=email if email is not None else f"{external_id}@resource.example.com",
        name=name,
        description=description,
        capacity=capacity,
        location_ref=(
            ResourceLocationRef(external_building_id=building, external_floor_id=floor)
            if building is not None
            else None
        ),
    )


def make_location(
    building: str = "building-1",
    floor: str = "1",
    *,
    building_name: str = "Main Building",
    floor_name: str | None = None,
) -> ResourceLocationData:
    """A provider building and floor. The floor name defaults to the floor id."""
    return ResourceLocationData(
        external_building_id=building,
        building_name=building_name,
        external_floor_id=floor,
        floor_name=floor if floor_name is None else floor_name,
    )
