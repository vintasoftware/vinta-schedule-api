"""Service for managing ExternalEventChangeRequest lifecycle.

``ExternalEventChangeRequestService`` is the single place that creates, supersedes,
approves, rejects, and auto-undoes change requests. It is DI-injected and consumes
``AuditService`` for audit trail emission.

Phase 3 implements the *create / supersede for inbound updates* path.
Phase 4 adds *create / supersede for inbound deletions*.
Phase 5a adds ``approve`` (apply locally) + the ``can_resolve`` eligibility gate.
Phase 5b adds ``reject`` (outbound undo): re-converge the external provider to the
retained (approved) state.

**Outbound-undo seam (Phase 5b / 6).** Re-converging the provider needs an
*authenticated* write adapter for the event's calendar. Rather than injecting a
``CalendarService`` into this service (``CalendarSyncService`` already depends on this
service, so injecting the facade back would risk an import cycle and couple the two),
the outbound-undo logic accepts the authenticated write capability **as a parameter**:
``_undo_on_provider(request, *, write_adapter)`` and ``reject(request, *, membership,
write_adapter)`` take a ``CalendarAdapter`` the caller has already authenticated. This
keeps the service free of provider-credential concerns and import-cycle-free.

- **Phase 8 (API reject)** authenticates a ``CalendarService`` for the event's calendar,
  resolves the write adapter via ``CalendarService._get_write_adapter_for_calendar`` and
  passes it to ``reject``.
- **Phase 6 (FORBIDDEN auto-undo, during sync)** already holds an authenticated adapter
  (``context.calendar_adapter``) and reuses ``_undo_on_provider`` directly.

Phase 6 extends this service with the FORBIDDEN auto-undo path, reusing
``_undo_on_provider``.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Annotated, Any

from django.db import transaction
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
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
from calendar_integration.services.dataclasses import CalendarEventAdapterInputData


if TYPE_CHECKING:
    from audit.services import AuditService
    from calendar_integration.models import CalendarEvent
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
    from organizations.models import OrganizationMembership


logger = logging.getLogger(__name__)


class ExternalEventChangeRequestService:
    """Service for creating and managing ExternalEventChangeRequest records.

    Handles the lifecycle of change requests created when inbound external
    provider changes are intercepted under the ``CHANGE_REQUEST`` or
    ``FORBIDDEN`` policy.

    Constructor arguments are DI-injected:
    - ``audit_service``: emits audit trail entries for each state transition.
    """

    @inject
    def __init__(
        self,
        audit_service: Annotated[AuditService | None, Provide["audit_service"]] = None,
    ) -> None:
        self.audit_service = audit_service

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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
        description, start_time, end_time) so Phase 5b can re-create the event
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
                re-create on rejection (Phase 5b).
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

        This is the single eligibility gate consumed by ``approve``, ``reject``
        (Phase 5b), and the Phase 8 REST/GraphQL API layer.

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
    # Outbound undo (Phase 5b / Phase 6)
    # ------------------------------------------------------------------

    def _undo_on_provider(
        self,
        request: ExternalEventChangeRequest,
        *,
        write_adapter: CalendarAdapter,
    ) -> None:
        """Re-converge the external provider to the retained (approved) local state.

        This is the shared outbound-undo body reused by both ``reject`` (Phase 5b,
        triggered by a user via the API) and the FORBIDDEN auto-undo path (Phase 6,
        triggered during sync). The caller must supply an **already-authenticated**
        ``write_adapter`` (a ``CalendarAdapter``) for the event's calendar — this
        service never resolves provider credentials itself (see module docstring for
        the rationale).

        Behavior per ``request.kind``:

        - **UPDATE**: the local event was never mutated (the interception in Phase 3
          kept it at the retained values), so its current field values *are* the
          retained state. We push them back to the provider via
          ``write_adapter.update_event(calendar_external_id, event_external_id, ...)``
          so the provider re-converges to match local. The event's ``external_id`` is
          unchanged.

        - **DELETE**: the local event still exists (Phase 4 did not delete it); the
          provider deleted it. We re-create it on the provider via
          ``write_adapter.create_event(...)``. Re-creation yields a **new** provider
          ``external_id`` — the old id is gone forever (external-id churn). We rebind
          the local event's ``external_id`` to the newly returned id and save. The
          adapter call is the commit boundary: the local ``external_id`` save runs
          inside the surrounding ``reject``/auto-undo transaction *after* a successful
          create, so a create that succeeds is always followed by the rebind (or the
          whole transaction rolls back, never leaving the local event pointing at a
          dead external id). A create that fails propagates before any local mutation.

        Args:
            request: The ``ExternalEventChangeRequest`` whose change must be undone on
                the provider. Its ``event`` must still exist (a PENDING request always
                has a live event for both kinds in this flow).
            write_adapter: An authenticated provider write adapter for the event's
                calendar.

        Raises:
            ChangeRequestNotPendingError: If the request's event has been deleted and
                there is nothing to re-converge.
        """
        event = request.event
        if event is None:
            # No live event to push back / re-create — should not happen for a PENDING
            # request in either the update or delete flow.
            raise ChangeRequestNotPendingError(
                "Cannot undo a change request whose event no longer exists."
            )

        calendar = event.calendar
        # The provider speaks in external ids, never the internal PKs.
        calendar_external_id = calendar.external_id if calendar is not None else ""

        adapter_input = CalendarEventAdapterInputData(
            calendar_external_id=calendar_external_id,
            title=event.title,
            description=event.description,
            start_time=event.start_time,
            end_time=event.end_time,
            timezone=event.timezone,
            attendees=[],
            external_id=event.external_id,
        )

        if request.kind == ExternalEventChangeKind.UPDATE:
            # Push the local (retained) values back so the provider re-converges to the
            # approved state. The external id is preserved.
            write_adapter.update_event(
                calendar_external_id,
                event.external_id,
                adapter_input,
            )
            return

        # DELETE: re-create the event on the provider. The external id churns — the
        # provider returns a brand-new id we must rebind locally.
        created = write_adapter.create_event(adapter_input)
        event.external_id = created.external_id
        event.save(update_fields=["external_id"])

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

        The outbound undo is delegated to ``_undo_on_provider`` (see its docstring):

        - **UPDATE**: ``write_adapter.update_event`` is called with the local event's
          current (retained) field values + its external id, so the provider re-converges
          to the approved state.
        - **DELETE**: ``write_adapter.create_event`` re-creates the event on the provider
          and the local event's ``external_id`` is rebound to the newly returned id.

        The provider re-convergence and the local status transition run in one
        ``transaction.atomic()`` block: if the adapter call (the commit boundary) fails,
        nothing is persisted; if it succeeds, the ``REJECTED`` transition and any
        external-id rebind commit together. An
        ``AuditAction.EXTERNAL_CHANGE_REJECTED`` entry is recorded with the rejecting
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

        # Capture values needed for the audit diff before any mutation.
        organization_id = request.organization_id
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

        with transaction.atomic():
            # Re-converge the provider first; the adapter call is the commit boundary.
            # On success the local state transition (and any external-id rebind) commit
            # together; on failure the whole transaction rolls back.
            self._undo_on_provider(request, write_adapter=write_adapter)

            resolved_at = timezone.now()
            request.status = ExternalEventChangeRequestStatus.REJECTED
            request.resolved_by_user_id = membership.user_id
            request.resolved_at = resolved_at
            request.save(update_fields=["status", "resolved_by_user_id", "resolved_at"])

        # Record audit after the atomic block so on_commit delivery fires after commit.
        if self.audit_service is not None:
            actor = self.audit_service.actor_from_membership(membership)
            subject = self.audit_service.subject_from_instance(request)
            self.audit_service.record(
                organization_id=organization_id,
                action=AuditAction.EXTERNAL_CHANGE_REJECTED,
                actor=actor,
                subject=subject,
                diff=diff or None,
            )

        return request
