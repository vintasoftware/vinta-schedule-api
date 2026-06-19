"""Single + recurring calendar-event CRUD, transfer, and expansion reads.

``CalendarEventService`` owns the event concern extracted from the
``CalendarService`` facade. It is a plain class (not a DI-container provider):
the facade constructs it once, *after* authentication, feeding it the shared
:class:`CalendarServiceContext` so it never re-authenticates or re-builds a
calendar adapter (the perf guardrail). Everything it needs arrives via the
constructor:

- ``context`` — the immutable auth snapshot (organization, user_or_token,
  account, calendar_adapter, permission_service, side_effects_service). Read
  through ``self._context``; the auth guards in ``type_guards.py`` inspect the
  same ``organization`` / ``account`` / ``calendar_adapter`` attributes the
  context exposes, so behavior is byte-for-byte identical to the former methods.
- ``recurrence_manager`` — the stateless :class:`RecurrenceManager` the facade
  also holds; the recurrence event methods delegate to it.
- ``calendar_cache`` — the facade-owned, per-instance ``{(org_id, id): Calendar}``
  cache (the lru_cache multi-tenant fix from Phase 0). Shared so lookups are not
  duplicated across the facade and this service.
- ``host`` — the :class:`EventServiceHost` (in Phase 2 the facade itself). The event
  concern routes three things back through it: availability queries
  (``get_availability_windows_in_range`` — extracted in Phase 4), bundle-event
  fan-out (``_create_bundle_event`` / ``_update_bundle_event`` /
  ``_delete_bundle_event`` — extracted in Phase 3), and the shared write-adapter /
  attendee-permission helpers (``_get_write_adapter_for_calendar`` /
  ``_grant_event_attendee_permissions``) that stay resident on the facade because the
  sync/availability flows and the existing test suite reference them there. Routing
  through the host keeps those single implementations and behavior byte-for-byte;
  later phases swap concrete sub-services in without touching this service.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q

from calendar_integration.constants import CalendarType
from calendar_integration.exceptions import NoAvailableTimeWindowsError
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarOwnership,
    EventAttendance,
    EventBulkModification,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    RecurrenceRule,
    RecurringMixin,
    ResourceAllocation,
)
from calendar_integration.services.calendar_service_utils import (
    convert_naive_utc_datetime_to_timezone as _convert_naive_utc_datetime_to_timezone,
)
from calendar_integration.services.calendar_service_utils import (
    get_calendar_by_id as _get_calendar_by_id_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event as _serialize_event_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event_data_input as _serialize_event_data_input_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event_external_attendee as _serialize_event_external_attendee_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event_internal_attendee as _serialize_event_internal_attendee_util,
)
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterInputData,
    CalendarEventData,
    CalendarEventInputData,
    CalendarSettingsData,
    EventAttendanceInputData,
    EventAttendeeData,
    EventExternalAttendanceInputData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
    ResourceData,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.type_guards import (
    is_authenticated_calendar_service,
    is_initialized_or_authenticated_calendar_service,
)
from public_api.models import SystemUser
from users.models import User


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from calendar_integration.querysets import CalendarEventQuerySet
    from calendar_integration.services.calendar_service_context import CalendarServiceContext
    from calendar_integration.services.dataclasses import AvailableTimeWindow
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
    from calendar_integration.services.recurrence_manager import RecurrenceManager


class EventServiceHost(Protocol):
    """The collaborator surface the event concern still routes back to the facade for.

    Three concerns are not extracted in Phase 2 and stay on the facade:

    - **availability** (``get_availability_windows_in_range``) — extracted in Phase 4;
    - **bundle fan-out** (``_create_bundle_event`` / ``_update_bundle_event`` /
      ``_delete_bundle_event``) — extracted in Phase 3;
    - **write-adapter resolution + attendee-permission granting**
      (``_get_write_adapter_for_calendar`` / ``_grant_event_attendee_permissions``) —
      these remain shared facade helpers (the existing test suite patches them on the
      facade, and other facade flows — e.g. sync — still call them), so the event
      service calls them through the host to keep that single implementation and keep
      behavior byte-for-byte.

    In Phase 2 the facade supplies *itself*. Later phases replace individual concerns
    (e.g. Phase 3 swaps a ``CalendarBundleService`` in for the bundle methods, Phase 4
    an ``AvailabilityService`` for availability) without changing this service's call
    sites — they keep calling ``self._host.<method>``.
    """

    def get_availability_windows_in_range(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> Iterable[AvailableTimeWindow]: ...

    def _get_write_adapter_for_calendar(self, calendar: Calendar) -> CalendarAdapter | None: ...

    def _grant_event_attendee_permissions(self, event: CalendarEvent) -> None: ...

    def create_event(
        self, calendar_id: int, event_data: CalendarEventInputData
    ) -> CalendarEvent: ...

    def delete_event(
        self, calendar_id: int, event_id: int, delete_series: bool = False
    ) -> None: ...

    def _create_bundle_event(
        self, bundle_calendar: Calendar, event_data: CalendarEventInputData
    ) -> CalendarEvent: ...

    def _update_bundle_event(
        self, bundle_event: CalendarEvent, event_data: CalendarEventInputData
    ) -> CalendarEvent: ...

    def _delete_bundle_event(self, bundle_event: CalendarEvent) -> None: ...


class CalendarEventService:
    """Owns single + recurring event CRUD, transfer, and expansion reads."""

    def __init__(
        self,
        context: CalendarServiceContext,
        recurrence_manager: RecurrenceManager,
        calendar_cache: dict[tuple[int, str | int], Calendar],
        host: EventServiceHost,
    ) -> None:
        self._context = context
        self._recurrence_manager = recurrence_manager
        self._calendar_cache = calendar_cache
        # Phase 2 seam: availability (Phase 4), bundle fan-out (Phase 3), and the
        # shared write-adapter / attendee-permission helpers are reached through the
        # host (the facade). See ``EventServiceHost``.
        self._host = host

    # ------------------------------------------------------------------
    # Internal helpers (event-write concern)
    # ------------------------------------------------------------------

    def _get_calendar_by_id(self, calendar_id: int) -> Calendar:
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        return _get_calendar_by_id_util(
            self._calendar_cache,
            calendar_id,
            context.organization,
        )

    @staticmethod
    def _scoped_system_user_owns_calendar(system_user: SystemUser, calendar: Calendar) -> bool:
        """Independently verify the scoped token's owner owns ``calendar``.

        This is the narrow, sanctioned event-creation allowance for owner-scoped public-API
        tokens. The check does NOT trust anything supplied by the caller: it re-derives the
        ownership relation from the database, scoped to the calendar's own organization, so a
        cross-organization or cross-owner token can never pass.

        Ownership is confirmed when a :class:`CalendarOwnership` row exists for ``calendar``
        whose ``user`` is the member behind the token's ``scoped_to_membership`` (an
        ``OrganizationMembership``). Org-wide tokens (``scoped_to_membership_fk_id is None``)
        always return ``False`` — event creation stays blocked for them.

        Args:
            system_user: The token (``SystemUser``) attempting the write.
            calendar: The target calendar the event would be created on.

        Returns:
            ``True`` only when the token is scoped AND its owner independently owns the
            target calendar in that calendar's organization; ``False`` otherwise.
        """
        if system_user.scoped_to_membership_fk_id is None:
            return False
        return (
            CalendarOwnership.objects.filter_by_organization(calendar.organization_id)
            .filter(
                calendar_fk_id=calendar.id,
                user__organization_memberships=system_user.scoped_to_membership_fk_id,
            )
            .exists()
        )

    def _serialize_event(self, event: CalendarEvent) -> CalendarEventData:
        """Build webhook payload for calendar event."""
        return _serialize_event_util(event)

    def _serialize_event_internal_attendee(
        self, attendance: EventAttendance
    ) -> EventInternalAttendeeData:
        return _serialize_event_internal_attendee_util(attendance)

    def _serialize_event_external_attendee(
        self, external_attendance: EventExternalAttendance
    ) -> EventExternalAttendeeData:
        return _serialize_event_external_attendee_util(external_attendance)

    def _serialize_event_data_input(
        self, event: CalendarEvent, event_data: CalendarEventInputData
    ) -> CalendarEventData:
        return _serialize_event_data_input_util(event, event_data, self._context.organization)

    def convert_naive_utc_datetime_to_timezone(
        self, datetime_obj: datetime.datetime, iana_tz: str
    ) -> datetime.datetime:
        """Return the naive local wall-clock of an instant in the given IANA timezone.

        Delegates to the shared module-level utility in ``calendar_service_utils``.
        See that function's docstring for full semantics.

        e.g. 12:00Z + "America/Recife" -> 09:00 (naive).
        """
        return _convert_naive_utc_datetime_to_timezone(datetime_obj, iana_tz)

    # ------------------------------------------------------------------
    # Public event CRUD
    # ------------------------------------------------------------------

    @transaction.atomic()
    def create_event(self, calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        """
        Create a new event in the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_data: Dictionary containing event details.
        :return: Response from the calendar client.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        calendar = self._get_calendar_by_id(calendar_id)

        # When True, authorization is granted by independently-verified calendar ownership
        # (the owner-scoped public-API allowance) rather than by the permission-token flow.
        is_owner_scoped_system_user = False

        if isinstance(context.user_or_token, User):
            context.calendar_permission_service.initialize_with_user(
                context.user_or_token,
                organization_id=calendar.organization_id,
                calendar_id=calendar_id,
            )
        elif isinstance(context.user_or_token, SystemUser):
            # Public-API event creation is blocked by default. The single sanctioned
            # exception: an owner-scoped token whose owner *independently* owns the target
            # calendar (verified against CalendarOwnership, NOT trusted from the caller).
            # Org-wide tokens (scoped_to_membership_fk_id is None) stay blocked — they must
            # route through single-use codes / public scheduling.
            if not self._scoped_system_user_owns_calendar(context.user_or_token, calendar):
                raise PermissionDenied("Events cannot be created through the Public API.")
            # Bundle calendars are rejected for scoped tokens: create_event would recurse
            # into per-child creates that span other providers' calendars, producing a
            # confusing partial failure. Reject up front with a clear, scoped error.
            if calendar.calendar_type == CalendarType.BUNDLE:
                raise PermissionDenied(
                    "Events cannot be scheduled on a bundle calendar through the Public API."
                )
            is_owner_scoped_system_user = True

        # The permission-token scheduling check applies to the User / token flows. The
        # owner-scoped path's authorization is the independently-verified ownership above,
        # so it bypasses this check (the permission service has no token initialized for it).
        if not is_owner_scoped_system_user and (
            not context.calendar_permission_service.can_perform_scheduling(
                calendar_id=calendar_id,
                calendar_settings=CalendarSettingsData(
                    manage_available_windows=calendar.manage_available_windows,
                    accepts_public_scheduling=calendar.accepts_public_scheduling,
                ),
                event=event_data,
            )
        ):
            raise PermissionDenied("You do not have permission to update this event.")

        if calendar.calendar_type == CalendarType.BUNDLE:
            return self._host._create_bundle_event(bundle_calendar=calendar, event_data=event_data)

        available_windows = self._host.get_availability_windows_in_range(
            calendar,
            event_data.start_time,
            event_data.end_time,
        )
        if not available_windows:
            raise NoAvailableTimeWindowsError()

        external_id = ""
        original_payload: dict = {}
        if calendar.calendar_type in [CalendarType.PERSONAL, CalendarType.RESOURCE] and (
            write_adapter := self._host._get_write_adapter_for_calendar(calendar)
        ):
            users_by_id = {
                u.id: u
                for u in User.objects.filter(id__in=[a.user_id for a in event_data.attendances])
            }
            resources_by_id = {
                r.id: r
                for r in Calendar.objects.filter_by_organization(context.organization.id).filter(
                    id__in=[r.resource_id for r in event_data.resource_allocations]
                )
            }

            created_event = write_adapter.create_event(
                CalendarEventAdapterInputData(
                    calendar_external_id=calendar.external_id,
                    title=event_data.title,
                    description=event_data.description,
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    timezone=event_data.timezone,
                    attendees=[
                        EventAttendeeData(
                            email=users_by_id[a.user_id].email,
                            name=(
                                users_by_id[a.user_id].get_full_name()
                                if hasattr(users_by_id[a.user_id], "profile")
                                and hasattr(users_by_id[a.user_id].profile, "__str__")
                                else None
                            )
                            or users_by_id[a.user_id].email,
                            status="pending",
                        )
                        for a in event_data.attendances
                    ],
                    resources=[
                        ResourceData(
                            email=resources_by_id[r.resource_id].email,
                            title=resources_by_id[r.resource_id].name,
                            external_id=resources_by_id[r.resource_id].external_id,
                            status="accepted",
                        )
                        for r in event_data.resource_allocations
                    ],
                    recurrence_rule=event_data.recurrence_rule,
                    is_recurring_instance=event_data.is_recurring_exception,
                )
            )
            external_id = created_event.external_id
            original_payload = created_event.original_payload or {}

        # Handle parent event for exceptions/instances
        parent_event = None
        if event_data.parent_event_id:
            parent_event = CalendarEvent.objects.get(
                id=event_data.parent_event_id,
                organization_id=context.organization.id,
            )

        # Create recurrence rule if provided
        recurrence_rule = None
        if event_data.recurrence_rule and not event_data.parent_event_id:
            recurrence_rule = RecurrenceRule.from_rrule_string(
                event_data.recurrence_rule, context.organization
            )
            recurrence_rule.save()

        # Create the event using the manager's create method to ensure proper organization handling
        event = CalendarEvent(
            calendar_fk=calendar,
            organization=context.organization,
            title=event_data.title,
            description=event_data.description or "",
            start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                event_data.start_time, event_data.timezone
            ),
            end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                event_data.end_time, event_data.timezone
            ),
            timezone=event_data.timezone,
            external_id=external_id,
            meta={"latest_original_payload": original_payload} if context.calendar_adapter else {},
            parent_recurring_object_fk=parent_event,
            is_recurring_exception=event_data.is_recurring_exception,
            recurrence_id=event_data.start_time if parent_event else None,
        )

        if recurrence_rule:
            event.recurrence_rule_fk = recurrence_rule  # type: ignore

        event.save()

        EventExternalAttendance.objects.bulk_create(
            [
                EventExternalAttendance(
                    organization=context.organization,
                    event=event,
                    external_attendee=ExternalAttendee.objects.create(
                        organization=context.organization,
                        email=attendance_data.external_attendee.email,
                        name=attendance_data.external_attendee.name,
                    ),
                )
                for attendance_data in event_data.external_attendances
            ]
        )

        EventAttendance.objects.bulk_create(
            [
                EventAttendance(
                    organization=context.organization,
                    event=event,
                    user_id=attendance_data.user_id,
                )
                for attendance_data in event_data.attendances
            ]
        )

        ResourceAllocation.objects.bulk_create(
            [
                ResourceAllocation(
                    organization=context.organization,
                    event=event,
                    calendar_fk_id=resource_allocation_data.resource_id,
                )
                for resource_allocation_data in event_data.resource_allocations
            ]
        )

        # Grant permissions to event attendees
        self._host._grant_event_attendee_permissions(event)

        # Resolve the audit actor *before* queueing the post-commit side-effect.
        # The owner-scoped public-API path never initializes a permission token, so
        # ``calendar_permission_service.token`` may be unset entirely — read it through
        # ``getattr`` to avoid an AttributeError after commit, and fall back to the
        # SystemUser caller so owner-scoped events are never actor-less.
        permission_token = getattr(context.calendar_permission_service, "token", None)
        if permission_token is not None and permission_token.user:
            audit_actor: Any = permission_token.user
        elif permission_token is not None:
            audit_actor = permission_token
        else:
            audit_actor = context.user_or_token

        transaction.on_commit(
            lambda: (
                context.calendar_side_effects_service.on_create_event(
                    actor=audit_actor,
                    event=self._serialize_event(event),
                    organization=event.organization,
                )
                if context.calendar_side_effects_service
                else None
            )
        )

        return event

    @transaction.atomic()
    def update_event(
        self, calendar_id: int, event_id: int, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        """
        Update an existing event in the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_id: Unique identifier of the event to update.
        :param event_data: Dictionary containing updated event details.
        :return: Updated CalendarEvent instance.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        event = CalendarEvent.objects.select_related("calendar").get(
            calendar_fk_id=calendar_id,
            id=event_id,
            organization_id=context.organization.id,
        )

        if isinstance(context.user_or_token, User):
            context.calendar_permission_service.initialize_with_user(
                context.user_or_token,
                organization_id=event.organization_id,
                event_id=event_id,
            )
        elif isinstance(context.user_or_token, SystemUser):
            raise PermissionDenied("Events cannot be created through the Public API.")

        serialized_old_event = self._serialize_event(event)
        if not context.calendar_permission_service.can_perform_update(
            old_event=serialized_old_event,
            new_event=self._serialize_event_data_input(event, event_data),
        ):
            raise PermissionDenied("You do not have permission to update this event.")

        if event.is_bundle_primary:
            return self._host._update_bundle_event(event, event_data)
        elif event.is_bundle_event:
            raise ValueError(
                "Cannot update an event created from bundle calendar from a non-primary "
                "calendar event"
            )

        original_payload: dict[str, Any] = {}
        if event.calendar.calendar_type in [
            CalendarType.PERSONAL,
            CalendarType.RESOURCE,
        ] and (write_adapter := self._host._get_write_adapter_for_calendar(event.calendar)):
            users_by_id = {
                u.id: u
                for u in User.objects.filter(id__in=[a.user_id for a in event_data.attendances])
            }
            attendance_by_user_id = {
                a.user_id: a
                for a in EventAttendance.objects.filter_by_organization(
                    context.organization.id
                ).filter(event__id=event_id, user_id__in=users_by_id.keys())
            }
            resources_by_id = {
                r.id: r
                for r in Calendar.objects.filter_by_organization(context.organization.id).filter(
                    id__in=[r.resource_id for r in event_data.resource_allocations]
                )
            }

            updated_event = write_adapter.update_event(
                event.calendar.id,
                event.id,
                CalendarEventAdapterInputData(
                    calendar_external_id=event.calendar.external_id,
                    title=event_data.title,
                    description=event_data.description,
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    timezone=event_data.timezone,
                    attendees=[
                        EventAttendeeData(
                            email=users_by_id[a.user_id].email,
                            name=(
                                users_by_id[a.user_id].get_full_name()
                                if hasattr(users_by_id[a.user_id], "profile")
                                and hasattr(users_by_id[a.user_id].profile, "__str__")
                                else None
                            )
                            or users_by_id[a.user_id].email,
                            status=(
                                attendance_by_user_id[a.user_id].status
                                if a.user_id in attendance_by_user_id
                                else "pending"
                            ),
                        )
                        for a in event_data.attendances
                    ],
                    external_id=event.external_id,
                    resources=[
                        ResourceData(
                            email=resources_by_id[r.resource_id].email,
                            title=resources_by_id[r.resource_id].name,
                            external_id=resources_by_id[r.resource_id].external_id,
                            status="accepted",
                        )
                        for r in event_data.resource_allocations
                    ],
                ),
            )
            original_payload = updated_event.original_payload or {}

        event.title = event_data.title
        event.description = event_data.description
        # ``start_time`` / ``end_time`` are DB-generated fields (``GeneratedField``
        # with ``db_persist=True``) that derive from ``start_time_tz_unaware`` and
        # the IANA ``timezone``.  Assigning to the generated fields is silently
        # ignored by Django's UPDATE statement, so we must update the underlying
        # writable fields instead.
        event.start_time_tz_unaware = self.convert_naive_utc_datetime_to_timezone(
            event_data.start_time, event_data.timezone
        )
        event.end_time_tz_unaware = self.convert_naive_utc_datetime_to_timezone(
            event_data.end_time, event_data.timezone
        )
        event.timezone = event_data.timezone
        if context.calendar_adapter:
            event.meta["latest_original_payload"] = original_payload

        # update recurrence rule
        if event_data.recurrence_rule:
            recurrence_rule = RecurrenceRule.from_rrule_string(
                rrule_string=event_data.recurrence_rule,
                organization=context.organization,
            )
            if event.recurrence_rule:
                recurrence_rule.id = event.recurrence_rule.id
            recurrence_rule.save()
            event.recurrence_rule = recurrence_rule
        elif event.recurrence_rule:
            # turn recurring event into non-recurring
            event.recurrence_rule.delete()
            event.recurrence_rule = None

        event.save()

        existing_attendances = {a.user_id: a for a in event.attendances.all()}
        existing_external_attendances = {
            a.external_attendee_fk_id: a for a in event.external_attendances.all()
        }
        existing_resource_allocation = {
            r.calendar_fk_id: r for r in event.resource_allocations.all()
        }

        maintained_external_attendees_ids = []
        external_attendees_to_update = []
        external_attendees_to_create = []
        external_attendances_to_create = []
        serialized_external_attendances_to_create = []
        serialized_external_attendances_to_update = []
        for external_attendance_data in event_data.external_attendances:
            if (
                external_attendance_data.external_attendee.id
                and external_attendance_data.external_attendee.id
                in existing_external_attendances.keys()
            ):
                attendance_to_update = existing_external_attendances[
                    external_attendance_data.external_attendee.id
                ]
                attendance_to_update.external_attendee.email = (
                    external_attendance_data.external_attendee.email
                )
                attendance_to_update.external_attendee.name = (
                    external_attendance_data.external_attendee.name
                )
                serialized_external_attendances_to_update.append(
                    self._serialize_event_external_attendee(attendance_to_update)
                )
                external_attendees_to_update.append(attendance_to_update.external_attendee)
            else:
                external_attendee = ExternalAttendee(
                    organization=context.organization,
                    email=external_attendance_data.external_attendee.email,
                    name=external_attendance_data.external_attendee.name,
                )
                external_attendees_to_create.append(external_attendee)
                external_attendance_instance = EventExternalAttendance(
                    organization=context.organization,
                    event=event,
                    external_attendee=external_attendee,
                )
                external_attendances_to_create.append(external_attendance_instance)
                serialized_external_attendances_to_create.append(
                    self._serialize_event_external_attendee(external_attendance_instance)
                )
            if external_attendance_data.external_attendee:
                maintained_external_attendees_ids.append(
                    external_attendance_data.external_attendee.id
                )
        ExternalAttendee.objects.bulk_update(external_attendees_to_update, ["email", "name"])
        ExternalAttendee.objects.bulk_create(external_attendees_to_create)
        EventExternalAttendance.objects.bulk_create(external_attendances_to_create)

        external_attendees_to_delete = set(existing_external_attendances.keys()) - set(
            maintained_external_attendees_ids
        )

        event_external_attendances_instance_to_delete = (
            EventExternalAttendance.objects.filter_by_organization(context.organization.id).filter(
                external_attendee_fk_id__in=external_attendees_to_delete
            )
        )
        serialized_external_attendances_to_delete = [
            self._serialize_event_external_attendee(external_attendance)
            for external_attendance in event_external_attendances_instance_to_delete
        ]

        event_external_attendances_instance_to_delete.delete()
        ExternalAttendee.objects.filter_by_organization(context.organization.id).filter(
            id__in=external_attendees_to_delete
        ).delete()

        maintained_attendees_ids = []
        event_attendances_to_create = []
        serialized_attendances_to_create = []
        for attendance_data in event_data.attendances:
            if not existing_attendances.get(attendance_data.user_id):
                event_attendance_instance = EventAttendance(
                    organization=context.organization,
                    event=event,
                    user_id=attendance_data.user_id,
                )
                event_attendances_to_create.append(event_attendance_instance)
                serialized_attendances_to_create.append(
                    self._serialize_event_internal_attendee(event_attendance_instance)
                )
            maintained_attendees_ids.append(attendance_data.user_id)

        EventAttendance.objects.bulk_create(event_attendances_to_create)

        # Grant permissions to newly added internal attendees
        if event_attendances_to_create and context.calendar_permission_service:
            for attendance in event_attendances_to_create:
                user = User.objects.get(id=attendance.user_id)
                # Check if user already has a token for this event
                existing_token = CalendarManagementToken.objects.filter(
                    user=user,
                    event_fk_id=event.id,
                    organization_id=context.organization.id,
                    revoked_at__isnull=True,
                ).first()

                if not existing_token:
                    context.calendar_permission_service.create_attendee_token(
                        organization_id=event.organization_id,
                        user=user,
                        permissions=None,  # Will use default attendee permissions
                        event_id=event.id,
                    )

        # Grant permissions to newly added external attendees
        if external_attendances_to_create and context.calendar_permission_service:
            for external_attendance in external_attendances_to_create:
                # Check if external attendee already has a token for this event
                existing_token = CalendarManagementToken.objects.filter(
                    organization_id=event.organization_id,
                    external_attendee_fk_id=external_attendance.external_attendee.id,
                    event_fk_id=event.id,
                    revoked_at__isnull=True,
                ).first()

                if not existing_token:
                    context.calendar_permission_service.create_external_attendee_update_token(
                        organization_id=event.organization_id,
                        event_id=event.id,
                        external_attendee_id=external_attendance.external_attendee.id,
                        permissions=None,  # Will use default external attendee permissions
                    )

        attendances_to_delete = set(existing_attendances.keys()) - set(maintained_attendees_ids)
        attendances_instances_to_delete = EventAttendance.objects.filter_by_organization(
            context.organization.id
        ).filter(user_id__in=attendances_to_delete)
        serialized_attendances_to_delete = [
            self._serialize_event_internal_attendee(attendance)
            for attendance in attendances_instances_to_delete
        ]
        attendances_instances_to_delete.delete()

        maintained_resources_ids = []
        resource_allocations_to_create = []
        for resource_allocation_data in event_data.resource_allocations:
            if resource_allocation_data.resource_id not in existing_resource_allocation.keys():
                resource_allocations_to_create.append(
                    ResourceAllocation(
                        organization_id=context.organization.id,
                        event=event,
                        calendar_fk_id=resource_allocation_data.resource_id,
                    )
                )
            maintained_resources_ids.append(resource_allocation_data.resource_id)

        ResourceAllocation.objects.bulk_create(resource_allocations_to_create)
        resources_to_delete = set(existing_resource_allocation) - set(maintained_resources_ids)
        ResourceAllocation.objects.filter_by_organization(context.organization.id).filter(
            calendar_fk_id__in=resources_to_delete
        ).delete()

        def call_side_effects():
            if not context.calendar_side_effects_service:
                return

            actor = (
                context.calendar_permission_service.token.user
                if (
                    context.calendar_permission_service.token
                    and context.calendar_permission_service.token.user
                )
                else context.calendar_permission_service.token
            )
            context.calendar_side_effects_service.on_update_event(
                actor=actor,
                event=self._serialize_event(event),
                organization=event.organization,
            )
            for payload in serialized_attendances_to_create:
                context.calendar_side_effects_service.on_add_attendee_to_event(
                    actor=actor,
                    event=self._serialize_event(event),
                    attendee=payload,
                    organization=event.organization,
                )
            for payload in serialized_attendances_to_delete:
                context.calendar_side_effects_service.on_remove_attendee_from_event(
                    actor=actor,
                    event=self._serialize_event(event),
                    attendee=payload,
                    organization=event.organization,
                )
            for payload in serialized_external_attendances_to_create:
                context.calendar_side_effects_service.on_add_attendee_to_event(
                    actor=actor,
                    event=self._serialize_event(event),
                    attendee=payload,
                    organization=event.organization,
                )
            for payload in serialized_external_attendances_to_delete:
                context.calendar_side_effects_service.on_remove_attendee_from_event(
                    actor=actor,
                    event=self._serialize_event(event),
                    attendee=payload,
                    organization=event.organization,
                )
            for payload in serialized_external_attendances_to_update:
                context.calendar_side_effects_service.on_update_attendee_on_event(
                    actor=actor,
                    event=self._serialize_event(event),
                    attendee=payload,
                    organization=event.organization,
                )

        transaction.on_commit(lambda: call_side_effects())

        return event

    def create_recurring_event(
        self,
        calendar_id: int,
        title: str,
        description: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        recurrence_rule: str,
        attendances: list[EventAttendanceInputData] | None = None,
        external_attendances: list[EventExternalAttendanceInputData] | None = None,
        resource_allocations: list[ResourceAllocationInputData] | None = None,
    ) -> CalendarEvent:
        """
        Create a recurring event with the specified recurrence rule.

        This method is just a shortcut, the `create_event` method also supports the
        creation of recurring events.

        :param calendar_id: Internal ID of the calendar
        :param title: Event title
        :param description: Event description
        :param start_time: Start time for the first occurrence
        :param end_time: End time for the first occurrence
        :param recurrence_rule: RRULE string defining the recurrence pattern
        :param attendances: List of internal attendees
        :param external_attendances: List of external attendees
        :param resource_allocations: List of resource allocations
        :return: Created CalendarEvent with recurrence rule
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        event_data = CalendarEventInputData(
            title=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            recurrence_rule=recurrence_rule,
            attendances=attendances or [],
            external_attendances=external_attendances or [],
            resource_allocations=resource_allocations or [],
        )
        return self.create_event(calendar_id, event_data)

    def create_recurring_event_exception(
        self,
        parent_event: CalendarEvent,
        exception_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        modified_timezone: str | None = None,
        is_cancelled: bool = False,
    ) -> CalendarEvent | None:
        """
        Create an exception for a recurring event (either cancelled or modified).

        If the exception is on the master event, this method makes the master event non-recurring
        and creates a new recurring event on the second occurrence

        :param parent_event: The recurring event to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_title: New title for the modified occurrence (if not cancelled)
        :param modified_description: New description for the modified occurrence (if not cancelled)
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param modified_timezone: New timezone for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified event or None if cancelled
        """

        def create_new_recurring_event(
            parent_obj: RecurringMixin,
            second_occurrence: RecurringMixin,
            new_recurrence_rule: RecurrenceRule,
        ) -> RecurringMixin:
            parent_event = cast(CalendarEvent, parent_obj)
            second_event = cast(CalendarEvent, second_occurrence)
            new_recurring_event = self.create_recurring_event(
                calendar_id=parent_event.calendar.id,
                title=parent_event.title,
                description=parent_event.description,
                start_time=second_event.start_time,
                end_time=second_event.end_time,
                timezone=parent_event.timezone,
                recurrence_rule=new_recurrence_rule.to_rrule_string(),
                attendances=[
                    EventAttendanceInputData(user_id=a.user_id)
                    for a in parent_event.attendances.all()
                ],
                external_attendances=[
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            email=ea.external_attendee.email,
                            name=ea.external_attendee.name,
                            id=ea.external_attendee.id,
                        )
                    )
                    for ea in parent_event.external_attendances.all()
                ],
                resource_allocations=[
                    ResourceAllocationInputData(resource_id=r.calendar_fk_id)  # type: ignore
                    for r in parent_event.resource_allocations.all()
                ],
            )
            return new_recurring_event

        def create_modified_event(
            parent_obj: RecurringMixin,
            exception_datetime: datetime.datetime,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent_event = cast(CalendarEvent, parent_obj)
            modified_event_data = CalendarEventInputData(
                title=modification_data.get("title") or parent_event.title,
                description=modification_data.get("description") or parent_event.description,
                start_time=modification_data.get("start_time") or exception_datetime,
                end_time=modification_data.get("end_time")
                or (exception_datetime + parent_event.duration),
                timezone=modification_data.get("timezone") or parent_event.timezone,
                parent_event_id=parent_event.id,
                is_recurring_exception=True,
            )
            return self.create_event(parent_event.calendar.id, modified_event_data)

        def update_exception_manager(
            parent_obj: RecurringMixin, new_recurring_obj: RecurringMixin
        ) -> None:
            EventRecurrenceException.objects.filter(parent_event=parent_obj).update(
                parent_event_fk=new_recurring_obj
            )

        def delete_exception_manager(parent_obj: RecurringMixin) -> None:
            EventRecurrenceException.objects.filter(parent_event=parent_obj).delete()

        modification_data = {
            "title": modified_title,
            "description": modified_description,
            "start_time": modified_start_time,
            "end_time": modified_end_time,
            "timezone": modified_timezone,
        }

        result = self._recurrence_manager.create_recurring_exception_generic(
            self._context,
            object_type_name="event",
            parent_object=parent_event,
            exception_date=exception_date,
            is_cancelled=is_cancelled,
            modification_data=modification_data,
            create_new_recurring_callback=create_new_recurring_event,
            create_modified_object_callback=create_modified_event,
            exception_manager_update_callback=update_exception_manager,
            exception_manager_delete_callback=delete_exception_manager,
        )
        return cast(CalendarEvent, result) if result else None

    def get_recurring_event_instances(
        self,
        recurring_event: CalendarEvent,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_exceptions: bool = True,
    ) -> list[CalendarEvent]:
        """
        Get all instances of a recurring event within a date range.

        :param recurring_event: The recurring event
        :param start_date: Start of the date range
        :param end_date: End of the date range
        :param include_exceptions: Whether to include modified exceptions
        :return: List of event instances
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        if not recurring_event.is_recurring:
            return [recurring_event] if start_date <= recurring_event.start_time <= end_date else []

        return recurring_event.get_occurrences_in_range(
            start_date, end_date, include_self=True, include_exceptions=include_exceptions
        )

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        optimize_queryset: Callable[[CalendarEventQuerySet], CalendarEventQuerySet] | None = None,
    ) -> list[CalendarEvent]:
        """
        Get all calendar events in a date range with recurring events expanded to instances.

        For all calendars (both external and internal), this method:
        1. Gets non-recurring events within the date range
        2. Gets recurring master events and generates their instances dynamically
        3. Includes synced exceptions (modified/cancelled instances from external providers)
        4. Excludes master recurring events from the final result (only instances are returned)

        External providers (Google, Microsoft) only store master recurring events and sync
        exceptions, so we generate instances on our side while respecting their exceptions.

        :param calendar: The calendar to get events from
        :param start_date: Start of the date range
        :param end_date: End of the date range
        :param optimize_queryset: Optional callable (typically a serializer's
            ``get_optimized_queryset``) applied to the master-event base queryset so its
            nested relations are prefetched. Generated occurrences reuse their master's
            prefetch cache, so the whole result serializes without per-event N+1s.
        :return: List of all event instances in the range
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        base_qs = (
            CalendarEvent.objects.annotate_recurring_occurrences_on_date_range(start_date, end_date)
            .select_related("recurrence_rule")
            .filter(
                parent_recurring_object__isnull=True,  # Master events only
            )
        )
        if calendar.calendar_type == CalendarType.BUNDLE:
            base_qs = base_qs.filter(
                organization_id=calendar.organization_id,
                calendar__in=calendar.bundle_children.all(),
            )
        else:
            base_qs = base_qs.filter(
                organization_id=calendar.organization_id,
                calendar=calendar,
            )

        # Get non-recurring events within the date range
        non_recurring_events = base_qs.filter(
            Q(start_time__range=(start_date, end_date)) | Q(end_time__range=(start_date, end_date)),
            recurrence_rule__isnull=True,  # Non-recurring only
            is_recurring_exception=False,  # Exclude exception objects
        )

        # Get recurring master events and generate their instances. Apply the
        # serializer optimization here so generated occurrences inherit prefetched
        # relations from their master (real events are optimized by the caller).
        recurring_events = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )
        if optimize_queryset is not None:
            recurring_events = optimize_queryset(recurring_events)

        events: list[CalendarEvent] = list(non_recurring_events)

        for master_event in recurring_events:
            instances = master_event.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True
            )
            # Occurrences are in-memory copies of the master (pk=None). Reuse the
            # master's prefetched relations so each occurrence serializes without
            # re-querying attendances/resources (occurrences inherit them by design).
            master_cache = getattr(master_event, "_prefetched_objects_cache", None)
            if master_cache:
                for instance in instances:
                    instance._prefetched_objects_cache = master_cache
            events.extend(instances)

        # Sort by start time
        events.sort(key=lambda x: x.start_time)

        # If this is a bundle calendar, filter out bundle representations to avoid duplicates
        if calendar.calendar_type == CalendarType.BUNDLE:
            # Remove duplicates (keep primary events, remove representations)
            seen_primary_events = set()
            unique_events = []

            for event in events:
                if event.is_bundle_representation:
                    # Skip representations - we want to show the primary event instead
                    continue
                elif event.is_bundle_primary:
                    # For bundle primary events, check if we've already seen this one
                    if event.id not in seen_primary_events:
                        seen_primary_events.add(event.id)
                        unique_events.append(event)
                else:
                    # For non-bundle events, include them normally
                    unique_events.append(event)

            events = unique_events
            events.sort(key=lambda x: x.start_time)

        return events

    @transaction.atomic()
    def delete_event(self, calendar_id: int, event_id: int, delete_series: bool = False) -> None:
        """
        Delete an event from the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_id: Unique identifier of the event to delete.
        :param delete_series: If True and the event is recurring, delete the entire series
        :return: None
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        event = CalendarEvent.objects.select_related("calendar").get(
            calendar_fk_id=calendar_id,
            id=event_id,
            organization_id=context.organization.id,
        )
        if isinstance(context.user_or_token, User):
            context.calendar_permission_service.initialize_with_user(
                context.user_or_token,
                organization_id=event.organization_id,
                event_id=event_id,
            )
        elif isinstance(context.user_or_token, SystemUser):
            raise PermissionDenied("Events cannot be created through the Public API.")

        serialized_old_event = self._serialize_event(event)
        if not context.calendar_permission_service.can_perform_update(
            old_event=serialized_old_event,
            new_event=None,
        ):
            raise PermissionDenied("You do not have permission to update this event.")

        if event.is_bundle_primary:
            self._host._delete_bundle_event(event)
            return

        if event.calendar.calendar_type in [
            CalendarType.PERSONAL,
            CalendarType.RESOURCE,
        ] and (write_adapter := self._host._get_write_adapter_for_calendar(event.calendar)):
            if event.is_recurring and delete_series:
                # Delete the entire recurring series from external calendar
                write_adapter.delete_event(event.calendar.external_id, event.external_id)
            elif event.is_recurring_instance and not delete_series:
                # Create a cancellation exception instead of deleting
                if event.parent_recurring_object:
                    event.parent_recurring_object.create_exception(
                        event.recurrence_id, is_cancelled=True
                    )
            else:
                # Delete single event or instance
                write_adapter.delete_event(event.calendar.external_id, event.external_id)

        if event.is_recurring and delete_series:
            # Delete the entire series including all instances and exceptions
            event.calendarevent_recurring_instances.all().delete()
            event.recurrence_exceptions.all().delete()
            if event.recurrence_rule:
                event.recurrence_rule.delete()
        elif event.is_recurring_instance and not delete_series:
            # For instances, we create an exception rather than delete
            if event.parent_recurring_object and event.recurrence_id:
                event.parent_recurring_object.create_exception(
                    event.recurrence_id, is_cancelled=True
                )

        serialized_event = self._serialize_event(event)

        event.delete()

        transaction.on_commit(
            lambda: (
                context.calendar_side_effects_service.on_delete_event(
                    actor=(
                        context.calendar_permission_service.token.user
                        if (
                            context.calendar_permission_service.token
                            and context.calendar_permission_service.token.user
                        )
                        else context.calendar_permission_service.token
                    ),
                    event=serialized_event,
                    organization=event.organization,
                )
                if context.calendar_side_effects_service
                else None
            )
        )

    def transfer_event(self, event: CalendarEvent, new_calendar: Calendar) -> CalendarEvent:
        """
        Transfer an event to a different calendar.
        :param event_id: Unique identifier of the event to transfer.
        :param new_calendar_external_id: External ID of the new calendar.
        :return: Transferred CalendarEvent instance.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_authenticated_calendar_service(context):
            raise

        event_data = context.calendar_adapter.get_event(
            event.calendar.external_id, event.external_id
        )

        # Create a new event in the target calendar
        new_event_data = CalendarEventInputData(
            title=event_data.title,
            description=event_data.description,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            timezone=event_data.timezone,
            recurrence_rule=event_data.recurrence_rule,
            attendances=[
                EventAttendanceInputData(
                    user_id=a.user_id,
                )
                for a in event.attendances.all()
            ],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        id=a.external_attendee.id,
                        email=a.external_attendee.email,
                        name=a.external_attendee.name,
                    )
                )
                for a in event.external_attendances.all()
            ],
            resource_allocations=[
                ResourceAllocationInputData(
                    resource_id=r.calendar_fk_id,
                )
                for r in event.resource_allocations.all()
                if r.calendar_fk_id
            ],
        )
        # Route create/delete through the host so the facade's public methods (the
        # original ``self.create_event`` / ``self.delete_event`` call targets) run —
        # preserving the facade-level call graph the existing transfer tests assert on.
        new_event = self._host.create_event(new_calendar.id, new_event_data)

        # Delete the old event
        self._host.delete_event(event.calendar.id, event.id)

        return new_event

    def create_recurring_event_bulk_modification(
        self,
        parent_event: CalendarEvent,
        modification_start_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        is_bulk_cancelled: bool = False,
        modification_rrule_string: str | None = None,
    ) -> CalendarEvent | None:
        """Create a bulk modification for a recurring event from the specified date onwards."""

        def truncate_parent(
            parent_obj: RecurringMixin,
            new_recurrence_rule: RecurrenceRule | None,
        ):
            parent = cast(CalendarEvent, parent_obj)
            return self.update_event(
                calendar_id=parent.calendar_fk_id,  # type: ignore
                event_id=parent.id,
                event_data=CalendarEventInputData(
                    title=parent.title,
                    description=parent.description,
                    start_time=parent.start_time,
                    end_time=parent.end_time,
                    timezone=parent.timezone,
                    resource_allocations=[
                        ResourceAllocationInputData(resource_id=ra.calendar_fk_id)  # type: ignore
                        for ra in parent.resource_allocations.all()
                    ],
                    attendances=[
                        EventAttendanceInputData(user_id=att.user_id)
                        for att in parent.attendances.all()
                    ],
                    external_attendances=[
                        EventExternalAttendanceInputData(
                            external_attendee=ExternalAttendeeInputData(
                                id=ext.external_attendee.id,
                                email=ext.external_attendee.email,
                                name=ext.external_attendee.name,
                            )
                        )
                        for ext in parent.external_attendances.all()
                    ],
                    # Recurrence fields
                    recurrence_rule=(
                        new_recurrence_rule.to_rrule_string() if new_recurrence_rule else None
                    ),
                    parent_event_id=(
                        parent.parent_recurring_object.id
                        if parent.parent_recurring_object
                        else None
                    ),
                    is_recurring_exception=parent.is_recurring_exception,
                ),
            )

        def create_continuation(
            parent_obj: RecurringMixin,
            start_dt: datetime.datetime,
            recurrence_rule: RecurrenceRule | None,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent = cast(CalendarEvent, parent_obj)
            # Compute new start/end based on offsets or mirror parent's times at start_dt
            new_start = (
                (start_dt + modification_data["start_time_offset"])
                if modification_data.get("start_time_offset")
                else start_dt
            )
            duration = parent.duration
            new_end = (
                new_start + modification_data["end_time_offset"]
                if modification_data.get("end_time_offset")
                else new_start + duration
            )

            return self.create_event(
                calendar_id=parent.calendar.id,
                event_data=CalendarEventInputData(
                    title=modification_data.get("title") or parent.title,
                    description=modification_data.get("description") or parent.description,
                    start_time=new_start,
                    end_time=new_end,
                    timezone=parent.timezone,
                    recurrence_rule=recurrence_rule.to_rrule_string() if recurrence_rule else None,
                    attendances=[
                        EventAttendanceInputData(user_id=a.user_id)
                        for a in parent.attendances.all()
                    ],
                    external_attendances=[
                        EventExternalAttendanceInputData(
                            external_attendee=ExternalAttendeeInputData(
                                email=ea.external_attendee.email,
                                name=ea.external_attendee.name,
                                id=ea.external_attendee.id,
                            )
                        )
                        for ea in parent.external_attendances.all()
                    ],
                    resource_allocations=[
                        ResourceAllocationInputData(resource_id=r.calendar_fk_id)  # type: ignore
                        for r in parent.resource_allocations.all()
                    ],
                ),
            )

        def record_bulk(
            parent_obj: RecurringMixin,
            start_dt: datetime.datetime,
            continuation_obj: RecurringMixin | None,
            cancelled: bool,
        ):
            EventBulkModification.objects.create(
                organization=parent_obj.organization,
                parent_event=parent_obj,
                modification_start_date=start_dt,
                modified_continuation=None,
                is_bulk_cancelled=cancelled,
            )

        modification_data = {
            "title": modified_title,
            "description": modified_description,
            "start_time_offset": modified_start_time_offset,
            "end_time_offset": modified_end_time_offset,
        }

        result = self._recurrence_manager.create_recurring_bulk_modification_generic(
            self._context,
            object_type_name="event",
            parent_object=parent_event,
            modification_start_date=modification_start_date,
            is_bulk_cancelled=is_bulk_cancelled,
            modification_data=modification_data,
            truncate_parent_callback=truncate_parent,
            create_continuation_callback=create_continuation,
            bulk_modification_record_callback=record_bulk,
            modification_rrule_string=modification_rrule_string,
        )
        return cast(CalendarEvent, result) if result else None

    def modify_recurring_event_from_date(
        self,
        parent_event: CalendarEvent,
        modification_start_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        modification_rrule_string: str | None = None,
    ) -> CalendarEvent | None:
        """Modify recurring event series from the given date onwards."""
        continuation = self.create_recurring_event_bulk_modification(
            parent_event=parent_event,
            modification_start_date=modification_start_date,
            modified_title=modified_title,
            modified_description=modified_description,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            is_bulk_cancelled=False,
            modification_rrule_string=modification_rrule_string,
        )

        return continuation

    def cancel_recurring_event_from_date(
        self,
        parent_event: CalendarEvent,
        modification_start_date: datetime.datetime,
        modification_rrule_string: str | None = None,
    ) -> None:
        """Cancel all occurrences from modification_start_date onwards."""
        self.create_recurring_event_bulk_modification(
            parent_event=parent_event,
            modification_start_date=modification_start_date,
            is_bulk_cancelled=True,
            modification_rrule_string=modification_rrule_string,
        )
