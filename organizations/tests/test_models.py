"""Tests for OrganizationMembership model additions."""

import django.db.transaction
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from common.organization_services import memberships
from organizations.models import (
    ExternalEventUpdatePolicy,
    Organization,
    OrganizationMembership,
    WeekStart,
)
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups


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
    """Unit tests for the parent FK and can_invite_organizations flag."""

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

    def test_get_branding_root_returns_self_when_no_parent(self):
        """get_branding_root() returns itself for a standalone (parentless)
        non-reseller org -- resolution was widened beyond resellers to any
        parentless organization. The reseller branch above is checked first and
        unchanged; this is the new fallback for the case that used to return None."""
        org = baker.make(Organization, can_invite_organizations=False)
        assert org.get_branding_root() == org

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
    """Unit tests for FK cardinality + unique constraint."""

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

        grant_membership_groups(
            OrganizationMembership.objects.create(
                user=user, organization=org_admin, is_active=True
            ),
            [GROUP_ORGANIZATION_ADMIN],
        )
        OrganizationMembership.objects.create(user=user, organization=org_member, is_active=True)

        assert user.is_organization_admin(org_admin) is True
        assert user.is_organization_admin(org_member) is False

    def test_is_organization_admin_inactive_membership_returns_false(self):
        """An inactive admin membership is not counted as admin access."""
        user = baker.make(User)
        org = baker.make(Organization)

        grant_membership_groups(
            OrganizationMembership.objects.create(user=user, organization=org, is_active=False),
            [GROUP_ORGANIZATION_ADMIN],
        )

        assert user.is_organization_admin(org) is False

    def test_resolver_ignores_inactive_membership_in_other_org(self):
        """With one active (org A) and one inactive (org B) membership, the active one wins."""
        user = baker.make(User)
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)

        active = OrganizationMembership.objects.create(
            user=user, organization=org_a, is_active=True
        )
        OrganizationMembership.objects.create(user=user, organization=org_b, is_active=False)

        resolved = memberships.resolve_for_user(user)

        assert resolved == active
        assert resolved.organization == org_a


# The composite-primary-key tests that lived here are gone with the composite
# primary key itself -- a ``ManyToManyField`` cannot hang off a composite-PK
# model, and the package's abstract membership base declares two. Their
# replacements, covering the surrogate ``id`` and the
# ``uniq_membership_user_organization`` constraint that outlived both primary
# keys, are in ``organizations/tests/test_membership_pk.py``.


@pytest.mark.django_db
class TestExternalEventUpdatePolicy:
    """Unit tests for external_event_update_policy field on Organization."""

    def test_default_is_change_request(self):
        """A freshly created Organization has external_event_update_policy=CHANGE_REQUEST."""
        org = baker.make(Organization)
        assert org.external_event_update_policy == ExternalEventUpdatePolicy.CHANGE_REQUEST
        assert org.external_event_update_policy == "change_request"

    def test_choices_are_allow_change_request_forbidden(self):
        """ExternalEventUpdatePolicy has exactly the three expected choices."""
        assert set(ExternalEventUpdatePolicy.choices) == {
            ("allow", "Allow direct updates"),
            ("change_request", "Updates create change requests"),
            ("forbidden", "Updates are forbidden"),
        }

    def test_choices_have_correct_values(self):
        """ExternalEventUpdatePolicy members have the correct values."""
        assert ExternalEventUpdatePolicy.ALLOW == "allow"
        assert ExternalEventUpdatePolicy.CHANGE_REQUEST == "change_request"
        assert ExternalEventUpdatePolicy.FORBIDDEN == "forbidden"

    def test_can_set_to_allow(self):
        """external_event_update_policy can be set to ALLOW."""
        org = baker.make(Organization, external_event_update_policy=ExternalEventUpdatePolicy.ALLOW)
        org.refresh_from_db()
        assert org.external_event_update_policy == ExternalEventUpdatePolicy.ALLOW

    def test_can_set_to_forbidden(self):
        """external_event_update_policy can be set to FORBIDDEN."""
        org = baker.make(
            Organization, external_event_update_policy=ExternalEventUpdatePolicy.FORBIDDEN
        )
        org.refresh_from_db()
        assert org.external_event_update_policy == ExternalEventUpdatePolicy.FORBIDDEN

    def test_can_set_to_change_request(self):
        """external_event_update_policy can be set to CHANGE_REQUEST."""
        org = baker.make(
            Organization, external_event_update_policy=ExternalEventUpdatePolicy.CHANGE_REQUEST
        )
        org.refresh_from_db()
        assert org.external_event_update_policy == ExternalEventUpdatePolicy.CHANGE_REQUEST


@pytest.mark.django_db
class TestWeekStart:
    """Unit tests for week_start field on Organization."""

    def test_default_is_monday(self):
        """A freshly created Organization has week_start=MONDAY."""
        org = baker.make(Organization)
        assert org.week_start == WeekStart.MONDAY
        assert org.week_start == "monday"

    def test_choices_are_monday_sunday(self):
        """WeekStart has exactly the two expected choices."""
        assert set(WeekStart.choices) == {
            ("monday", "Monday"),
            ("sunday", "Sunday"),
        }

    def test_choices_have_correct_values(self):
        """WeekStart members have the correct values."""
        assert WeekStart.MONDAY == "monday"
        assert WeekStart.SUNDAY == "sunday"

    def test_can_set_to_sunday(self):
        """week_start can be set to SUNDAY."""
        org = baker.make(Organization, week_start=WeekStart.SUNDAY)
        org.refresh_from_db()
        assert org.week_start == WeekStart.SUNDAY

    def test_can_set_to_monday(self):
        """week_start can be set to MONDAY."""
        org = baker.make(Organization, week_start=WeekStart.MONDAY)
        org.refresh_from_db()
        assert org.week_start == WeekStart.MONDAY

    def test_db_default_applies_to_existing_organizations(self):
        """An Organization row inserted without specifying week_start reads Monday.

        This test verifies the DB-level default backfills for existing rows,
        ensuring that rows created before the migration (via raw SQL or the
        old schema) read the correct Monday default after migration 0018.
        """
        from django.db import connection

        # Insert an Organization row without specifying week_start, so the
        # Postgres db_default applies. This simulates a row created before
        # the migration. Include all required columns to satisfy NOT NULL constraints.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO organizations_organization
                (name, slug, should_sync_rooms, external_event_update_policy, created, modified, can_invite_organizations)
                VALUES (%s, %s, %s, %s, NOW(), NOW(), %s)
                """,
                ["Pre-migration Org", "pre-migration-org", False, "change_request", False],
            )

        # Read the row back via the ORM and verify week_start is Monday.
        org = Organization.objects.get(name="Pre-migration Org")
        assert org.week_start == WeekStart.MONDAY
        assert org.week_start == "monday"


@pytest.mark.django_db
class TestOrganizationSlug:
    """Unit tests for Organization.slug (self-serve organization slug)."""

    def test_an_organization_saved_without_a_slug_gets_an_opaque_one(self):
        """``slug`` is NOT NULL, so ``save()`` mints one -- and it is opaque.

        Not ``slugify(name)``: the slug is public (it appears in branded login
        URLs), so a name-derived default would publish the organization's name
        for every row saved without an explicit slug.
        """
        org = Organization.objects.create(name="Acme Incorporated")

        assert org.slug.startswith("org-")
        assert "acme" not in org.slug
        org.refresh_from_db()
        assert org.slug.startswith("org-")

    def test_slug_can_be_set_on_creation(self):
        """slug can be supplied at creation time."""
        org = baker.make(Organization, slug="my-org")
        assert org.slug == "my-org"

    def test_two_organizations_saved_without_a_slug_do_not_collide(self):
        """The minted slugs are distinct, so the unique index admits both."""
        org_a = Organization.objects.create(name="Same Name")
        org_b = Organization.objects.create(name="Same Name")

        assert org_a.slug != org_b.slug

    def test_a_blank_slug_is_refused_by_the_database(self):
        """``organization_slug_not_blank`` refuses ``''`` even past ``save()``.

        This is what makes the branding gate's retired ``NO_SLUG`` condition
        permanently unreachable rather than merely hard to reach: ``save()``
        would have replaced the blank value, so the interesting write is the one
        that goes around it.
        """
        org = baker.make(Organization, slug="a-real-slug")

        with pytest.raises(IntegrityError):
            with django.db.transaction.atomic():
                Organization.objects.filter(pk=org.pk).update(slug="")

    def test_duplicate_slug_raises_integrity_error(self):
        """Two organizations cannot share the same non-null slug."""
        baker.make(Organization, slug="duplicate-slug")

        with pytest.raises(IntegrityError):
            with django.db.transaction.atomic():
                baker.make(Organization, slug="duplicate-slug")

    def test_changing_an_existing_slug_succeeds(self):
        """slug is mutable after being set."""
        org = baker.make(Organization, slug="first-slug")
        org.slug = "second-slug"
        org.save()

        org.refresh_from_db()
        assert org.slug == "second-slug"
