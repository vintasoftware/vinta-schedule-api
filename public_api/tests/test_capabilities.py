"""Tests for the reseller capability gate."""

import pytest
from graphql import GraphQLError
from model_bakery import baker
from rest_framework.exceptions import PermissionDenied

from organizations.models import Organization
from public_api.capabilities import assert_org_can_invite, assert_target_in_subtree


@pytest.mark.django_db
class TestAssertOrgCanInvite:
    """Unit tests for assert_org_can_invite gate."""

    def test_assert_org_can_invite_passes_when_flag_true(self):
        """assert_org_can_invite raises nothing when can_invite_organizations=True."""
        org = baker.make(Organization, can_invite_organizations=True)
        # Should not raise
        assert_org_can_invite(org)

    def test_assert_org_can_invite_raises_when_flag_false(self):
        """assert_org_can_invite raises PermissionDenied when can_invite_organizations=False."""
        org = baker.make(Organization, can_invite_organizations=False)
        with pytest.raises(PermissionDenied):
            assert_org_can_invite(org)

    def test_assert_org_can_invite_raises_with_meaningful_message(self):
        """The raised PermissionDenied carries a clear message."""
        org = baker.make(Organization, can_invite_organizations=False)
        with pytest.raises(PermissionDenied) as exc_info:
            assert_org_can_invite(org)
        assert "permission to invite" in str(exc_info.value.detail).lower()


@pytest.mark.django_db
class TestAssertTargetInSubtree:
    """Unit tests for assert_target_in_subtree tenant-isolation guard."""

    def test_acting_org_itself_passes(self):
        """assert_target_in_subtree(acting, acting) — acting org itself → no raise."""
        acting = baker.make(Organization)
        # Should not raise
        assert_target_in_subtree(acting, acting)

    def test_direct_child_passes(self):
        """A direct child of the acting org is in the subtree → no raise."""
        acting = baker.make(Organization)
        child = baker.make(Organization, parent=acting)
        # Should not raise
        assert_target_in_subtree(acting, child)

    def test_grandchild_passes(self):
        """A grandchild (deep descendant) is in the subtree → no raise."""
        acting = baker.make(Organization)
        child = baker.make(Organization, parent=acting)
        grandchild = baker.make(Organization, parent=child)
        # Should not raise
        assert_target_in_subtree(acting, grandchild)

    def test_unrelated_org_raises(self):
        """An org with no parent link to acting → raises GraphQLError."""
        acting = baker.make(Organization)
        unrelated = baker.make(Organization)
        with pytest.raises(GraphQLError):
            assert_target_in_subtree(acting, unrelated)

    def test_cross_reseller_tree_leak_raises(self):
        """Cross-reseller-tree isolation: R1 must not reach into R2's subtree.

        Build two independent reseller trees R1→R1_child and R2→R2_child.
        assert_target_in_subtree(R1, R2_child) must raise GraphQLError — this is
        the exact highest-risk failure mode for multi-tenant isolation.
        """
        r1 = baker.make(Organization, name="Reseller1")
        r1_child = baker.make(Organization, name="R1Child", parent=r1)  # noqa: F841
        r2 = baker.make(Organization, name="Reseller2")
        r2_child = baker.make(Organization, name="R2Child", parent=r2)

        with pytest.raises(GraphQLError):
            assert_target_in_subtree(r1, r2_child)

    def test_parent_cycle_terminates_with_graphql_error(self):
        """A parent cycle (A.parent=B, B.parent=A) must TERMINATE (raises GraphQLError, not hang).

        Build the cycle by saving A and B then setting parents and calling .save().
        The function must not loop indefinitely — the visited-set cycle guard must fire.
        """
        acting = baker.make(Organization, name="Acting")
        a = baker.make(Organization, name="CycleA")
        b = baker.make(Organization, name="CycleB")

        # Create a cycle: a.parent=b, b.parent=a
        a.parent = b
        a.save()
        b.parent = a
        b.save()

        # acting is unrelated to the cycle; walking up from 'a' hits the cycle and must terminate.
        with pytest.raises(GraphQLError):
            assert_target_in_subtree(acting, a)
