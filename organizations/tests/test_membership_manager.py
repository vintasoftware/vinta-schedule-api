"""``OrganizationMembership.objects`` must **not** scope to the current organization.

This is the one scoped model whose default manager has to stay unscoped, and
getting it wrong is quiet rather than loud: a membership is the row you read to
work out *which* organization to select, so scoping it to the selected
organization is circular. Listing the organizations a user belongs to,
provisioning the first membership right after signup, and checking whether an
invitation's user is already a member all run before anything is bound.

Django builds the reverse accessors (``user.memberships``,
``organization.memberships``) from ``_default_manager.__class__``, so the
manager's base class decides their behaviour too -- which is why the assertions
below cover the accessors and not only ``objects``.
"""

from django.db.models import QuerySet

import pytest
from model_bakery import baker
from vinta_orgs.state import organization_context

from organizations.managers import OrganizationMembershipManager
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.querysets import OrganizationMembershipQuerySet
from users.models import User


class TestManagerPlumbing:
    def test_the_manager_inherits_the_packages_unscoped_manager(self):
        from vinta_orgs.managers import SingleOrganizationUnscopedManager

        assert issubclass(OrganizationMembershipManager, SingleOrganizationUnscopedManager)

    def test_the_manager_is_not_the_packages_scoped_manager(self):
        """The failure this module exists for. ``SingleOrganizationModelManager``
        filters ``get_queryset()`` by the bound organization; inheriting it would
        make every pre-selection membership lookup return nothing."""
        from vinta_orgs.managers import SingleOrganizationModelManager

        assert not issubclass(OrganizationMembershipManager, SingleOrganizationModelManager)

    def test_the_queryset_class_matches_the_methods_the_manager_exposes(self):
        """``from_queryset`` copies methods across; ``_queryset_class`` decides
        what ``get_queryset()`` returns. A mismatch leaves half the manager's
        methods delegating to a class that does not implement them."""
        assert OrganizationMembership.objects._queryset_class is OrganizationMembershipQuerySet

    def test_the_default_manager_is_objects(self):
        assert OrganizationMembership._meta.default_manager.name == "objects"
        assert isinstance(OrganizationMembership.objects, OrganizationMembershipManager)

    def test_the_base_manager_does_not_filter(self):
        """``_base_manager`` backs ``save()``, ``refresh_from_db()``, the delete
        collector and forward-relation fetches; it must see every row."""
        from vinta_orgs.managers import SingleOrganizationModelManager

        assert not isinstance(OrganizationMembership._base_manager, SingleOrganizationModelManager)


@pytest.mark.django_db
class TestUnscopedReadsWithNoOrganizationBound:
    def _membership(self, user: User, organization: Organization) -> OrganizationMembership:
        return OrganizationMembership.objects.create(
            user=user, organization=organization, role=OrganizationRole.MEMBER
        )

    def test_user_memberships_returns_rows_with_nothing_bound(self):
        user = baker.make(User)
        first = self._membership(user, baker.make(Organization))
        second = self._membership(user, baker.make(Organization))

        assert set(user.memberships.all()) == {first, second}

    def test_organization_memberships_returns_rows_with_nothing_bound(self):
        organization = baker.make(Organization)
        membership = self._membership(baker.make(User), organization)

        assert list(organization.memberships.all()) == [membership]

    def test_objects_returns_rows_across_organizations_with_nothing_bound(self):
        user = baker.make(User)
        self._membership(user, baker.make(Organization))
        self._membership(user, baker.make(Organization))

        assert OrganizationMembership.objects.filter(user=user).count() == 2

    def test_active_for_user_works_before_an_organization_is_selected(self):
        """The org-switcher's query. It runs on a caller who has not chosen an
        organization yet -- that is the point of it."""
        user = baker.make(User)
        self._membership(user, baker.make(Organization))
        self._membership(user, baker.make(Organization))

        assert OrganizationMembership.objects.active_for_user(user).count() == 2


@pytest.mark.django_db
class TestUnscopedReadsWithAnUnrelatedOrganizationBound:
    """The scoped-manager failure mode, if it were ever introduced, would show
    up here rather than above: binding organization A and reading B's
    memberships. Binds through ``vinta_orgs.state`` -- the package's own
    contextvar, which is what ``SingleOrganizationUnscopedManager`` would read
    from if it read anything -- rather than ``common.organization_context``,
    which is this repo's separate, not-yet-consulted binding (Phase 2a's
    precondition, not this manager's)."""

    def test_a_bound_organization_does_not_hide_another_organizations_memberships(self):
        user = baker.make(User)
        organization_a = baker.make(Organization)
        organization_b = baker.make(Organization)
        OrganizationMembership.objects.create(user=user, organization=organization_a)
        OrganizationMembership.objects.create(user=user, organization=organization_b)

        with organization_context(organization_a):
            assert user.memberships.count() == 2
            assert OrganizationMembership.objects.filter(user=user).count() == 2


@pytest.mark.django_db
class TestDomainMethodsSurvivedTheManagerChange:
    def test_occupying_a_seat_counts_only_active_memberships(self):
        organization = baker.make(Organization)
        active = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, is_active=True
        )
        OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, is_active=False
        )

        seats = OrganizationMembership.objects.occupying_a_seat([organization.id])

        assert list(seats) == [active]

    def test_billing_recipients_returns_admins_and_billing_owners(self):
        organization = baker.make(Organization)
        admin = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, role=OrganizationRole.ADMIN
        )
        billing_owner = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_billing_owner=True,
        )
        OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, role=OrganizationRole.MEMBER
        )

        recipients = OrganizationMembership.objects.billing_recipients(organization.id)

        assert set(recipients) == {admin, billing_owner}

    def test_the_domain_methods_still_return_the_projects_queryset_class(self):
        organization = baker.make(Organization)

        result = OrganizationMembership.objects.occupying_a_seat([organization.id])

        assert isinstance(result, OrganizationMembershipQuerySet)
        assert isinstance(result, QuerySet)
