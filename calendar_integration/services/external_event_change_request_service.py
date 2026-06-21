"""Service for managing ExternalEventChangeRequest lifecycle.

``ExternalEventChangeRequestService`` is the single place that creates, supersedes,
approves, rejects, and auto-undoes change requests. It is DI-injected and consumes
``AuditService`` for audit trail emission.

Phase 3 implements the *create / supersede for inbound updates* path.
Phase 4 adds *create / supersede for inbound deletions*.
Phases 5-6 extend this service with approve/reject and the FORBIDDEN auto-undo path.
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


if TYPE_CHECKING:
    from audit.services import AuditService
    from calendar_integration.models import CalendarEvent
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
                    # CalendarEvent stores timezone-unaware local-clock values.
                    # The values in proposed_values are UTC instants as ISO strings.
                    # Strip tzinfo (store as tz-unaware per AGENTS.md CalendarEvent convention).
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
