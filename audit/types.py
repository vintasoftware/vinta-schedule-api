from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class SubjectRef:
    """Soft reference to the subject of an audited action.

    Portable across any backend; survives row deletion; no ORM coupling.
    """

    subject_type: str
    subject_id: str
    subject_label: str | None = None


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


@dataclass(frozen=True)
class AuditQuery:
    """Filter/search object for repository queries.

    All fields are optional; only non-None fields participate in the query.
    The repository translates this into backend-specific filtering.
    """

    organization_id: int | None = None
    actions: list[str] | None = None
    actor_type: str | None = None
    actor_id: int | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    # Org-scoped user_id identifying the affected membership to filter by.
    # Per the OrganizationMembershipForeignKey convention, a membership is
    # identified by (organization_id, user_id); this field carries the user_id.
    affected_membership_id: int | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    has_diff: bool | None = None
    search: str | None = None


@dataclass(frozen=True)
class AuditPage:
    """Paginated audit records returned by query."""

    items: list[AuditRecord]
    total: int
