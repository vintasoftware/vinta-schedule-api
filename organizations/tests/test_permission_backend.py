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

**Amended for package ``0.3.0``.** Two things changed here, and nothing else:

* ``TestAuthenticationBackendsWiring`` was added, pinning that our
  ``AUTHENTICATION_BACKENDS`` entry is the package's class, unsubclassed, in
  the right order, resolving against *our* concrete ``OrganizationMembership``
  model. ``0.3.0`` filters ``is_active`` inside the package's own
  ``_get_membership``, so the repo-owned subclass Phase 3.5 had planned is not
  written at all -- and this is what would catch one being reintroduced.
* ``TestADeactivatedMembershipResolvesNoPermissions`` inverted. Under ``0.2.0``
  a deactivated membership still resolved its group permissions, and that was
  pinned here as *observed* behaviour Phase 4 would have had to handle
  deliberately; ``0.3.0`` fixes it upstream, so the assertion is now that it
  resolves nothing.

The isolation, union and caching classes below were **not** pruned: they pin
that *our* concrete membership model and *our* catalog resolve correctly
through that wiring, which a stock package install says nothing about. The one
thing the ``0.3.0`` commit did delete was a mutation control that reached into
the package's ``_get_membership``; it is restored below
(``TestTheIsolationAssertionCanFail``), because the non-vacuity proof is worth
more than the tests it proves.

**Nothing in the application reads any of this yet.** Every permission class
still checks ``role`` / ``is_billing_owner``; Phase 4 is what migrates them.
These tests pin the foundation Phase 4 will stand on.
"""

from __future__ import annotations

import importlib

from django.apps import apps as global_apps
from django.contrib.auth.models import Group, Permission
from django.db import connection

import pytest
from model_bakery import baker
from vinta_orgs.auth_backends import OrganizationModelBackend

from common.organization_context import organization_context
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


# Migration module names start with a digit, so they can only be reached through
# ``importlib``. Reached here because ``TestTheSeededGroupsExist`` drives the
# seed itself rather than reading whatever happens to be in the database -- see
# that class's docstring.
SEED_MIGRATION = importlib.import_module("organizations.migrations.0028_seed_permission_groups")


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


class TestAuthenticationBackendsWiring:
    """The wiring claim this module exists to pin: our settings, not the
    package's resolution logic.

    ``django.contrib.auth.backends.ModelBackend`` first (a live session records
    the dotted path of the backend that authenticated it, and dropping it would
    sign out every existing session on deploy), the package's
    ``OrganizationModelBackend`` second -- **unsubclassed**. There is no
    repo-owned subclass to register instead: ``0.3.0`` filters ``is_active``
    inside the package's own ``_get_membership``, so Phase 3 registers the
    package's class directly rather than adding one, and this test is what
    would catch either a reordering or a reintroduced subclass.
    """

    def test_lists_the_stock_backend_first_and_the_package_backend_second(self):
        from django.conf import settings

        assert settings.AUTHENTICATION_BACKENDS == [
            "django.contrib.auth.backends.ModelBackend",
            f"{OrganizationModelBackend.__module__}.{OrganizationModelBackend.__qualname__}",
        ]

    def test_the_package_backend_is_not_subclassed(self):
        from django.conf import settings

        # The class named in settings *is* the package's own -- not a
        # same-named repo module shadowing it, and not a subclass of it.
        assert (
            settings.AUTHENTICATION_BACKENDS[1]
            == "vinta_orgs.auth_backends.OrganizationModelBackend"
        )
        assert OrganizationModelBackend.__module__ == "vinta_orgs.auth_backends"

    def test_the_membership_model_it_resolves_against_is_ours(self):
        from vinta_orgs.conf import get_organization_membership_model

        assert get_organization_membership_model() is OrganizationMembership


@pytest.mark.django_db
class TestTheSeededGroupsExist:
    """``0028_seed_permission_groups`` leaves the catalog behind.

    Every other class here depends on it, and the failure mode without it is
    "the admin has no permissions", which reads like a backend bug rather than a
    missing seed.

    **Driven, not observed.** The root ``conftest.py`` registers
    ``vinta_orgs.testing``, whose autouse ``seeded_organization_groups`` fixture
    reseeds these exact three groups before every test with a database (it has
    to: a transactional test's flush wipes them for the rest of the worker's
    session). That fixture recreates precisely the state asserted below, so
    reading the ambient database here would pass even if ``0028`` were deleted
    outright. The fixture below therefore drops the three groups and calls the
    *migration's own* seeding function, so what these assertions describe is the
    migration and nothing else.
    """

    @pytest.fixture(autouse=True)
    def _seeded_by_the_migration(self):
        Group.objects.filter(
            name__in=[
                GROUP_ORGANIZATION_ADMIN,
                GROUP_ORGANIZATION_BILLING_OWNER,
                GROUP_ORGANIZATION_MEMBER,
            ]
        ).delete()

        SEED_MIGRATION.seed_permission_groups(global_apps, connection.schema_editor())

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

    Deleted by the ``0.3.0`` bump as "package mechanics" and restored here: what
    it exercises is whether *our* assertions discriminate, which is a fact about
    this module and not about the package. Keeping the isolation tests while
    dropping the proof that they can fail is the worst of the two options.
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
class TestADeactivatedMembershipResolvesNoPermissions:
    """**Inverted for package ``0.3.0``.**

    Under ``0.2.0`` this asserted the opposite: ``OrganizationModelBackend
    ._get_membership`` did not filter ``is_active``, so a deactivated
    membership still resolved its group permissions, and the assertion below
    was pinned as *observed* behaviour Phase 4 would have had to handle
    deliberately (clear groups on deactivation, or gate the resolver) rather
    than inherit by accident from a mechanical ``has_perm`` swap.

    ``0.3.0`` closes the gap at the source: ``is_active`` now filters *inside*
    ``_get_membership``, so a deactivated administrator resolves exactly what
    a non-member resolves -- nothing. Phase 4 no longer has to carry this gate
    itself; the flip below is the regression test for that fix, against *our*
    concrete membership model.
    """

    def test_deactivating_an_admin_membership_takes_its_permissions_away(self):
        """Both directions, in one test, on one membership.

        "A deactivated admin resolves nothing" is satisfied by *any* reason the
        set came back empty -- the group never attached, the seed is missing,
        the user has no membership -- so on its own it proves nothing. Its
        positive control has to be the same membership a moment earlier, not a
        similarly-shaped one in another class: ``-n auto`` can schedule two
        classes on two workers, and a hazard whose repair lives on another
        worker is not a repair.
        """
        user = baker.make(User, is_superuser=False, is_active=True)
        organization = _organization("Alpha", "alpha-inactive")
        membership = _membership(
            user,
            organization,
            GROUP_ORGANIZATION_ADMIN,
            role=OrganizationRole.ADMIN,
        )

        with organization_context(organization):
            while_active = _reloaded(user).get_all_permissions()

        assert set(ADMIN_PERMISSIONS) <= while_active

        membership.is_active = False
        membership.save(update_fields=["is_active"])

        with organization_context(organization):
            once_deactivated = _reloaded(user).get_all_permissions()

        assert once_deactivated == set()


@pytest.mark.django_db
class TestASuperuserResolvesEveryPermissionOnceAnOrganizationIsBound:
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
