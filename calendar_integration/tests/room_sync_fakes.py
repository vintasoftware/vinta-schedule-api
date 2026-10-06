"""In-memory fakes of the room directory contracts, for the room sync engine tests.

``FakeRoomDirectory`` implements ``ResourceDirectoryAdapter`` and
``FakeRoomDirectoryResolver`` implements ``ResourceDirectoryAdapterResolver``.
Both are assigned to the protocol types at the bottom of this module, so mypy
fails here if a fake and its protocol drift apart.
"""

import datetime
from collections.abc import Callable, Collection

from calendar_integration.constants import CalendarProvider
from calendar_integration.exceptions import (
    ResourceDirectoryNotFoundError,
    ResourceDirectoryNotWriteEnabledError,
)
from calendar_integration.services.dataclasses import (
    BusyWindow,
    ResourceLocationData,
    RoomDirectoryData,
    RoomWriteData,
)
from calendar_integration.services.protocols.resource_directory_adapter import (
    ResourceDirectoryAdapter,
    ResourceDirectoryAdapterResolver,
)
from organizations.models import Organization


class FakeRoomDirectory:
    """A provider room directory held in a dict.

    Write methods record their name in ``calls`` before doing anything, so a test
    can count provider calls, including failed ones. Set ``failures`` to make the
    next write calls raise, one exception per call in order. Set ``on_write`` to
    run code in the middle of a provider call, for example to edit the link while
    the push is in flight.
    """

    def __init__(self, provider: str = CalendarProvider.GOOGLE) -> None:
        self.provider = provider
        self.rooms: dict[str, RoomDirectoryData] = {}
        self.calls: list[str] = []
        self.failures: list[BaseException] = []
        self.on_write: Callable[[str], None] | None = None

    def _write(self, method: str) -> None:
        self.calls.append(method)
        if self.on_write is not None:
            self.on_write(method)
        if self.failures:
            raise self.failures.pop(0)

    def list_locations(self) -> list[ResourceLocationData]:
        return []

    def list_rooms(self) -> list[RoomDirectoryData]:
        return list(self.rooms.values())

    def create_room(self, data: RoomWriteData) -> RoomDirectoryData:
        self._write("create_room")
        # Idempotent on the provisional key, like the real adapters.
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
        self._write("update_room")
        if external_id not in self.rooms:
            raise ResourceDirectoryNotFoundError()
        room = self.rooms[external_id]
        for field in fields:
            setattr(room, field, getattr(data, field))
        return room

    def delete_room(self, external_id: str) -> None:
        self._write("delete_room")
        if self.rooms.pop(external_id, None) is None:
            raise ResourceDirectoryNotFoundError()

    def get_free_busy(
        self, room_email: str, start: datetime.datetime, end: datetime.datetime
    ) -> list[BusyWindow]:
        return []


class FakeRoomDirectoryResolver:
    """Hands out one ``FakeRoomDirectory``. ``write_enabled=False`` acts as flag off."""

    def __init__(self, adapter: FakeRoomDirectory, write_enabled: bool = True) -> None:
        self.adapter = adapter
        self.write_enabled = write_enabled

    def adapter_for(self, organization: Organization, provider: str) -> ResourceDirectoryAdapter:
        if not self.is_write_enabled(organization, provider):
            raise ResourceDirectoryNotWriteEnabledError()
        return self.adapter

    def is_write_enabled(self, organization: Organization, provider: str) -> bool:
        return self.write_enabled and provider == self.adapter.provider


_adapter_check: ResourceDirectoryAdapter = FakeRoomDirectory()
_resolver_check: ResourceDirectoryAdapterResolver = FakeRoomDirectoryResolver(FakeRoomDirectory())
