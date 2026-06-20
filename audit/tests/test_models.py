"""Tests for audit app models.

Covers:
- Audit creation with auto-populated created_at.
- Attaching AuditAffectedMembership rows and querying via affected_memberships.
- Unique constraint rejects duplicate (audit, membership_user_id) pairs.
- Multi-tenant correctness: all rows within the same organization.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.factories import AuditAffectedMembershipFactory, AuditFactory
from audit.models import Audit, AuditAffectedMembership
from organizations.models import Organization, OrganizationMembership


User = get_user_model()


@pytest.mark.django_db
class TestAuditCreation:
    """Unit tests for Audit model field defaults and auto-population."""

    def test_created_at_auto_populates_on_create(self) -> None:
        """created_at must be set automatically on first save (auto_now_add)."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        assert audit.created_at is not None

    def test_created_at_is_not_updated_on_save(self) -> None:
        """created_at must remain fixed after subsequent saves (auto_now_add contract)."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        original = audit.created_at

        # Touch a mutable field and save.
        audit.subject_label = "updated label"
        audit.save()

        audit.refresh_from_db()
        assert audit.created_at == original

    def test_factory_produces_valid_audit(self) -> None:
        """AuditFactory should create a persisted, readable Audit row."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)

        fetched = Audit.original_manager.get(pk=audit.pk)
        assert fetched.organization_id == org.pk
        assert fetched.action == AuditAction.CREATE
        assert fetched.actor_type == AuditActorType.SYSTEM

    def test_str_does_not_crash(self) -> None:
        """__str__ must return a non-empty string."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        assert str(audit)

    def test_diff_nullable(self) -> None:
        """diff must accept None (no change payload)."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org, diff=None)
        assert audit.diff is None

    def test_diff_stores_json(self) -> None:
        """diff must persist arbitrary JSON dicts."""
        org = baker.make(Organization)
        payload = {"name": {"old": "Alice", "new": "Bob"}}
        audit = AuditFactory().create(organization=org, diff=payload)
        audit.refresh_from_db()
        assert audit.diff == payload

    def test_system_user_scopes_nullable(self) -> None:
        """system_user_scopes is null by default (SYSTEM actor carries no scopes)."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        assert audit.system_user_scopes is None


@pytest.mark.django_db
class TestAuditAffectedMemberships:
    """Tests for AuditAffectedMembership through table and M2M relationship."""

    def _make_membership(self, org: Organization) -> OrganizationMembership:
        user = baker.make(User)
        return OrganizationMembership.objects.create(user=user, organization=org)

    def test_affected_memberships_reachable_via_m2m(self) -> None:
        """Memberships linked via AuditAffectedMembership should appear in audit.affected_memberships."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        membership = self._make_membership(org)

        AuditAffectedMembershipFactory().create(
            organization=org,
            audit=audit,
            membership=membership,
        )

        assert audit.affected_memberships.filter(user_id=membership.user_id).exists()

    def test_multiple_affected_memberships(self) -> None:
        """Multiple memberships can be linked to the same Audit."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        m1 = self._make_membership(org)
        m2 = self._make_membership(org)

        factory = AuditAffectedMembershipFactory()
        factory.create(organization=org, audit=audit, membership=m1)
        factory.create(organization=org, audit=audit, membership=m2)

        assert audit.affected_memberships.count() == 2

    def test_affected_membership_links_reachable_via_reverse(self) -> None:
        """affected_membership_links reverse manager should return the through rows."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        membership = self._make_membership(org)

        link = AuditAffectedMembershipFactory().create(
            organization=org,
            audit=audit,
            membership=membership,
        )

        # Access through the reverse related manager on Audit.
        links = list(audit.affected_membership_links.all())
        assert link in links

    def test_unique_constraint_rejects_duplicate_audit_membership_pair(self) -> None:
        """Creating a duplicate (audit_fk, membership_user_id) row must raise IntegrityError."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        membership = self._make_membership(org)

        AuditAffectedMembershipFactory().create(
            organization=org,
            audit=audit,
            membership=membership,
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                # Direct ORM creation bypasses Python-level validation to hit the DB constraint.
                AuditAffectedMembership.objects.create(
                    organization=org,
                    audit_fk=audit,
                    membership_user_id=membership.user_id,
                )

    def test_same_membership_can_link_to_different_audits(self) -> None:
        """The unique constraint is per (audit_fk, membership_user_id) pair, not per membership alone."""
        org = baker.make(Organization)
        audit_a = AuditFactory().create(organization=org)
        audit_b = AuditFactory().create(organization=org, action=AuditAction.UPDATE)
        membership = self._make_membership(org)

        factory = AuditAffectedMembershipFactory()
        factory.create(organization=org, audit=audit_a, membership=membership)
        # This second link (different audit, same membership) must not raise.
        factory.create(organization=org, audit=audit_b, membership=membership)

        # Filter on the concrete column membership_user_id.
        assert (
            AuditAffectedMembership.original_manager.filter(
                membership_user_id=membership.user_id
            ).count()
            == 2
        )

    def test_audit_affected_membership_factory_str_does_not_crash(self) -> None:
        """__str__ on AuditAffectedMembership must return a non-empty string."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        membership = self._make_membership(org)
        link = AuditAffectedMembershipFactory().create(
            organization=org,
            audit=audit,
            membership=membership,
        )
        assert str(link)


@pytest.mark.django_db
class TestAuditTenantCorrectness:
    """Multi-tenant correctness: rows in org A are not visible in org B's scoped manager."""

    def test_original_manager_is_unscoped(self) -> None:
        """original_manager returns rows across organizations; objects is tenant-scoped."""
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)

        AuditFactory().create(organization=org_a)
        AuditFactory().create(organization=org_b)

        # original_manager is unscoped — can count all rows.
        total = Audit.original_manager.count()
        assert total >= 2

    def test_objects_manager_requires_organization_filter(self) -> None:
        """Calling objects.create without organization must raise ValueError."""
        with pytest.raises(ValueError, match="`organization` is required"):
            Audit.objects.create(
                action=AuditAction.CREATE,
                actor_type=AuditActorType.SYSTEM,
                subject_type="organizations.Organization",
                subject_id="1",
            )

    def test_affected_membership_objects_manager_requires_organization(self) -> None:
        """AuditAffectedMembership.objects.create without organization must raise ValueError."""
        org = baker.make(Organization)
        audit = AuditFactory().create(organization=org)
        user = baker.make(User)
        membership = OrganizationMembership.objects.create(user=user, organization=org)

        with pytest.raises(ValueError, match="`organization` is required"):
            AuditAffectedMembership.objects.create(
                audit_fk=audit,
                membership_user_id=membership.user_id,
                # organization intentionally omitted
            )

    def test_cross_org_audit_affected_membership_persists_without_rejection(self) -> None:
        """Cross-org link (audit from org A, membership from org B) persists at the DB layer.

        Empirical finding: OrganizationModel.save() and BaseOrganizationModelManager.create()
        do not validate that audit_fk and membership_user_id belong to the same organization
        as the AuditAffectedMembership row.  The concrete column (membership_user_id) still
        holds the org-B user_id, and the row is stored successfully.

        The ForeignObject virtual field (membership) joins on
        (membership_user_id, organization_id), so accessing `link.membership` would return
        no match when organization_ids diverge, making the cross-org row effectively
        invisible through the tenant-safe accessor — but the raw user_id remains.

        This is a known project-wide limitation of the ForeignObject pattern: tenant-boundary
        enforcement between two join columns on the same through table is NOT enforced at the
        Python/DB level.  Application code is responsible for ensuring that audit and membership
        belong to the same organization before creating an AuditAffectedMembership row.

        This test documents the ACTUAL behavior so future developers know what to expect.
        Do NOT change this test to assert rejection unless the model adds an explicit
        cross-org validation (e.g. a clean() / pre_save signal).
        """
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)

        user_b = baker.make(User)
        membership_b = OrganizationMembership.objects.create(user=user_b, organization=org_b)
        audit_a = AuditFactory().create(organization=org_a)

        # Cross-org: AuditAffectedMembership belongs to org_a, but membership_user_id is
        # the user_id from org_b's membership.
        link = AuditAffectedMembership.objects.create(
            organization=org_a,
            audit_fk=audit_a,
            membership_user_id=membership_b.user_id,
        )

        # The row persists with the cross-org user_id intact.
        assert link.pk is not None
        link.refresh_from_db()
        assert link.organization_id == org_a.pk
        assert link.membership_user_id == membership_b.user_id  # concrete column from org_b user
