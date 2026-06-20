"""Tests for OrganizationMembership model additions (Phase 1)."""

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import (
    Organization,
    OrganizationMembership,
    OrganizationRole,
    get_active_organization_membership,
)


User = get_user_model()


@pytest.mark.django_db
class TestOrganizationMembershipIsActive:
    """Unit tests for the is_active field on OrganizationMembership."""

    def test_is_active_defaults_to_true(self):
        """A freshly created OrganizationMembership is active by default."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        assert membership.is_active is True

    def test_is_active_can_be_set_false(self):
        """is_active can be set to False to deactivate a membership."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        membership.is_active = False
        membership.save()

        refreshed = OrganizationMembership.objects.get(pk=membership.pk)
        assert refreshed.is_active is False

    def test_factory_can_produce_inactive_membership(self):
        """baker can create an OrganizationMembership with is_active=False."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            is_active=False,
        )
        assert membership.is_active is False

    def test_factory_produces_active_by_default(self):
        """baker creates an active membership when is_active is not specified."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = baker.make(OrganizationMembership, user=user, organization=org)
        assert membership.is_active is True


@pytest.mark.django_db
class TestInactiveMembershipGating:
    """Integration tests: inactive membership is treated as gated at tenant endpoints."""

    def _make_inactive_member_client(self):
        """Create a user with an inactive membership, return (user, APIClient)."""
        from users.factories import UserFactory

        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return user, org, client

    def _make_active_member_client(self):
        """Create a user with an active membership, return (user, org, APIClient)."""
        from users.factories import UserFactory

        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return user, org, client

    def test_inactive_membership_gets_empty_list_on_calendar_endpoint(self):
        """An inactive member gets an empty calendar list — not 500 or real data."""
        from calendar_integration.models import Calendar

        _user, org, client = self._make_inactive_member_client()
        baker.make(Calendar, organization=org)

        url = reverse("api:Calendars-list")
        response = client.get(url)

        # Clean response — empty list, not 500
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["results"] == []

    def test_active_membership_sees_calendars(self):
        """An active member can see their organization's calendars."""
        from calendar_integration.models import Calendar, CalendarOwnership

        user, org, client = self._make_active_member_client()
        calendar = baker.make(Calendar, organization=org)
        # Non-admin members only list calendars they own (owner-scoping).
        CalendarOwnership.objects.create(
            organization=org, calendar=calendar, membership_user_id=user.id
        )

        url = reverse("api:Calendars-list")
        response = client.get(url)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body["results"]) == 1

    def test_inactive_membership_denied_on_invitations_endpoint(self):
        """An inactive member is denied access to the invitations endpoint."""
        _user, _org, client = self._make_inactive_member_client()

        url = reverse("api:OrganizationInvitations-list")
        response = client.get(url)

        # OrganizationInvitationPermission now gates inactive members
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_reactivation_restores_access(self):
        """Reactivating a membership restores tenant access.

        Note: the client re-authenticates after reactivation so that the request
        user object does not carry a stale cached membership (Django caches the
        reverse OneToOne result on the user instance).
        """
        from calendar_integration.models import Calendar, CalendarOwnership

        user, org, client = self._make_inactive_member_client()
        calendar = baker.make(Calendar, organization=org)
        # Non-admin members only list calendars they own (owner-scoping).
        CalendarOwnership.objects.create(
            organization=org, calendar=calendar, membership_user_id=user.id
        )

        # Verify inactive = empty
        url = reverse("api:Calendars-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"] == []

        # Reactivate in the DB
        OrganizationMembership.objects.filter(user=user).update(is_active=True)

        # Re-authenticate with a fresh user instance so the cached membership is not stale
        user.refresh_from_db()
        client.force_authenticate(user=user)

        # Verify active = data visible
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()["results"]) == 1


@pytest.mark.django_db
class TestOrganizationParentAndCapabilities:
    """Unit tests for Phase 0 — parent FK and can_invite_organizations flag."""

    def test_can_invite_organizations_defaults_false(self):
        """A freshly created Organization has can_invite_organizations=False."""
        org = baker.make(Organization)
        assert org.can_invite_organizations is False

    def test_can_invite_organizations_can_be_set_true(self):
        """can_invite_organizations can be set to True (for resellers)."""
        org = baker.make(Organization, can_invite_organizations=True)
        assert org.can_invite_organizations is True

    def test_parent_defaults_null(self):
        """A freshly created Organization has parent=NULL."""
        org = baker.make(Organization)
        assert org.parent is None

    def test_parent_can_be_set_to_another_org(self):
        """parent can be set to another Organization (self-FK)."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller)
        assert child.parent == reseller

    def test_is_reseller_true_when_can_invite(self):
        """is_reseller() returns True when can_invite_organizations is True."""
        org = baker.make(Organization, can_invite_organizations=True)
        assert org.is_reseller() is True

    def test_is_reseller_false_by_default(self):
        """is_reseller() returns False for a newly created org (default flag=False)."""
        org = baker.make(Organization)
        assert org.is_reseller() is False

    def test_get_branding_root_returns_self_when_reseller(self):
        """get_branding_root() returns self when this org is a reseller."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        assert reseller.get_branding_root() == reseller

    def test_get_branding_root_returns_parent_when_child(self):
        """get_branding_root() returns the parent when parent is a reseller."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)
        assert child.get_branding_root() == reseller

    def test_get_branding_root_walks_up_chain_to_reseller(self):
        """get_branding_root() walks up the chain to find the reseller ancestor."""
        reseller = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)
        assert grandchild.get_branding_root() == reseller

    def test_get_branding_root_returns_none_when_no_reseller_ancestor(self):
        """get_branding_root() returns None when there is no reseller in the chain."""
        org = baker.make(Organization, can_invite_organizations=False)
        child = baker.make(Organization, parent=org, can_invite_organizations=False)
        assert child.get_branding_root() is None

    def test_get_branding_root_none_when_no_parent(self):
        """get_branding_root() returns None for a standalone non-reseller org."""
        org = baker.make(Organization, can_invite_organizations=False)
        assert org.get_branding_root() is None

    def test_parent_protect_prevents_deletion_of_reseller_with_children(self):
        """on_delete=PROTECT prevents deleting a reseller that has children."""
        from django.db import IntegrityError

        reseller = baker.make(Organization, can_invite_organizations=True)
        _child = baker.make(Organization, parent=reseller)

        with pytest.raises(IntegrityError):
            reseller.delete()

    def test_get_branding_root_handles_parent_cycle_without_hanging(self):
        """get_branding_root() terminates even when a parent cycle exists with no reseller.

        Creates a cycle A.parent=B, B.parent=A (both non-reseller), then asserts
        get_branding_root() returns None and does not hang indefinitely.
        """
        # Create two orgs first (must exist to reference each other)
        org_a = baker.make(Organization, can_invite_organizations=False)
        org_b = baker.make(Organization, can_invite_organizations=False)

        # Set up cycle: A.parent=B, B.parent=A
        org_a.parent = org_b
        org_a.save()
        org_b.parent = org_a
        org_b.save()

        # Should return None (no reseller in cycle) and terminate (not hang)
        result = org_a.get_branding_root()
        assert result is None

        # Also verify from org_b
        result = org_b.get_branding_root()
        assert result is None


@pytest.mark.django_db
class TestMultiOrgMembership:
    """Unit tests for Phase 1 — FK cardinality + unique constraint."""

    def test_user_can_hold_memberships_in_two_different_orgs(self):
        """A user may have OrganizationMembership rows in two distinct orgs."""
        user = baker.make(User)
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)

        m_a = OrganizationMembership.objects.create(user=user, organization=org_a)
        m_b = OrganizationMembership.objects.create(user=user, organization=org_b)

        assert OrganizationMembership.objects.filter(user=user).count() == 2
        assert m_a.organization == org_a
        assert m_b.organization == org_b

    def test_unique_constraint_rejects_duplicate_membership_in_same_org(self):
        """Creating a second membership for the same (user, organization) raises IntegrityError."""
        user = baker.make(User)
        org = baker.make(Organization)

        OrganizationMembership.objects.create(user=user, organization=org)

        with pytest.raises(IntegrityError):
            OrganizationMembership.objects.create(user=user, organization=org)

    def test_is_organization_admin_is_per_org(self):
        """is_organization_admin returns True only for the org where the user is ADMIN."""
        user = baker.make(User)
        org_admin = baker.make(Organization)
        org_member = baker.make(Organization)

        OrganizationMembership.objects.create(
            user=user, organization=org_admin, role=OrganizationRole.ADMIN, is_active=True
        )
        OrganizationMembership.objects.create(
            user=user, organization=org_member, role=OrganizationRole.MEMBER, is_active=True
        )

        assert user.is_organization_admin(org_admin) is True
        assert user.is_organization_admin(org_member) is False

    def test_is_organization_admin_inactive_membership_returns_false(self):
        """An inactive admin membership is not counted as admin access."""
        user = baker.make(User)
        org = baker.make(Organization)

        OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.ADMIN, is_active=False
        )

        assert user.is_organization_admin(org) is False

    def test_get_active_membership_ignores_inactive_membership_in_other_org(self):
        """With one active (org A) and one inactive (org B) membership, the active one wins."""
        user = baker.make(User)
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)

        active = OrganizationMembership.objects.create(
            user=user, organization=org_a, is_active=True
        )
        OrganizationMembership.objects.create(user=user, organization=org_b, is_active=False)

        resolved = get_active_organization_membership(user)

        assert resolved == active
        assert resolved.organization == org_a
