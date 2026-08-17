"""``POST /organization-members/{user_id}/groups/`` — the group-assignment endpoint.

Replaces ``POST /organization-members/{user_id}/update-role/``. Three
properties of the old endpoint had to survive the change of representation, and
each is the reason for one class below: assigning what is already there is a
success, an unknown group is refused, and the organization can never be left
with nobody who can manage members.

The last one is the interesting one. It used to be counted with
``filter(role=ADMIN, is_active=True)``. ``role`` is no longer what authorizes
anybody, so the guard now counts *capability* --
``organizations.manage_members``, resolved exactly the way
``IsOrganizationAdmin`` resolves it. The tests below prove the new counting on
both sides: it must still refuse when the target is the last such member even
though other members exist, and it must admit as soon as a second one does.
"""

from __future__ import annotations

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
    MANAGE_BILLING,
    MANAGE_BRANDING,
    MANAGE_MEMBERS,
    MANAGE_ORGANIZATION,
)
from organizations.tests.helpers import (
    make_admin_membership,
    make_billing_owner_membership,
    make_membership,
)
from users.models import User


ADMIN_PERMISSIONS = sorted([MANAGE_MEMBERS, MANAGE_ORGANIZATION, MANAGE_BRANDING, MANAGE_BILLING])


def groups_url(membership: OrganizationMembership) -> str:
    return reverse("api:OrganizationMembers-assign-groups", kwargs={"user_id": membership.user_id})


def group_names(membership: OrganizationMembership) -> set[str]:
    membership.refresh_from_db()
    return set(membership.groups.values_list("name", flat=True))


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization, name="Acme Inc")


@pytest.fixture
def admin_client(auth_client, user, organization):
    """The caller: an administrator of ``organization``, so the gate is open."""
    make_admin_membership(user=user, organization=organization)
    return auth_client


@pytest.mark.django_db
class TestAssigningGroups:
    def test_idempotent_assignment_keeps_a_sole_direct_permission_holder(
        self, auth_client, organization
    ):
        """A sole direct holder may replace groups without losing that grant."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        target = make_membership(user=baker.make(User), organization=organization)
        target.permissions.add(
            Permission.objects.get(
                codename="manage_members",
                content_type=ContentType.objects.get_for_model(OrganizationMembership),
            )
        )

        auth_client.force_authenticate(target.user)
        response = auth_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert group_names(target) == {GROUP_ORGANIZATION_MEMBER}
        assert target.permissions.filter(codename="manage_members").exists()

    def test_promoting_a_member_grants_every_capability(self, admin_client, organization):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_ADMIN]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert sorted(response.json()["permissions"]) == ADMIN_PERMISSIONS
        assert "role" not in response.json()
        assert group_names(target) == {GROUP_ORGANIZATION_ADMIN}

    def test_demoting_an_admin_removes_every_capability(self, admin_client, organization):
        other_admin = make_admin_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(other_admin), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["permissions"] == []
        assert group_names(other_admin) == {GROUP_ORGANIZATION_MEMBER}

    def test_billing_owner_can_be_granted_without_admin(self, admin_client, organization):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_BILLING_OWNER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["permissions"] == [MANAGE_BILLING]
        assert group_names(target) == {GROUP_ORGANIZATION_BILLING_OWNER}

    def test_billing_owner_can_be_revoked(self, admin_client, organization):
        target = make_billing_owner_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["permissions"] == []
        assert group_names(target) == {GROUP_ORGANIZATION_MEMBER}

    def test_a_capability_group_alongside_member_stores_the_capability_group_alone(
        self, admin_client, organization
    ):
        """Canonicalisation, not a silent partial write.

        ``organization_member`` means "no capabilities"; naming it beside
        ``organization_admin`` is contradictory. The stored set has to be one
        the dual-write shim would itself produce, or the next role-changing
        write anywhere in the codebase would overwrite it.
        """
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target),
            {"groups": [GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert group_names(target) == {GROUP_ORGANIZATION_ADMIN}
        assert sorted(response.json()["permissions"]) == ADMIN_PERMISSIONS

    def test_admin_and_billing_owner_together_keep_both_groups(self, admin_client, organization):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target),
            {"groups": [GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_BILLING_OWNER]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert group_names(target) == {
            GROUP_ORGANIZATION_ADMIN,
            GROUP_ORGANIZATION_BILLING_OWNER,
        }


@pytest.mark.django_db
class TestIdempotency:
    """Setting the current value is a success, exactly as ``update-role`` was."""

    def test_reassigning_the_groups_a_member_already_holds_succeeds(
        self, admin_client, organization
    ):
        target = make_admin_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_ADMIN]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert group_names(target) == {GROUP_ORGANIZATION_ADMIN}

    def test_the_sole_admin_may_reassign_their_own_admin_group(
        self, admin_client, user, organization
    ):
        """The last-admin guard fires on *losing* the capability, not on writing it.

        Without this, the one caller who can always reach this endpoint -- the
        sole administrator -- would be refused a no-op write on themselves.
        """
        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_ADMIN]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert group_names(sole_admin) == {GROUP_ORGANIZATION_ADMIN}

    def test_assigning_member_to_a_member_succeeds(self, admin_client, organization):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["permissions"] == []


@pytest.mark.django_db
class TestTheLastManageMembersGuard:
    """ "Cannot demote the last active admin", counted by capability.

    The old rule counted ``role == ADMIN`` rows. If the new implementation had
    kept counting rows of any other kind -- members, active memberships, group
    rows regardless of which permission they carry -- the first test here would
    pass vacuously, which is why the organization in it deliberately contains
    several other members.
    """

    def test_the_last_member_who_can_manage_members_cannot_lose_it(
        self, admin_client, user, organization
    ):
        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)
        # Other members exist, and none of them can manage members. A guard
        # that counted memberships rather than capabilities would admit this.
        for _ in range(3):
            make_membership(user=baker.make(User), organization=organization)
        make_billing_owner_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert group_names(sole_admin) == {GROUP_ORGANIZATION_ADMIN}

    def test_a_second_member_who_can_manage_members_unblocks_the_demotion(
        self, admin_client, user, organization
    ):
        """The other side of the same rule -- otherwise it could refuse always."""
        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)
        make_admin_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert group_names(sole_admin) == {GROUP_ORGANIZATION_MEMBER}

    def test_an_inactive_second_admin_does_not_count(self, admin_client, user, organization):
        """ "Last **active** member" -- a deactivated admin cannot administer anything."""
        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)
        make_admin_membership(user=baker.make(User), organization=organization, is_active=False)

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert group_names(sole_admin) == {GROUP_ORGANIZATION_ADMIN}

    def test_an_admin_in_another_organization_does_not_count(
        self, admin_client, user, organization
    ):
        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)
        make_admin_membership(
            user=baker.make(User), organization=baker.make(Organization, name="Other Co")
        )

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_demoting_to_billing_owner_still_loses_manage_members(
        self, admin_client, user, organization
    ):
        """The guard is about the capability, not about the group name.

        ``organization_billing_owner`` is not ``organization_member``, but it
        carries no ``manage_members`` either, so it is just as much a demotion.
        """
        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_BILLING_OWNER]}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert group_names(sole_admin) == {GROUP_ORGANIZATION_ADMIN}

    def test_a_member_holding_manage_members_directly_counts_as_a_remaining_admin(
        self, admin_client, user, organization
    ):
        """The direct per-membership grant authorizes, so it must also count.

        ``OrganizationMembership.permissions`` is empty across this codebase,
        but the backend unions it in, so such a member *would* pass
        ``IsOrganizationAdmin``. A guard that ignored it would refuse a
        demotion that leaves a real administrator behind.
        """
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        sole_admin = OrganizationMembership.objects.get(user=user, organization=organization)
        directly_granted = make_membership(user=baker.make(User), organization=organization)
        directly_granted.permissions.add(
            Permission.objects.get(
                codename="manage_members",
                content_type=ContentType.objects.get_for_model(OrganizationMembership),
            )
        )

        response = admin_client.post(
            groups_url(sole_admin), {"groups": [GROUP_ORGANIZATION_MEMBER]}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK

    def test_a_group_grant_and_a_direct_grant_together_count_the_membership_once(
        self, organization
    ):
        """``holding_permission`` joins two M2Ms with an ``OR`` -- ``distinct()`` pins.

        A membership in ``organization_admin`` *and* directly granted
        ``manage_members`` satisfies both halves of
        ``vinta_orgs.querysets.filter_memberships_holding_permission``'s
        ``Q(permissions=...) | Q(groups__permissions=...)``. Without
        ``distinct()`` the join would return that one row twice, and
        ``.count()`` -- what the guard above compares to zero -- would report
        two members where there is one. That happens to still leave the ``==
        0`` check unharmed for *this* specific comparison, but the guard reuses
        this exact queryset method, so a silent regression here is a silent
        regression there; asserted directly against the queryset rather than
        through the view so the count itself, not just the view's derived
        decision, is what is pinned.
        """
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        doubly_granted = make_admin_membership(user=baker.make(User), organization=organization)
        doubly_granted.permissions.add(
            Permission.objects.get(
                codename="manage_members",
                content_type=ContentType.objects.get_for_model(OrganizationMembership),
            )
        )

        count = (
            OrganizationMembership.objects.filter(organization_id=organization.id, is_active=True)
            .holding_permission(MANAGE_MEMBERS)
            .count()
        )

        assert count == 1


@pytest.mark.django_db
class TestRejections:
    def test_an_unknown_group_name_is_refused(self, admin_client, organization):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target), {"groups": ["organization_owner"]}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert group_names(target) == {GROUP_ORGANIZATION_MEMBER}

    def test_an_unknown_group_alongside_a_known_one_is_refused_wholesale(
        self, admin_client, organization
    ):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(
            groups_url(target),
            {"groups": [GROUP_ORGANIZATION_ADMIN, "superuser"]},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert group_names(target) == {GROUP_ORGANIZATION_MEMBER}

    def test_an_empty_group_list_is_refused(self, admin_client, organization):
        target = make_admin_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(groups_url(target), {"groups": []}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert group_names(target) == {GROUP_ORGANIZATION_ADMIN}

    def test_a_missing_groups_key_is_refused(self, admin_client, organization):
        target = make_membership(user=baker.make(User), organization=organization)

        response = admin_client.post(groups_url(target), {}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_non_admin_caller_is_refused(self, auth_client, user, organization):
        make_membership(user=user, organization=organization)
        target = make_membership(user=baker.make(User), organization=organization)

        response = auth_client.post(
            groups_url(target), {"groups": [GROUP_ORGANIZATION_ADMIN]}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert group_names(target) == {GROUP_ORGANIZATION_MEMBER}

    def test_a_member_of_another_organization_is_not_found(self, admin_client):
        elsewhere = make_membership(
            user=baker.make(User), organization=baker.make(Organization, name="Other Co")
        )

        response = admin_client.post(
            groups_url(elsewhere), {"groups": [GROUP_ORGANIZATION_ADMIN]}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert group_names(elsewhere) == {GROUP_ORGANIZATION_MEMBER}

    def test_the_old_update_role_route_is_gone(self):
        from django.urls.exceptions import NoReverseMatch

        with pytest.raises(NoReverseMatch):
            reverse("api:OrganizationMembers-update-role", kwargs={"user_id": 1})
