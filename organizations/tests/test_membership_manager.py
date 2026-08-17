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

from datetime import timedelta

from django.db.models import QuerySet
from django.utils import timezone

import pytest
from model_bakery import baker
from vinta_orgs.managers import SingleOrganizationModelManager, SingleOrganizationUnscopedManager

from common.organization_context import organization_context
from organizations.managers import OrganizationMembershipManager
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
)
from organizations.querysets import OrganizationMembershipQuerySet
from organizations.services import assign_membership_groups
from users.models import User


class TestManagerPlumbing:
    def test_the_manager_inherits_the_packages_unscoped_manager(self):
        assert issubclass(OrganizationMembershipManager, SingleOrganizationUnscopedManager)

    def test_the_manager_is_not_the_packages_scoped_manager(self):
        """The failure this module exists for. ``SingleOrganizationModelManager``
        filters ``get_queryset()`` by the bound organization; inheriting it would
        make every pre-selection membership lookup return nothing."""
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
        assert not isinstance(OrganizationMembership._base_manager, SingleOrganizationModelManager)


@pytest.mark.django_db
class TestUnscopedReadsWithNoOrganizationBound:
    def _membership(self, user: User, organization: Organization) -> OrganizationMembership:
        return OrganizationMembership.objects.create(
            user=user,
            organization=organization,
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

    def test_active_for_user_returns_active_rows_oldest_first(self):
        """The three properties ``OrganizationViewSet.mine`` depends on, none of
        which a ``count()`` would notice.

        This repo's local ``active_for_user`` override was deleted in favour
        of the package's, on the grounds that the two were byte-equivalent. That
        is true today and says nothing about tomorrow: ``is_active``, the
        ``created`` ordering and the ``select_related`` are now the package's to
        change, and the org switcher renders whatever order it is handed. So the
        contract is pinned on our side rather than assumed from the package's.

        ``created`` is written explicitly because two rows inserted in the same
        test can land on timestamps close enough that "oldest first" and
        "insertion order" are indistinguishable -- and they are inserted in the
        *reverse* of the expected order, so a query with no ``order_by`` at all
        does not accidentally agree.
        """
        user = baker.make(User)
        newer = self._membership(user, baker.make(Organization))
        older = self._membership(user, baker.make(Organization))
        deactivated = self._membership(user, baker.make(Organization))
        OrganizationMembership.objects.filter(pk=newer.pk).update(
            created=timezone.now() - timedelta(days=1)
        )
        OrganizationMembership.objects.filter(pk=older.pk).update(
            created=timezone.now() - timedelta(days=7)
        )
        OrganizationMembership.objects.filter(pk=deactivated.pk).update(
            created=timezone.now() - timedelta(days=30), is_active=False
        )

        memberships = list(OrganizationMembership.objects.active_for_user(user))

        assert memberships == [older, newer]

    def test_active_for_user_fetches_the_organization_in_the_same_query(
        self, django_assert_num_queries
    ):
        """``select_related('organization')``, pinned as a query count.

        Every caller goes straight on to read ``membership.organization`` --
        ``MyMembershipSerializer`` renders it for each row -- so losing it turns
        the switcher's one query into one per membership.
        """
        user = baker.make(User)
        self._membership(user, baker.make(Organization))
        self._membership(user, baker.make(Organization))

        with django_assert_num_queries(1):
            for membership in OrganizationMembership.objects.active_for_user(user):
                assert membership.organization.name


@pytest.mark.django_db
class TestUnscopedReadsWithAnUnrelatedOrganizationBound:
    """The scoped-manager failure mode, if it were ever introduced, would show
    up here rather than above: binding organization A and reading B's
    memberships. Binds through ``common.organization_context``, which now
    *is* the package's own contextvar -- the one
    ``SingleOrganizationUnscopedManager`` would read from if it read anything.
    (This file used to bind through ``vinta_orgs.state`` directly, because the
    two were still separate contextvars and only the package's was
    consulted; ``0.4.0`` deleted that module-level function, and the shim in
    ``common.organization_context`` is now the only spelling.)"""

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
        """``billing_recipients`` reads ``payments.manage_billing``, which a
        membership holds only through its groups -- so a membership written
        straight through ``objects.create`` has to be put in them the way
        ``OrganizationService`` does. The query itself is covered in
        ``payments/tests/test_dunning_recipients.py``; this stays a test that
        the *manager* still exposes the method."""
        organization = baker.make(Organization)
        admin = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
        )
        billing_owner = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
        )
        plain_member = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
        )
        assign_membership_groups(admin, [GROUP_ORGANIZATION_ADMIN])
        assign_membership_groups(billing_owner, [GROUP_ORGANIZATION_BILLING_OWNER])
        assign_membership_groups(plain_member, [GROUP_ORGANIZATION_MEMBER])

        recipients = OrganizationMembership.objects.billing_recipients(organization.id)

        assert set(recipients) == {admin, billing_owner}

    def test_the_domain_methods_still_return_the_projects_queryset_class(self):
        organization = baker.make(Organization)

        result = OrganizationMembership.objects.occupying_a_seat([organization.id])

        assert isinstance(result, OrganizationMembershipQuerySet)
        assert isinstance(result, QuerySet)
