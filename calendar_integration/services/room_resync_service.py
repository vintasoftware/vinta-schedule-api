"""The hourly resync that brings provider-side room and location changes into Vinta Schedule.

``RoomResyncService.resync(organization, provider)`` reads one organization's
room directory on one provider and makes Vinta Schedule match it, in four steps:

1. **Locations.** Every building and floor the provider lists is upserted into
   ``ResourceLocation``. Locations it no longer lists are removed at the end of
   the run: deleted when no room points at them, kept and marked inactive when
   one still does.
2. **Linked rooms** (links in ``for_resync``), one transaction per link, each
   holding the link's row lock taken with ``skip_locked``. A link the push
   engine is working on is skipped and picked up next hour.

   - **(c) Changed on the provider.** For each field the provider changed since
     the last successful sync (``fields_changed_by_provider``), the provider's
     value overwrites ``Calendar`` (or the link's location) and the snapshot.
     A queued Vinta Schedule edit to such a field is dropped. When its value
     differs from the provider's, org admins get an email and the audit trail
     records it. Queued edits to other fields stay queued.
   - **(d) Gone from the provider.** A ``SYNCED`` or ``PENDING_UPDATE`` room the
     provider no longer lists is archived: ``Calendar.visibility = INACTIVE``,
     ``archived_at``, status ``ARCHIVED``. When it still has future bookings,
     ``flagged_bookings_at`` is set and org admins get an email.

3. **Rooms new to the sync**, in one transaction:

   - **(a)** A provider room whose ``(external_id, provider)`` matches a live
     ``RESOURCE`` calendar with no link gets a ``SYNCED`` link.
   - **(b)** Any other provider room is imported as a ``RESOURCE`` calendar with
     a ``SYNCED`` link, capped by the organization's ``resource_calendars``
     headroom exactly like the on-demand room import.

   Both send ``resource_room_synced(created=False)`` on commit.

4. Locations not seen in step 1 are removed, as described there.

Provider calls (``list_locations``, ``list_rooms``) happen before any
transaction opens, so no lock is held across the network.

The caller must bind the organization with ``organization_context``; the
``resync_organization_rooms_task`` Celery task does. Running twice with no
provider change between the runs changes no room, link or calendar, sends no
email and no signal. The only column the second run touches is
``ResourceLocation.last_seen_at``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.utils import timezone

from audit_integration.constants import AuditAction
from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    ResourceSyncStatus,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    ResourceCalendarProviderLink,
    ResourceLocation,
)
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import (
    CalendarResourceData,
    ResourceLocationData,
    RoomDirectoryData,
)
from calendar_integration.signals import resource_room_archived, resource_room_synced


if TYPE_CHECKING:
    import datetime

    from vinta_billing.services.entitlement_service import EntitlementService

    from audit_integration.services import OrganizationAuditService
    from calendar_integration.services.calendar_sync_service import SyncServiceHost
    from calendar_integration.services.protocols.resource_directory_adapter import (
        ResourceDirectoryAdapterResolver,
    )
    from calendar_integration.services.room_sync_notifier import RoomSyncNotifier
    from organizations.models import Organization


logger = logging.getLogger(__name__)

#: The providers the hourly resync covers, in the order the fan-out enqueues them.
ROOM_RESYNC_PROVIDERS: tuple[str, ...] = (CalendarProvider.GOOGLE, CalendarProvider.MICROSOFT)

#: The ``Calendar`` columns that mirror a synced room field of the same name.
#: ``location_ref`` is the fourth synced field and lives on the link instead.
_CALENDAR_SYNCED_FIELDS: tuple[str, ...] = ("name", "description", "capacity")

#: Statuses whose room is archived when the provider no longer lists it. A link
#: that failed an update is left alone: the spec only archives synced rooms.
_ARCHIVABLE_STATUSES = frozenset({ResourceSyncStatus.SYNCED, ResourceSyncStatus.PENDING_UPDATE})

#: Statuses that go back to ``SYNCED`` when the resync drops their last queued edit,
#: because nothing is left to push.
_SETTLES_WHEN_NOTHING_PENDING = frozenset(
    {ResourceSyncStatus.PENDING_UPDATE, ResourceSyncStatus.SYNC_FAILED}
)

_LocationKey = tuple[str, str]


@dataclass
class RoomResyncResult:
    """What one ``resync`` call changed. Calendar and link ids only, never room content."""

    locations_created: int = 0
    locations_updated: int = 0
    locations_deactivated: int = 0
    locations_deleted: int = 0
    linked_calendar_ids: list[int] = dataclass_field(default_factory=list)
    imported_calendar_ids: list[int] = dataclass_field(default_factory=list)
    updated_calendar_ids: list[int] = dataclass_field(default_factory=list)
    archived_calendar_ids: list[int] = dataclass_field(default_factory=list)
    skipped_locked_link_ids: list[int] = dataclass_field(default_factory=list)
    import_warning: str | None = None


class RoomResyncService:
    """Imports provider-side room and location changes for one organization and provider.

    Stateless. Every dependency arrives through ``di_core.containers``.
    """

    def __init__(
        self,
        resource_directory_adapter_resolver: ResourceDirectoryAdapterResolver,
        room_sync_notifier: RoomSyncNotifier,
        audit_service: OrganizationAuditService,
        entitlement_service: EntitlementService,
    ) -> None:
        self.resource_directory_adapter_resolver = resource_directory_adapter_resolver
        self.room_sync_notifier = room_sync_notifier
        self.audit_service = audit_service
        self.entitlement_service = entitlement_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resync(self, organization: Organization, provider: str) -> RoomResyncResult | None:
        """Make ``organization``'s rooms and locations on ``provider`` match the provider.

        Returns ``None`` without calling the provider when the organization is not
        write-enabled for ``provider`` (which includes the feature flag being off).
        Provider failures propagate as the ``ResourceDirectoryError`` family; they
        are raised before anything is written.
        """
        resolver = self.resource_directory_adapter_resolver
        if not resolver.is_write_enabled(organization, provider):
            return None
        adapter = resolver.adapter_for(organization, provider)
        provider_locations = adapter.list_locations()
        provider_rooms = adapter.list_rooms()

        now = timezone.now()
        result = RoomResyncResult()
        locations = self._upsert_locations(organization, provider, provider_locations, now, result)
        rooms_by_external_id = _index_rooms(provider_rooms)

        self._resync_linked_rooms(
            organization, provider, rooms_by_external_id, locations, now, result
        )
        self._link_and_import_new_rooms(
            organization, provider, rooms_by_external_id, locations, now, result
        )
        self._retire_unseen_locations(organization, provider, now, result)

        logger.info(
            "Room resync for organization %s on %s: linked=%s imported=%s updated=%s "
            "archived=%s skipped_locked=%s",
            organization.id,
            provider,
            result.linked_calendar_ids,
            result.imported_calendar_ids,
            result.updated_calendar_ids,
            result.archived_calendar_ids,
            result.skipped_locked_link_ids,
        )
        return result

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    def _upsert_locations(
        self,
        organization: Organization,
        provider: str,
        provider_locations: Iterable[ResourceLocationData],
        now: datetime.datetime,
        result: RoomResyncResult,
    ) -> dict[_LocationKey, ResourceLocation]:
        """Create or refresh every location the provider lists. Returns them by provider ref.

        A location whose names and active flag already match is only stamped with
        ``last_seen_at``, in one bulk update, so an unchanged run saves no row.
        """
        with transaction.atomic():
            existing: dict[_LocationKey, ResourceLocation] = {
                (location.external_building_id, location.external_floor_id): location
                for location in ResourceLocation.objects.filter_by_organization(
                    organization.id
                ).for_provider(provider)
            }
            seen: dict[_LocationKey, ResourceLocation] = {}
            unchanged_ids: list[int] = []
            for data in provider_locations:
                key = (data.external_building_id, data.external_floor_id)
                if key in seen:
                    continue
                location = existing.get(key)
                if location is None:
                    location = ResourceLocation.objects.create(
                        organization=organization,
                        provider=provider,
                        external_building_id=data.external_building_id,
                        external_floor_id=data.external_floor_id,
                        building_name=data.building_name,
                        floor_name=data.floor_name,
                        is_active=True,
                        last_seen_at=now,
                    )
                    result.locations_created += 1
                elif (location.building_name, location.floor_name, location.is_active) != (
                    data.building_name,
                    data.floor_name,
                    True,
                ):
                    location.building_name = data.building_name
                    location.floor_name = data.floor_name
                    location.is_active = True
                    location.last_seen_at = now
                    location.save(
                        update_fields=["building_name", "floor_name", "is_active", "last_seen_at"]
                    )
                    result.locations_updated += 1
                else:
                    unchanged_ids.append(location.id)
                seen[key] = location
            if unchanged_ids:
                ResourceLocation.objects.filter_by_organization(organization.id).filter(
                    id__in=unchanged_ids
                ).update(last_seen_at=now)
        return {**existing, **seen}

    def _retire_unseen_locations(
        self,
        organization: Organization,
        provider: str,
        now: datetime.datetime,
        result: RoomResyncResult,
    ) -> None:
        """Remove locations this run did not see.

        Runs after the rooms, so a room the provider moved away from a location no
        longer holds it. Unreferenced rows are deleted; a row a room still points at
        is kept and marked inactive, because the link's foreign key protects it.
        """
        with transaction.atomic():
            unseen = (
                ResourceLocation.objects.filter_by_organization(organization.id)
                .for_provider(provider)
                .filter(last_seen_at__lt=now)
            )
            deletable_ids = list(unseen.unreferenced().values_list("id", flat=True))
            if deletable_ids:
                _total, per_model = (
                    ResourceLocation.objects.filter_by_organization(organization.id)
                    .filter(id__in=deletable_ids)
                    .delete()
                )
                result.locations_deleted = per_model.get(ResourceLocation._meta.label, 0)
            # Read first, so a run with nothing to retire sends no UPDATE at all.
            deactivate_ids = list(unseen.filter(is_active=True).values_list("id", flat=True))
            if deactivate_ids:
                result.locations_deactivated = (
                    ResourceLocation.objects.filter_by_organization(organization.id)
                    .filter(id__in=deactivate_ids)
                    .update(is_active=False)
                )

    # ------------------------------------------------------------------
    # Linked rooms: (c) provider changes and (d) provider deletions
    # ------------------------------------------------------------------

    def _resync_linked_rooms(
        self,
        organization: Organization,
        provider: str,
        rooms_by_external_id: Mapping[str, RoomDirectoryData],
        locations: Mapping[_LocationKey, ResourceLocation],
        now: datetime.datetime,
        result: RoomResyncResult,
    ) -> None:
        link_ids = list(
            ResourceCalendarProviderLink.objects.filter_by_organization(organization.id)
            .for_resync(provider)
            .order_by("id")
            .values_list("id", flat=True)
        )
        # An empty listing archives every room the organization has. Directory
        # outages raise rather than list nothing, but a wrong empty answer would
        # cost an unrecoverable archive, so it is not trusted.
        archive_missing = bool(rooms_by_external_id)
        if not archive_missing and link_ids:
            logger.warning(
                "Room resync for organization %s on %s: the provider listed no rooms; "
                "not archiving any of the %s linked rooms.",
                organization.id,
                provider,
                len(link_ids),
            )
        for link_id in link_ids:
            self._resync_link(
                link_id, rooms_by_external_id, locations, now, result, archive_missing
            )

    def _resync_link(
        self,
        link_id: int,
        rooms_by_external_id: Mapping[str, RoomDirectoryData],
        locations: Mapping[_LocationKey, ResourceLocation],
        now: datetime.datetime,
        result: RoomResyncResult,
        archive_missing: bool,
    ) -> None:
        with transaction.atomic():
            link = (
                ResourceCalendarProviderLink.objects.locked_for_update(link_id, skip_locked=True)
                .select_related("calendar")
                .first()
            )
            if link is None:
                # The push engine holds the row. Its push will settle the link;
                # the next hourly run reads whatever it left.
                result.skipped_locked_link_ids.append(link_id)
                return
            # The status may have moved between the listing and the lock.
            if not link.is_bookable or not link.calendar.external_id:
                return
            room = rooms_by_external_id.get(link.calendar.external_id)
            if room is None:
                if archive_missing and link.sync_status in _ARCHIVABLE_STATUSES:
                    self._archive(link, now, result)
                return
            self._apply_provider_changes(link, room, locations, now, result)

    def _apply_provider_changes(
        self,
        link: ResourceCalendarProviderLink,
        room: RoomDirectoryData,
        locations: Mapping[_LocationKey, ResourceLocation],
        now: datetime.datetime,
        result: RoomResyncResult,
    ) -> None:
        """(c): the provider wins for every field it changed since the last sync."""
        provider_values = room.synced_values()
        changed = link.fields_changed_by_provider(provider_values)
        if not changed:
            return

        calendar = link.calendar
        calendar_fields = [field for field in _CALENDAR_SYNCED_FIELDS if field in changed]
        for field in calendar_fields:
            setattr(calendar, field, provider_values[field])
        if "location_ref" in changed:
            link.location = _location_for(locations, provider_values["location_ref"])

        # A queued edit to a changed field is dropped either way. Admins only hear
        # about it when the queued value differs from what the provider now has.
        discarded = {
            field: value
            for field, value in link.pending_fields.items()
            if field in changed and value != provider_values[field]
        }
        had_pending = bool(link.pending_fields)
        link.pending_fields = {
            field: value for field, value in link.pending_fields.items() if field not in changed
        }
        link.provider_snapshot = {
            **link.provider_snapshot,
            **{field: provider_values[field] for field in changed},
        }
        link.last_synced_at = now
        link_fields = ["location", "pending_fields", "provider_snapshot", "last_synced_at"]
        if (
            had_pending
            and not link.pending_fields
            and link.sync_status in _SETTLES_WHEN_NOTHING_PENDING
        ):
            # Every queued edit was overtaken by the provider, so there is nothing
            # left to push or to retry. A push already queued finds SYNCED and stops.
            link.sync_status = ResourceSyncStatus.SYNCED
            link.failed_operation = ""
            link.last_error = ""
            link.attempt_count = 0
            link.retry_deadline = None
            link_fields += [
                "sync_status",
                "failed_operation",
                "last_error",
                "attempt_count",
                "retry_deadline",
            ]

        if calendar_fields:
            calendar.save(update_fields=calendar_fields)
        link.save(update_fields=link_fields)
        result.updated_calendar_ids.append(calendar.id)

        if discarded:
            self.room_sync_notifier.notify_edit_discarded(calendar.id, sorted(discarded))
            self.audit_service.record(
                action=AuditAction.EXTERNAL_CHANGE_ROOM_EDIT_DISCARDED,
                actor=self.audit_service.system_actor(),
                subject=self.audit_service.subject_from_instance(calendar),
                diff={
                    field: {"old": value, "new": provider_values[field]}
                    for field, value in discarded.items()
                },
                scope=self.audit_service.scope_from_organization_id(calendar.organization_id),
            )

    def _archive(
        self,
        link: ResourceCalendarProviderLink,
        now: datetime.datetime,
        result: RoomResyncResult,
    ) -> None:
        """(d): the provider deleted the room. Archive it and flag its future bookings."""
        calendar = link.calendar
        calendar.visibility = CalendarVisibility.INACTIVE
        calendar.save(update_fields=["visibility"])

        flagged_count = (
            CalendarEvent.objects.filter_by_organization(calendar.organization_id)
            .future_bookings_of_room(calendar, now)
            .count()
        )
        link.sync_status = ResourceSyncStatus.ARCHIVED
        link.archived_at = now
        link.retry_deadline = None
        link_fields = ["sync_status", "archived_at", "retry_deadline"]
        if flagged_count:
            link.flagged_bookings_at = now
            link_fields.append("flagged_bookings_at")
        link.save(update_fields=link_fields)
        result.archived_calendar_ids.append(calendar.id)

        if flagged_count:
            self.room_sync_notifier.notify_bookings_flagged(calendar.id, flagged_count)
        _send_on_commit_archived(calendar.id, link.provider)

    # ------------------------------------------------------------------
    # New rooms: (a) link existing calendars, (b) import unknown rooms
    # ------------------------------------------------------------------

    def _link_and_import_new_rooms(
        self,
        organization: Organization,
        provider: str,
        rooms_by_external_id: Mapping[str, RoomDirectoryData],
        locations: Mapping[_LocationKey, ResourceLocation],
        now: datetime.datetime,
        result: RoomResyncResult,
    ) -> None:
        links = list(
            ResourceCalendarProviderLink.objects.filter_by_organization(organization.id)
            .filter(provider=provider)
            .select_related("calendar")
        )
        linked_external_ids = {link.calendar.external_id for link in links}
        create_markers = _create_markers(links)
        new_rooms = [
            room
            for room in rooms_by_external_id.values()
            if room.external_id not in linked_external_ids
            and not _is_claimed_by_a_link(room, create_markers)
        ]
        if not new_rooms:
            return

        existing_calendars = {
            calendar.external_id: calendar
            for calendar in Calendar.objects.filter_by_organization(organization.id).filter(
                provider=provider, external_id__in=[room.external_id for room in new_rooms]
            )
        }
        rooms_to_link: list[tuple[RoomDirectoryData, Calendar]] = []
        rooms_to_import: list[RoomDirectoryData] = []
        for room in new_rooms:
            calendar = existing_calendars.get(room.external_id)
            if calendar is None:
                rooms_to_import.append(room)
            elif calendar.visibility == CalendarVisibility.INACTIVE:
                # Disabled in Vinta Schedule on purpose. The on-demand import leaves
                # such a row disabled too, so the resync neither links nor revives it.
                continue
            elif calendar.calendar_type == CalendarType.RESOURCE:
                rooms_to_link.append((room, calendar))
            else:
                # A live calendar of another type with the room's id. The on-demand
                # import promotes it to a room, and charges headroom for that.
                rooms_to_import.append(room)

        with transaction.atomic():
            for room, calendar in rooms_to_link:
                self._overwrite_calendar_with_room(calendar, room)
                if self._create_synced_link(organization, provider, calendar, room, locations, now):
                    result.linked_calendar_ids.append(calendar.id)

            importable, warning = self._cap_to_headroom(organization, provider, rooms_to_import)
            result.import_warning = warning
            for room in importable:
                calendar = self._upsert_room_calendar(organization, provider, room)
                if self._create_synced_link(organization, provider, calendar, room, locations, now):
                    result.imported_calendar_ids.append(calendar.id)

    def _cap_to_headroom(
        self, organization: Organization, provider: str, rooms: list[RoomDirectoryData]
    ) -> tuple[list[RoomDirectoryData], str | None]:
        """Keep only the rooms the ``resource_calendars`` headroom allows.

        Reuses the on-demand import's own cap, unchanged, so both import paths
        charge the plan limit by one rule. Must run inside the transaction that
        writes the rooms: the cap locks the billing root until it commits.
        """
        if not rooms:
            return [], None
        sync_service = CalendarSyncService(
            context=CalendarServiceContext(
                organization=organization,
                user_or_token=None,
                account=None,
                calendar_adapter=None,
                calendar_permission_service=None,
                calendar_side_effects_service=None,
                entitlement_service=self.entitlement_service,
            ),
            calendar_cache={},
            # The cap reads only the context's entitlement service; it never
            # reaches the host.
            host=cast("SyncServiceHost", None),
        )
        resources = [
            CalendarResourceData(
                name=room.name,
                description=room.description or "",
                provider=provider,
                external_id=room.external_id,
                email=room.email,
                capacity=room.capacity,
            )
            for room in rooms
        ]
        kept, warning = sync_service._cap_resources_to_resource_calendar_headroom(
            organization, resources, bypass_limits=False
        )
        kept_ids = {resource.external_id for resource in kept}
        return [room for room in rooms if room.external_id in kept_ids], warning

    def _upsert_room_calendar(
        self, organization: Organization, provider: str, room: RoomDirectoryData
    ) -> Calendar:
        """Create the room's calendar, or promote the live calendar that has its id.

        Keyed like the on-demand import, on ``(external_id, provider, organization)``,
        which is ``Calendar``'s unique constraint.
        """
        defaults: dict[str, Any] = {
            "name": room.name,
            "email": room.email,
            "capacity": room.capacity,
            "calendar_type": CalendarType.RESOURCE,
        }
        if room.description is not None:
            defaults["description"] = room.description
        calendar, _created = Calendar.objects.update_or_create(
            organization=organization,
            provider=provider,
            external_id=room.external_id,
            defaults=defaults,
        )
        return calendar

    def _overwrite_calendar_with_room(self, calendar: Calendar, room: RoomDirectoryData) -> None:
        """Give a calendar being linked the provider's values, saving only what differs.

        The link's snapshot is about to hold the provider's values, so a calendar
        left with older ones would never be corrected: the next resync compares the
        provider with the snapshot, not with the calendar.
        """
        values: dict[str, Any] = {"name": room.name, "capacity": room.capacity}
        if room.description is not None:
            values["description"] = room.description
        if room.email:
            values["email"] = room.email
        changed_fields = [
            field for field, value in values.items() if getattr(calendar, field) != value
        ]
        for field in changed_fields:
            setattr(calendar, field, values[field])
        if changed_fields:
            calendar.save(update_fields=changed_fields)

    def _create_synced_link(
        self,
        organization: Organization,
        provider: str,
        calendar: Calendar,
        room: RoomDirectoryData,
        locations: Mapping[_LocationKey, ResourceLocation],
        now: datetime.datetime,
    ) -> bool:
        """Give ``calendar`` a ``SYNCED`` link whose snapshot is the provider's values.

        Returns False when another run linked the calendar first. Sends
        ``resource_room_synced(created=False)`` on commit for a new link.
        """
        provider_values = room.synced_values()
        _link, created = ResourceCalendarProviderLink.objects.get_or_create(
            organization=organization,
            calendar=calendar,
            defaults={
                "provider": provider,
                "sync_status": ResourceSyncStatus.SYNCED,
                "location": _location_for(locations, provider_values["location_ref"]),
                "provider_snapshot": provider_values,
                "pending_fields": {},
                "last_synced_at": now,
            },
        )
        if created:
            _send_on_commit_synced(calendar.id, provider)
        return created


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _index_rooms(rooms: Iterable[RoomDirectoryData]) -> dict[str, RoomDirectoryData]:
    """Rooms by ``external_id``. The first listing of a repeated id wins, as in the import."""
    indexed: dict[str, RoomDirectoryData] = {}
    for room in rooms:
        if room.external_id:
            indexed.setdefault(room.external_id, room)
    return indexed


def _location_for(
    locations: Mapping[_LocationKey, ResourceLocation], location_ref: Mapping[str, str] | None
) -> ResourceLocation | None:
    """The local location a ``location_ref`` points at, or None when there is none."""
    if location_ref is None:
        return None
    return locations.get(
        (location_ref["external_building_id"], location_ref.get("external_floor_id", ""))
    )


def _create_markers(links: Iterable[ResourceCalendarProviderLink]) -> set[str]:
    """The provider-side ids Vinta Schedule's own creates use, one pair per link.

    The push engine creates a room under an id derived from the link's
    ``provisional_key``: Google ``resourceId`` ``vinta-<key>``, Microsoft tag
    ``vinta-link-<key>``. Between the provider call and the push's commit, the
    room is on the provider while its calendar has no ``external_id`` yet. These
    markers keep the resync from importing that room a second time.
    """
    markers: set[str] = set()
    for link in links:
        markers.add(f"vinta-{link.provisional_key}")
        markers.add(f"vinta-link-{link.provisional_key}")
    return markers


def _is_claimed_by_a_link(room: RoomDirectoryData, create_markers: set[str]) -> bool:
    """Whether ``room`` was created by Vinta Schedule for a link that already exists."""
    if room.external_id in create_markers:
        return True
    tags = room.provider_payload.get("tags")
    if isinstance(tags, list):
        return any(isinstance(tag, str) and tag in create_markers for tag in tags)
    return False


def _send_on_commit_synced(calendar_id: int, provider: str) -> None:
    transaction.on_commit(
        lambda: resource_room_synced.send(
            sender=RoomResyncService, calendar_id=calendar_id, provider=provider, created=False
        )
    )


def _send_on_commit_archived(calendar_id: int, provider: str) -> None:
    transaction.on_commit(
        lambda: resource_room_archived.send(
            sender=RoomResyncService, calendar_id=calendar_id, provider=provider
        )
    )
