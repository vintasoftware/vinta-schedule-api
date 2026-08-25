from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime


@dataclass(frozen=True)
class ActorSnapshot:
    """Snapshot of actor context at the time the audit record was emitted.

    Captures mutable actor state synchronously (in the main request) so the
    Celery task never re-reads a changed membership role or system-user scopes.
    """

    actor_type: str
    actor_id: int | None
    actor_role: str | None = None
    system_user_scopes: list[str] | None = None
    # The org-scoped user_id of the membership this system-user token is scoped to
    # (OrganizationMembershipForeignKey convention: identity = (organization_id, user_id)).
    # Null when the system-user token is org-wide.
    system_user_scoped_to_membership: int | None = None

    @property
    def ref(self) -> ActorRef:
        """This actor's identity, snapshot fields dropped — ready to filter with."""
        return ActorRef(actor_type=self.actor_type, actor_id=self.actor_id)


@dataclass(frozen=True)
class ActorRef:
    """The identity half of an actor: what ``AuditQuery.actors`` matches on.

    An actor is identified by the *pair* ``(actor_type, actor_id)`` — ids are
    only unique within a type, so a membership 7 and a system user 7 are
    different actors. Both fields are required: ``actor_id=None`` means the
    actor genuinely has no id (a SYSTEM actor), not "any id of this type". To
    match every actor of a type regardless of id, use ``AuditQuery.actor_types``.
    """

    actor_type: str
    actor_id: int | None


@dataclass(frozen=True)
class SubjectKey:
    """The identity half of a subject: what ``AuditQuery.subjects`` matches on.

    ``SubjectRef`` minus the human-readable label, which is a snapshot rather
    than part of the subject's identity and must not participate in a filter.
    Both fields are required; to match every subject of a type, use
    ``AuditQuery.subject_types``.
    """

    subject_type: str
    subject_id: str


@dataclass(frozen=True)
class SubjectRef:
    """Soft reference to the subject of an audited action.

    Portable across any backend; survives row deletion; no ORM coupling.
    """

    subject_type: str
    subject_id: str
    subject_label: str | None = None

    @property
    def key(self) -> SubjectKey:
        """This subject's identity, label dropped — ready to filter with."""
        return SubjectKey(subject_type=self.subject_type, subject_id=self.subject_id)


@dataclass(frozen=True)
class AuditRecordData:
    """Portable audit record data, passed in Celery payload for persistence.

    This is what AuditService.record() constructs and enqueues to be persisted.
    """

    organization_id: int
    action: str
    actor: ActorSnapshot
    subject: SubjectRef
    # List of org-scoped user_ids identifying the OrganizationMemberships affected
    # by this action (OrganizationMembershipForeignKey convention: identity is
    # (organization_id, user_id), so these are user_ids, not membership PKs).
    affected_membership_ids: list[int] = field(default_factory=list)
    diff: dict | None = None
    # Stable identity of this record ACROSS repositories. Generated once, at emit
    # time, and carried unchanged into every backend the record is written to --
    # the main one and each replica. It is what makes every write an upsert:
    # re-persisting the same record (a retried Celery task, a re-run backfill)
    # targets the existing row instead of appending a duplicate. The ORM primary
    # key cannot serve this purpose: it is assigned per backend, so two copies of
    # the same record hold different ids.
    #
    # Version 7, so the value is time-ordered: the unique index every repository
    # keeps on this column takes its inserts at the index's right edge rather
    # than scattered across it.
    uid: uuid.UUID = field(default_factory=uuid.uuid7)
    # Emit time, captured synchronously in AuditService.record(). None means "let
    # the repository stamp its own clock", which is only correct for the FIRST
    # write of a record -- a replica must reuse the value the record already
    # carries, otherwise the copies drift apart and no longer compare equal.
    created_at: datetime | None = None


@dataclass(frozen=True)
class AuditRecord:
    """Complete audit record returned by the repository (includes id + created_at).

    Flattened representation — all fields from AuditRecordData plus id and
    created_at. The repository maps between the ORM model and this DTO.
    """

    id: int
    created_at: datetime
    organization_id: int
    action: str
    actor: ActorSnapshot
    subject: SubjectRef
    # List of org-scoped user_ids identifying the affected OrganizationMemberships.
    # These are user_ids (not membership PKs) per the OrganizationMembershipForeignKey
    # convention: membership identity = (organization_id, user_id).
    affected_membership_ids: list[int] = field(default_factory=list)
    diff: dict | None = None
    # Cross-repository identity — see AuditRecordData.uid. `id` is the backend's
    # own key and differs between copies; `uid` is the same in all of them.
    uid: uuid.UUID = field(default_factory=uuid.uuid7)

    def to_data(self) -> AuditRecordData:
        """Reduce this record back to the portable input DTO, identity intact.

        Used by the replication and sync paths: writing the result into another
        repository must preserve ``uid`` (so the write upserts rather than
        appends) and ``created_at`` (so the copy carries the original emit time
        rather than the replica's write time). ``id`` is deliberately dropped —
        it belongs to the repository this record was read from.
        """
        return AuditRecordData(
            organization_id=self.organization_id,
            action=self.action,
            actor=self.actor,
            subject=self.subject,
            affected_membership_ids=list(self.affected_membership_ids),
            diff=self.diff,
            uid=self.uid,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class AuditQuery:
    """Backend-agnostic filter/search object for repository reads.

    Every repository — ORM-backed or not — accepts this same object and must
    give it the same meaning. ``audit.filtering`` holds the reference
    implementation of those semantics in pure Python; the ORM repository
    translates them to SQL. When a field is added here, both must learn it.

    **Every filter is a set membership test** (SQL's ``IN``), so one query
    covers "this one action" and "any of these six" without a second field per
    filter. Match one value by passing a one-element list.

    Combination rules, uniformly:

    * Fields AND together — a record must satisfy every non-None field.
    * Values within a field OR together.
    * ``None`` means "this filter is not active".
    * An ``[]`` EMPTY list is an active filter that nothing satisfies, exactly
      like SQL's ``IN ()``. It is not the same as ``None``, and the difference
      matters: code that builds a filter from a computed set gets an empty
      result rather than silently querying everything.

    The two exceptions to the list shape are the ones where a set makes no
    sense: the ``created_after`` / ``created_before`` range, and the tri-state
    ``has_diff`` / free-text ``search``.
    """

    # The tenant scope, deliberately singular: a read either stays inside one
    # organization or (with None) deliberately spans every one of them, and
    # there is no use case between those two that a list would serve.
    organization_id: int | None = None
    # Cross-repository identities. Used by the sync path to ask a target
    # repository which of a batch it already holds.
    uids: list[uuid.UUID] | None = None
    # action IN (...)
    actions: list[str] | None = None
    # actor_type IN (...) — every actor of these kinds, whatever their id.
    actor_types: list[str] | None = None
    # (actor_type, actor_id) IN (...) — these specific actors. Ids are unique
    # only within a type, which is why the pair travels together.
    actors: list[ActorRef] | None = None
    # subject_type IN (...) — every subject of these kinds, whatever their id.
    subject_types: list[str] | None = None
    # (subject_type, subject_id) IN (...) — these specific subjects.
    subjects: list[SubjectKey] | None = None
    # Records affecting ANY of these memberships. Per the
    # OrganizationMembershipForeignKey convention a membership is identified by
    # (organization_id, user_id), so these are user_ids, not membership PKs.
    affected_membership_ids: list[int] | None = None
    # Half-open range [created_after, created_before): the lower bound is
    # inclusive and the upper exclusive, so consecutive windows tile without
    # overlapping or dropping the record that lands exactly on a boundary.
    # That is what lets a sync walk a log in windows.
    created_after: datetime | None = None
    created_before: datetime | None = None
    # True: only records carrying a diff. False: only records without one.
    has_diff: bool | None = None
    # Free-text, case-insensitive, across the subject columns (plus actor_id
    # when the term is numeric) — see audit.filtering for the exact set.
    search: str | None = None

    def narrowed_to_uids(self, uids: list[uuid.UUID]) -> AuditQuery:
        """Return a copy of this query additionally restricted to ``uids``."""
        return replace(self, uids=list(uids))


@dataclass(frozen=True)
class AuditPage:
    """Paginated audit records returned by query."""

    items: list[AuditRecord]
    total: int


@dataclass(frozen=True)
class AuditSyncResult:
    """Outcome of backfilling one repository from another.

    Returned by ``AuditService.sync_repository``. ``written`` counts records
    handed to the target's ``bulk_add`` — because that write is an upsert,
    it counts records *reconciled*, not rows inserted, and re-running a
    completed sync reports the same number again with no duplicates created.
    """

    source: str
    target: str
    read: int = 0
    written: int = 0
    failed: int = 0
    # One entry per batch that raised, in the order the batches ran. A sync
    # keeps going after a failed batch so one bad chunk cannot strand the rest.
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every record read was written."""
        return self.failed == 0
