"""AuditService — DI-injected service for recording audit trail entries.

Usage (from a caller that has been injected with AuditService):

    actor = self.audit_service.actor_from_membership(membership)
    subject = SubjectRef(
        subject_type="organizations.OrganizationMembership",
        subject_id=str(membership.pk),
        subject_label=str(membership),
    )
    self.audit_service.record(
        organization_id=membership.organization_id,
        action=AuditAction.UPDATE,
        actor=actor,
        subject=subject,
        diff=diff,
    )

Callers must NOT call record() from inside a background task that already is the
async persistence boundary — that is the job of persist_audit_record.
"""

import dataclasses
import logging
from collections.abc import Sequence
from typing import Annotated

from django.db import transaction

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditActorType
from audit.repositories import AuditRepository
from audit.tasks import persist_audit_record
from audit.types import ActorSnapshot, AuditRecordData, SubjectRef


logger = logging.getLogger(__name__)


class AuditService:
    """Service that records audit trail entries asynchronously via Celery.

    Actor context is captured synchronously at call time and serialized into
    the Celery task payload so the worker never re-reads mutable state that
    may have changed or been deleted by the time it runs.
    """

    @inject
    def __init__(
        self,
        repository: Annotated[AuditRepository, Provide["audit_repository"]],
    ) -> None:
        self.repository = repository

    # ------------------------------------------------------------------
    # Actor builder helpers — capture snapshots SYNCHRONOUSLY
    # ------------------------------------------------------------------

    @staticmethod
    def actor_from_membership(membership: object) -> ActorSnapshot:
        """Build an ActorSnapshot from an OrganizationMembership.

        Captures membership.role at call time so the Celery task never needs to
        re-read a membership row that may have changed or been deleted.

        Args:
            membership: An OrganizationMembership instance.

        Returns:
            An ActorSnapshot with actor_type=MEMBERSHIP and actor_role set.
        """
        return ActorSnapshot(
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=membership.id,  # type: ignore[attr-defined]
            actor_role=membership.role,  # type: ignore[attr-defined]
        )

    @staticmethod
    def actor_from_system_user(system_user: object) -> ActorSnapshot:
        """Build an ActorSnapshot from a SystemUser.

        Captures system_user_scopes (from available_resources) at call time.
        The scopes queryset is evaluated now so the snapshot is correct even if
        the system user's ResourceAccess rows change before the task runs.

        Args:
            system_user: A public_api.SystemUser instance.

        Returns:
            An ActorSnapshot with actor_type=SYSTEM_USER, scopes list, and
            scoped_to_membership from the FK.
        """
        scopes = [
            ra.resource_name
            for ra in system_user.available_resources.all()  # type: ignore[attr-defined]
        ]
        return ActorSnapshot(
            actor_type=AuditActorType.SYSTEM_USER,
            actor_id=system_user.id,  # type: ignore[attr-defined]
            system_user_scopes=scopes,
            system_user_scoped_to_membership=system_user.scoped_to_membership_fk_id,  # type: ignore[attr-defined]
        )

    @staticmethod
    def actor_from_single_use_code(token: object) -> ActorSnapshot:
        """Build an ActorSnapshot from a CalendarManagementToken (single-use code).

        Args:
            token: A CalendarManagementToken instance.

        Returns:
            An ActorSnapshot with actor_type=SINGLE_USE_CODE and actor_id=token.id.
        """
        return ActorSnapshot(
            actor_type=AuditActorType.SINGLE_USE_CODE,
            actor_id=token.id,  # type: ignore[attr-defined]
        )

    @staticmethod
    def system_actor() -> ActorSnapshot:
        """Build an ActorSnapshot representing the system itself.

        Returns:
            An ActorSnapshot with actor_type=SYSTEM and actor_id=None.
        """
        return ActorSnapshot(
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
        )

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        organization_id: int,
        action: str,
        actor: ActorSnapshot,
        subject: SubjectRef,
        affected_membership_ids: Sequence[int] = (),
        diff: dict | None = None,
    ) -> None:
        """Record an audit trail entry asynchronously.

        Builds an AuditRecordData, serializes it to a JSON-safe dict, and
        enqueues the persist_audit_record Celery task. The task runs the
        repository write out of band so a slow or failing write never blocks
        the caller.

        Enqueue errors (broker unavailability, serialization problems) are
        caught, logged, and swallowed so the business action that triggered
        the audit record is never affected. Repository errors happen in the
        worker and are therefore already off the caller's critical path.

        Args:
            organization_id: ID of the organization this record belongs to.
            action: The action string (from AuditAction or a custom value).
            actor: Pre-built ActorSnapshot (must be built synchronously before
                any async boundary).
            subject: The subject reference for the audited object.
            affected_membership_ids: Optional sequence of OrganizationMembership
                IDs affected by this action.
            diff: Optional diff dict in {field: {"old": ..., "new": ...}} shape.
                Pass None (or omit) when there is no diff. An empty dict is
                treated the same as None — the repository normalizes it to NULL.
        """
        data = AuditRecordData(
            organization_id=organization_id,
            action=action,
            actor=actor,
            subject=subject,
            affected_membership_ids=list(affected_membership_ids),
            diff=diff or None,
        )

        payload = dataclasses.asdict(data)

        def _enqueue() -> None:
            try:
                persist_audit_record.delay(payload)
            except Exception:
                logger.exception(
                    "Failed to enqueue audit record for action %r on organization %s. "
                    "The record will not be persisted.",
                    action,
                    organization_id,
                )

        transaction.on_commit(_enqueue)
