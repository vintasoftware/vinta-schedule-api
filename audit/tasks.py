"""Celery task for persisting audit records asynchronously.

The write path: AuditService.record() enqueues persist_audit_record with a
JSON-safe dict payload; this task rebuilds the AuditRecordData from the payload
and calls the repository.

CELERY_TASK_ACKS_LATE = True means this task must be idempotent: on worker
failure it may re-run. The repository write is naturally idempotent for our
purposes since re-persisting a duplicate audit record creates an additional row
(not a conflict), which is acceptable for an append-only audit log.

Task failures are logged and swallowed (not re-raised) so a bad payload does
not crash the worker process. The record is lost in that case, which is
acceptable for the fire-and-forget audit trail.

DI injection pattern: this task uses @app.task (on top) + @inject (below), with
the repository injected as a keyword argument via Annotated[..., Provide[...]] = None
(the webhooks/tasks.py convention). The @inject decorator resolves audit_repository
from the container at call time; no runtime container import is needed.
"""

import logging
from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject

from audit.types import ActorSnapshot, AuditRecordData, SubjectRef
from vinta_schedule_api.celery import app


if TYPE_CHECKING:
    from audit.repositories import AuditRepository


logger = logging.getLogger(__name__)


@app.task
@inject
def persist_audit_record(
    payload: dict,
    *,
    repository: Annotated["AuditRepository | None", Provide["audit_repository"]] = None,
) -> None:
    """Persist a single audit record via the repository.

    Reconstructs an AuditRecordData from the JSON payload produced by
    AuditService.record() and calls repository.add(). Failures are logged and
    swallowed so the worker stays alive even when given a malformed payload or
    when the database is temporarily unavailable.

    The AuditRepository is injected via @inject / Provide["audit_repository"]
    (the webhooks/tasks.py convention) — no runtime container import is needed.

    Args:
        payload: A JSON-safe dict produced by dataclasses.asdict(AuditRecordData).
        repository: Injected by the DI container; callers must not pass this explicitly
            unless overriding in tests.
    """
    if repository is None:
        logger.error(
            "persist_audit_record: repository is not injected (DI not wired?). "
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
    except Exception:
        logger.exception(
            "persist_audit_record: malformed payload, cannot reconstruct AuditRecordData. "
            "Payload: %r",
            payload,
        )
        return

    try:
        repository.add(data)
    except Exception:
        logger.exception(
            "persist_audit_record: repository.add() failed for action %r on organization %s.",
            payload.get("action"),
            payload.get("organization_id"),
        )
