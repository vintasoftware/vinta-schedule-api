"""Calendar / account / organization-resource import + the event sync state machine.

``CalendarSyncService`` owns the sync concern extracted from the
``CalendarService`` facade. It is a plain class (not a DI-container provider):
the facade constructs it (fresh per request, after authentication) feeding it
the shared :class:`CalendarServiceContext` so it never re-authenticates or
re-builds a calendar adapter (the perf guardrail). Everything it needs arrives
via the constructor:

- ``context`` — the immutable auth snapshot (organization, user_or_token,
  account, calendar_adapter, permission_service, side_effects_service). Read
  through ``self._context``; the auth guards in ``type_guards.py`` inspect the
  same ``organization`` / ``account`` / ``calendar_adapter`` attributes the
  context exposes, so behavior is byte-for-byte identical to the former methods.
- ``calendar_cache`` — the facade-owned, per-instance ``{(org_id, id): Calendar}``
  cache (the lru_cache multi-tenant fix from Phase 0). Shared so lookups are not
  duplicated across the facade and this service.
- ``host`` — the :class:`SyncServiceHost` (in Phase 5 the facade itself). The sync
  concern routes two things back through it:

  - **available-time pruning** (``_remove_available_time_windows_that_overlap_with_blocked_times_and_events``)
    — the availability concern, extracted in Phase 4; reaching it through the host
    keeps a single implementation and the call graph the existing test suite asserts on.
  - **owner-permission granting** (``_grant_calendar_owner_permissions``) — a shared
    facade helper used by import flows; routed through the host so it has one
    implementation and stays byte-for-byte.

The sync diff/merge machine (``_process_events_for_sync`` / ``_apply_sync_changes``),
the existing-data lookup, the full-sync deletion pass, and the orphan-linking pass
are moved verbatim — no added queries inside loops, no changed query structure or
bulk-operation ordering, no algorithmic-complexity change.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Literal, Protocol, cast

from django.db import transaction
from django.db.models import Q

from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import (
    CalendarOrganizationResourceImportStatus,
    CalendarProvider,
    CalendarSyncStatus,
    CalendarSyncTriggerSource,
    CalendarType,
    CalendarVisibility,
)
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarOwnership,
    CalendarSync,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
)
from calendar_integration.services.calendar_service_utils import (
    convert_naive_utc_datetime_to_timezone as _convert_naive_utc_datetime_to_timezone,
)
from calendar_integration.services.dataclasses import EventsSyncChanges
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)
from calendar_integration.services.type_guards import is_authenticated_calendar_service
from users.models import User


if TYPE_CHECKING:
    from collections.abc import Iterable

    from calendar_integration.services.calendar_service_context import CalendarServiceContext
    from calendar_integration.services.dataclasses import (
        CalendarEventAdapterOutputData,
        CalendarResourceData,
    )


logger = logging.getLogger(__name__)


class SyncServiceHost(Protocol):
    """The collaborator surface the sync concern routes back to the facade for.

    Two concerns are not part of the sync concern's extracted surface and stay on
    the facade:

    - **available-time pruning**
      (``_remove_available_time_windows_that_overlap_with_blocked_times_and_events``)
      — the availability concern (Phase 4); reached through the host to keep one
      implementation and the call graph the existing test suite patches via the facade;
    - **owner-permission granting** (``_grant_calendar_owner_permissions``) — a shared
      facade helper used by the import flows; routed through the host for a single
      implementation.

    Two sync entry points are *also* routed back through the host even though their
    real implementation lives in this service. The existing test suite patches
    ``request_calendar_sync`` and ``_execute_organization_calendar_resources_import``
    on the facade and then drives an outer import flow (``import_account_calendars`` /
    ``import_organization_calendar_resources`` /
    ``_execute_organization_calendar_resources_import``) expecting the patched method to
    intercept. Routing the inner calls through the host preserves that interception
    point; when unpatched, the facade simply re-delegates to a fresh sync service, so
    behavior is byte-for-byte identical.

    In Phase 5 the facade supplies *itself*. Later phases may swap individual concerns
    without changing this service's call sites.
    """

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self,
        calendar_id: int,
        blocked_times: Iterable[BlockedTime],
        events: Iterable[CalendarEvent],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None: ...

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None: ...

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source: CalendarSyncTriggerSource = CalendarSyncTriggerSource.MANUAL,
    ) -> CalendarSync | None: ...

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> Iterable[CalendarResourceData]: ...


class CalendarSyncService:
    """Owns calendar/account/org-resource import and the event sync state machine."""

    def __init__(
        self,
        context: CalendarServiceContext,
        calendar_cache: dict[tuple[int, str | int], Calendar],
        host: SyncServiceHost,
    ) -> None:
        self._context = context
        self._calendar_cache = calendar_cache
        # Phase 5 seam: available-time pruning (Phase 4) and the shared owner-permission
        # helper are reached through the host (the facade). See ``SyncServiceHost``.
        self._host = host

    # ------------------------------------------------------------------
    # Organization-resource import
    # ------------------------------------------------------------------

    def request_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        from calendar_integration.tasks import import_organization_calendar_resources_task

        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
            organization=context.organization,
            start_time=start_time,
            end_time=end_time,
        )

        # Capture ids by value so the closure is independent of mutable self state.
        _account_type = (
            "google_service_account"
            if isinstance(context.account, GoogleCalendarServiceAccount)
            else "social_account"
        )
        _account_id = context.account.id
        _organization_id = context.organization.id
        _import_workflow_state_id = import_workflow_state.id

        transaction.on_commit(
            lambda: import_organization_calendar_resources_task.delay(  # type: ignore
                account_type=_account_type,
                account_id=_account_id,
                organization_id=_organization_id,
                import_workflow_state_id=_import_workflow_state_id,
            )
        )

    def import_organization_calendar_resources(
        self,
        import_workflow_state: CalendarOrganizationResourcesImport,
    ) -> None:
        """
        Import organization calendar resources within a specified time range.
        :param start_time: Start time for the availability check.
        :param end_time: End time for the availability check.
        :return: List of available resources.
        """
        if not is_authenticated_calendar_service(cast("BaseCalendarService", self._context)):
            raise

        import_workflow_state.status = CalendarOrganizationResourceImportStatus.IN_PROGRESS
        import_workflow_state.save(update_fields=["status"])

        try:
            with transaction.atomic():
                self._host._execute_organization_calendar_resources_import(
                    start_time=import_workflow_state.start_time,
                    end_time=import_workflow_state.end_time,
                )
        except Exception as e:  # noqa: BLE001
            import_workflow_state.status = CalendarOrganizationResourceImportStatus.FAILED
            import_workflow_state.error_message = str(e)
            import_workflow_state.save(update_fields=["status", "error_message"])
            return

        import_workflow_state.status = CalendarOrganizationResourceImportStatus.SUCCESS
        import_workflow_state.save(update_fields=["status"])

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> Iterable[CalendarResourceData]:
        """
        Import organization calendar resources within a specified time range.
        :param start_time: Start time for the availability check.
        :param end_time: End time for the availability check.
        :return: List of available resources.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        if not context.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

        resources = context.calendar_adapter.get_available_calendar_resources(start_time, end_time)
        for resource in resources:
            self._host.request_calendar_sync(
                calendar=Calendar.objects.update_or_create(
                    external_id=resource.external_id,
                    organization=context.organization,
                    defaults={
                        "name": resource.name,
                        "description": resource.description,
                        "provider": CalendarProvider(resource.provider),
                        "email": resource.email,
                        "calendar_type": CalendarType.RESOURCE,
                    },
                )[0],
                start_datetime=start_time,
                end_datetime=end_time,
                should_update_events=True,
            )
        return resources

    # ------------------------------------------------------------------
    # Account-calendar import
    # ------------------------------------------------------------------

    def request_calendars_import(self, sync_after_import: bool = True) -> None:
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.

        :param sync_after_import: When True (default), each imported sync-enabled
            calendar is also synced. Pass False to only discover/refresh calendar
            rows without pulling events.
        """
        from calendar_integration.tasks import import_account_calendars_task

        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        # Capture ids by value so the closure is independent of mutable self state.
        _account_type = (
            "google_service_account"
            if isinstance(context.account, GoogleCalendarServiceAccount)
            else "social_account"
        )
        _account_id = context.account.id
        _organization_id = context.organization.id
        _sync_after_import = sync_after_import

        transaction.on_commit(
            lambda: import_account_calendars_task.delay(  # type: ignore
                account_type=_account_type,
                account_id=_account_id,
                organization_id=_organization_id,
                sync_after_import=_sync_after_import,
            )
        )

    @staticmethod
    def _sync_enabled_default_for_access_role(access_role: str | None) -> bool:
        """Decide whether a freshly imported calendar should sync by default.

        Calendars the account owns or can write to (the user's own calendars) sync.
        Subscribed read-only calendars — holidays, birthdays, shared org-wide
        calendars — default to disabled: their events typically duplicate events
        already on the user's own calendars, and they aren't useful for scheduling.
        Unknown access role (e.g. a provider that doesn't report one) defaults to
        enabled to preserve prior behavior.
        """
        if access_role is None:
            return True
        return access_role.lower() in ("owner", "writer")

    @transaction.atomic()
    def import_account_calendars(self, sync_after_import: bool = True):
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.

        :param sync_after_import: When True (default), enqueue an event sync for each
            imported calendar that has sync enabled. The per-calendar ``sync_enabled``
            flag still gates whether a sync actually runs.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        calendars = context.calendar_adapter.get_account_calendars()

        for calendar_data in calendars:
            calendar, _ = Calendar.objects.update_or_create(
                external_id=calendar_data.external_id,
                organization=context.organization,
                defaults={
                    "name": calendar_data.name,
                    "description": calendar_data.description,
                    "email": calendar_data.email,
                    "provider": CalendarProvider(calendar_data.provider),
                    "meta": {
                        "latest_original_payload": calendar_data.original_payload or {},
                    },
                },
                # calendar_type, sync_enabled and visibility are seeded only on first import
                # (create), never on re-import. calendar_type must stay out of the lookup so
                # that resource calendars returned by the provider's calendarList (rooms visible
                # to the user) don't collide with the unique (external_id, provider, org)
                # constraint — and don't accidentally get re-typed as PERSONAL.
                create_defaults={
                    "name": calendar_data.name,
                    "description": calendar_data.description,
                    "email": calendar_data.email,
                    "provider": CalendarProvider(calendar_data.provider),
                    "meta": {
                        "latest_original_payload": calendar_data.original_payload or {},
                    },
                    "calendar_type": CalendarType.PERSONAL,
                    "sync_enabled": self._sync_enabled_default_for_access_role(
                        calendar_data.access_role
                    ),
                    "visibility": CalendarVisibility.ACTIVE,
                    # Imported calendars manage their own availability windows by
                    # default. Seeded on create only (create_defaults), so a later
                    # user toggle via PATCH /calendars/{id}/ is never clobbered on
                    # re-import.
                    "manage_available_windows": True,
                },
            )

            # Resource calendars are owned and synced via the rooms-sync path; skip
            # personal ownership and sync for them here.
            if calendar.calendar_type == CalendarType.RESOURCE:
                continue

            CalendarOwnership.objects.update_or_create(
                organization=context.organization,
                calendar=calendar,
                user=context.account.user if context.account else None,
                defaults={"is_default": calendar_data.is_default},
            )

            # Grant permissions to calendar owners
            self._host._grant_calendar_owner_permissions(calendar)

            if sync_after_import:
                self._host.request_calendar_sync(
                    calendar=calendar,
                    start_datetime=datetime.datetime.now(datetime.UTC),
                    end_datetime=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
                    should_update_events=True,
                    trigger_source=CalendarSyncTriggerSource.IMPORT,
                )

    # ------------------------------------------------------------------
    # Sync request + execution
    # ------------------------------------------------------------------

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source: CalendarSyncTriggerSource = CalendarSyncTriggerSource.MANUAL,
    ) -> CalendarSync | None:
        """
        Request a calendar synchronization for a specific date range.
        :param calendar: The calendar to synchronize.
        :param start_datetime: Start date for the event search.
        :param end_datetime: End date for the event search.
        :param should_update_events: Whether to update existing events.
        :param trigger_source: What kicked off this sync (import/manual/webhook/admin).
        :return: Created CalendarSync instance, or None if the calendar has sync disabled.
        """
        from calendar_integration.tasks import sync_calendar_task

        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        if not context.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

        # Honor the per-calendar opt-out (holidays, birthdays, org-wide calendars, etc.).
        if not calendar.sync_enabled:
            logging.getLogger(__name__).info(
                "Skipping sync for calendar %s: sync_enabled is False.", calendar.id
            )
            return None

        calendar_sync = CalendarSync.objects.create(
            calendar=calendar,
            organization_id=calendar.organization_id,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=should_update_events,
            trigger_source=trigger_source,
        )
        account_type: Literal["social_account", "google_service_account"] = (
            "social_account"
            if isinstance(context.account, SocialAccount)
            else "google_service_account"
        )

        if not context.account or not context.account.id:
            raise NotImplementedError("Account is not set for the current service instance.")

        # Capture ids by value so the closure is independent of mutable self state.
        _account_type = account_type
        _account_id = context.account.id
        _calendar_sync_id = calendar_sync.id
        _organization_id = calendar.organization_id

        transaction.on_commit(
            lambda: sync_calendar_task.delay(  # type: ignore
                _account_type, _account_id, _calendar_sync_id, _organization_id
            )
        )
        return calendar_sync

    def sync_events(
        self,
        calendar_sync: CalendarSync,
    ) -> None:
        """
        Synchronize events for a calendar within a specified date range.
        :param calendar: The calendar to synchronize.
        :param start_date: Start date for the event search.
        :param end_date: End date for the event search.
        :param update_events: Whether to update existing events.
        :param sync_token: Token for incremental sync, if available.
        """
        if not is_authenticated_calendar_service(cast("BaseCalendarService", self._context)):
            raise

        latest_sync = calendar_sync.calendar.latest_sync

        calendar_sync.status = CalendarSyncStatus.IN_PROGRESS
        calendar_sync.save(update_fields=["status"])

        try:
            with transaction.atomic():
                self._execute_calendar_sync(
                    calendar_sync,
                    latest_sync.next_sync_token if latest_sync else None,
                )
        except Exception as e:  # noqa: BLE001
            # Handle exceptions during synchronization
            # This could include logging the error or re-raising it
            calendar_sync.status = CalendarSyncStatus.FAILED
            calendar_sync.error_message = str(e)
            calendar_sync.save(update_fields=["status", "error_message"])
            return

        calendar_sync.status = CalendarSyncStatus.SUCCESS
        calendar_sync.save(update_fields=["status"])

    def _execute_calendar_sync(
        self,
        calendar_sync: CalendarSync,
        sync_token: str | None = None,
    ) -> None:
        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        calendar: Calendar = calendar_sync.calendar
        start_date = calendar_sync.start_datetime
        end_date = calendar_sync.end_datetime
        should_update_events = calendar_sync.should_update_events

        events_dict = context.calendar_adapter.get_events(
            calendar.external_id, calendar.is_resource, start_date, end_date, sync_token
        )
        # Materialize so we can collect the incoming external ids up front; the
        # batch is already held fully in memory while building `changes` below.
        events = list(events_dict["events"])
        next_sync_token = events_dict["next_sync_token"]

        # Match existing rows by the external ids actually being synced, regardless
        # of the sync window. An event whose stored instant falls outside this
        # window (boundary/multi-day events, timezone shifts) must still update its
        # existing row instead of re-inserting it and colliding with the
        # (calendar_fk_id, external_id) unique constraint.
        incoming_external_ids = {e.external_id for e in events if e.external_id}

        # Prepare existing data mappings
        (
            calendar_events_by_external_id,
            blocked_times_by_external_id,
        ) = self._get_existing_calendar_data(
            calendar.id, start_date, end_date, incoming_external_ids
        )

        # Process events and collect changes
        changes = self._process_events_for_sync(
            events,
            calendar_events_by_external_id,
            blocked_times_by_external_id,
            calendar,
            should_update_events,
        )

        # Handle deletions for full sync
        if not sync_token:
            self._handle_deletions_for_full_sync(
                calendar.id,
                calendar_events_by_external_id,
                changes.matched_event_ids,
                start_date,
            )
        else:
            calendar_sync.next_sync_token = next_sync_token or ""
            calendar_sync.save(update_fields=["next_sync_token"])

        # Apply all changes to database
        self._apply_sync_changes(calendar.id, changes)

        # Update available time windows if needed
        if calendar.manage_available_windows:
            self._host._remove_available_time_windows_that_overlap_with_blocked_times_and_events(
                calendar.id,
                changes.blocked_times_to_create + changes.blocked_times_to_update,
                changes.events_to_update,
                start_date,
                end_date,
            )

    def _get_existing_calendar_data(
        self,
        calendar_id: int,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        incoming_external_ids: set[str] | None = None,
    ):
        """Get existing calendar events and blocked times to reconcile against.

        Loads rows that are either (a) inside the sync window — needed so the
        full-sync deletion pass can spot rows that vanished from the provider — or
        (b) carry one of the ``incoming_external_ids`` being synced now, even if
        their stored instant sits outside the window. Without (b), an out-of-window
        event is treated as new and re-inserted, colliding with the
        ``(calendar_fk_id, external_id)`` unique constraint.
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            return ({}, {})

        window = Q(start_time__gte=start_date, end_time__lte=end_date)
        if incoming_external_ids:
            window |= Q(external_id__in=incoming_external_ids)

        calendar_events_by_external_id = {
            e.external_id: e
            for e in CalendarEvent.objects.filter(
                window,
                calendar_fk_id=calendar_id,
                organization_id=context.organization.id,
            )
        }
        blocked_times_by_external_id = {
            e.external_id: e
            for e in BlockedTime.objects.filter(
                window,
                calendar_fk_id=calendar_id,
                organization_id=context.organization.id,
            )
        }
        return calendar_events_by_external_id, blocked_times_by_external_id

    # ------------------------------------------------------------------
    # Diff/merge machine
    # ------------------------------------------------------------------

    def _process_events_for_sync(
        self,
        events: Iterable[CalendarEventAdapterOutputData],
        calendar_events_by_external_id: dict,
        blocked_times_by_external_id: dict,
        calendar: Calendar,
        update_events: bool,
    ) -> EventsSyncChanges:
        """Process events and determine what changes need to be made."""
        changes = EventsSyncChanges()

        for event in events:
            existing_event = calendar_events_by_external_id.get(event.external_id)
            existing_blocked_time = blocked_times_by_external_id.get(event.external_id)

            if existing_event:
                self._process_existing_event(event, existing_event, changes, update_events)
            elif existing_blocked_time:
                self._process_existing_blocked_time(event, existing_blocked_time, changes)
            else:
                self._process_new_event(event, calendar, changes)

        return changes

    def _process_existing_event(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
        update_events: bool,
    ):
        """Process an existing calendar event."""
        if not update_events:
            return

        if event.status == "cancelled":
            changes.events_to_delete.append(existing_event.external_id)
            changes.matched_event_ids.add(existing_event.external_id)
            return

        # Update existing event
        existing_event.title = event.title
        existing_event.description = event.description
        existing_event.start_time = event.start_time
        existing_event.end_time = event.end_time
        existing_event.meta["latest_original_payload"] = event.original_payload or {}
        changes.events_to_update.append(existing_event)
        changes.matched_event_ids.add(existing_event.external_id)

        # Process attendees
        self._process_event_attendees(event, existing_event, changes)

    def _process_existing_blocked_time(
        self,
        event: CalendarEventAdapterOutputData,
        existing_blocked_time: BlockedTime,
        changes: EventsSyncChanges,
    ):
        """Process an existing blocked time."""
        if event.status == "cancelled":
            changes.blocks_to_delete.append(existing_blocked_time.external_id)
            changes.matched_event_ids.add(existing_blocked_time.external_id)
            return

        # Update existing blocked time
        existing_blocked_time.start_time = event.start_time
        existing_blocked_time.end_time = event.end_time
        existing_blocked_time.reason = event.title
        existing_blocked_time.external_id = event.external_id
        existing_blocked_time.meta["latest_original_payload"] = event.original_payload or {}
        changes.blocked_times_to_update.append(existing_blocked_time)
        changes.matched_event_ids.add(existing_blocked_time.external_id)

    def _process_new_event(
        self, event: CalendarEventAdapterOutputData, calendar: Calendar, changes: EventsSyncChanges
    ):
        """Process a new event by creating appropriate records."""
        if event.recurring_event_id:
            # This is an instance of a recurring event from external service
            try:
                parent_event = CalendarEvent.objects.get(
                    external_id=event.recurring_event_id,
                    organization_id=calendar.organization_id,
                )
                # Parent exists in our system, so this instance should be a CalendarEvent
                # (because the parent was created through our API)
                calendar_event = CalendarEvent(
                    calendar_fk=calendar,
                    start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.start_time, event.timezone
                    ),
                    end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.end_time, event.timezone
                    ),
                    timezone=event.timezone,
                    title=event.title,
                    description=event.description,
                    external_id=event.external_id,
                    meta={"latest_original_payload": event.original_payload or {}},
                    organization_id=calendar.organization_id,
                    parent_recurring_object_fk=parent_event,
                    recurrence_id=event.start_time,
                    is_recurring_exception=True,
                )
                changes.events_to_create.append(calendar_event)
            except CalendarEvent.DoesNotExist:
                # Parent doesn't exist in our system, so this is an instance of an externally-created
                # recurring event. Create as BlockedTime since we shouldn't modify external events.
                changes.blocked_times_to_create.append(
                    BlockedTime(
                        calendar_fk=calendar,
                        start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                            event.start_time, event.timezone
                        ),
                        end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                            event.end_time, event.timezone
                        ),
                        timezone=event.timezone,
                        reason=event.title,
                        external_id=event.external_id,
                        meta={
                            "latest_original_payload": event.original_payload or {},
                            "pending_parent_external_id": event.recurring_event_id,
                        },
                        organization_id=calendar.organization_id,
                    )
                )
        elif event.recurrence_rule:
            # This is a master recurring event coming from external sync
            # We need to determine if this was created through our API or externally
            # For now, if it's coming through sync, we'll assume it was created externally
            # and store as CalendarEvent with recurrence rule for visibility, but instances will be BlockedTime
            recurrence_rule = RecurrenceRule.from_rrule_string(
                event.recurrence_rule, calendar.organization
            )
            calendar_event = CalendarEvent(
                calendar_fk=calendar,
                start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                    event.start_time, event.timezone
                ),
                end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                    event.end_time, event.timezone
                ),
                timezone=event.timezone,
                title=event.title,
                description=event.description,
                external_id=event.external_id,
                meta={"latest_original_payload": event.original_payload or {}},
                organization_id=calendar.organization_id,
                recurrence_rule_fk=recurrence_rule,
            )
            changes.events_to_create.append(calendar_event)
            changes.recurrence_rules_to_create.append(recurrence_rule)
        else:
            # Regular single event from external sync - create as BlockedTime
            # since we shouldn't modify events created externally
            changes.blocked_times_to_create.append(
                BlockedTime(
                    calendar_fk=calendar,
                    start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.start_time, event.timezone
                    ),
                    end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.end_time, event.timezone
                    ),
                    timezone=event.timezone,
                    reason=event.title,
                    external_id=event.external_id,
                    meta={"latest_original_payload": event.original_payload or {}},
                    organization_id=calendar.organization_id,
                )
            )

        changes.matched_event_ids.add(event.external_id)

    def _process_event_attendees(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
    ):
        """Process attendees for an existing event."""
        for attendee in event.attendees:
            user = User.objects.filter(email=attendee.email).first()

            if user and not existing_event.attendees.filter(id=user.id).exists():
                changes.attendances_to_create.append(
                    EventAttendance(
                        event=existing_event,
                        user=None,
                        status=attendee.status,
                    )
                )
            elif (
                not user
                and not existing_event.external_attendances.filter(
                    external_attendee__email=attendee.email
                ).exists()
            ):
                external_attendee, _created = ExternalAttendee.objects.get_or_create(
                    email=attendee.email,
                    organization_id=existing_event.calendar.organization_id,
                    defaults={"name": attendee.name},
                )
                changes.external_attendances_to_create.append(
                    EventExternalAttendance(
                        event=existing_event,
                        external_attendee=external_attendee,
                        status=attendee.status,
                        organization_id=existing_event.calendar.organization_id,
                    )
                )
            else:
                # Update existing attendance status if needed
                attendance = (
                    existing_event.attendances.filter(user=user).first()
                    or existing_event.external_attendances.filter(
                        external_attendee__email=attendee.email
                    ).first()
                )
                if attendance:
                    attendance.status = attendee.status

    def _handle_deletions_for_full_sync(
        self,
        calendar_id: int,
        calendar_events_by_external_id: dict,
        matched_event_ids: set[str],
        start_date: datetime.datetime,
    ):
        """Handle deletions when doing a full sync (no sync_token)."""
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            return

        deleted_ids = set(calendar_events_by_external_id.keys()) - matched_event_ids
        CalendarEvent.objects.filter(
            calendar_fk_id=calendar_id,
            external_id__in=deleted_ids,
            start_time__gte=start_date,
            organization_id=context.organization.id,
        ).delete()

    def _apply_sync_changes(self, calendar_id: int, changes: EventsSyncChanges):
        """Apply all the collected changes to the database."""
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        # Create recurrence rules first
        if changes.recurrence_rules_to_create:
            RecurrenceRule.objects.bulk_create(changes.recurrence_rules_to_create)

        # Create events (which may reference recurrence rules)
        if changes.events_to_create:
            CalendarEvent.objects.bulk_create(changes.events_to_create)

        if changes.blocked_times_to_create:
            BlockedTime.objects.bulk_create(changes.blocked_times_to_create)

        if changes.events_to_update:
            CalendarEvent.objects.bulk_update(
                changes.events_to_update, ["title", "description", "start_time", "end_time"]
            )

        if changes.attendances_to_create:
            EventAttendance.objects.bulk_create(changes.attendances_to_create)

        if changes.external_attendances_to_create:
            EventExternalAttendance.objects.bulk_create(changes.external_attendances_to_create)

        if changes.blocked_times_to_update:
            BlockedTime.objects.bulk_update(
                changes.blocked_times_to_update,
                ["start_time_tz_unaware", "end_time_tz_unaware", "reason", "external_id"],
            )

        if changes.events_to_delete:
            CalendarEvent.objects.filter(
                calendar_fk_id=calendar_id,
                external_id__in=changes.events_to_delete,
                organization=context.organization,
            ).delete()

        if changes.blocks_to_delete:
            BlockedTime.objects.filter(
                calendar_fk_id=calendar_id,
                external_id__in=changes.blocks_to_delete,
                organization=context.organization,
            ).delete()

        # After all changes are applied, link orphaned recurring instances to their parents
        self._link_orphaned_recurring_instances(calendar_id)

    def _link_orphaned_recurring_instances(self, calendar_id: int):
        """
        Link recurring event instances that were created before their parent events
        were synced. This happens when webhook events come out of order.
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            return

        # Find events that have a pending parent external ID in their meta
        orphaned_instances = CalendarEvent.objects.filter(
            calendar_fk_id=calendar_id,
            organization_id=context.organization.id,
            parent_recurring_object__isnull=True,
            meta__pending_parent_external_id__isnull=False,
        )

        # Also find blocked times that might be orphaned instances
        orphaned_blocked_times = BlockedTime.objects.filter(
            calendar_fk_id=calendar_id,
            organization_id=context.organization.id,
            meta__pending_parent_external_id__isnull=False,
        )

        # Link orphaned CalendarEvent instances
        for instance in orphaned_instances:
            parent_external_id = instance.meta.get("pending_parent_external_id")
            if parent_external_id:
                try:
                    parent_event = CalendarEvent.objects.get(
                        external_id=parent_external_id,
                        organization_id=context.organization.id,
                    )
                    # Link the instance to its parent
                    instance.parent_recurring_object_fk = parent_event
                    instance.recurrence_id = instance.start_time
                    # Clear the pending parent ID
                    instance.meta.pop("pending_parent_external_id", None)
                    instance.save(
                        update_fields=["parent_recurring_object_fk", "recurrence_id", "meta"]
                    )
                except CalendarEvent.DoesNotExist:
                    # Parent still not synced, leave it for next sync
                    continue

        # For orphaned BlockedTime instances, we just clear the pending parent ID
        # since BlockedTime doesn't have parent relationships
        for blocked_time in orphaned_blocked_times:
            parent_external_id = blocked_time.meta.get("pending_parent_external_id")
            if parent_external_id:
                try:
                    # Check if parent exists now
                    CalendarEvent.objects.get(
                        external_id=parent_external_id,
                        organization_id=context.organization.id,
                    )
                    # Parent exists, clear the pending flag
                    blocked_time.meta.pop("pending_parent_external_id", None)
                    blocked_time.save(update_fields=["meta"])
                except CalendarEvent.DoesNotExist:
                    # Parent still not synced, leave it for next sync
                    continue

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def convert_naive_utc_datetime_to_timezone(
        self, datetime_obj: datetime.datetime, iana_tz: str
    ) -> datetime.datetime:
        """Return the naive local wall-clock of an instant in the given IANA timezone.

        Delegates to the shared module-level utility in ``calendar_service_utils``.
        """
        return _convert_naive_utc_datetime_to_timezone(datetime_obj, iana_tz)
