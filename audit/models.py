"""Audit models.

`Audit` and `AuditAffectedMembership` are both OrganizationModel subclasses.
They are append-only by intent — the repository never calls update or delete.

Unscoped read access (Phase 3):
  OrganizationModel already provides `original_manager = models.Manager()` on
  every subclass.  Phase 3's DjangoORMAuditRepository and Phase 6's admin
  should use `Audit.original_manager.all()` (filtered by `organization_id`
  where needed) rather than the tenant-scoped `objects` manager, because
  staff context has no active-membership tenant scope.
"""

from typing import ClassVar

from django.db import models

from audit.constants import AuditAction, AuditActorType
from organizations.models import OrganizationForeignKey, OrganizationModel, OrganizationRole


class Audit(OrganizationModel):
    """Immutable audit record capturing an action taken by an actor on a subject.

    Every field is populated at emit time and never mutated afterwards.
    """

    # Duplicates BaseModel.created by design: created_at is append-only and
    # semantically clearer for audit ordering. Prefer created_at over created
    # when ordering or filtering Audit records.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    action = models.CharField(max_length=100, choices=AuditAction.choices, db_index=True)

    # --- actor snapshot ---
    actor_type = models.CharField(max_length=20, choices=AuditActorType.choices, db_index=True)
    actor_id = models.BigIntegerField(null=True, blank=True)  # null for SYSTEM
    # Snapshot of the membership role at emit time; null unless actor_type=MEMBERSHIP.
    # null=True on CharField is intentional: empty string and "no role" are distinct.
    actor_role = models.CharField(  # noqa: DJ001
        max_length=20,
        choices=OrganizationRole.choices,
        null=True,
        blank=True,
    )
    # list[str] of PublicAPIResources values; null unless actor_type=SYSTEM_USER.
    system_user_scopes = models.JSONField(null=True, blank=True)
    # Membership id snapshot (BigInt, not FK — soft ref consistent with snapshot model).
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
        # through_fields must name the concrete FK columns (_fk suffix) because
        # TenantSafeForeignKey contributes a ForeignObject (virtual) and a real
        # ForeignKey under <name>_fk; Django's M2M plumbing resolves ambiguity
        # by looking at ForeignKey fields on the through model.
        through_fields=("audit_fk", "membership_fk"),
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


class AuditAffectedMembership(OrganizationModel):
    """Through table linking an Audit to the OrganizationMembership(s) it affected."""

    audit = OrganizationForeignKey(
        Audit,
        on_delete=models.CASCADE,
        related_name="affected_membership_links",
    )
    # related_name uses a unique prefix to avoid clashes; "+" is not valid
    # because TenantSafeForeignKey generates "<related_name>_fk_rel" for the
    # concrete ForeignKey's reverse accessor.
    membership = OrganizationForeignKey(
        "organizations.OrganizationMembership",
        on_delete=models.CASCADE,
        related_name="audit_affected_links",
    )

    class Meta:
        constraints: ClassVar = [
            # Use the _fk concrete columns (OrganizationForeignKey generates <name>_fk).
            # organization is included per project convention (lead composite keys with org).
            # audit_fk already pins the org, so the uniqueness guarantee is equivalent.
            models.UniqueConstraint(
                fields=["organization", "audit_fk", "membership_fk"],
                name="uniq_audit_membership",
            ),
        ]
        indexes: ClassVar = [
            # Lead with organization per project convention for tenant-scoped tables.
            models.Index(fields=["organization", "membership_fk"]),
        ]

    def __str__(self) -> str:
        return (
            f"AuditAffectedMembership(audit={self.audit_fk_id}, membership={self.membership_fk_id})"
        )
