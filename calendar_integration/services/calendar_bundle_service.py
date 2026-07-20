"""Bundle calendar CRUD and bundle-event fan-out logic.

``CalendarBundleService`` owns the bundle concern extracted from the
``CalendarService`` facade. It is a plain class (not a DI-container provider):
the facade constructs it, after authentication, feeding it the shared
:class:`CalendarServiceContext` so it never re-authenticates or re-builds a
calendar adapter (the perf guardrail). Everything it needs arrives via the
constructor:

- ``context`` — the immutable auth snapshot (organization, user_or_token,
  account, calendar_adapter, permission_service, side_effects_service). Read
  through ``self._context``; the auth guards in ``type_guards.py`` inspect the
  same ``organization`` / ``account`` / ``calendar_adapter`` attributes the
  context exposes so behavior is byte-for-byte identical to the former methods.
- ``host`` — the :class:`BundleServiceHost` (in Phase 3 the facade itself).
  The bundle concern routes three things back through it:

  - **availability** (``get_availability_windows_in_range``) — not extracted
    until Phase 4, stays on the facade.
  - **event CRUD** (``create_event`` / ``update_event`` / ``delete_event``) —
    extracted in Phase 2 but still reached via the facade's public surface so
    the call graph the existing test suite asserts on is preserved.
  - **timezone conversion** (``convert_naive_utc_datetime_to_timezone``) —
    extracted as a util in Phase 0 but delegated through the host for
    byte-for-byte consistency with the original call pattern in blocked-time
    updates.

Routing through the host keeps single implementations and behaviour
byte-for-byte; later phases swap concerns in without touching this service.

The facade's ``_create_bundle_event`` / ``_update_bundle_event`` /
``_delete_bundle_event`` methods become one-line delegations to this service.
``CalendarEventService`` already calls those methods through the
``EventServiceHost`` protocol — that call path is unchanged.

Recursion guard
---------------
``CalendarEventService.create_event`` on a BUNDLE calendar calls
``host._create_bundle_event`` → ``CalendarBundleService.create_bundle_event``
→ calls ``host.create_event`` per child calendar. Children are never BUNDLE
calendars (enforced by ``create_bundle_calendar`` / ``update_bundle_calendar``),
so the child create_event calls take the normal (non-bundle) path. No loop.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Protocol, cast

from django.db import transaction

from audit.constants import AuditAction, AuditActorType
from audit.diff import compute_diff
from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.calendar_service_utils import (
    convert_naive_utc_datetime_to_timezone as _convert_naive_utc_datetime_to_timezone,
)
from calendar_integration.services.calendar_service_utils import (
    resolve_acting_single_use_token,
)
from calendar_integration.services.dataclasses import (
    CalendarEventInputData,
    EventAttendanceInputData,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.type_guards import (
    is_initialized_or_authenticated_calendar_service,
)
from organizations.models import OrganizationMembership
from payments.billing_constants import LimitedResource
from payments.exceptions import OverLimitError
from users.models import User


if TYPE_CHECKING:
    from collections.abc import Iterable

    from calendar_integration.services.calendar_service_context import CalendarServiceContext
    from calendar_integration.services.dataclasses import AvailableTimeWindow
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter


class BundleServiceHost(Protocol):
    """The collaborator surface the bundle concern routes back to the facade for.

    Three concerns are not extracted in Phase 3 and stay on the facade:

    - **availability** (``get_availability_windows_in_range``) — extracted in Phase 4;
    - **event CRUD** (``create_event`` / ``update_event`` / ``delete_event``) —
      the public facade methods; reaching them through the host keeps the call
      graph the existing test suite patches via the facade;
    - **timezone conversion** (``convert_naive_utc_datetime_to_timezone``) —
      a util but exposed on the facade so blocked-time update paths keep the
      exact delegation chain the original used.

    In Phase 3 the facade supplies *itself*. Later phases may swap individual
    concerns without changing this service's call sites.
    """

    def get_availability_windows_in_range(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> Iterable[AvailableTimeWindow]: ...

    def create_event(
        self,
        calendar_id: int,
        event_data: CalendarEventInputData,
        *,
        bypass_limits: bool = False,
        _enforce_policy: bool = True,
        _check_postpaid_allowance: bool = True,
    ) -> CalendarEvent: ...

    def update_event(
        self, calendar_id: int, event_id: int, event_data: CalendarEventInputData
    ) -> CalendarEvent: ...

    def delete_event(
        self, calendar_id: int, event_id: int, delete_series: bool = False
    ) -> None: ...

    def convert_naive_utc_datetime_to_timezone(
        self, datetime_obj: datetime.datetime, iana_tz: str
    ) -> datetime.datetime: ...

    def _get_write_adapter_for_calendar(self, calendar: Calendar) -> CalendarAdapter | None: ...

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None: ...


class CalendarBundleService:
    """Owns bundle calendar CRUD and bundle-event fan-out."""

    def __init__(
        self,
        context: CalendarServiceContext,
        host: BundleServiceHost,
    ) -> None:
        self._context = context
        # Phase 3 seam: availability (Phase 4), event CRUD, and the shared
        # write-adapter / permission helpers are reached through the host (the facade).
        # See ``BundleServiceHost``.
        self._host = host

    def _audit_bundle_write(
        self,
        action: str,
        subject_instance: Calendar | CalendarEvent,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a bundle-level business write.

        Resolves the actor from the context's ``user_or_token`` auth state. A no-op
        when no ``audit_service`` or ``organization`` is bound (e.g. a context built
        directly in a test without DI), so instrumentation never breaks a write path.
        """
        audit_service = self._context.audit_service
        organization = self._context.organization
        if audit_service is None or organization is None:
            return

        label = (
            subject_instance.name
            if isinstance(subject_instance, Calendar)
            else subject_instance.title
        )
        actor = audit_service.actor_from_user_or_token(
            self._context.user_or_token,
            organization.id,
            single_use_token=resolve_acting_single_use_token(
                self._context.user_or_token, self._context.calendar_permission_service
            ),
        )
        # For bundle EVENTS, carry the affected memberships (internal attendees plus
        # the acting member). Bundle CALENDARS have no attendees, so the set is empty.
        affected: set[int] = set()
        if isinstance(subject_instance, CalendarEvent):
            affected = set(
                subject_instance.attendances.filter(membership_user_id__isnull=False).values_list(
                    "membership_user_id", flat=True
                )
            )
            if actor.actor_type == AuditActorType.MEMBERSHIP and actor.actor_id is not None:
                affected.add(actor.actor_id)
        audit_service.record(
            organization_id=organization.id,
            action=action,
            actor=actor,
            subject=audit_service.subject_from_instance(subject_instance, label=label),
            affected_membership_ids=sorted(affected),
            diff=diff,
        )

    # ------------------------------------------------------------------
    # Bundle calendar CRUD
    # ------------------------------------------------------------------

    @transaction.atomic()
    def create_bundle_calendar(
        self,
        name: str,
        description: str | None = None,
        child_calendars: Iterable[Calendar] | None = None,
        primary_calendar: Calendar | None = None,
        accepts_public_scheduling: bool = False,
        bypass_limits: bool = False,
    ) -> Calendar:
        """
        Create a new bundle calendar in the application without linking to an external provider.

        :param name: Name of the calendar.
        :param description: Description of the calendar.
        :param child_calendars: Iterable of child Calendar instances to include in the bundle.
        :param primary_calendar: The child calendar to be designated as primary. Must be in
            child_calendars.
        :param accepts_public_scheduling: If True, the bundle can be booked via codeless public
            scheduling links. Defaults to False (private).
        :param bypass_limits: When True, skips the ``bundle_calendars`` limit guard below.
            Only management commands and one-off repair scripts should pass this -- never a
            request-handling path.
        :raises OverLimitError: When the organization is at its effective ``bundle_calendars``
            ceiling. Nothing is created. Checked and locked (``SELECT ... FOR UPDATE`` on the
            billing root's subscription) inside this method's own transaction.
        :return: Created Calendar instance.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        child_calendars_list = list(child_calendars or [])

        # Validate primary calendar
        if primary_calendar and primary_calendar not in child_calendars_list:
            raise ValueError("Primary calendar must be one of the child calendars")

        entitlement_service = self._context.entitlement_service
        if not bypass_limits and entitlement_service is not None:
            result = entitlement_service.check_limit(
                context.organization, LimitedResource.BUNDLE_CALENDARS, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        bundle_calendar = Calendar.objects.create(
            organization=context.organization,
            name=name,
            description=description or "",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.BUNDLE,
            accepts_public_scheduling=accepts_public_scheduling,
        )

        for calendar in child_calendars_list:
            if calendar.organization_id != context.organization.id:
                raise ValueError(
                    "All child calendars must belong to the same organization as the bundle."
                )

            if calendar.calendar_type == CalendarType.BUNDLE:
                raise ValueError(
                    "Child calendars of a bundle must not be bundle calendars themselves."
                )

            is_primary = primary_calendar is not None and calendar.id == primary_calendar.id
            ChildrenCalendarRelationship.objects.create(
                bundle_calendar=bundle_calendar,
                child_calendar=calendar,
                organization=context.organization,
                is_primary=is_primary,
            )

        # Create calendar ownership for the user who created it. Guard the
        # membership-scoped FK (same as the sync path): only set
        # membership_user_id when the creator is a member of this org, else create
        # an orphan ownership (membership_user_id NULL) so a non-member
        # user_or_token never triggers an FK IntegrityError aborting the request.
        if isinstance(context.user_or_token, User):
            owner_membership_user_id = (
                context.user_or_token.id
                if OrganizationMembership.objects.filter(
                    user_id=context.user_or_token.id,
                    organization_id=context.organization.id,
                ).exists()
                else None
            )
            CalendarOwnership.objects.create(
                organization=context.organization,
                calendar=bundle_calendar,
                membership_user_id=owner_membership_user_id,
                is_default=False,
            )

        # Grant permissions to calendar owners
        self._host._grant_calendar_owner_permissions(bundle_calendar)

        self._audit_bundle_write(AuditAction.CREATE, bundle_calendar)

        return bundle_calendar

    @transaction.atomic()
    def update_bundle_calendar(
        self,
        bundle_calendar: Calendar,
        child_calendars: Iterable[Calendar],
        primary_calendar: Calendar | None = None,
    ) -> Calendar:
        """
        Reconcile the children and primary designation for an existing bundle calendar.

        Adds ``ChildrenCalendarRelationship`` rows for newly-added children, removes rows
        for dropped children, and updates ``is_primary`` so that exactly one row is primary
        when ``primary_calendar`` is provided.

        :param bundle_calendar: The bundle Calendar instance to update.
        :param child_calendars: Full desired set of child Calendar instances.
        :param primary_calendar: The child to designate as primary; must be in child_calendars.
        :return: The (unchanged) bundle_calendar instance after reconciliation.
        :raises ValueError: If bundle_calendar is not a BUNDLE type, children are cross-org,
                            or primary_calendar is not in child_calendars.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        if bundle_calendar.calendar_type != CalendarType.BUNDLE:
            raise ValueError("Calendar is not a bundle.")

        child_calendars_list = list(child_calendars)

        if primary_calendar and primary_calendar not in child_calendars_list:
            raise ValueError("Primary calendar must be one of the child calendars.")

        for calendar in child_calendars_list:
            if calendar.organization_id != context.organization.id:
                raise ValueError(
                    "All child calendars must belong to the same organization as the bundle."
                )
            if calendar.calendar_type == CalendarType.BUNDLE:
                raise ValueError(
                    "Child calendars of a bundle must not be bundle calendars themselves."
                )

        desired_ids = {cal.id for cal in child_calendars_list}

        existing_relationships = list(
            ChildrenCalendarRelationship.objects.filter(
                bundle_calendar=bundle_calendar,
                organization=context.organization,
            )
        )
        existing_ids = {rel.child_calendar_fk_id for rel in existing_relationships}

        # Remove dropped children
        for rel in existing_relationships:
            if rel.child_calendar_fk_id not in desired_ids:
                rel.delete()

        # Add new children
        for calendar in child_calendars_list:
            if calendar.id not in existing_ids:
                is_primary = primary_calendar is not None and calendar.id == primary_calendar.id
                ChildrenCalendarRelationship.objects.create(
                    bundle_calendar=bundle_calendar,
                    child_calendar=calendar,
                    organization=context.organization,
                    is_primary=is_primary,
                )

        # Reconcile is_primary on remaining + newly-added relationships
        if primary_calendar is not None:
            ChildrenCalendarRelationship.objects.filter(
                bundle_calendar=bundle_calendar,
                organization=context.organization,
            ).exclude(
                child_calendar_fk_id=primary_calendar.id,
            ).update(is_primary=False)

            ChildrenCalendarRelationship.objects.filter(
                bundle_calendar=bundle_calendar,
                organization=context.organization,
                child_calendar_fk_id=primary_calendar.id,
            ).update(is_primary=True)

        # This method only reconciles child relationships; it never mutates scalar
        # fields on the bundle Calendar, so the action + subject capture the change
        # with no field-level diff.
        self._audit_bundle_write(AuditAction.UPDATE, bundle_calendar, diff=None)

        return bundle_calendar

    # ------------------------------------------------------------------
    # Bundle-event fan-out (called from EventServiceHost seam)
    # ------------------------------------------------------------------

    def create_bundle_event(
        self,
        bundle_calendar: Calendar,
        event_data: CalendarEventInputData,
        *,
        bypass_limits: bool = False,
    ) -> CalendarEvent:
        """
        Create an event in a bundle calendar by:
        1. Selecting the designated primary child calendar
        2. Creating the main event in the primary calendar
        3. Creating BlockedTime entries in other PROVIDER calendars
        4. Creating CalendarEvent entries in INTERNAL calendars
        5. Adding users from non-primary calendars as attendees

        :param bundle_calendar: The bundle Calendar instance.
        :param event_data: Event creation data.
        :return: The created primary CalendarEvent.
        :raises ValueError: If the calendar is not a bundle, has no children, or
            any child has no availability in the requested time window.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        if bundle_calendar.calendar_type != CalendarType.BUNDLE:
            raise ValueError("Calendar must be a bundle calendar")

        child_calendars = list(bundle_calendar.bundle_children.all())
        if not child_calendars:
            raise ValueError("Bundle calendar has no child calendars")

        # Check availability across all child calendars
        for child_calendar in child_calendars:
            available_windows = self._host.get_availability_windows_in_range(
                child_calendar, event_data.start_time, event_data.end_time
            )
            if not available_windows:
                raise ValueError(f"No availability in child calendar {child_calendar.name}")

        # Get the designated primary calendar
        primary_calendar = self._get_primary_calendar(bundle_calendar)

        # Postpaid ``event_occurrences`` allowance guard -- checked ONCE, for the whole
        # fan-out, before anything is written. Each per-child ``create_event`` call
        # below passes ``_check_postpaid_allowance=False`` so it is not re-checked
        # (see ``CalendarEventService.create_event``'s docstring for why a per-child
        # recheck would be redundant rather than merely wasted work: nothing is
        # metered until a Celery sweep runs, so a re-check would ask the same
        # question against an unchanged usage count).
        #
        # Skipped when this call, or the whole service
        # (``CalendarService.authenticate(bypass_limits=True)``), is in bypass mode --
        # the same two conditions ``CalendarEventService._postpaid_entitlement_service``
        # resolves for the single-event path.
        entitlement_service = (
            None
            if bypass_limits or self._context.bypass_entitlement_limits
            else self._context.entitlement_service
        )
        if entitlement_service is not None:
            billable_units = self._bundle_event_billable_units(primary_calendar.id, child_calendars)
            result = entitlement_service.check_postpaid_allowance(
                context.organization, delta=billable_units, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        # Collect all attendees from child calendar ownerships
        all_attendees = self._collect_bundle_attendees(child_calendars, event_data)

        # Create the primary event
        primary_event_data = CalendarEventInputData(
            title=event_data.title,
            description=event_data.description,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            timezone=event_data.timezone,
            attendances=all_attendees,
            external_attendances=event_data.external_attendances,
            resource_allocations=event_data.resource_allocations,
            recurrence_rule=event_data.recurrence_rule,
        )

        # Policy was already enforced once at the top-level CalendarService.create_event
        # entry (using resolve_for_bundle). Skip re-enforcement for all child creates to
        # avoid: (a) N redundant policy resolutions and buffer fetches, and (b) false
        # rejections when a child has its own stricter individual policy that would block
        # a booking the bundle policy (and Phase-5 discovery) correctly permits.
        primary_event = self._host.create_event(
            primary_calendar.id,
            primary_event_data,
            _enforce_policy=False,
            _check_postpaid_allowance=False,
        )

        # Mark primary event as part of bundle
        primary_event.bundle_calendar = bundle_calendar
        primary_event.is_bundle_primary = True
        primary_event.save()

        # Create representations in other calendars
        for child_calendar in child_calendars:
            if child_calendar.id == primary_calendar.id:
                continue

            if self._child_gets_full_event(child_calendar):
                # Create full CalendarEvent for internal calendars
                child_event_data = CalendarEventInputData(
                    title=f"[Bundle] {event_data.title}",
                    description=f"Bundle event from {bundle_calendar.name}\n\n{event_data.description}",
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    timezone=event_data.timezone,
                    attendances=[],  # No direct attendances for linked events
                    external_attendances=[],
                    resource_allocations=[],
                )

                child_event = self._host.create_event(
                    child_calendar.id,
                    child_event_data,
                    _enforce_policy=False,
                    _check_postpaid_allowance=False,
                )

                # Link to primary event and bundle
                child_event.bundle_calendar = bundle_calendar
                child_event.bundle_primary_event = primary_event
                child_event.save()

            else:
                # Create BlockedTime for other PROVIDER calendars
                BlockedTime.objects.create(
                    calendar=child_calendar,
                    start_time_tz_unaware=_convert_naive_utc_datetime_to_timezone(
                        event_data.start_time, event_data.timezone
                    ),
                    end_time_tz_unaware=_convert_naive_utc_datetime_to_timezone(
                        event_data.end_time, event_data.timezone
                    ),
                    reason=f"Bundle event: {event_data.title}",
                    organization=child_calendar.organization,
                    bundle_calendar=bundle_calendar,
                    bundle_primary_event=primary_event,
                )

        self._audit_bundle_write(AuditAction.CREATE, primary_event)

        return primary_event

    def update_bundle_event(
        self, bundle_event: CalendarEvent, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        """Update a bundle event and all its representations.

        :param bundle_event: The primary bundle CalendarEvent to update.
        :param event_data: Updated event data.
        :return: The updated primary CalendarEvent.
        :raises ValueError: If the event is not a bundle primary event or
            organization is missing from context.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        if not bundle_event.is_bundle_primary:
            raise ValueError("Event must be a bundle primary event")

        bundle_calendar = bundle_event.bundle_calendar

        # Capture the primary event's own scalar fields BEFORE the update so we can
        # emit a cheap, in-memory diff. Only the plain-text scalars (title,
        # description, timezone) are diffed here: ``start_time`` / ``end_time`` are
        # db-persisted GeneratedFields whose stored representation (IANA-local, via
        # ``convert_naive_utc_to_timezone``) is NOT directly comparable to the
        # tz-aware UTC instants on ``event_data``, so comparing them would emit
        # misleading "changes". The time change is still captured by the UPDATE
        # action + subject; only the field-level time diff is intentionally omitted.
        before_scalars = {
            "title": bundle_event.title,
            "description": bundle_event.description,
            "timezone": bundle_event.timezone,
        }
        after_scalars = {
            "title": event_data.title,
            "description": event_data.description,
            "timezone": event_data.timezone,
        }

        # Update the primary event
        updated_primary = self._host.update_event(
            bundle_event.calendar.id, bundle_event.id, event_data
        )

        # Update all representation events
        representation_events = CalendarEvent.objects.filter(
            organization_id=context.organization.id, bundle_primary_event=bundle_event
        )

        for representation_event in representation_events:
            representation_data = CalendarEventInputData(
                title=f"[Bundle] {event_data.title}",
                description=f"Bundle event from {bundle_calendar.name}\n\n{event_data.description}",
                start_time=event_data.start_time,
                end_time=event_data.end_time,
                timezone=event_data.timezone,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            self._host.update_event(
                representation_event.calendar.id,
                representation_event.id,
                representation_data,
            )

        # Update all blocked time representations
        blocked_time_representations = BlockedTime.objects.filter(
            organization_id=context.organization.id, bundle_primary_event=bundle_event
        )

        for blocked_time in blocked_time_representations:
            blocked_time.start_time_tz_unaware = _convert_naive_utc_datetime_to_timezone(
                event_data.start_time, event_data.timezone
            )
            blocked_time.end_time_tz_unaware = _convert_naive_utc_datetime_to_timezone(
                event_data.end_time, event_data.timezone
            )
            blocked_time.reason = f"Bundle event: {event_data.title}"
            blocked_time.save(
                update_fields=["start_time_tz_unaware", "end_time_tz_unaware", "reason"]
            )

        self._audit_bundle_write(
            AuditAction.UPDATE,
            updated_primary,
            diff=compute_diff(before_scalars, after_scalars),
        )

        return updated_primary

    def delete_bundle_event(self, bundle_event: CalendarEvent) -> None:
        """Delete a bundle event and all its representations.

        :param bundle_event: The primary bundle CalendarEvent to delete.
        :raises ValueError: If the event is not a bundle primary event.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        if not bundle_event.is_bundle_primary:
            raise ValueError("Event must be a bundle primary event")

        # Emit the audit record BEFORE deleting the primary row, while the instance's
        # pk and title are still authoritative (subject_from_instance reads the pk).
        # record() itself only fires on transaction commit, so ordering relative to
        # the deletes below is irrelevant to persistence — but building the subject
        # here keeps the soft-reference correct regardless of in-memory pk lifecycle.
        self._audit_bundle_write(AuditAction.DELETE, bundle_event)

        # Delete all representation events
        representation_events = CalendarEvent.objects.filter(
            organization_id=context.organization.id, bundle_primary_event=bundle_event
        )

        for representation_event in representation_events:
            self._host.delete_event(representation_event.calendar.id, representation_event.id)

        # Delete all blocked time representations
        BlockedTime.objects.filter(
            organization_id=context.organization.id, bundle_primary_event=bundle_event
        ).delete()

        # Delete the primary event
        self._host.delete_event(bundle_event.calendar.id, bundle_event.id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _child_gets_full_event(child_calendar: Calendar) -> bool:
        """Whether a non-primary bundle child gets a real ``CalendarEvent`` (rather
        than a non-billable ``BlockedTime``) when a bundle event is created.

        The single definition of that predicate. ``create_bundle_event``'s write
        loop calls this to decide what to create, and
        ``_bundle_event_billable_units`` calls the identical function to decide
        what to charge for -- so "which children become billable rows" cannot be
        answered two different ways by two pieces of code that drift apart over
        time. The primary calendar is handled separately by its caller (it always
        gets a real event, unconditionally); this only decides for the rest.
        """
        return child_calendar.provider == CalendarProvider.INTERNAL

    @classmethod
    def _bundle_event_billable_units(
        cls, primary_calendar_id: int, child_calendars: list[Calendar]
    ) -> int:
        """How many ``event_occurrences`` units one bundle-event booking costs.

        Binding decision (Phase 7 tracking doc, "what a bundle booking costs"):
        **1 + n_internal_children**. The primary calendar always gets a real
        ``CalendarEvent`` (the actual booking); every other child gets one too only
        when ``_child_gets_full_event`` says so -- the exact predicate
        ``create_bundle_event``'s write loop uses to decide "full CalendarEvent vs.
        BlockedTime" for that same calendar. A ``BlockedTime`` is never billable
        anywhere in this plan, so a bundle over five Google calendars costs 1, not
        5, and this must never be computed a second, independent way.

        Not derived by calling ``MeteringService.expand_occurrence_identities``:
        nothing exists to expand yet -- the events this counts are about to be
        created, not already in the database. This exists so the guard counts
        exactly the rows the write loop below is about to write, using the same
        predicate the write loop uses, rather than a second guess at what the
        meter will later see.
        """
        return 1 + sum(
            1
            for calendar in child_calendars
            if calendar.id != primary_calendar_id and cls._child_gets_full_event(calendar)
        )

    def _get_primary_calendar(self, bundle_calendar: Calendar) -> Calendar:
        """Get the designated primary calendar for a bundle."""
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        primary_relationship = ChildrenCalendarRelationship.objects.filter(
            bundle_calendar=bundle_calendar,
            is_primary=True,
            organization=context.organization,
        ).first()

        if not primary_relationship:
            raise ValueError("Bundle calendar has no designated primary child calendar")

        return primary_relationship.child_calendar

    def _collect_bundle_attendees(
        self, child_calendars: list[Calendar], event_data: CalendarEventInputData
    ) -> list[EventAttendanceInputData]:
        """Collect attendees from calendar ownerships and explicit attendances."""
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        attendee_user_ids = {attendance.user_id for attendance in event_data.attendances}

        # Add memberships that own child calendars (membership-backed owners only;
        # orphan ownerships with a null membership are intentionally excluded).
        owner_user_ids = (
            CalendarOwnership.objects.filter_by_organization(context.organization.id)
            .filter(
                calendar_fk_id__in=[calendar.id for calendar in child_calendars],
                membership_user_id__isnull=False,
            )
            .values_list("membership_user_id", flat=True)
            .distinct()
        )

        attendee_user_ids.update(owner_user_ids)

        return [EventAttendanceInputData(user_id=user_id) for user_id in attendee_user_ids]
