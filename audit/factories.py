"""Factories for audit app models.

Use these in tests to create Audit and AuditAffectedMembership instances.
Always require `organization` explicitly — never default it so that missing-org
bugs fail loudly at test creation time.
"""

from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.models import Audit, AuditAffectedMembership


class AuditFactory:
    """Factory for creating Audit instances in tests."""

    def create(self, organization, **overrides) -> Audit:
        """Create and persist an Audit row for the given organization.

        Provides sensible defaults for required fields while allowing any
        field to be overridden via keyword arguments.

        Args:
            organization: The Organization instance this audit belongs to.
            **overrides: Any Audit field values to override the defaults.

        Returns:
            A persisted Audit instance.
        """
        defaults: dict = {
            "organization": organization,
            "action": AuditAction.CREATE,
            "actor_type": AuditActorType.SYSTEM,
            "actor_id": None,
            "subject_type": "organizations.Organization",
            "subject_id": str(organization.pk),
        }
        defaults.update(overrides)
        return baker.make(Audit, **defaults)


class AuditAffectedMembershipFactory:
    """Factory for creating AuditAffectedMembership through-table rows in tests.

    Memberships are identified by org-scoped user_id per the
    OrganizationMembershipForeignKey convention: the concrete column is
    membership_user_id, not membership_fk.
    """

    def create(self, organization, audit, membership, **overrides) -> AuditAffectedMembership:
        """Create and persist an AuditAffectedMembership row.

        Args:
            organization: The Organization instance (must match audit and membership).
            audit: The Audit instance this row links from.
            membership: The OrganizationMembership instance this row links to.
                        The membership's user_id is stored in membership_user_id
                        (OrganizationMembershipForeignKey convention).
            **overrides: Any additional field values to override.

        Returns:
            A persisted AuditAffectedMembership instance.
        """
        return AuditAffectedMembership.objects.create(
            organization=organization,
            audit_fk=audit,
            membership_user_id=membership.user_id,
            **overrides,
        )
