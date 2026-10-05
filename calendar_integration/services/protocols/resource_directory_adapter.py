"""Contracts for reading and writing rooms in a provider's room directory.

The push engine, the hourly resync and the busy check code against these
protocols, so they can be built and tested with fakes before the real Google
(Directory API) and Microsoft (Graph Places API) adapters exist.

Every method that calls the provider raises the ``ResourceDirectoryError`` family
from ``calendar_integration.exceptions`` on failure, never a provider SDK error:

- ``ResourceDirectoryInvalidInputError``: the provider rejected the request
  (400 / 409 / 412 / 422). Not transient.
- ``ResourceDirectoryPermissionError``: credentials or scope refused (401 / 403).
  Transient, because an IT admin can restore the permission.
- ``ResourceDirectoryNotFoundError``: the room or location does not exist (404).
  Not transient.
- ``ResourceDirectoryError``: anything else, transient by default (5xx, 429,
  timeouts).
"""

import datetime
from collections.abc import Collection
from typing import TYPE_CHECKING, Protocol

from calendar_integration.services.dataclasses import (
    BusyWindow,
    ResourceLocationData,
    RoomDirectoryData,
    RoomWriteData,
)


if TYPE_CHECKING:
    from organizations.models import Organization


class ResourceDirectoryAdapter(Protocol):
    """One organization's room directory on one provider, using organization-level credentials."""

    provider: str

    def list_locations(self) -> list[ResourceLocationData]:
        """Every building and floor in the directory.

        Google: each building, once per entry in its ``floorNames``. Microsoft: each
        building, once per floor or section under it. A building with no floors is
        listed once with an empty ``external_floor_id``.
        """
        ...

    def list_rooms(self) -> list[RoomDirectoryData]:
        """Every meeting room in the directory, with no free/busy filter."""
        ...

    def create_room(self, data: RoomWriteData) -> RoomDirectoryData:
        """Create a room and return it as the provider stored it.

        Must be idempotent on ``data.provisional_key``: a replay after a create
        that already succeeded returns the existing room instead of making a
        second one.
        """
        ...

    def update_room(
        self, external_id: str, data: RoomWriteData, fields: Collection[str]
    ) -> RoomDirectoryData:
        """Update the room ``external_id`` and return it as the provider stored it.

        Only the synced fields named in ``fields`` are sent (a subset of
        ``RESOURCE_SYNCED_FIELDS``); the rest of ``data`` is ignored. Sending only
        what Vinta Schedule changed keeps the push from overwriting a field the
        provider changed since the last resync.
        """
        ...

    def delete_room(self, external_id: str) -> None:
        """Delete the room ``external_id``.

        Raises ``ResourceDirectoryNotFoundError`` when the room is already gone.
        Callers treat that as a completed delete.
        """
        ...

    def get_free_busy(
        self, room_email: str, start: datetime.datetime, end: datetime.datetime
    ) -> list[BusyWindow]:
        """The windows in which the room is busy between ``start`` and ``end``.

        ``start`` and ``end`` are timezone-aware. Google answers from Calendar
        ``freebusy.query``; Microsoft from app-only ``getSchedule``.
        """
        ...


class ResourceDirectoryAdapterResolver(Protocol):
    """Finds the room directory adapter for an organization and provider."""

    def adapter_for(self, organization: "Organization", provider: str) -> ResourceDirectoryAdapter:
        """The adapter for ``organization`` on ``provider``.

        Raises ``ResourceDirectoryNotWriteEnabledError`` when ``is_write_enabled``
        is False for the pair.
        """
        ...

    def is_write_enabled(self, organization: "Organization", provider: str) -> bool:
        """Whether Vinta Schedule may write rooms for ``organization`` on ``provider``.

        True only when the ``resource_calendar_provider_sync`` flag is on for the
        organization and its connection for the provider has ``write_enabled`` set.
        """
        ...
