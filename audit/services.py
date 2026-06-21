"""AuditService — DI-injected service for recording audit trail entries.

Usage (from a caller that has been injected with AuditService):

    actor = self.audit_service.actor_from_membership(membership)
    subject = SubjectRef(
        subject_type="organizations.OrganizationMembership",
        subject_id=str(membership.user_id),
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
            actor_id=membership.user_id,  # type: ignore[attr-defined]
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
            system_user_scoped_to_membership=system_user.scoped_to_membership_user_id,  # type: ignore[attr-defined]
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

    @staticmethod
    def actor_from_user(user: object, organization_id: int) -> ActorSnapshot:
        """Resolve a ``User`` acting within an organization to an actor snapshot.

        Looks up the OrganizationMembership identifying this user in the org and,
        when present, returns a MEMBERSHIP actor capturing its role. Falls back to
        a SYSTEM actor when the user has no membership in the organization — this
        mirrors the orphan-ownership guard (a non-member acting leaves no
        membership FK to point at), so the record is still emitted with a stable
        actor rather than a dangling membership reference.

        Args:
            user: A users.User instance (the acting principal).
            organization_id: The organization the action happens in.

        Returns:
            A MEMBERSHIP ActorSnapshot when a membership exists, else a SYSTEM one.
        """
        # Lazy import: audit is a leaf app; importing organizations at module load
        # would create an import cycle (organizations services import audit_service).
        from organizations.models import OrganizationMembership

        membership = OrganizationMembership.objects.filter(
            user_id=user.id,  # type: ignore[attr-defined]
            organization_id=organization_id,
        ).first()
        if membership is None:
            return AuditService.system_actor()
        return AuditService.actor_from_membership(membership)

    @staticmethod
    def actor_from_user_or_token(
        user_or_token: object,
        organization_id: int,
        single_use_token: object | None = None,
    ) -> ActorSnapshot:
        """Resolve a calendar service ``user_or_token`` value to an actor snapshot.

        The calendar services carry a ``user_or_token`` of ``User | str | SystemUser
        | None`` on their auth context. This maps each variant to the right actor:

        - ``User``      -> membership actor (or system, via actor_from_user)
        - ``SystemUser`` -> system-user actor with scopes
        - ``str``       -> a single-use CalendarManagementToken *code*. When the
          resolved token row is supplied via ``single_use_token`` (the calendar
          permission service resolves the code and exposes the row), attribute the
          action to that token (SINGLE_USE_CODE); otherwise fall back to system.
        - ``None``      -> system actor.

        Args:
            user_or_token: The context principal (User, SystemUser, token str, None).
            organization_id: The organization the action happens in.
            single_use_token: The resolved CalendarManagementToken row backing a
                ``str`` code, when available. Ignored for non-str principals.

        Returns:
            The most specific ActorSnapshot resolvable from the principal.
        """
        # Lazy imports for the same import-cycle reason as actor_from_user.
        from public_api.models import SystemUser
        from users.models import User

        if isinstance(user_or_token, User):
            return AuditService.actor_from_user(user_or_token, organization_id)
        if isinstance(user_or_token, SystemUser):
            return AuditService.actor_from_system_user(user_or_token)
        if isinstance(user_or_token, str) and single_use_token is not None:
            return AuditService.actor_from_single_use_code(single_use_token)
        return AuditService.system_actor()

    @staticmethod
    def subject_from_instance(instance: object, label: str | None = None) -> SubjectRef:
        """Build a SubjectRef from a Django model instance.

        Derives ``subject_type`` as ``"<app_label>.<ModelName>"`` and ``subject_id``
        from the instance pk, so call sites don't repeat the soft-reference shape.

        ``subject_label`` is left ``None`` unless a caller passes one. We deliberately
        do NOT default to ``str(instance)``: a model ``__str__`` can dereference
        related rows (e.g. a profile) and raise, and building the audit payload must
        never break the business action it describes. Pass a cheap label explicitly
        (a name already in memory) when a human-readable label is worthwhile.

        Args:
            instance: A Django model instance (must have ``_meta`` and ``pk``).
            label: Optional human-readable label; not auto-computed from ``str()``.

        Returns:
            A SubjectRef referencing the instance.
        """
        meta = instance._meta  # type: ignore[attr-defined]
        return SubjectRef(
            subject_type=f"{meta.app_label}.{instance.__class__.__name__}",
            subject_id=str(instance.pk),  # type: ignore[attr-defined]
            subject_label=label,
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
