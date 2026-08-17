"""Audit models.

`Audit` and `AuditAffectedMembership` are both organization-scoped
(`SingleOrganizationModelMixin`). They are append-only by intent — the
repository never calls update or delete.

Unscoped read access:
  `SingleOrganizationModelMixin` provides `original_manager` (the package's
  `SingleOrganizationUnscopedManager`) on every subclass. The audit repository
  and admin use `Audit.original_manager.all()` (filtered by `organization_id`
  where needed) rather than the implicitly-scoped `objects` manager, because
  staff context has no active-membership tenant scope and, under
  `STRICT_ORGANIZATION_FILTER`, an unbound read through `objects` raises.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models

from vinta_orgs.mixins import SingleOrganizationModelMixin

from audit.constants import AuditAction, AuditActorType
from common.fields import OrganizationMembershipForeignKey, OrganizationSafeForeignKey
from common.managers import OrganizationScopedManager
from common.models import BaseModel, SafeRelationNullInitMixin
from organizations.authorization import (
    MEMBERSHIP_ROLE_LABEL_ADMIN,
    MEMBERSHIP_ROLE_LABEL_MEMBER,
)


class Audit(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
    """Immutable audit record capturing an action taken by an actor on a subject.

    Every field is populated at emit time and never mutated afterwards.
    """

    objects: ClassVar[OrganizationScopedManager] = OrganizationScopedManager()

    # Duplicates BaseModel.created by design: created_at is append-only and
    # semantically clearer for audit ordering. Prefer created_at over created
    # when ordering or filtering Audit records.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    action = models.CharField(max_length=100, choices=AuditAction, db_index=True)

    # --- actor snapshot ---
    actor_type = models.CharField(max_length=20, choices=AuditActorType, db_index=True)
    actor_id = models.BigIntegerField(null=True, blank=True)  # null for SYSTEM
    # Snapshot of the membership's role *label* at emit time; null unless
    # actor_type=MEMBERSHIP. null=True on CharField is intentional: empty string
    # and "no role" are distinct.
    #
    # The choices used to be a ``TextChoices`` enum on ``organizations``,
    # deleted along with the ``role`` column.
    # The two published values are unchanged -- see
    # ``organizations.authorization.membership_role_label``, which derives them
    # from ``organizations.manage_members`` -- because every row already on disk
    # holds one of them and ``AuditRepository.query`` matches the value exactly.
    actor_role = models.CharField(  # noqa: DJ001
        max_length=20,
        choices=[
            (MEMBERSHIP_ROLE_LABEL_MEMBER, "Member"),
            (MEMBERSHIP_ROLE_LABEL_ADMIN, "Admin"),
        ],
        null=True,
        blank=True,
    )
    # list[str] of PublicAPIResources values; null unless actor_type=SYSTEM_USER.
    system_user_scopes = models.JSONField(null=True, blank=True)
    # Org-scoped user_id snapshot of the scoped-to membership (BigInt, not FK —
    # soft ref consistent with snapshot model). Identifies the membership via
    # OrganizationMembershipForeignKey convention (org_id + user_id).
    # Null when the system-user token is org-wide.
    system_user_scoped_to_membership = models.BigIntegerField(null=True, blank=True)

    # --- subject (soft reference — no DB FK; survives row deletion) ---
    subject_type = models.CharField(max_length=255, db_index=True)  # "app_label.ModelName"
    subject_id = models.CharField(max_length=255, db_index=True)  # string for PK-shape portability
    # Human-readable snapshot; null=True intentional (absent = label not captured at emit time).
    subject_label = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001

    # --- payload ---
    # {field: {"old": ..., "new": ...}}; null unless action=UPDATE or caller provides one.
    diff = models.JSONField(null=True, blank=True)

    affected_memberships = models.ManyToManyField(
        "organizations.OrganizationMembership",
        through="audit.AuditAffectedMembership",
        # Both ends name the organization-carrying relation, never a ``_fk``
        # column. The target end is the ``OrganizationMembershipForeignKey``
        # descriptor ``membership`` (the CalendarOwnership convention); the
        # source end is the ``OrganizationSafeForeignKey`` descriptor ``audit``,
        # so Audit -> AuditAffectedMembership joins on
        # ``(audit_fk, organization)`` rather than on the key alone. Naming
        # ``audit_fk`` here would drop the organization out of the ``ON`` clause,
        # and the related manager an M2M builds is *not* organization-scoped
        # (see ``common.managers.OrganizationScopedManager``), so nothing else
        # would put it back. ``Calendar.memberships`` is spelled the same way.
        through_fields=("audit", "membership"),
        related_name="+",
        blank=True,
    )

    class Meta:
        indexes: ClassVar = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["organization", "action", "created_at"]),
            models.Index(fields=["organization", "actor_type", "actor_id"]),
            models.Index(fields=["organization", "subject_type", "subject_id"]),
        ]

    def __str__(self) -> str:
        return f"Audit({self.action}, {self.actor_type}, {self.subject_type}:{self.subject_id})"


class AuditAffectedMembership(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
    """Through table linking an Audit to the OrganizationMembership(s) it affected.

    Audit is APPEND-ONLY and must SURVIVE membership deletion.  We deliberately
    do NOT add the per-table raw-SQL composite FK that CalendarOwnership uses
    (PROTECT).  The ForeignObject carries no DB constraint; membership deletion
    never blocks on or cascades to audit rows.
    """

    objects: ClassVar[OrganizationScopedManager] = OrganizationScopedManager()

    audit = OrganizationSafeForeignKey(
        Audit,
        on_delete=models.CASCADE,
        related_name="affected_membership_links",
    )
    # OrganizationMembershipForeignKey contributes:
    #   - membership_user_id (BigIntegerField) — concrete column, the org-scoped user_id
    #   - membership (ForeignObject) — joins (organization_id, membership_user_id) →
    #     OrganizationMembership(organization_id, user_id)
    # on_delete=DO_NOTHING: no DB constraint is added (audit is append-only and
    # must survive membership deletion). PROTECT integrity from the raw-SQL composite
    # FK used by CalendarOwnership is intentionally OMITTED here.
    membership = OrganizationMembershipForeignKey(
        on_delete=models.DO_NOTHING,
        related_name="audit_affected_links",
        null=False,
        blank=False,
    )

    if TYPE_CHECKING:
        # ``OrganizationMembershipForeignKey`` contributes this column in
        # ``contribute_to_class``, which django-stubs' plugin does not follow --
        # so every read of it (here, and in ``audit.repositories``) is an
        # ``attr-defined`` error without this declaration. Annotation only: an
        # annotation with no value is not a Django field, and this block does
        # not run at all.
        membership_user_id: int

    class Meta:
        constraints: ClassVar = [
            # (audit_fk, membership_user_id) is the unique pair — audit_fk pins org,
            # mirroring the CalendarOwnership precedent (calendar_fk, membership_user_id).
            # null=False on membership_user_id means no partial condition is needed.
            models.UniqueConstraint(
                fields=["audit_fk", "membership_user_id"],
                name="uniq_audit_membership",
            ),
        ]
        indexes: ClassVar = [
            # Lead with organization per project convention for tenant-scoped tables.
            models.Index(
                fields=["organization", "membership_user_id"],
                name="auditaffected_org_member_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AuditAffectedMembership(audit={self.audit_fk_id}, membership_user_id={self.membership_user_id})"
