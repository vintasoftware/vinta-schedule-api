from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Q

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


# Ordered set of allowed ordering fields. Only these values are accepted in
# DjangoORMAuditRepository.query; anything else falls back to the default.
# Phase 6 (admin) must extend this whitelist if it needs to order by additional
# fields (e.g. action, actor_type); unknown orderings silently fall back to
# _DEFAULT_ORDERING rather than raising.
_ALLOWED_ORDERING_FIELDS: frozenset[str] = frozenset(
    [
        "created_at",
        "-created_at",
    ]
)
_DEFAULT_ORDERING: str = "-created_at"


class AuditRepository(abc.ABC):
    """Backend-agnostic interface for audit record storage.

    Read + append only. No update, no delete.
    """

    @abc.abstractmethod
    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record.

        Args:
            data: The record data to persist.

        Returns:
            The persisted AuditRecord with id and created_at populated.
        """
        ...

    @abc.abstractmethod
    def get(self, audit_id: int) -> AuditRecord | None:
        """Retrieve a single audit record by id.

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
        ordering: str = "-created_at",
    ) -> AuditPage:
        """Query audit records with filters, pagination, and ordering.

        Args:
            q: The query filter/search object.
            offset: Number of records to skip (default 0).
            limit: Maximum records to return (default 50).
            ordering: Field(s) to order by, with optional - prefix for descending
                (default "-created_at").

        Returns:
            AuditPage containing items and total count.
        """
        ...


class DjangoORMAuditRepository(AuditRepository):
    """ORM-backed implementation of AuditRepository.

    Uses Audit.original_manager (unscoped) for all reads so that staff admin
    context (which has no active-membership tenant scope) can read across
    organisations. Reads are then explicitly filtered by organization_id when
    the caller supplies one.

    Writes use Audit.objects.create (the tenant-scoped manager) with an
    explicit organization_id= / organization= kwarg, which satisfies the
    BaseOrganizationModelManager.create guard. The through-table rows are
    bulk-created via the same scoped manager passing organization_id explicitly.
    """

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add(self, data: AuditRecordData) -> AuditRecord:
        """Persist an audit record and its affected-membership links.

        Runs inside a single transaction.atomic() so the Audit row and its
        AuditAffectedMembership through rows are committed together or not at all.

        Duplicate membership ids in data.affected_membership_ids are silently
        deduplicated before bulk_create to avoid violating the unique constraint
        (organization, audit_fk, membership_fk).

        Diff invariant: diff is always either None or a NON-EMPTY dict.  An
        empty dict ({}) means "no changes" and is normalized to None here so
        that the has_diff filter (which uses diff__isnull) is meaningful.
        Phase 4's compute_diff returns None for no-change, so callers should
        rarely pass {} — but we normalize defensively.

        Returns:
            The persisted AuditRecord.
        """
        # Deferred import to avoid loading Django models before the app registry
        # is ready (audit/__init__.py is imported at app-load time and triggers
        # this module; model imports must not execute at that point).
        from audit.models import Audit, AuditAffectedMembership

        with transaction.atomic():
            # Create the Audit row.
            # Audit.objects.create requires organization_id or organization to be
            # supplied (BaseOrganizationModelManager.create guard). We pass
            # organization_id=data.organization_id which satisfies that check.
            # Normalize diff: empty dict → None so diff__isnull reflects has_diff.
            audit = Audit.objects.create(
                organization_id=data.organization_id,
                action=data.action,
                actor_type=data.actor.actor_type,
                actor_id=data.actor.actor_id,
                actor_role=data.actor.actor_role,
                system_user_scopes=data.actor.system_user_scopes,
                system_user_scoped_to_membership=data.actor.system_user_scoped_to_membership,
                subject_type=data.subject.subject_type,
                subject_id=data.subject.subject_id,
                subject_label=data.subject.subject_label,
                diff=(data.diff or None),
            )

            # Deduplicate membership ids to avoid hitting the unique constraint.
            unique_membership_ids = list(dict.fromkeys(data.affected_membership_ids))

            if unique_membership_ids:
                AuditAffectedMembership.objects.bulk_create(
                    [
                        AuditAffectedMembership(
                            organization_id=data.organization_id,
                            audit_fk=audit,
                            membership_fk_id=membership_id,
                        )
                        for membership_id in unique_membership_ids
                    ]
                )

        # Reload from DB with prefetched links to build the canonical DTO.
        # Use self.get() to avoid duplicating the fetch-and-map logic.
        result = self.get(audit.pk)
        if result is None:
            raise RuntimeError(f"Audit row {audit.pk} disappeared immediately after creation")
        return result

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

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str = "-created_at",
    ) -> AuditPage:
        """Query audit records with filters, pagination, and ordering.

        Starts from the unscoped manager so the repository can be used in staff
        admin without an active-membership tenant scope. The organization_id
        filter in AuditQuery narrows to a single tenant when supplied.

        Each AuditQuery field is applied only when non-None:
        - organization_id → organization_id=...
        - actions → action__in=...
        - actor_type → actor_type=...
        - actor_id → actor_id=...
        - subject_type → subject_type=...
        - subject_id → subject_id=...
        - affected_membership_id → filter via through-table reverse relation +
          .distinct() to avoid row multiplication from the JOIN.
        - created_after → created_at__gte=...
        - created_before → created_at__lt=...
        - has_diff True → diff__isnull=False; False → diff__isnull=True
        - search → Q(OR) across subject_type/subject_id/subject_label __icontains,
          and actor_id= when the search term is all-digits (non-numeric terms
          skip the integer column to avoid type errors).

        Ordering is whitelisted to _ALLOWED_ORDERING_FIELDS; invalid values fall
        back to _DEFAULT_ORDERING.

        total is counted on the fully-filtered queryset before pagination so
        callers always get the complete match count, not the page size.
        """
        from audit.models import Audit

        qs = Audit.original_manager.all()

        # --- filters ---
        if q.organization_id is not None:
            qs = qs.filter(organization_id=q.organization_id)

        if q.actions is not None:
            qs = qs.filter(action__in=q.actions)

        if q.actor_type is not None:
            qs = qs.filter(actor_type=q.actor_type)

        if q.actor_id is not None:
            qs = qs.filter(actor_id=q.actor_id)

        if q.subject_type is not None:
            qs = qs.filter(subject_type=q.subject_type)

        if q.subject_id is not None:
            qs = qs.filter(subject_id=q.subject_id)

        if q.affected_membership_id is not None:
            # Join via the through table's reverse relation. Use distinct() to
            # avoid duplicate Audit rows when a membership appears multiple times
            # (though the unique constraint prevents it in practice; safe guard).
            qs = qs.filter(
                affected_membership_links__membership_fk_id=q.affected_membership_id
            ).distinct()

        if q.created_after is not None:
            qs = qs.filter(created_at__gte=q.created_after)

        if q.created_before is not None:
            qs = qs.filter(created_at__lt=q.created_before)

        if q.has_diff is not None:
            # Relies on the diff invariant enforced by add(): diff is None or a
            # NON-EMPTY dict; empty dicts are normalized to None at write time.
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

        # --- total (before pagination) ---
        total = qs.count()

        # --- ordering ---
        safe_ordering = ordering if ordering in _ALLOWED_ORDERING_FIELDS else _DEFAULT_ORDERING
        qs = qs.order_by(safe_ordering)

        # --- pagination ---
        page_qs = qs[offset : offset + limit]

        # Prefetch affected_membership_links so _to_record doesn't N+1.
        page_qs = page_qs.prefetch_related("affected_membership_links")

        items = [self._to_record(audit) for audit in page_qs]
        return AuditPage(items=items, total=total)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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
            link.membership_fk_id for link in audit.affected_membership_links.all()
        )
        return AuditRecord(
            id=audit.pk,
            created_at=audit.created_at,
            organization_id=audit.organization_id,
            action=audit.action,
            actor=actor,
            subject=subject,
            affected_membership_ids=affected_membership_ids,
            diff=audit.diff,
        )
