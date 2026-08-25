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

Multiple repositories
---------------------

The service holds one **main** repository and any number of **additional** ones,
each under a caller-chosen alias:

    AuditService(repository=orm_repo, additional_repositories={"warehouse": ...})

Every record is written to the main repository first; that write is the one the
audit trail's durability rests on. It is then *tentatively* replicated to each
additional repository -- best effort, one at a time, and a failure is logged and
swallowed rather than allowed to unwind the main write. A replica that misses
records is expected to happen and is repaired by ``sync_repository``, not by
failing the action that emitted the record.

Reads take a repository alias, so a caller can ask the same question of any of
them:

    service.query(AuditQuery(organization_id=org.id))                     # main
    service.query(AuditQuery(organization_id=org.id), repository="warehouse")

Nothing here duplicates records across repositories: every record carries a
``uid`` generated once at emit time, and every repository write is an upsert on
it, so replicating or re-syncing a record the target already holds converges on
the row it has.
"""

import dataclasses
import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated

from django.db import transaction
from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditActorType
from audit.exceptions import UnknownAuditRepositoryError
from audit.filtering import DEFAULT_ORDERING
from audit.repositories import (
    DEFAULT_ITERATION_CHUNK_SIZE,
    AuditRepository,
)
from audit.tasks import persist_audit_record
from audit.types import (
    ActorSnapshot,
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    AuditSyncResult,
    SubjectRef,
)
from organizations.authorization import membership_role_label
from organizations.models import OrganizationMembership
from public_api.models import SystemUser
from users.models import User


logger = logging.getLogger(__name__)


#: Alias of the main repository. Reserved: an entry under this key in
#: ``additional_repositories`` would make ``repository="main"`` ambiguous, so
#: the constructor drops it rather than letting the two disagree.
MAIN_REPOSITORY_ALIAS = "main"

#: Records handed to a target repository per ``bulk_add`` during a sync.
DEFAULT_SYNC_BATCH_SIZE = 500


class AuditService:
    """Service that records audit trail entries asynchronously via Celery.

    Actor context is captured synchronously at call time and serialized into
    the Celery task payload so the worker never re-reads mutable state that
    may have changed or been deleted by the time it runs.

    Writes land in the main repository and are then replicated, best effort, to
    each additional one; reads and syncs name whichever repository they want by
    alias. See the module docstring for the shape of that arrangement.
    """

    @inject
    def __init__(
        self,
        repository: Annotated[AuditRepository, Provide["audit_repository"]],
        additional_repositories: Annotated[
            Mapping[str, AuditRepository] | None,
            Provide["audit_additional_repositories"],
        ] = None,
    ) -> None:
        self.repository = repository
        # Copied, so a container-held mapping cannot be mutated through the
        # service, and stripped of MAIN_REPOSITORY_ALIAS, which the main
        # repository owns.
        self.additional_repositories: dict[str, AuditRepository] = {
            alias: repo
            for alias, repo in (additional_repositories or {}).items()
            if alias != MAIN_REPOSITORY_ALIAS
        }

    # ------------------------------------------------------------------
    # Repository selection
    # ------------------------------------------------------------------

    @property
    def repository_aliases(self) -> tuple[str, ...]:
        """Every alias :meth:`get_repository` accepts, main first."""
        return (MAIN_REPOSITORY_ALIAS, *self.additional_repositories)

    def get_repository(self, repository: str | None = None) -> AuditRepository:
        """Resolve a repository alias to the repository itself.

        Args:
            repository: An alias, or None / ``"main"`` for the main repository.

        Returns:
            The named repository.

        Raises:
            UnknownAuditRepositoryError: The alias is not configured. Deliberately
                not a silent fallback to the main repository: answering a
                question about one store with another store's data is worse than
                failing.
        """
        if repository is None or repository == MAIN_REPOSITORY_ALIAS:
            return self.repository
        try:
            return self.additional_repositories[repository]
        except KeyError:
            raise UnknownAuditRepositoryError(repository, self.repository_aliases) from None

    # ------------------------------------------------------------------
    # Actor builder helpers — capture snapshots SYNCHRONOUSLY
    # ------------------------------------------------------------------

    @staticmethod
    def actor_from_membership(membership: object) -> ActorSnapshot:
        """Build an ActorSnapshot from an OrganizationMembership.

        Captures the membership's role *label* at call time so the Celery task
        never needs to re-read a membership row that may have changed or been
        deleted.

        The label comes off ``organizations.manage_members``, which names the
        same set the retired ``membership.role`` column used to. The two
        published values (``"admin"`` / ``"member"``) are deliberately unchanged
        -- every ``audit_audit.actor_role`` row already on disk holds one of
        them, and ``AuditRepository.query`` matches the value exactly, so
        writing a new spelling would silently split the audit history in two.

        Args:
            membership: An OrganizationMembership instance.

        Returns:
            An ActorSnapshot with actor_type=MEMBERSHIP and actor_role set.
        """
        return ActorSnapshot(
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=membership.user_id,  # type: ignore[attr-defined]
            actor_role=membership_role_label(membership),  # type: ignore[arg-type]
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
            # Both identity and emit time are fixed HERE, synchronously, for the
            # same reason the actor snapshot is: they must describe the action,
            # not the worker that eventually writes it. The uid additionally
            # makes the write idempotent -- CELERY_TASK_ACKS_LATE means this
            # task can run twice, and with a uid the second run upserts the
            # record the first one wrote instead of appending a duplicate.
            uid=uuid.uuid7(),
            created_at=timezone.now(),
        )

        payload = self.serialize(data)

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

    @staticmethod
    def serialize(data: AuditRecordData) -> dict:
        """Reduce an AuditRecordData to the JSON-safe dict the Celery task takes.

        ``CELERY_TASK_SERIALIZER = "json"``, so the two fields that are not JSON
        scalars are converted by hand: ``uid`` to its string form and
        ``created_at`` to ISO 8601. ``audit.tasks.deserialize_record_data``
        is the inverse and the two must be changed together.

        Args:
            data: The record data to serialize.

        Returns:
            A dict that survives ``json.dumps`` / ``json.loads`` unchanged.
        """
        payload = dataclasses.asdict(data)
        payload["uid"] = str(data.uid)
        payload["created_at"] = data.created_at.isoformat() if data.created_at else None
        return payload

    def persist(self, data: AuditRecordData) -> AuditRecord:
        """Write one record to the main repository, then replicate it.

        The persistence boundary itself: called by ``persist_audit_record`` once
        the record has crossed into the worker. Splitting it from ``record()``
        keeps the "which repositories does a record go to" policy in the service
        rather than in the task.

        The main write is allowed to raise — the task above it decides what a
        failed audit write means. Replication is not: see :meth:`replicate`.

        Args:
            data: The record to persist.

        Returns:
            The record as stored in the main repository.
        """
        record = self.repository.add(data)
        self.replicate(record)
        return record

    def replicate(
        self, record: AuditRecord, *, targets: Sequence[str] | None = None
    ) -> dict[str, bool]:
        """Copy an already-persisted record into the additional repositories.

        Tentative by design. Each target is written independently and a failure
        is logged and swallowed, because the record is already durable in the
        main repository and an unreachable replica must not be able to fail —
        or, worse, roll back — the write that succeeded. Reconciling what a
        replica missed is :meth:`sync_repository`'s job.

        The write is an upsert on ``record.uid``, so replicating a record a
        target already holds is a no-op rather than a duplicate. That is what
        makes it safe to call this after a retried task, and what makes a later
        sync over the same window safe too.

        Args:
            record: The record as returned by the main repository, carrying the
                uid and created_at every copy must share.
            targets: Aliases to replicate to; defaults to every additional
                repository. Unknown aliases are reported as failures rather than
                raised, so one bad name cannot stop the others.

        Returns:
            Per-target success flags, keyed by alias.
        """
        aliases = tuple(targets) if targets is not None else tuple(self.additional_repositories)
        data = record.to_data()
        results: dict[str, bool] = {}
        for alias in aliases:
            try:
                self.get_repository(alias).bulk_add([data])
            except Exception:
                logger.exception(
                    "Failed to replicate audit record %s (action %r, organization %s) to "
                    "repository %r. The main repository still holds it; run "
                    "AuditService.sync_repository(%r) to reconcile.",
                    record.uid,
                    record.action,
                    record.organization_id,
                    alias,
                    alias,
                )
                results[alias] = False
            else:
                results[alias] = True
        return results

    # ------------------------------------------------------------------
    # Read path — every method takes the repository to read from
    # ------------------------------------------------------------------

    def get(self, audit_id: int, *, repository: str | None = None) -> AuditRecord | None:
        """Fetch a record by the named repository's own id.

        ``audit_id`` is backend-local: the same record has a different id in
        each repository. Reach for :meth:`get_by_uid` when the id has to mean
        the same thing in more than one of them.

        Args:
            audit_id: The record id, as assigned by the repository being read.
            repository: Alias of the repository to read from; None means main.

        Returns:
            The record if that repository holds it, else None.
        """
        return self.get_repository(repository).get(audit_id)

    def get_by_uid(self, uid: uuid.UUID, *, repository: str | None = None) -> AuditRecord | None:
        """Fetch a record by its cross-repository identity.

        Args:
            uid: The record's stable identity.
            repository: Alias of the repository to read from; None means main.

        Returns:
            The record if that repository holds it, else None.
        """
        return self.get_repository(repository).get_by_uid(uid)

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
        repository: str | None = None,
    ) -> AuditPage:
        """Run a filter against the named repository.

        The same ``AuditQuery`` means the same thing whichever repository it is
        pointed at — that contract lives in ``audit.filtering``.

        Args:
            q: The filters to apply.
            offset: Records to skip.
            limit: Maximum records to return.
            ordering: Ordering field(s); unknown values fall back to the default.
            repository: Alias of the repository to read from; None means main.

        Returns:
            The matching page.
        """
        return self.get_repository(repository).query(
            q, offset=offset, limit=limit, ordering=ordering
        )

    def count(self, q: AuditQuery, *, repository: str | None = None) -> int:
        """Count the records matching ``q`` in the named repository.

        Comparing this across two aliases is the cheap way to see how far a
        replica has drifted from the main repository.

        Args:
            q: The filters to count under.
            repository: Alias of the repository to read from; None means main.

        Returns:
            The number of matching records.
        """
        return self.get_repository(repository).count(q)

    def iter_records(
        self,
        q: AuditQuery,
        *,
        chunk_size: int = DEFAULT_ITERATION_CHUNK_SIZE,
        repository: str | None = None,
    ) -> Iterator[AuditRecord]:
        """Stream every record matching ``q`` from the named repository.

        Oldest first, in bounded memory. Use it to export or reconcile a whole
        log without paginating by hand.

        Args:
            q: The filters to walk under.
            chunk_size: Records per round-trip.
            repository: Alias of the repository to read from; None means main.

        Yields:
            Matching records, oldest first.
        """
        yield from self.get_repository(repository).iter_records(q, chunk_size=chunk_size)

    # ------------------------------------------------------------------
    # Sync / backfill
    # ------------------------------------------------------------------

    def sync_repository(
        self,
        target: str,
        *,
        source: str | None = None,
        query: AuditQuery | None = None,
        batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    ) -> AuditSyncResult:
        """Backfill one repository from another, batch by batch.

        The repair path for everything replication is allowed to lose: a target
        that was unreachable when a record was emitted, one added after the log
        already existed, one that fell behind during an incident.

        Safe to re-run and safe to overlap with live replication, because every
        write is an upsert on the record's ``uid``. Syncing a window that is
        already in step rewrites those records with identical content rather
        than duplicating them, so the conservative move — sync a wider window
        than you think you need — costs time and nothing else.

        Reads stream through ``iter_records`` (oldest first, bounded memory), so
        the whole log never has to fit in memory however large it is. A batch
        that fails is counted, its error recorded, and the walk continues: one
        bad chunk cannot strand every record behind it. Inspect
        ``AuditSyncResult.ok`` to find out whether a re-run is needed.

        Args:
            target: Alias of the repository to write into. Must be an additional
                repository — see the ValueError below.
            source: Alias of the repository to read from; None means main.
            query: Restrict the sync to part of the log — most usefully by
                ``organization_id`` or a ``created_after`` / ``created_before``
                window. None syncs everything the source holds.
            batch_size: Records per ``bulk_add`` against the target.

        Returns:
            An AuditSyncResult with per-run counts and any batch errors.

        Raises:
            UnknownAuditRepositoryError: Either alias is not configured.
            ValueError: ``target`` names the same repository as ``source``,
                which would rewrite a log with itself.
        """
        source_alias = source or MAIN_REPOSITORY_ALIAS
        source_repository = self.get_repository(source_alias)
        target_repository = self.get_repository(target)
        if source_repository is target_repository:
            raise ValueError(
                f"Cannot sync audit repository {target!r} from itself "
                f"(source {source_alias!r} resolves to the same repository)."
            )

        # Counters rather than a rebuilt AuditSyncResult per record: the result
        # is frozen, and a dataclasses.replace for every one of a million
        # records is a million allocations to produce one integer.
        read = 0
        written = 0
        failed = 0
        errors: list[str] = []
        batch: list[AuditRecordData] = []

        def flush() -> None:
            # `batch` is rebound rather than cleared in place: the target keeps a
            # reference to the list it was handed, and clearing would empty that
            # too.
            nonlocal batch, written, failed
            if not batch:
                return
            try:
                target_repository.bulk_add(batch)
            except Exception as exc:
                logger.exception(
                    "Audit sync %s -> %s: batch of %d records failed.",
                    source_alias,
                    target,
                    len(batch),
                )
                failed += len(batch)
                errors.append(f"{type(exc).__name__}: {exc}")
            else:
                written += len(batch)
            batch = []

        for record in source_repository.iter_records(query or AuditQuery(), chunk_size=batch_size):
            read += 1
            batch.append(record.to_data())
            if len(batch) >= batch_size:
                flush()
        flush()

        logger.info(
            "Audit sync %s -> %s finished: read=%d written=%d failed=%d.",
            source_alias,
            target,
            read,
            written,
            failed,
        )
        return AuditSyncResult(
            source=source_alias,
            target=target,
            read=read,
            written=written,
            failed=failed,
            errors=errors,
        )

    def sync_all_repositories(
        self,
        *,
        source: str | None = None,
        query: AuditQuery | None = None,
        batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    ) -> dict[str, AuditSyncResult]:
        """Backfill every additional repository from ``source``.

        A convenience over :meth:`sync_repository`; the source repository is
        skipped if it happens to be one of the additional ones.

        Args:
            source: Alias of the repository to read from; None means main.
            query: Restrict the sync to part of the log.
            batch_size: Records per ``bulk_add`` against each target.

        Returns:
            One AuditSyncResult per target, keyed by alias.
        """
        source_alias = source or MAIN_REPOSITORY_ALIAS
        return {
            alias: self.sync_repository(
                alias, source=source_alias, query=query, batch_size=batch_size
            )
            for alias in self.additional_repositories
            if alias != source_alias
        }
