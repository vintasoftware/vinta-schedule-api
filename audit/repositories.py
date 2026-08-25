from __future__ import annotations

import abc
import operator
import uuid
from collections.abc import Iterable, Iterator, Sequence
from functools import reduce
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.filtering import (
    DEFAULT_ORDERING,
    STABLE_ITERATION_ORDERING,
    apply_query,
    normalize_ordering,
)
from audit.types import (
    ActorSnapshot,
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    SubjectRef,
)


if TYPE_CHECKING:
    from audit.models import Audit


#: How many records ``iter_records`` pulls per round-trip when the caller does
#: not say. Big enough that walking a large log is not dominated by round-trips,
#: small enough that a page of records with diffs stays comfortably in memory.
DEFAULT_ITERATION_CHUNK_SIZE = 500

#: How many records the ORM repository upserts per ``bulk_create`` statement.
#: Bounds the size of a single ``INSERT ... ON CONFLICT`` and, with it, how long
#: one statement holds its locks.
DEFAULT_BATCH_SIZE = 500


def _or_group(conditions: Iterable[Q]) -> Q:
    """OR a series of Q objects into one, matching nothing when there are none.

    ``Q(pk__in=[])`` is the empty-set identity: an empty ``AuditQuery`` list
    field is an active filter that nothing satisfies, so folding zero conditions
    must produce "match nothing" rather than the bare ``Q()`` that would match
    everything.
    """
    conditions = list(conditions)
    if not conditions:
        return Q(pk__in=[])
    return reduce(operator.or_, conditions)


class AuditRepository(abc.ABC):
    """Backend-agnostic interface for audit record storage.

    Read + append only. No update, no delete — with one deliberate nuance: an
    append is an **upsert keyed on** ``AuditRecordData.uid``, not a blind insert.
    A record carries the same ``uid`` into every repository it is written to, so
    writing one twice — a retried Celery task, a re-run backfill, a replica
    catching up on records it already received — converges on the single
    existing record instead of appending a copy. That is what makes replication
    and sync safe to repeat, and it is a requirement of this interface, not an
    optimization an implementation may skip.

    ``query`` / ``count`` / ``iter_records`` all take the same ``AuditQuery``,
    so a caller can point the identical filter at any repository. The semantics
    of that filter are defined once in ``audit.filtering``; an implementation
    that pushes the filters down to its store must agree with them, and one that
    cannot push them down can implement ``query`` by handing its records to
    ``audit.filtering.apply_query``.
    """

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record, upserting on ``data.uid``.

        Args:
            data: The record data to persist.

        Returns:
            The persisted AuditRecord with id and created_at populated.
        """
        ...

    @abc.abstractmethod
    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Persist many audit records in as few round-trips as the backend allows.

        Same upsert-on-``uid`` contract as :meth:`add`, applied to a batch: the
        entry point for replication and for backfilling a repository from
        another one.

        Duplicate ``uid`` values *within* one call are the caller's problem to
        avoid; an implementation may collapse them or may raise.

        Args:
            data: The records to persist. May be empty.

        Returns:
            The persisted AuditRecords, in the order they were given.
        """
        ...

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve a single audit record by this backend's own id.

        Backend-local: the id of a record differs between repositories holding
        the same log. Use :meth:`get_by_uid` to look the same record up in more
        than one repository.

        Args:
            audit_id: The audit record id.

        Returns:
            The AuditRecord if found, None otherwise.
        """
        ...

    @abc.abstractmethod
    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
    ) -> AuditPage:
        """Query audit records with filters, pagination, and ordering.

        Args:
            q: The query filter/search object.
            offset: Number of records to skip (default 0).
            limit: Maximum records to return (default 50).
            ordering: Field or fields to order by, each with an optional ``-``
                prefix for descending. Values outside
                ``audit.filtering.ALLOWED_ORDERING_FIELDS`` are dropped rather
                than raising; if none survive the default ordering applies.

        Returns:
            AuditPage containing items and total count.
        """
        ...

    def get_by_uid(self, uid: uuid.UUID) -> AuditRecord | None:
        """Retrieve a single audit record by its cross-repository identity.

        The portable lookup: unlike :meth:`get`, the same argument finds the
        same record in every repository the record was written to. Implemented
        here in terms of :meth:`query` so every backend has it; override when
        the backend can index ``uid`` directly.

        Args:
            uid: The record's stable identity.

        Returns:
            The AuditRecord if found, None otherwise.
        """
        page = self.query(AuditQuery(uids=[uid]), offset=0, limit=1)
        return page.items[0] if page.items else None

    def count(self, q: AuditQuery) -> int:
        """Count records matching ``q`` without fetching them.

        Implemented here via a zero-length page, which every ``query`` already
        has to total correctly. Override when the backend can count without
        building a page.

        Args:
            q: The filters to count under.

        Returns:
            The number of matching records.
        """
        return self.query(q, offset=0, limit=0).total

    def iter_records(
        self,
        q: AuditQuery,
        *,
        chunk_size: int = DEFAULT_ITERATION_CHUNK_SIZE,
    ) -> Iterator[AuditRecord]:
        """Stream every record matching ``q``, oldest first, in bounded memory.

        The read side of replication and sync: a caller walks a whole log
        without ever holding more than ``chunk_size`` records at once.

        Ordering is fixed to ``audit.filtering.STABLE_ITERATION_ORDERING``
        (``created_at`` ascending, ``uid`` breaking ties) and is not a parameter,
        because the correctness of the walk depends on it. An audit log only
        appends, so ascending order puts rows written *during* the walk past the
        cursor rather than shifting rows already passed; the ``uid`` tiebreak
        stops same-instant records from reshuffling across a page boundary,
        which is how offset pagination loses a record.

        Args:
            q: The filters to walk under.
            chunk_size: Records to fetch per round-trip.

        Yields:
            Matching AuditRecords, oldest first.
        """
        offset = 0
        while True:
            page = self.query(
                q, offset=offset, limit=chunk_size, ordering=STABLE_ITERATION_ORDERING
            )
            if not page.items:
                return
            yield from page.items
            if len(page.items) < chunk_size:
                return
            offset += len(page.items)


class DjangoORMAuditRepository(AuditRepository):
    """ORM-backed implementation of AuditRepository.

    Uses Audit.original_manager (unscoped) for all reads so that staff admin
    context (which has no active-membership tenant scope) can read across
    organizations -- and so that a read never depends on an organization being
    bound to the context, which under STRICT_ORGANIZATION_FILTER would raise
    rather than return nothing. Reads are then explicitly filtered by
    organization_id when the caller supplies one.

    vinta-django-orgs keeps inserts unscoped. Writes go through
    Audit.objects.bulk_create with an explicit organization_id on every
    instance, so each row lands in the organization named by the caller even
    when a different organization -- or none -- is bound. The through-table rows
    are bulk-created the same way: every instance carries its own organization.

    Writes are upserts on the unique ``uid`` column, via
    ``bulk_create(update_conflicts=True)`` -> ``INSERT ... ON CONFLICT (uid) DO
    UPDATE``. Persisting the same record twice therefore rewrites the one row
    rather than appending a second, which is what lets the Celery task be
    retried and a backfill be re-run.
    """

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    #: Payload columns rewritten when an upsert hits an existing ``uid``.
    #: Everything a record carries except ``uid`` itself (the conflict target)
    #: and ``id`` (the row's own key).
    #:
    #: ``organization_id`` is deliberately NOT here. vinta-django-orgs refuses an
    #: ``update_conflicts`` that names it without ``unsafe_organization_update``,
    #: and it is right to: a conflicting write that could move a row to another
    #: tenant is a tenant-isolation hole, and no legitimate audit write needs it.
    #: A record's uid and its organization are both fixed at emit time, so a
    #: conflict on uid where the organization differs is a uuid collision or a
    #: corrupted feed -- something to leave alone and investigate, not to
    #: overwrite.
    _UPSERT_UPDATE_FIELDS = (
        "created_at",
        "action",
        "actor_type",
        "actor_id",
        "actor_role",
        "system_user_scopes",
        "system_user_scoped_to_membership",
        "subject_type",
        "subject_id",
        "subject_label",
        "diff",
    )

    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record and its affected-membership links.

        Upserts on ``data.uid``: see :meth:`bulk_add`, which this delegates to
        so the single-record and batch paths cannot drift apart.

        Returns:
            The persisted AuditRecord.
        """
        return self.bulk_add([data])[0]

    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Upsert a batch of audit records and their affected-membership links.

        Runs inside a single transaction.atomic() so the Audit rows and their
        AuditAffectedMembership through rows are committed together or not at
        all.

        The Audit rows go in with ``update_conflicts=True`` on ``uid``, so a
        record already present is rewritten in place. Postgres returns the ids
        of both inserted and updated rows, which is what lets the through rows
        below be attached without a second lookup.

        The through rows go in with ``ignore_conflicts=True`` against the
        ``(audit_fk, membership_user_id)`` unique constraint. Ignore rather than
        update because the pair *is* the whole row -- there is nothing left to
        rewrite -- and because it makes re-persisting a record leave its
        existing links untouched.

        Duplicate membership ids within one record are deduplicated before the
        write, since the unique constraint would otherwise reject the batch.

        Diff invariant: diff is always either None or a NON-EMPTY dict. An empty
        dict ({}) means "no changes" and is normalized to None here so that the
        has_diff filter (which uses diff__isnull) is meaningful. compute_diff
        returns None for no-change, so callers should rarely pass {} -- but we
        normalize defensively.

        Args:
            data: The records to upsert, in any order. May be empty.

        Returns:
            The persisted AuditRecords, in the order given.
        """
        # Deferred import to avoid loading Django models before the app registry
        # is ready (audit/__init__.py is imported at app-load time and triggers
        # this module; model imports must not execute at that point). Verified:
        # hoisting it raises `AppRegistryNotReady: Apps aren't loaded yet.` during
        # `django.setup()`. Every `audit.models` import in this file is late for
        # that one reason; imports of anything else belong at the top.
        from audit.models import Audit, AuditAffectedMembership

        if not data:
            return []

        # One clock reading for the whole batch, so records emitted together and
        # left without a created_at do not fan out over the write's duration.
        now = timezone.now()
        audits = [
            Audit(
                uid=item.uid,
                organization_id=item.organization_id,
                created_at=item.created_at or now,
                action=item.action,
                actor_type=item.actor.actor_type,
                actor_id=item.actor.actor_id,
                actor_role=item.actor.actor_role,
                system_user_scopes=item.actor.system_user_scopes,
                system_user_scoped_to_membership=item.actor.system_user_scoped_to_membership,
                subject_type=item.subject.subject_type,
                subject_id=item.subject.subject_id,
                subject_label=item.subject.subject_label,
                diff=(item.diff or None),
            )
            for item in data
        ]

        with transaction.atomic():
            Audit.objects.bulk_create(
                audits,
                batch_size=DEFAULT_BATCH_SIZE,
                update_conflicts=True,
                unique_fields=["uid"],
                update_fields=list(self._UPSERT_UPDATE_FIELDS),
            )

            links = [
                AuditAffectedMembership(
                    organization_id=item.organization_id,
                    audit_fk=audit,
                    membership_user_id=membership_id,
                )
                for item, audit in zip(data, audits, strict=True)
                # dict.fromkeys deduplicates while preserving order.
                for membership_id in dict.fromkeys(item.affected_membership_ids)
            ]
            if links:
                AuditAffectedMembership.objects.bulk_create(
                    links, batch_size=DEFAULT_BATCH_SIZE, ignore_conflicts=True
                )

        # Reload with prefetched links to build the canonical DTOs. Reading back
        # by uid (rather than mapping the in-memory instances) is what makes the
        # returned records reflect what is actually stored after the upsert --
        # including the links an already-present record brought with it.
        stored = {
            audit.uid: audit
            for audit in Audit.original_manager.prefetch_related(
                "affected_membership_links"
            ).filter(uid__in=[item.uid for item in data])
        }
        missing = [item.uid for item in data if item.uid not in stored]
        if missing:
            raise RuntimeError(f"Audit rows {missing} disappeared immediately after being written")
        return [self._to_record(stored[item.uid]) for item in data]

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve a single audit record by id, or None if not found.

        Uses the unscoped original_manager so staff admin (no active-membership
        context) can read any audit. Prefetches affected_membership_links to
        avoid N+1 when _to_record iterates through them.
        """
        from audit.models import Audit

        try:
            audit = Audit.original_manager.prefetch_related("affected_membership_links").get(
                pk=audit_id
            )
        except Audit.DoesNotExist:
            return None
        return self._to_record(audit)

    def get_by_uid(self, uid: uuid.UUID) -> AuditRecord | None:
        """Retrieve a single audit record by its cross-repository identity.

        Overrides the interface's query-based default to hit the unique ``uid``
        index directly.
        """
        from audit.models import Audit

        audit = (
            Audit.original_manager.prefetch_related("affected_membership_links")
            .filter(uid=uid)
            .first()
        )
        return self._to_record(audit) if audit is not None else None

    def count(self, q: AuditQuery) -> int:
        """Count matching records with a COUNT query and no row fetch."""
        return self._filtered_queryset(q).count()

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
    ) -> AuditPage:
        """Query audit records with filters, pagination, and ordering.

        Filtering is delegated to :meth:`_filtered_queryset`, which translates
        ``AuditQuery`` into SQL; the semantics it implements are the ones spelled
        out in ``audit.filtering``.

        Ordering is whitelisted by ``audit.filtering.normalize_ordering``;
        unknown fields are dropped and an ordering left with nothing falls back
        to newest-first.

        total is counted on the fully-filtered queryset before pagination so
        callers always get the complete match count, not the page size.
        """
        qs = self._filtered_queryset(q)

        # --- total (before pagination) ---
        total = qs.count()

        # --- ordering ---
        qs = qs.order_by(*normalize_ordering(ordering))

        # --- pagination ---
        page_qs = qs[offset : offset + limit]

        # Prefetch affected_membership_links so _to_record doesn't N+1.
        page_qs = page_qs.prefetch_related("affected_membership_links")

        items = [self._to_record(audit) for audit in page_qs]
        return AuditPage(items=items, total=total)

    def iter_records(
        self,
        q: AuditQuery,
        *,
        chunk_size: int = DEFAULT_ITERATION_CHUNK_SIZE,
    ) -> Iterator[AuditRecord]:
        """Stream matching records without re-counting the whole log per chunk.

        Same contract and same ordering as the interface's default; this exists
        only to drop a ``COUNT(*)`` that the default cannot avoid. ``query``
        totals the filtered set on every call, which a paginated read wants and
        a backfill does not: walking a million records in chunks of 500 would
        run two thousand counts over the same filter to produce a number nobody
        reads.
        """
        qs = self._filtered_queryset(q).order_by(*normalize_ordering(STABLE_ITERATION_ORDERING))
        offset = 0
        while True:
            page = list(
                qs[offset : offset + chunk_size].prefetch_related("affected_membership_links")
            )
            if not page:
                return
            yield from (self._to_record(audit) for audit in page)
            if len(page) < chunk_size:
                return
            offset += len(page)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _filtered_queryset(self, q: AuditQuery):
        """Translate an AuditQuery into a filtered (unordered) queryset.

        Starts from the unscoped manager so the repository can be used in staff
        admin without an active-membership tenant scope. The organization_id
        filter in AuditQuery narrows to a single tenant when supplied.

        Every list-valued field becomes an ``IN`` and is applied only when it is
        not None -- so ``[]`` reaches the database as ``IN ()`` and matches
        nothing, which is the documented meaning. The two composite filters
        (``actors``, ``subjects``) become an OR of equality pairs, because the
        identity they match is a pair rather than a column.

        Field by field:
        - organization_id → organization_id=...
        - uids → uid__in=...
        - actions → action__in=...
        - actor_types → actor_type__in=...
        - actors → OR of (actor_type=..., actor_id=...)
        - subject_types → subject_type__in=...
        - subjects → OR of (subject_type=..., subject_id=...)
        - affected_membership_ids → through-table reverse relation __in +
          .distinct(), which the JOIN makes necessary as soon as a record
          affects more than one of the named memberships.
        - created_after → created_at__gte=... (inclusive)
        - created_before → created_at__lt=... (exclusive)
        - has_diff True → diff__isnull=False; False → diff__isnull=True
        - search → Q(OR) across subject_type/subject_id/subject_label __icontains,
          and actor_id= when the search term is all-digits (non-numeric terms
          skip the integer column to avoid type errors).
        """
        from audit.models import Audit

        qs = Audit.original_manager.all()

        if q.organization_id is not None:
            qs = qs.filter(organization_id=q.organization_id)

        if q.uids is not None:
            qs = qs.filter(uid__in=q.uids)

        if q.actions is not None:
            qs = qs.filter(action__in=q.actions)

        if q.actor_types is not None:
            qs = qs.filter(actor_type__in=q.actor_types)

        if q.actors is not None:
            qs = qs.filter(
                _or_group(
                    Q(actor_type=actor.actor_type, actor_id=actor.actor_id) for actor in q.actors
                )
            )

        if q.subject_types is not None:
            qs = qs.filter(subject_type__in=q.subject_types)

        if q.subjects is not None:
            qs = qs.filter(
                _or_group(
                    Q(subject_type=subject.subject_type, subject_id=subject.subject_id)
                    for subject in q.subjects
                )
            )

        if q.affected_membership_ids is not None:
            # Join via the through table's reverse relation. distinct() because
            # a record affecting two of the named memberships would otherwise
            # come back twice -- once per matching through row.
            qs = qs.filter(
                affected_membership_links__membership_user_id__in=q.affected_membership_ids
            ).distinct()

        if q.created_after is not None:
            qs = qs.filter(created_at__gte=q.created_after)

        if q.created_before is not None:
            qs = qs.filter(created_at__lt=q.created_before)

        if q.has_diff is not None:
            # Relies on the diff invariant enforced by bulk_add(): diff is None
            # or a NON-EMPTY dict; empty dicts are normalized to None at write
            # time.
            qs = qs.filter(diff__isnull=not q.has_diff)

        if q.search is not None:
            term = q.search
            search_q = (
                Q(subject_type__icontains=term)
                | Q(subject_id__icontains=term)
                | Q(subject_label__icontains=term)
            )
            # actor_id is a BigIntegerField — only add the equality filter when
            # the search term looks like an integer, to avoid a DB type error.
            if term.isdigit():
                search_q |= Q(actor_id=int(term))
            qs = qs.filter(search_q)

        return qs

    def _to_record(self, audit: Audit) -> AuditRecord:
        """Map an Audit model instance to the portable AuditRecord DTO.

        Expects affected_membership_links to have been prefetched; if not,
        this will trigger a per-row query (N+1). Always call via a queryset
        that includes .prefetch_related("affected_membership_links").

        The affected_membership_ids list is sorted for stable comparisons in
        tests and callers.
        """
        actor = ActorSnapshot(
            actor_type=audit.actor_type,
            actor_id=audit.actor_id,
            actor_role=audit.actor_role,
            system_user_scopes=audit.system_user_scopes,
            system_user_scoped_to_membership=audit.system_user_scoped_to_membership,
        )
        subject = SubjectRef(
            subject_type=audit.subject_type,
            subject_id=audit.subject_id,
            subject_label=audit.subject_label,
        )
        # Access the prefetched reverse manager; .all() returns the cached result.
        affected_membership_ids = sorted(
            link.membership_user_id for link in audit.affected_membership_links.all()
        )
        return AuditRecord(
            id=audit.pk,
            uid=audit.uid,
            created_at=audit.created_at,
            organization_id=audit.organization_id,
            action=audit.action,
            actor=actor,
            subject=subject,
            affected_membership_ids=affected_membership_ids,
            diff=audit.diff,
        )


class InMemoryAuditRepository(AuditRepository):
    """Process-local AuditRepository, backed by a dict keyed on ``uid``.

    Exists for two reasons, both about the multi-repository design rather than
    about convenience:

    * It is the **second implementation** the interface was designed for. A
      replication or sync path that has only ever run ORM-to-ORM proves very
      little; wiring this one in as an additional repository exercises the parts
      that must not assume the ORM -- portable identity, portable filtering, an
      id space that is not the ORM's.
    * It is the reference for a real non-ORM backend. Every read goes through
      ``audit.filtering.apply_query``, so the filter semantics come out
      identical to the ORM repository's for free.

    Not durable and not shared between processes: a Celery worker and the web
    process each get their own. Use it in tests and in local development, never
    as somewhere production audit records are expected to survive.
    """

    def __init__(self) -> None:
        # Keyed on uid, which is what makes a write an upsert: the same record
        # written twice replaces its entry instead of adding one.
        self._records: dict[uuid.UUID, AuditRecord] = {}
        # Stands in for an autoincrement primary key, so `id` is this
        # repository's own and does not accidentally match the ORM's.
        self._next_id = 1

    def add(self, data: AuditRecordData) -> AuditRecord:
        """Upsert one record. See :meth:`bulk_add`."""
        return self.bulk_add([data])[0]

    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Upsert a batch of records, keyed on uid.

        A record whose uid is already present keeps the id it was first given
        (an id is stable within a repository) and has every other field
        rewritten from the incoming data.
        """
        now = timezone.now()
        results = []
        for item in data:
            existing = self._records.get(item.uid)
            if existing is None:
                record_id = self._next_id
                self._next_id += 1
            else:
                record_id = existing.id
            record = AuditRecord(
                id=record_id,
                uid=item.uid,
                created_at=item.created_at or now,
                organization_id=item.organization_id,
                action=item.action,
                actor=item.actor,
                subject=item.subject,
                affected_membership_ids=sorted(dict.fromkeys(item.affected_membership_ids)),
                diff=item.diff or None,
            )
            self._records[item.uid] = record
            results.append(record)
        return results

    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve by this repository's own id."""
        return next(
            (record for record in self._records.values() if record.id == audit_id),
            None,
        )

    def get_by_uid(self, uid: uuid.UUID) -> AuditRecord | None:
        """Retrieve by cross-repository identity — a dict lookup here."""
        return self._records.get(uid)

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = DEFAULT_ORDERING,
    ) -> AuditPage:
        """Filter, order and paginate via the shared pure-Python implementation."""
        return apply_query(self._records.values(), q, offset=offset, limit=limit, ordering=ordering)
