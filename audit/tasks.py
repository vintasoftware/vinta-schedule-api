"""Celery task for persisting audit records asynchronously.

The write path: AuditService.record() enqueues persist_audit_record with a
JSON-safe dict payload; this task rebuilds the AuditRecordData from the payload
and calls the repository.

CELERY_TASK_ACKS_LATE = True means this task must be idempotent: on worker
failure it may re-run. The repository write is naturally idempotent for our
purposes since re-persisting a duplicate audit record creates an additional row
(not a conflict), which is acceptable for an append-only audit log.

Task failures are logged and swallowed (not re-raised) so a bad payload does
not crash the worker process. The record is lost in that case — acceptable for
the fire-and-forget audit trail (see Guiding Decisions §Write path).

DI resolution pattern: this task resolves the AuditRepository directly from
`di_core.containers.container` at runtime rather than using `@inject`. This
avoids the wiring-order problem that occurs when audit.tasks is imported inside
di_core/containers.py → audit.services before container.wire() runs. The
`container` module-level variable is set by di_core/apps.py's `ready()` before
any task runs (eager or deferred), so the runtime resolution is always safe.
"""

from __future__ import annotations

import logging

from audit.types import ActorSnapshot, AuditRecordData, SubjectRef
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


@app.task
def persist_audit_record(payload: dict) -> None:
    """Persist a single audit record via the repository.

    Reconstructs an AuditRecordData from the JSON payload produced by
    AuditService.record() and calls repository.add(). Failures are logged and
    swallowed so the worker stays alive even when given a malformed payload or
    when the database is temporarily unavailable.

    The AuditRepository is resolved from the DI container at call time
    (di_core.containers.container.audit_repository()) rather than via @inject
    to avoid the import-before-wiring ordering issue — see module docstring.

    Args:
        payload: A JSON-safe dict produced by dataclasses.asdict(AuditRecordData).
    """
    # Resolve repository from the DI container at runtime.
    # di_core.containers.container is set in di_core/apps.py's ready(), which
    # always runs before any task executes (eager or deferred).
    from di_core import containers

    di_container = containers.container
    if di_container is None:
        logger.error(
            "persist_audit_record: DI container is not initialized. "
            "Audit record will not be persisted. Payload: %r",
            payload,
        )
        return

    try:
        actor_payload = payload["actor"]
        subject_payload = payload["subject"]

        actor = ActorSnapshot(
            actor_type=actor_payload["actor_type"],
            actor_id=actor_payload["actor_id"],
            actor_role=actor_payload.get("actor_role"),
            system_user_scopes=actor_payload.get("system_user_scopes"),
            system_user_scoped_to_membership=actor_payload.get("system_user_scoped_to_membership"),
        )
        subject = SubjectRef(
            subject_type=subject_payload["subject_type"],
            subject_id=subject_payload["subject_id"],
            subject_label=subject_payload.get("subject_label"),
        )
        data = AuditRecordData(
            organization_id=payload["organization_id"],
            action=payload["action"],
            actor=actor,
            subject=subject,
            affected_membership_ids=payload.get("affected_membership_ids") or [],
            diff=payload.get("diff"),
        )
    except (KeyError, TypeError):
        logger.exception(
            "persist_audit_record: malformed payload, cannot reconstruct AuditRecordData. "
            "Payload: %r",
            payload,
        )
        return

    try:
        repository = di_container.audit_repository()
        repository.add(data)
    except Exception:
        logger.exception(
            "persist_audit_record: repository.add() failed for action %r on organization %s.",
            payload.get("action"),
            payload.get("organization_id"),
        )
