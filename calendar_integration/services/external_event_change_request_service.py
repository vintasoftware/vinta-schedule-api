"""Service for managing ExternalEventChangeRequest lifecycle.

``ExternalEventChangeRequestService`` is the single place that creates, supersedes,
approves, rejects, and auto-undoes change requests. It is DI-injected and consumes
``AuditService`` for audit trail emission.

Phase 3 implements the *create / supersede for inbound updates* path.
Phase 4 adds *create / supersede for inbound deletions*.
Phases 5-6 extend this service with approve/reject and the FORBIDDEN auto-undo path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from django.db import transaction

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from calendar_integration.constants import (
    CalendarProvider,
    ExternalEventChangeKind,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.models import ExternalEventChangeRequest


if TYPE_CHECKING:
    from audit.services import AuditService
    from calendar_integration.models import CalendarEvent


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
