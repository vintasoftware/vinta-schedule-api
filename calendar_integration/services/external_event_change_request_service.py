"""Service for managing ExternalEventChangeRequest lifecycle.

``ExternalEventChangeRequestService`` is the single place that creates, supersedes,
approves, rejects, and auto-undoes change requests. It is DI-injected and consumes
``AuditService`` for audit trail emission.

The service handles several paths:

- *create / supersede for inbound updates*.
- *create / supersede for inbound deletions*.
- ``approve`` (apply locally) plus the ``can_resolve`` eligibility check.
- ``reject`` (outbound undo): re-converge the external provider to the retained
  (approved) state.
- ``auto_undo_inbound_change`` (FORBIDDEN auto-undo): called during sync to
  immediately undo inbound edits/deletions, record an AUTO_UNDONE row, and emit an
  audit entry — all without requiring any approver.

**Outbound-undo seam.** Re-converging the provider needs an
*authenticated* write adapter for the event's calendar. Rather than injecting a
``CalendarService`` into this service (``CalendarSyncService`` already depends on this
service, so injecting the facade back would risk an import cycle and couple the two),
the outbound-undo logic accepts the authenticated write capability **as a parameter**:
``_undo_on_provider(request, *, write_adapter)`` and ``reject(request, *, membership,
write_adapter)`` take a ``CalendarAdapter`` the caller has already authenticated. This
keeps the service free of provider-credential concerns and import-cycle-free.

- **API reject** authenticates a ``CalendarService`` for the event's calendar,
  resolves the write adapter via ``CalendarService._get_write_adapter_for_calendar`` and
  passes it to ``reject``.
- **FORBIDDEN auto-undo (during sync)** already holds an authenticated adapter
  (``context.calendar_adapter``) and passes it directly to
  ``auto_undo_inbound_change(..., write_adapter=context.calendar_adapter)``.

**Shared orchestration (_resolve_with_undo).** The safe provider-call-outside-transaction
+ atomic local commit + compensating delete pattern is factored into ``_resolve_with_undo``
so both ``reject`` and ``auto_undo_inbound_change`` share one code path. The caller
supplies the final status, resolver, audit action, and actor.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from django.db import transaction
from django.utils import timezone

from dependency_injector.wiring import Provide
from vintasend.constants import NotificationTypes
from vintasend.services.notification_service import NotificationContextDict

from audit.constants import AuditAction, AuditActorType
from audit.types import ActorSnapshot
from calendar_integration.constants import (
    CalendarProvider,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.exceptions import (
    ChangeRequestIneligibleError,
    ChangeRequestNotPendingError,
)
from calendar_integration.models import EventAttendance, ExternalEventChangeRequest
from calendar_integration.services.calendar_service_utils import (
    serialize_event_external_attendee,
    serialize_event_internal_attendee,
)
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterInputData,
    EventAttendeeData,
    ResourceData,
)
from organizations.models import OrganizationMembership, OrganizationRole


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationService

    from audit.services import AuditService
    from calendar_integration.models import CalendarEvent
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter


logger = logging.getLogger(__name__)


class ExternalEventChangeRequestService:
    """Service for creating and managing ExternalEventChangeRequest records.

    Handles the lifecycle of change requests created when inbound external
    provider changes are intercepted under the ``CHANGE_REQUEST`` or
    ``FORBIDDEN`` policy.

    Constructor arguments are supplied by the DI container, which passes them
    explicitly (see ``di_core/containers.py``) rather than through ``@inject``. There is
    deliberately **no** ``@inject`` here: this module carries
    ``from __future__ import annotations``, which stringifies the ``Annotated[...,
    Provide[...]]`` markers ``@inject`` introspects at wiring time, making the decorator
    a silent no-op (``dependency_injector`` emits ``DIWiringWarning`` and returns the
    function unpatched). Leaving it on would imply a wiring mechanism that is not
    running. See ``organizations.models.resolve_branding_for_display`` for the
    deferred-container-import pattern used where injection is actually needed under
    the same constraint.

    - ``audit_service``: emits audit trail entries for each state transition.
    - ``notification_service``: dispatches in-app notifications to eligible
      approvers when a new ``PENDING`` request is created.
    """

    def __init__(
        self,
        audit_service: Annotated[AuditService | None, Provide["audit_service"]] = None,
        notification_service: Annotated[
            NotificationService | None, Provide["notification_service"]
        ] = None,
    ) -> None:
        self.audit_service = audit_service
        self.notification_service = notification_service

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_adapter_input(self, event: CalendarEvent) -> CalendarEventAdapterInputData:
        """Build a full-fidelity ``CalendarEventAdapterInputData`` from a local event.

        The Google adapter's ``create_event``/``update_event`` are full-replace PUTs,
        so any field omitted here is *wiped* on the provider. This mirrors the
        canonical hydration in ``CalendarEventService.create_event`` /
        ``CalendarEventService.update_event`` (see those methods, ~L465 / ~L688): the
        full set of internal member attendees, external attendees, resource
        allocations, and the recurrence RRULE must be carried so the undo
        re-converges the provider to the complete retained state rather than
        clobbering attendees / recurrence.

        Reuse of ``CalendarEventService``'s inline builder would require injecting /
        importing that service here, which risks an import cycle (``CalendarSyncService``
        already depends on this service); instead the field hydration is replicated
        inline against the event's own relations, using the shared serialization utils
        in ``calendar_service_utils``.
        """
        calendar = event.calendar
        # The provider speaks in external ids, never the internal PKs.
        calendar_external_id = calendar.external_id if calendar is not None else ""

        attendees: list[EventAttendeeData] = []
        # Internal (member) attendances — resolve membership identity to email/name.
        for attendance in event.attendances.all():
            serialized = serialize_event_internal_attendee(attendance)
            if serialized is None:
                # Orphan attendance (no membership-backed identity) — skip, matching
                # the canonical serialization path.
                continue
            attendees.append(
                EventAttendeeData(
                    email=serialized.email,
                    name=serialized.name or serialized.email,
                    status=serialized.status,
                )
            )
        # External attendees.
        for external_attendance in event.external_attendances.all():
            ext_serialized = serialize_event_external_attendee(external_attendance)
            attendees.append(
                EventAttendeeData(
                    email=ext_serialized.email,
                    name=ext_serialized.name or ext_serialized.email,
                    status=ext_serialized.status,
                )
            )

        resources: list[ResourceData] = [
            ResourceData(
                email=resource_allocation.calendar.email or "",
                title=resource_allocation.calendar.name,
                external_id=resource_allocation.calendar.external_id,
                status="accepted",
            )
            for resource_allocation in event.resource_allocations.all()
            if resource_allocation.calendar is not None
        ]

        recurrence_rule = (
            event.recurrence_rule.to_rrule_string() if event.recurrence_rule is not None else None
        )

        return CalendarEventAdapterInputData(
            calendar_external_id=calendar_external_id,
            title=event.title,
            description=event.description,
            start_time=event.start_time,
            end_time=event.end_time,
            timezone=event.timezone,
            attendees=attendees,
            resources=resources,
            external_id=event.external_id,
            recurrence_rule=recurrence_rule,
        )

    def _supersede_pending(self, event: CalendarEvent) -> None:
        """Mark any existing PENDING request for *event* as STALE.

        Shared helper used by both ``create_or_supersede_update_request`` and
        ``create_or_supersede_delete_request``.  Must be called inside a
        ``transaction.atomic()`` block so the stale-mark and the new-PENDING
        creation are a single unit.
        """
        ExternalEventChangeRequest.objects.filter(
            organization_id=event.organization_id,
            event=event,
            status=ExternalEventChangeRequestStatus.PENDING,
        ).update(status=ExternalEventChangeRequestStatus.STALE)

    def _notify_eligible_approvers(
        self, request: ExternalEventChangeRequest, event: CalendarEvent | None
    ) -> None:
        """Dispatch in-app notifications to each eligible approver for *request*.

        Eligible approvers are (deduplication by user id):
        - Member-attendees of the event (``EventAttendance`` rows with a non-NULL
          ``membership_user_id`` for the same event).
        - Organization admins (``OrganizationMembership`` rows with
          ``role=ADMIN`` in the same organization).

        Each notification is wrapped in ``transaction.on_commit`` so it only
        fires after the ``PENDING`` request row commits to the database.  This
        method is a no-op when ``notification_service`` is not injected (i.e.
        when the service is built without DI in tests).

        The event title is captured at call time (before any possible deletion
        via an approve/reject later) so the notification body remains meaningful.

        Args:
            request: The newly created ``PENDING`` ``ExternalEventChangeRequest``.
            event: The associated ``CalendarEvent`` (passed to avoid an extra DB query).
                If ``None``, no approvers can be identified — the method returns silently.
        """
        if self.notification_service is None:
            return

        if event is None:
            # Guard: the event was deleted between request creation and this call.
            # No approvers can be identified — skip silently.
            return

        organization_id = request.organization_id
        event_title: str = event.title
        change_kind: str = request.kind
        change_request_id: int = request.pk

        # Collect eligible approver user ids, deduplicated.
        approver_user_ids: set[int] = set()

        # Member-attendees: EventAttendance rows for this event with a non-NULL membership
        # that is ACTIVE (matching the admin query which filters is_active=True).
        attendee_user_ids = (
            EventAttendance.objects.filter(
                organization_id=organization_id,
                event_fk_id=request.event_fk_id,
                membership_user_id__isnull=False,
                membership__is_active=True,
            )
            .values_list("membership_user_id", flat=True)
            .distinct()
        )
        approver_user_ids.update(attendee_user_ids)

        # Organization admins (all active admins in the organization).
        admin_user_ids = (
            OrganizationMembership.objects.filter(
                organization_id=organization_id,
                role=OrganizationRole.ADMIN,
                is_active=True,
            )
            .values_list("user_id", flat=True)
            .distinct()
        )
        approver_user_ids.update(admin_user_ids)

        # Dispatch one in-app notification per approver, wrapped in on_commit so
        # the notification only fires after the PENDING row is visible in the DB.
        # ``self.notification_service`` was already guarded non-None above (early return).
        # Bind to a local with a narrowed type annotation so mypy does not see
        # ``Optional[NotificationService]`` inside the nested _send closure.
        notification_service: NotificationService = self.notification_service  # type: ignore[assignment]

        # Use a factory function to capture the loop variable correctly — a
        # default-arg lambda would also work, but an explicit closure is clearer
        # and mypy can infer the return type (None) without ambiguity.
        def _make_callback(uid: int) -> Callable[[], None]:
            def _send() -> None:
                notification_service.create_notification(
                    user_id=uid,
                    notification_type=NotificationTypes.IN_APP.value,
                    title="Pending external calendar change requires your approval",
                    body_template=(
                        "calendar_integration/in_app/"
                        "external_event_change_request_approver.body.txt"
                    ),
                    context_name="external_event_change_request_approver_context",
                    context_kwargs=NotificationContextDict(
                        {
                            "change_request_id": change_request_id,
                            "event_title": event_title,
                            "change_kind": change_kind,
                            "organization_id": organization_id,
                        }
                    ),
                )

            return _send

        for user_id in approver_user_ids:
            transaction.on_commit(_make_callback(user_id))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_or_supersede_update_request(
        self,
        *,
        event: CalendarEvent,
        proposed_values: dict[str, Any],
        retained_values: dict[str, Any],
        payload: dict[str, Any],
        provider: CalendarProvider | str,
    ) -> ExternalEventChangeRequest:
        """Create a new PENDING update request, superseding any prior PENDING one.

        Marks any existing ``PENDING`` request for *event* as ``STALE``, then
        creates a new ``PENDING`` request with ``kind=UPDATE``.  Both writes are
        executed in a single ``transaction.atomic()`` block so there is never a
        window where no valid PENDING request exists and the partial unique
        constraint can only be violated by a concurrent racing call (the DB
        constraint is the final backstop).

        An audit entry is recorded via the injected ``audit_service`` using the
        SYSTEM actor (no human user initiated this — it is a consequence of an
        inbound provider event).

        Args:
            event: The local ``CalendarEvent`` the inbound change targets.
            proposed_values: Dict of incoming field values (title, description,
                start_time, end_time serialized as ISO strings).
            retained_values: Snapshot of the local event fields before the
                change, used to undo on rejection or in FORBIDDEN mode.
            payload: Raw provider payload (for debugging / replay).
            provider: Calendar provider string (``CalendarProvider.GOOGLE``).

        Returns:
            The newly created ``PENDING`` ``ExternalEventChangeRequest``.
        """
        with transaction.atomic():
            # Mark any existing PENDING request for this event as STALE.
            self._supersede_pending(event)

            # Create the new PENDING request.
            change_request = ExternalEventChangeRequest.objects.create(
                organization=event.organization,
                event=event,
                kind=ExternalEventChangeKind.UPDATE,
                status=ExternalEventChangeRequestStatus.PENDING,
                provider=provider,
                proposed_values=proposed_values,
                proposed_payload=payload,
                retained_values=retained_values,
            )

        # Record audit entry after the atomic block so the commit-based
        # on_commit delivery in AuditService fires after the row is visible.
        if self.audit_service is not None:
            actor = self.audit_service.system_actor()
            subject = self.audit_service.subject_from_instance(change_request)
            diff: dict[str, Any] = {
                field: {"old": retained_values.get(field), "new": proposed_values.get(field)}
                for field in set(proposed_values) | set(retained_values)
                if retained_values.get(field) != proposed_values.get(field)
            }
            self.audit_service.record(
                organization_id=event.organization_id,
                action=AuditAction.EXTERNAL_CHANGE_REQUESTED,
                actor=actor,
                subject=subject,
                diff=diff or None,
            )

        # Notify each eligible approver in-app via on_commit (fires after the PENDING row commits).
        # Pass the event to avoid an extra DB query via request.event.
        self._notify_eligible_approvers(change_request, event)

        return change_request

    def create_or_supersede_delete_request(
        self,
        *,
        event: CalendarEvent,
        retained_values: dict[str, Any],
        payload: dict[str, Any],
        provider: CalendarProvider | str,
    ) -> ExternalEventChangeRequest:
        """Create a new PENDING delete request, superseding any prior PENDING one.

        Marks any existing ``PENDING`` request for *event* as ``STALE``, then
        creates a new ``PENDING`` request with ``kind=DELETE``.  Both writes are
        executed in a single ``transaction.atomic()`` block so there is never a
        window where the partial unique constraint is violated by two coexistent
        PENDING rows.

        For a deletion there are no proposed values to apply — instead,
        ``retained_values`` captures the local event's current state (title,
        description, start_time, end_time) so ``reject`` can re-create the event
        on GCal when a delete request is rejected.  ``payload`` is the raw
        inbound provider payload stored for debugging / replay.

        An audit entry is recorded via the injected ``audit_service`` using the
        SYSTEM actor.  The diff captures the deletion intent: retained_values as
        "from" (old) and ``None`` as "to" (new), reflecting that the fields will
        disappear if the deletion is approved.

        Args:
            event: The local ``CalendarEvent`` the inbound deletion targets.
            retained_values: Snapshot of the local event fields (title,
                description, start_time, end_time as ISO strings) used to
                re-create on rejection.
            payload: Raw provider payload (for debugging / replay).
            provider: Calendar provider string (``CalendarProvider.GOOGLE``).

        Returns:
            The newly created ``PENDING`` ``ExternalEventChangeRequest``.
        """
        with transaction.atomic():
            self._supersede_pending(event)

            # Create the new PENDING delete request.
            # proposed_values is empty for a deletion — there are no incoming
            # field values to apply; what matters is retained_values for undo.
            change_request = ExternalEventChangeRequest.objects.create(
                organization=event.organization,
                event=event,
                kind=ExternalEventChangeKind.DELETE,
                status=ExternalEventChangeRequestStatus.PENDING,
                provider=provider,
                proposed_values={},
                proposed_payload=payload,
                retained_values=retained_values,
            )

        # Record audit entry after the atomic block so the commit-based
        # on_commit delivery in AuditService fires after the outermost transaction commits.
        if self.audit_service is not None:
            actor = self.audit_service.system_actor()
            subject = self.audit_service.subject_from_instance(change_request)
            # Diff: all retained_values fields shown as "old" (what existed), None as "new"
            # (nothing remains if the deletion is approved).
            diff: dict[str, Any] = {
                field: {"old": retained_values.get(field), "new": None} for field in retained_values
            }
            self.audit_service.record(
                organization_id=event.organization_id,
                action=AuditAction.EXTERNAL_CHANGE_REQUESTED,
                actor=actor,
                subject=subject,
                diff=diff or None,
            )

        # Notify each eligible approver in-app via on_commit (fires after the PENDING row commits).
        # Pass the event to avoid an extra DB query via request.event.
        self._notify_eligible_approvers(change_request, event)

        return change_request

    def can_resolve(
        self,
        request: ExternalEventChangeRequest,
        membership: OrganizationMembership,
    ) -> bool:
        """Return True if *membership* is eligible to resolve *request*.

        Eligibility rules (both scoped to the same organization):

        - **Admin**: ``membership.is_admin`` grants resolution rights on any
          event's change request in the organization.
        - **Member-attendee**: the membership has an ``EventAttendance`` row for
          the same event as the request.

        This is the single eligibility check consumed by ``approve``, ``reject``,
        and the REST/GraphQL API layer.

        Args:
            request: The ``ExternalEventChangeRequest`` to check.
            membership: The ``OrganizationMembership`` attempting resolution.

        Returns:
            ``True`` when the membership may resolve this request.
        """
        # Organization scope: both the request and membership must belong to the same org.
        if request.organization_id != membership.organization_id:
            return False

        # Admins can resolve any request in their organization.
        if membership.is_admin:
            return True

        # Non-admins: must be a member-attendee of the event targeted by the request.
        # If the event has been deleted (event_fk_id is NULL), no attendee check
        # is possible and eligibility is False for non-admins.
        if request.event_fk_id is None:
            return False

        return EventAttendance.objects.filter(
            organization_id=request.organization_id,
            event_fk_id=request.event_fk_id,
            membership_user_id=membership.user_id,
            membership__is_active=True,
        ).exists()

    def approve(
        self,
        request: ExternalEventChangeRequest,
        *,
        membership: OrganizationMembership,
    ) -> ExternalEventChangeRequest:
        """Apply a PENDING change request and mark it APPROVED.

        Eligibility is checked first via ``can_resolve``.  Only ``PENDING``
        requests can be approved; any other status raises
        ``ChangeRequestNotPendingError``.

        For ``kind=UPDATE``: the proposed field values are written onto the
        local ``CalendarEvent`` (title, description) and the datetime fields
        (start_time_tz_unaware, end_time_tz_unaware) are updated by parsing
        the ISO-string values from ``proposed_values``.

        For ``kind=DELETE``: the local ``CalendarEvent`` is deleted. Django's
        SET_NULL cascade nullifies ``request.event_fk``, so the request row
        survives as an audit record with ``event=NULL``, ``status=APPROVED``.

        An ``AuditAction.EXTERNAL_CHANGE_APPROVED`` entry is recorded via the
        injected ``audit_service`` with the approving membership as the actor.

        Args:
            request: The ``ExternalEventChangeRequest`` to approve. Must be
                ``PENDING``; must belong to the same organization as
                ``membership``.
            membership: The ``OrganizationMembership`` approving the request.
                Must satisfy ``can_resolve``.

        Returns:
            The updated ``ExternalEventChangeRequest`` instance with
            ``status=APPROVED``, ``resolved_by``, and ``resolved_at`` set.

        Raises:
            ChangeRequestNotPendingError: If ``request.status`` is not ``PENDING``.
            ChangeRequestIneligibleError: If ``membership`` is not eligible to
                resolve this request.
        """
        if request.status != ExternalEventChangeRequestStatus.PENDING:
            raise ChangeRequestNotPendingError()

        if not self.can_resolve(request, membership):
            raise ChangeRequestIneligibleError()

        # Capture values needed for audit BEFORE the event may be deleted.
        organization_id = request.organization_id
        proposed_values = dict(request.proposed_values)
        retained_values = dict(request.retained_values)
        kind = request.kind

        # Build diff for audit — same shape as create/supersede helpers.
        if kind == ExternalEventChangeKind.UPDATE:
            diff: dict[str, Any] = {
                field: {"old": retained_values.get(field), "new": proposed_values.get(field)}
                for field in set(proposed_values) | set(retained_values)
                if retained_values.get(field) != proposed_values.get(field)
            }
        else:
            # DELETE: retained fields disappear; proposed fields are empty.
            diff = {
                field: {"old": retained_values.get(field), "new": None} for field in retained_values
            }

        with transaction.atomic():
            if kind == ExternalEventChangeKind.UPDATE:
                event = request.event
                if event is None:
                    # Should not happen for a PENDING request, but guard defensively.
                    raise ChangeRequestNotPendingError(
                        "Cannot approve an update request whose event has been deleted."
                    )
                # Apply proposed values to the local CalendarEvent.
                update_fields: list[str] = []
                if "title" in proposed_values:
                    event.title = proposed_values["title"] or ""
                    update_fields.append("title")
                if "description" in proposed_values:
                    event.description = proposed_values["description"] or ""
                    update_fields.append("description")
                if "start_time" in proposed_values and proposed_values["start_time"] is not None:
                    parsed_start = datetime.datetime.fromisoformat(proposed_values["start_time"])
                    # proposed_values carry tz-aware ISO strings expressed in the event's OWN
                    # local timezone (the DB-generated start_time/end_time fields). Stripping
                    # tzinfo recovers the local wall-clock digits, which is exactly what
                    # start_time_tz_unaware stores (matching the canonical models.py creation
                    # path: parsed.astimezone(event_tz).replace(tzinfo=None)).
                    event.start_time_tz_unaware = parsed_start.replace(tzinfo=None)
                    update_fields.append("start_time_tz_unaware")
                if "end_time" in proposed_values and proposed_values["end_time"] is not None:
                    parsed_end = datetime.datetime.fromisoformat(proposed_values["end_time"])
                    event.end_time_tz_unaware = parsed_end.replace(tzinfo=None)
                    update_fields.append("end_time_tz_unaware")
                if update_fields:
                    event.save(update_fields=update_fields)

            elif kind == ExternalEventChangeKind.DELETE:
                event = request.event
                if event is not None:
                    event.delete()
                # After event.delete(), the SET_NULL cascade has run inside the
                # atomic block: request.event_fk_id is now NULL in the DB.
                # We must refresh to see the nulled FK before updating status.
                request.refresh_from_db()

            # Mark the request as resolved.
            resolved_at = timezone.now()
            request.status = ExternalEventChangeRequestStatus.APPROVED
            request.resolved_by_user_id = membership.user_id
            request.resolved_at = resolved_at
            request.save(update_fields=["status", "resolved_by_user_id", "resolved_at"])

        # Record audit entry after the atomic block so on_commit delivery fires
        # after the transaction is committed and all rows are visible.
        if self.audit_service is not None:
            actor = self.audit_service.actor_from_membership(membership)
            subject = self.audit_service.subject_from_instance(request)
            self.audit_service.record(
                organization_id=organization_id,
                action=AuditAction.EXTERNAL_CHANGE_APPROVED,
                actor=actor,
                subject=subject,
                diff=diff or None,
            )

        return request

    # ------------------------------------------------------------------
    # Outbound undo
    # ------------------------------------------------------------------

    def _undo_on_provider_for_event(
        self,
        *,
        event: CalendarEvent,
        kind: str,
        write_adapter: CalendarAdapter,
    ) -> str | None:
        """Re-converge the external provider to the retained (approved) local state.

        The shared provider-write body used by both ``reject`` and the
        FORBIDDEN auto-undo path. Callers must supply an already-authenticated
        ``write_adapter`` — this service never resolves provider credentials itself (see
        module docstring for the rationale).

        This method performs the provider write OUTSIDE any DB transaction block the
        calling method opens, because the provider write is a non-transactional side
        effect that cannot be rolled back. The adapter input is hydrated with the event's
        full attendees / resources / recurrence via ``_build_adapter_input`` so the
        full-replace PUT does not wipe them on the provider.

        Behavior per ``kind``:

        - **UPDATE**: push the local (retained) values back to the provider via
          ``write_adapter.update_event``. The event's ``external_id`` is unchanged — this
          is an idempotent re-convergence to the retained state (no new id is created), so
          a subsequent local-commit failure is not catastrophic and no compensation is
          needed.

        - **DELETE**: re-create the event on the provider via ``write_adapter.create_event``.
          Re-creation yields a brand-new provider ``external_id`` (external-id churn). The
          local ``external_id`` rebind is deferred to the caller's atomic block; the caller
          compensates (deletes the just-created provider event) if that local commit fails,
          so a successful create is never orphaned by a DB rollback.

        Args:
            event: The live ``CalendarEvent`` whose retained state must be pushed back to
                the provider.
            kind: ``ExternalEventChangeKind.UPDATE`` or ``ExternalEventChangeKind.DELETE``.
            write_adapter: An authenticated provider write adapter for the event's calendar.

        Returns:
            For ``DELETE``: the new provider ``external_id`` the caller must rebind the
            local event to. For ``UPDATE``: ``None``.
        """
        adapter_input = self._build_adapter_input(event)

        if kind == ExternalEventChangeKind.UPDATE:
            # Push the local (retained) values back so the provider re-converges to the
            # approved state. The external id is preserved.
            write_adapter.update_event(
                adapter_input.calendar_external_id,
                event.external_id,
                adapter_input,
            )
            return None

        # DELETE: re-create the event on the provider. The external id churns — the
        # provider returns a brand-new id the caller must rebind locally.
        created = write_adapter.create_event(adapter_input)
        return created.external_id

    def _undo_on_provider(
        self,
        request: ExternalEventChangeRequest,
        *,
        write_adapter: CalendarAdapter,
    ) -> str | None:
        """Re-converge the external provider to the retained (approved) local state.

        Thin wrapper around ``_undo_on_provider_for_event`` that extracts ``event`` and
        ``kind`` from an existing ``ExternalEventChangeRequest``. Kept for backward
        compatibility with callers that already hold a saved request (e.g. ``reject``).

        Raises:
            ChangeRequestIneligibleError: If the request has no associated event to undo
                against (the event was deleted), so there is nothing to re-converge.
        """
        event = request.event
        if event is None:
            # No live event to push back / re-create — the request has no associated
            # event to undo against (e.g. the event was deleted out from under it).
            raise ChangeRequestIneligibleError(
                "Cannot undo a change request that has no associated event to undo against."
            )
        return self._undo_on_provider_for_event(
            event=event, kind=request.kind, write_adapter=write_adapter
        )

    def _resolve_with_undo(
        self,
        request: ExternalEventChangeRequest | None,
        *,
        write_adapter: CalendarAdapter,
        final_status: str,
        resolved_by_user_id: int | None,
        audit_action: AuditAction,
        actor: ActorSnapshot,
        diff: dict[str, Any],
        event: CalendarEvent,
        organization_id: int,
        kind: str,
        prepare_fn: Any = None,
    ) -> ExternalEventChangeRequest:
        """Shared outbound-undo orchestration used by both ``reject`` and ``auto_undo_inbound_change``.

        Performs the safe provider-call-outside-transaction + atomic local commit +
        compensating-delete pattern. The caller supplies the semantics that differ
        between reject and auto-undo (final status, resolver, audit action, actor).

        Sequence:
        1. Call ``_undo_on_provider`` (UPDATE→update_event; DELETE→create_event) OUTSIDE
           any DB transaction this method opens. The provider write is a non-transactional
           side effect that cannot be rolled back. (Under the outer ``sync_events``
           transaction these all sit inside a single outer transaction; this step is
           "outside" the atomic block *this method* opens, not necessarily outside every
           surrounding transaction.) For DELETE, returns the new provider external id.
        2. Open a SINGLE ``transaction.atomic()`` to perform ALL DB writes atomically:
           - If ``prepare_fn`` is supplied (auto-undo path): call it to create the
             change-request row and supersede any prior PENDING request for the event.
             This ensures no AUTO_UNDONE row or STALE supersede is persisted unless the
             provider call already succeeded.
           - For DELETE: rebind the local event's ``external_id`` to the new provider id.
           - Flip ``request.status`` (or the newly-created request) to ``final_status``
             + set resolved fields.
        3. If the local commit fails AFTER a successful provider create (DELETE kind):
           compensate by deleting the just-created provider event so it is never orphaned,
           then re-raise the original exception.
        4. Record an audit entry AFTER the atomic block so ``on_commit`` delivery fires
           after the transaction is committed and the rows are visible.

        Args:
            request: The ``ExternalEventChangeRequest`` to resolve via undo. Pass ``None``
                when ``prepare_fn`` will create the request inside the atomic block
                (auto-undo path). Must be non-``None`` for the reject path.
            write_adapter: Authenticated provider write adapter for the event's calendar.
            final_status: The ``ExternalEventChangeRequestStatus`` value to set on success.
            resolved_by_user_id: The user ID of the resolver (``None`` for SYSTEM).
            audit_action: The ``AuditAction`` to record on success.
            actor: The actor snapshot to record in the audit entry.
            diff: The pre-computed audit diff dict.
            event: The live ``CalendarEvent`` associated with the request. The caller must
                verify this is not ``None`` before calling.
            organization_id: The organization id for the audit record.
            kind: The ``ExternalEventChangeKind`` of the request.
            prepare_fn: Optional zero-argument callable invoked INSIDE the atomic block
                before the status flip. It must create and return the
                ``ExternalEventChangeRequest`` row (and may perform ``_supersede_pending``).
                Used by the auto-undo path to ensure the row is only created after the
                provider call succeeds. Must be ``None`` when ``request`` is already saved
                (reject path).

        Returns:
            The mutated ``ExternalEventChangeRequest`` instance with the updated status
            and resolved fields.
        """
        # Perform the provider re-convergence OUTSIDE any atomic block this method opens:
        # it is a non-transactional side effect that a DB rollback cannot undo. For DELETE
        # this returns the new provider external id we must rebind locally. If this raises,
        # nothing is committed — no AUTO_UNDONE row, no STALE supersede, no DB mutation.
        #
        # Note: under the outer ``sync_events`` transaction all of this sits inside a
        # single outer transaction; "outside" here means outside the savepoint this method
        # opens, so a failure in the next block can roll back the DB writes without leaving
        # a partially-committed state that a later savepoint release would expose.
        new_external_id = self._undo_on_provider_for_event(
            event=event, kind=kind, write_adapter=write_adapter
        )

        try:
            with transaction.atomic():
                # If prepare_fn is provided (auto-undo path), create the change-request
                # row + supersede any prior PENDING request INSIDE this atomic block. This
                # guarantees: if the provider call above raised, we never reach here (no
                # dangling AUTO_UNDONE row); if the block below fails, the create + supersede
                # roll back with it (no orphan row, no destroyed PENDING).
                if prepare_fn is not None:
                    request = prepare_fn()

                if request is None:
                    # Should never happen: prepare_fn must return a row (auto-undo path),
                    # and the reject path always passes a non-None request.
                    raise RuntimeError("_resolve_with_undo: request is None after prepare_fn")

                if kind == ExternalEventChangeKind.DELETE and new_external_id is not None:
                    # Rebind the local event to the re-created provider event's new id.
                    event.external_id = new_external_id
                    event.save(update_fields=["external_id"])

                resolved_at = timezone.now()
                request.status = final_status
                request.resolved_by_user_id = resolved_by_user_id
                request.resolved_at = resolved_at
                save_fields: list[str] = ["status", "resolved_by_user_id", "resolved_at"]
                request.save(update_fields=save_fields)
        except Exception:
            # The local commit failed AFTER the provider re-convergence already
            # succeeded. For DELETE, the re-created provider event would otherwise be
            # orphaned (the DB does not reference its new id) and surface as a duplicate
            # on the next sync — compensate by deleting it. Best-effort: if the
            # compensating delete also fails, let the ORIGINAL exception propagate.
            if kind == ExternalEventChangeKind.DELETE and new_external_id is not None:
                try:
                    write_adapter.delete_event(
                        event.calendar.external_id if event.calendar is not None else "",
                        new_external_id,
                    )
                except Exception:
                    logger.exception(
                        "Compensating delete of orphaned provider event %s failed during "
                        "undo rollback for change request %s.",
                        new_external_id,
                        request.pk if request is not None else "<unsaved>",
                    )
            raise

        # Record audit after the atomic block so on_commit delivery fires after commit.
        if self.audit_service is not None:
            subject = self.audit_service.subject_from_instance(request)
            self.audit_service.record(
                organization_id=organization_id,
                action=audit_action,
                actor=actor,
                subject=subject,
                diff=diff or None,
            )

        return request

    def reject(
        self,
        request: ExternalEventChangeRequest,
        *,
        membership: OrganizationMembership,
        write_adapter: CalendarAdapter,
    ) -> ExternalEventChangeRequest:
        """Reject a PENDING change request, re-converging the provider, mark REJECTED.

        Eligibility is checked first via ``can_resolve`` (the same gate as ``approve``).
        Only ``PENDING`` requests can be rejected; any other status raises
        ``ChangeRequestNotPendingError``. An ineligible membership raises
        ``ChangeRequestIneligibleError``.

        The outbound undo is delegated to ``_resolve_with_undo`` (see its docstring):

        - **UPDATE**: ``write_adapter.update_event`` is called with the local event's
          current (retained) field values + its external id, so the provider re-converges
          to the approved state.
        - **DELETE**: ``write_adapter.create_event`` re-creates the event on the provider
          and the local event's ``external_id`` is rebound to the newly returned id.

        **Transaction boundary (de-orphaning the provider side effect).** The provider
        write is a non-transactional side effect that cannot be rolled back, so it runs
        **outside / before** the DB transaction that flips the request to ``REJECTED``.
        Only after the provider call succeeds is a short ``transaction.atomic()`` opened
        to persist the status transition (and, for DELETE, the external-id rebind).

        For **DELETE** this matters: the re-created provider event has a brand-new id the
        DB must reference. If the local commit fails *after* a successful create, we
        **compensate** by deleting the just-created provider event so it cannot become a
        duplicate on the next sync, then re-raise. (Best-effort: if the compensating
        delete also fails, the original exception propagates — it is not swallowed.)

        For **UPDATE** no compensation is needed: ``update_event`` is an idempotent
        re-convergence to the retained state (no new id is created), so a status-flip
        failure after a successful update leaves no provider duplicate.

        An ``AuditAction.EXTERNAL_CHANGE_REJECTED`` entry is recorded with the rejecting
        membership as the actor.

        Args:
            request: The ``ExternalEventChangeRequest`` to reject. Must be ``PENDING``
                and belong to the same organization as ``membership``.
            membership: The ``OrganizationMembership`` rejecting the request. Must
                satisfy ``can_resolve``.
            write_adapter: An authenticated provider write adapter for the event's
                calendar (resolved + authenticated by the caller — see module docstring).

        Returns:
            The updated ``ExternalEventChangeRequest`` with ``status=REJECTED``,
            ``resolved_by``, and ``resolved_at`` set.

        Raises:
            ChangeRequestNotPendingError: If ``request.status`` is not ``PENDING``.
            ChangeRequestIneligibleError: If ``membership`` is not eligible to resolve
                this request.
        """
        if request.status != ExternalEventChangeRequestStatus.PENDING:
            raise ChangeRequestNotPendingError()

        if not self.can_resolve(request, membership):
            raise ChangeRequestIneligibleError()

        # Guard: no live event → no undo possible. Check before provider call.
        if request.event is None:
            raise ChangeRequestIneligibleError(
                "Cannot reject a change request that has no associated event to undo against."
            )

        # Capture values needed for the audit diff before any mutation.
        retained_values = dict(request.retained_values)
        proposed_values = dict(request.proposed_values)
        kind = request.kind

        if kind == ExternalEventChangeKind.UPDATE:
            # The undo restores retained_values over the (rejected) proposed_values:
            # "old" is what the inbound change proposed, "new" is what we re-converge to.
            diff: dict[str, Any] = {
                field: {"old": proposed_values.get(field), "new": retained_values.get(field)}
                for field in set(proposed_values) | set(retained_values)
                if proposed_values.get(field) != retained_values.get(field)
            }
        else:
            # DELETE: the inbound deletion (proposed: nothing) is undone by re-creating
            # the event with its retained values.
            diff = {
                field: {"old": None, "new": retained_values.get(field)} for field in retained_values
            }

        if self.audit_service is not None:
            actor: ActorSnapshot = self.audit_service.actor_from_membership(membership)
        else:
            # No audit service — build a minimal actor so _resolve_with_undo signature is
            # satisfied; the audit call inside will be skipped anyway.
            actor = ActorSnapshot(
                actor_type=AuditActorType.MEMBERSHIP,
                actor_id=membership.user_id,
            )

        # request.event was already guarded non-None above; cast for mypy.
        event: CalendarEvent = request.event  # type: ignore[assignment]

        return self._resolve_with_undo(
            request,
            write_adapter=write_adapter,
            final_status=ExternalEventChangeRequestStatus.REJECTED,
            resolved_by_user_id=membership.user_id,
            audit_action=AuditAction.EXTERNAL_CHANGE_REJECTED,
            actor=actor,
            diff=diff,
            event=event,
            organization_id=request.organization_id,
            kind=request.kind,
        )

    def auto_undo_inbound_change(
        self,
        *,
        event: CalendarEvent,
        kind: str,
        proposed_values: dict[str, Any],
        retained_values: dict[str, Any],
        payload: dict[str, Any],
        provider: CalendarProvider | str,
        write_adapter: CalendarAdapter,
    ) -> ExternalEventChangeRequest:
        """Immediately undo an inbound external change under the FORBIDDEN policy.

        Called during sync when ``external_event_update_policy == FORBIDDEN``.  Unlike
        the ``CHANGE_REQUEST`` path (which creates a ``PENDING`` row for later human
        review), FORBIDDEN auto-undoes the change on the external provider right now and
        records an ``AUTO_UNDONE`` row for history and audit.

        No human approver is involved — the SYSTEM actor is used for both row creation
        and the audit entry.

        The safe outbound-undo orchestration (provider-call-outside-txn + single atomic
        block for all DB writes + compensating-delete) is shared with ``reject`` via
        ``_resolve_with_undo``.

        Sequence (delegated to ``_resolve_with_undo``):
        1. Call the provider write (``update_event`` or ``create_event``) OUTSIDE the
           atomic block this method opens. If this raises, nothing is committed — no
           AUTO_UNDONE row, no STALE supersede, no DB mutation.
        2. Open a SINGLE ``transaction.atomic()`` that performs ALL DB writes:
           - Supersede any existing PENDING request for this event (``_supersede_pending``).
           - Create the ``AUTO_UNDONE`` ``ExternalEventChangeRequest`` row (no PENDING
             intermediate — the change is handled immediately).
           - For DELETE: rebind the local event's ``external_id`` to the new provider id.
           - Set ``status=AUTO_UNDONE``, ``resolved_at``, ``resolved_by_user_id=None``.
        3. If the atomic block fails AFTER a successful provider ``create_event`` (DELETE):
           compensate by ``delete_event(new_external_id)`` so no provider orphan survives.
        4. Emit an ``EXTERNAL_CHANGE_AUTO_UNDONE`` audit entry via the SYSTEM actor.

        Args:
            event: The local ``CalendarEvent`` targeted by the inbound change.
            kind: ``ExternalEventChangeKind.UPDATE`` or ``ExternalEventChangeKind.DELETE``.
            proposed_values: Dict of incoming field values (title, description, start_time,
                end_time as ISO strings). Empty for DELETE kind.
            retained_values: Snapshot of local event field values before the inbound change.
                Used to push back to the provider (UPDATE) or re-create (DELETE).
            payload: Raw provider payload stored for debugging / replay.
            provider: Calendar provider string (``CalendarProvider.GOOGLE``).
            write_adapter: Authenticated provider write adapter for the event's calendar.
                Must NOT be ``None`` — the caller (sync) must raise ``ImproperlyConfigured``
                before reaching here if it is.

        Returns:
            The ``ExternalEventChangeRequest`` row recorded as ``AUTO_UNDONE``.
        """
        # Build the audit diff (same shape as reject/create helpers).
        if kind == ExternalEventChangeKind.UPDATE:
            # Undo: "old" = proposed (what inbound claimed), "new" = retained (what we restore).
            diff: dict[str, Any] = {
                field: {"old": proposed_values.get(field), "new": retained_values.get(field)}
                for field in set(proposed_values) | set(retained_values)
                if proposed_values.get(field) != retained_values.get(field)
            }
        else:
            # DELETE undo: retained fields re-appear ("new" = retained, "old" = None).
            diff = {
                field: {"old": None, "new": retained_values.get(field)} for field in retained_values
            }

        # Resolve the system actor for the audit entry.
        if self.audit_service is not None:
            system_actor: ActorSnapshot = self.audit_service.system_actor()
        else:
            # No audit service — build a minimal actor; the audit call will be skipped.
            system_actor = ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None)

        def _prepare() -> ExternalEventChangeRequest:
            # Called INSIDE the single atomic block in _resolve_with_undo, AFTER the
            # provider call succeeds. This guarantees:
            # - If the provider call raised → we never reach here → no AUTO_UNDONE row,
            #   no STALE supersede (invariant 4 of the atomicity contract).
            # - If the block below fails → both the row creation and supersede roll back
            #   with it → no dangling AUTO_UNDONE row, no destroyed PENDING (invariant 2).
            self._supersede_pending(event)
            return ExternalEventChangeRequest.objects.create(
                organization=event.organization,
                event=event,
                kind=kind,
                status=ExternalEventChangeRequestStatus.AUTO_UNDONE,
                provider=provider,
                proposed_values=proposed_values,
                proposed_payload=payload,
                retained_values=retained_values,
            )

        return self._resolve_with_undo(
            None,
            write_adapter=write_adapter,
            final_status=ExternalEventChangeRequestStatus.AUTO_UNDONE,
            resolved_by_user_id=None,
            audit_action=AuditAction.EXTERNAL_CHANGE_AUTO_UNDONE,
            actor=system_actor,
            diff=diff,
            event=event,
            organization_id=event.organization_id,
            kind=kind,
            prepare_fn=_prepare,
        )
