"""``OrganizationModelBackend``: what ``has_perm`` answers, and in which organization.

The backend (``vinta_orgs.auth_backends.OrganizationModelBackend``, added to
``AUTHENTICATION_BACKENDS`` in this phase) unions two independent sources:

* the user's **global** permissions -- ``user.user_permissions`` and
  ``user.groups``, exactly what the stock ``ModelBackend`` answers, organization
  or no organization;
* the permissions their ``OrganizationMembership`` carries in the organization
  bound to the current ``vinta_orgs.state`` contextvar -- and *only* that
  organization.

The second half is what this module is really about. A membership's groups must
be invisible from any other organization, and invisible with nothing bound;
otherwise a user who administers organization A silently administers B.

**Nothing in the application reads any of this yet.** Every permission class
still checks ``role`` / ``is_billing_owner``; Phase 4 is what migrates them.
These tests pin the foundation Phase 4 will stand on -- including the two
behaviours (below) that Phase 4 has to deal with deliberately rather than
inherit by accident.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, Permission

import pytest
from model_bakery import baker
from vinta_orgs.auth_backends import OrganizationModelBackend
from vinta_orgs.state import organization_context

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
    MANAGE_BILLING,
    MANAGE_BRANDING,
    MANAGE_MEMBERS,
    MANAGE_ORGANIZATION,
)
from users.models import User


# Pinned as literals rather than read off ``GROUP_PERMISSIONS``: the point is
# what an admin can do, not that a dict agrees with itself.
ADMIN_PERMISSIONS = [
    "organizations.manage_members",
    "organizations.manage_organization",
    "organizations.manage_branding",
    "payments.manage_billing",
]


def _organization(name: str, slug: str) -> Organization:
    return baker.make(Organization, name=name, slug=slug)


def _membership(
    user: User,
    organization: Organization,
    group_name: str,
    *,
    role: str = OrganizationRole.MEMBER,
    is_active: bool = True,
) -> OrganizationMembership:
    membership = OrganizationMembership.objects.create(
        user=user, organization=organization, role=role, is_active=is_active
    )
    membership.groups.add(Group.objects.get(name=group_name))
    return membership


def _reloaded(user: User) -> User:
    """A user object with no permission caches on it.

    ``ModelBackend`` and ``OrganizationModelBackend`` both stash resolved
    permission sets as attributes on the user *instance*. Anywhere a test wants
    to observe a fresh resolution rather than a cached one, it has to start from
    a fresh instance -- which is also what a real request does.
    """
    return User.objects.get(pk=user.pk)


@pytest.mark.django_db
class TestTheSeededGroupsExist:
    """The migrations ran and left the catalog behind.

    Every other class here depends on this, and the failure mode without it is
    "the admin has no permissions", which reads like a backend bug rather than a
    missing seed.
    """

    def test_the_three_groups_are_present(self):
        names = set(
            Group.objects.filter(
                name__in=[
                    GROUP_ORGANIZATION_ADMIN,
                    GROUP_ORGANIZATION_BILLING_OWNER,
                    GROUP_ORGANIZATION_MEMBER,
                ]
            ).values_list("name", flat=True)
        )

        assert names == {
            "organization_admin",
            "organization_billing_owner",
            "organization_member",
        }

    def test_the_admin_group_carries_exactly_the_four_capabilities(self):
        group = Group.objects.get(name=GROUP_ORGANIZATION_ADMIN)

        labelled = {
            f"{app_label}.{codename}"
            for app_label, codename in group.permissions.values_list(
                "content_type__app_label", "codename"
            )
        }

        assert labelled == set(ADMIN_PERMISSIONS)

    def test_the_billing_owner_group_carries_only_manage_billing(self):
        group = Group.objects.get(name=GROUP_ORGANIZATION_BILLING_OWNER)

        labelled = {
            f"{app_label}.{codename}"
            for app_label, codename in group.permissions.values_list(
                "content_type__app_label", "codename"
            )
        }

        assert labelled == {"payments.manage_billing"}

    def test_the_member_group_is_deliberately_empty(self):
        group = Group.objects.get(name=GROUP_ORGANIZATION_MEMBER)

        assert group.permissions.count() == 0


@pytest.mark.django_db
class TestAnAdminMembershipUnderItsOwnOrganization:
    def test_resolves_all_four_capability_permissions(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Acme", "acme-backend-a")
        _membership(user, organization, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        with organization_context(organization):
            resolved = _reloaded(user).get_all_permissions()

        for permission in ADMIN_PERMISSIONS:
            assert permission in resolved

    def test_has_perm_answers_true_for_each_of_them(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Acme", "acme-backend-b")
        _membership(user, organization, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        reloaded = _reloaded(user)
        with organization_context(organization):
            assert reloaded.has_perm(MANAGE_MEMBERS)
            assert reloaded.has_perm(MANAGE_ORGANIZATION)
            assert reloaded.has_perm(MANAGE_BRANDING)
            assert reloaded.has_perm(MANAGE_BILLING)

    def test_a_billing_owner_gets_manage_billing_and_nothing_else(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Acme", "acme-backend-c")
        _membership(user, organization, GROUP_ORGANIZATION_BILLING_OWNER)

        reloaded = _reloaded(user)
        with organization_context(organization):
            assert reloaded.has_perm(MANAGE_BILLING)
            assert not reloaded.has_perm(MANAGE_MEMBERS)
            assert not reloaded.has_perm(MANAGE_ORGANIZATION)
            assert not reloaded.has_perm(MANAGE_BRANDING)

    def test_a_plain_member_gets_nothing(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Acme", "acme-backend-d")
        _membership(user, organization, GROUP_ORGANIZATION_MEMBER)

        with organization_context(organization):
            resolved = _reloaded(user).get_all_permissions()

        assert resolved == set()


@pytest.mark.django_db
class TestTheOrganizationHalfIsConfinedToTheBoundOrganization:
    """The isolation guarantee. See ``TestTheIsolationAssertionCanFail`` below --
    these assertions are mutation-tested, not merely written."""

    def test_an_admin_of_a_resolves_nothing_under_b(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization_a = _organization("Alpha", "alpha-isolation")
        organization_b = _organization("Beta", "beta-isolation")
        _membership(user, organization_a, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        with organization_context(organization_b):
            resolved = _reloaded(user).get_all_permissions()

        assert resolved == set()

    def test_an_admin_of_a_who_is_also_a_plain_member_of_b_resolves_nothing_under_b(self):
        """The realistic shape: the user *does* have a membership in B, so the
        backend finds a row -- it just must not find A's groups on it."""
        user = baker.make(User, is_superuser=False, is_active=True)
        organization_a = _organization("Alpha", "alpha-isolation-2")
        organization_b = _organization("Beta", "beta-isolation-2")
        _membership(user, organization_a, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)
        _membership(user, organization_b, GROUP_ORGANIZATION_MEMBER)

        reloaded = _reloaded(user)
        with organization_context(organization_b):
            assert not reloaded.has_perm(MANAGE_MEMBERS)
            assert not reloaded.has_perm(MANAGE_BILLING)

    def test_nothing_bound_resolves_no_organization_permissions(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Alpha", "alpha-unbound")
        _membership(user, organization, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        resolved = _reloaded(user).get_all_permissions()

        assert resolved == set()

    def test_the_answer_tracks_the_binding_on_one_user_object(self):
        """The caching question, asked directly.

        ``OrganizationModelBackend`` caches resolved organization permissions on
        the *user instance*, and a permission check inside a request reuses one
        instance throughout. The caches are keyed by organization primary key
        (``_organization_perm_cache`` and friends), so re-binding within the same
        process must re-resolve rather than serve the previous organization's
        answer -- including on the way *back* to an organization already seen.
        """
        user = baker.make(User, is_superuser=False, is_active=True)
        organization_a = _organization("Alpha", "alpha-cache")
        organization_b = _organization("Beta", "beta-cache")
        _membership(user, organization_a, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)
        _membership(user, organization_b, GROUP_ORGANIZATION_MEMBER)

        reloaded = _reloaded(user)

        with organization_context(organization_a):
            assert reloaded.has_perm(MANAGE_MEMBERS)
        with organization_context(organization_b):
            assert not reloaded.has_perm(MANAGE_MEMBERS)
        with organization_context(organization_a):
            assert reloaded.has_perm(MANAGE_MEMBERS)
        assert not reloaded.has_perm(MANAGE_MEMBERS)


@pytest.mark.django_db
class TestTheIsolationAssertionCanFail:
    """Proof that the class above is not vacuous.

    ``_get_membership`` is the one place the backend consults the bound
    organization on the way to a membership's groups. Replacing it with a
    version that ignores its ``organization`` argument -- the exact defect the
    isolation tests exist to catch -- must turn those assertions red. If it does
    not, they were passing for some other reason (an empty group, a missing
    seed, a user with no membership at all) and prove nothing.
    """

    def test_a_backend_that_ignores_the_bound_organization_leaks_across_organizations(
        self, monkeypatch
    ):
        def _membership_ignoring_the_organization(self, user_obj, organization):
            return OrganizationMembership.objects.filter(user=user_obj).order_by("pk").first()

        monkeypatch.setattr(
            OrganizationModelBackend,
            "_get_membership",
            _membership_ignoring_the_organization,
        )

        user = baker.make(User, is_superuser=False, is_active=True)
        organization_a = _organization("Alpha", "alpha-mutation")
        organization_b = _organization("Beta", "beta-mutation")
        _membership(user, organization_a, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)
        _membership(user, organization_b, GROUP_ORGANIZATION_MEMBER)

        with organization_context(organization_b):
            leaked = _reloaded(user).get_all_permissions()

        # The mutant grants A's four capabilities while B is bound. The
        # unmutated backend resolves the empty set here -- which is precisely
        # what ``TestTheOrganizationHalfIsConfinedToTheBoundOrganization``
        # asserts, so that assertion discriminates.
        assert leaked == set(ADMIN_PERMISSIONS)


@pytest.mark.django_db
class TestTheUnionWithGlobalPermissions:
    """Global permissions are organization-independent and must survive the union.

    ``OrganizationModelBackend`` shares its global-permission cache attribute
    names with the stock ``ModelBackend`` (both are in ``AUTHENTICATION_BACKENDS``
    and both write ``_perm_cache`` / ``_user_perm_cache`` / ``_group_perm_cache``).
    The two fill them with the same content, but "same content" is a claim worth
    a test rather than a comment.
    """

    @staticmethod
    def _a_global_permission() -> Permission:
        return Permission.objects.get(
            content_type__app_label="organizations", codename="add_organizationinvitation"
        )

    def test_a_direct_global_grant_resolves_with_nothing_bound(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        user.user_permissions.add(self._a_global_permission())

        assert _reloaded(user).has_perm("organizations.add_organizationinvitation")

    def test_a_direct_global_grant_resolves_under_every_binding(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization_a = _organization("Alpha", "alpha-global")
        organization_b = _organization("Beta", "beta-global")
        user.user_permissions.add(self._a_global_permission())
        _membership(user, organization_a, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        reloaded = _reloaded(user)
        with organization_context(organization_a):
            assert reloaded.has_perm("organizations.add_organizationinvitation")
        with organization_context(organization_b):
            assert reloaded.has_perm("organizations.add_organizationinvitation")

    def test_the_two_halves_are_unioned_not_replaced(self):
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Alpha", "alpha-union")
        user.user_permissions.add(self._a_global_permission())
        _membership(user, organization, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        with organization_context(organization):
            resolved = _reloaded(user).get_all_permissions()

        assert resolved == {*ADMIN_PERMISSIONS, "organizations.add_organizationinvitation"}

    def test_a_global_django_group_resolves_too(self):
        """The other global source: ``user.groups``, which is a different M2M
        from ``membership.groups`` and must not be confused with it."""
        user = baker.make(User, is_superuser=False, is_active=True)
        global_group = Group.objects.create(name="global_invite_senders")
        global_group.permissions.add(self._a_global_permission())
        user.groups.add(global_group)

        assert _reloaded(user).has_perm("organizations.add_organizationinvitation")

    def test_a_membership_group_is_not_a_global_group(self):
        """The inverse, and the reason the union is safe: putting a *membership*
        in ``organization_admin`` must not put the *user* in it globally."""
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Alpha", "alpha-not-global")
        _membership(user, organization, GROUP_ORGANIZATION_ADMIN, role=OrganizationRole.ADMIN)

        reloaded = _reloaded(user)

        assert reloaded.groups.count() == 0
        assert not reloaded.has_perm(MANAGE_MEMBERS)


@pytest.mark.django_db
class TestTwoBehavioursPhase4MustHandleDeliberately:
    """Not defects in this phase -- nothing reads any of this yet -- but both
    differ from what ``role``-based checks do today, so a mechanical swap in
    Phase 4 would change an authorization outcome. Pinned here so the change is
    a decision rather than a surprise."""

    def test_an_inactive_membership_still_resolves_its_group_permissions(self):
        """``OrganizationModelBackend._get_membership`` does not filter
        ``is_active``. Today every role check goes through
        ``get_active_organization_membership``, which does -- a deactivated
        member is treated exactly like a non-member. Phase 4 must keep the
        ``is_active`` gate somewhere (the resolver, or a membership whose groups
        are cleared on deactivation); ``has_perm`` alone will not carry it.
        """
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Alpha", "alpha-inactive")
        _membership(
            user,
            organization,
            GROUP_ORGANIZATION_ADMIN,
            role=OrganizationRole.ADMIN,
            is_active=False,
        )

        with organization_context(organization):
            resolved = _reloaded(user).get_all_permissions()

        assert set(ADMIN_PERMISSIONS) <= resolved

    def test_a_superuser_resolves_every_permission_once_an_organization_is_bound(self):
        """With an organization bound, the backend answers ``Permission.objects.all()``
        for a superuser even with no membership at all. ``PermissionsMixin.has_perm``
        already short-circuits superusers to ``True``, so this changes no outcome --
        it is recorded because ``get_all_permissions()`` is also read directly (the
        Phase 5 API surface reports it) and a superuser's list is the whole catalog,
        not four capabilities."""
        user = baker.make(User, is_superuser=True, is_active=True)
        organization = _organization("Alpha", "alpha-superuser")

        with organization_context(organization):
            resolved = _reloaded(user).get_all_permissions()

        assert set(ADMIN_PERMISSIONS) <= resolved
        assert len(resolved) == Permission.objects.count()
