"""Celery tasks for persisting and reconciling audit records.

The write path: AuditService.record() enqueues persist_audit_record with a
JSON-safe dict payload; this task rebuilds the AuditRecordData from the payload
and hands it to AuditService.persist(), which writes to the main repository and
then tentatively replicates to the additional ones.

CELERY_TASK_ACKS_LATE = True means this task must be idempotent: on worker
failure it may re-run. It is. Every record carries a ``uid`` generated once at
emit time, and every repository write is an upsert on that uid, so a re-run
rewrites the record the first run wrote instead of appending a second copy.

Task failures are logged and swallowed (not re-raised) so a bad payload does
not crash the worker process. The record is lost in that case, which is
acceptable for the fire-and-forget audit trail.

DI injection pattern: these tasks use @app.task (on top) + @inject (below), with
the service injected as a keyword argument via Annotated[..., Provide[...]] = None
(the webhooks/tasks.py convention). The @inject decorator resolves audit_service
from the container at call time; no runtime container import is needed. The
service is injected rather than the repository because "main repository plus
replicas" is the service's policy to own, not the task's.
"""

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject

from audit.types import ActorSnapshot, AuditQuery, AuditRecordData, SubjectRef
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


if TYPE_CHECKING:
    from audit.services import AuditService


logger = logging.getLogger(__name__)


def deserialize_record_data(payload: dict) -> AuditRecordData:
    """Rebuild an AuditRecordData from the dict AuditService.serialize() produced.

    The inverse of ``AuditService.serialize``; the two must change together.
    ``uid`` and ``created_at`` cross the broker as a string and an ISO 8601
    string respectively, because CELERY_TASK_SERIALIZER is json.

    A payload from before those two fields existed still loads: a missing uid
    gets a fresh one and a missing created_at leaves the repository to stamp its
    own clock. That only matters for tasks already sitting in the queue during a
    deploy, and it is the one case where a retry could write a duplicate — the
    record has no stable identity to upsert on.

    Args:
        payload: The JSON-safe dict carried in the task message.

    Returns:
        The reconstructed record data.

    Raises:
        KeyError, TypeError, ValueError: The payload is malformed. Callers log
            and swallow rather than crashing the worker.
    """
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
    raw_uid = payload.get("uid")
    raw_created_at = payload.get("created_at")
    return AuditRecordData(
        organization_id=payload["organization_id"],
        action=payload["action"],
        actor=actor,
        subject=subject,
        affected_membership_ids=payload.get("affected_membership_ids") or [],
        diff=payload.get("diff"),
        uid=uuid.UUID(raw_uid) if raw_uid else uuid.uuid7(),
        created_at=datetime.fromisoformat(raw_created_at) if raw_created_at else None,
    )


@app.task
@inject
def persist_audit_record(
    payload: dict,
    *,
    audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
) -> None:
    """Persist a single audit record via the audit service.

    Reconstructs an AuditRecordData from the JSON payload produced by
    AuditService.record() and calls audit_service.persist(), which writes the
    main repository and then replicates, best effort, to the additional ones.
    Failures are logged and swallowed so the worker stays alive even when given
    a malformed payload or when the database is temporarily unavailable.

    Args:
        payload: A JSON-safe dict produced by AuditService.serialize().
        audit_service: Injected by the DI container; callers must not pass this
            explicitly unless overriding in tests.
    """
    if audit_service is None:
        logger.error(
            "persist_audit_record: audit_service is not injected (DI not wired?). "
            "Audit record will not be persisted. Payload: %r",
            payload,
        )
        return

    try:
        data = deserialize_record_data(payload)
    except Exception:
        logger.exception(
            "persist_audit_record: malformed payload, cannot reconstruct AuditRecordData. "
            "Payload: %r",
            payload,
        )
        return

    # `data.organization_id` is the only organization boundary this task ever
    # sees. Resolved eagerly (not via a lazy binding) so a stale/deleted
    # organization id is caught here, at the task boundary, rather than
    # surfacing later as a bound-but-null organization deep inside a manager
    # that consults the context -- mirrors
    # `calendar_integration/tasks/calendar_sync_tasks.py`'s
    # `organization = Organization.objects.filter(id=organization_id).first()`
    # / `if not organization: return` guard.
    organization = Organization.objects.filter(id=data.organization_id).first()
    if organization is None:
        logger.error(
            "persist_audit_record: organization %s no longer exists; audit record for "
            "action %r will not be persisted. Payload: %r",
            data.organization_id,
            payload.get("action"),
            payload,
        )
        return

    try:
        with organization_context(organization):
            audit_service.persist(data)
    except Exception:
        logger.exception(
            "persist_audit_record: persist() failed for action %r on organization %s.",
            payload.get("action"),
            payload.get("organization_id"),
        )


@app.task
@inject
def sync_audit_repository(
    target: str,
    *,
    source: str | None = None,
    organization_id: int | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    batch_size: int = 500,
    audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
) -> dict | None:
    """Backfill one additional audit repository from another, out of band.

    The operational entry point for the reconciliation that
    ``AuditService.replicate`` deliberately does not do: filling in whatever a
    replica missed while it was unreachable, or loading a newly-added
    repository with the history that predates it. A backfill walks the whole
    window it is given, so it belongs in a worker rather than in a request.

    Re-running it is safe. Every write is an upsert on the record's uid, so a
    window that is already in step is rewritten with identical content rather
    than duplicated — which also makes it safe to run while live replication
    continues.

    The window arrives as ISO 8601 strings rather than datetimes because
    CELERY_TASK_SERIALIZER is json.

    Args:
        target: Alias of the repository to write into.
        source: Alias of the repository to read from; None means main.
        organization_id: Restrict the sync to one tenant.
        created_after: ISO 8601 lower bound on created_at, inclusive.
        created_before: ISO 8601 upper bound on created_at, exclusive.
        batch_size: Records per bulk_add against the target.
        audit_service: Injected by the DI container.

    Returns:
        The AuditSyncResult as a dict (JSON-safe, so it can be a task result),
        or None when the sync could not be started.
    """
    if audit_service is None:
        logger.error(
            "sync_audit_repository: audit_service is not injected (DI not wired?). "
            "Sync to %r will not run.",
            target,
        )
        return None

    try:
        query = AuditQuery(
            organization_id=organization_id,
            created_after=datetime.fromisoformat(created_after) if created_after else None,
            created_before=datetime.fromisoformat(created_before) if created_before else None,
        )
    except ValueError:
        logger.exception(
            "sync_audit_repository: unparseable window (created_after=%r, created_before=%r); "
            "sync to %r will not run.",
            created_after,
            created_before,
            target,
        )
        return None

    try:
        result = audit_service.sync_repository(
            target, source=source, query=query, batch_size=batch_size
        )
    except Exception:
        logger.exception("sync_audit_repository: sync to %r failed to run.", target)
        return None

    return {
        "source": result.source,
        "target": result.target,
        "read": result.read,
        "written": result.written,
        "failed": result.failed,
        "errors": result.errors,
    }
