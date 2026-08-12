"""``OrganizationMembership.objects`` is, and must stay, the *unscoped* manager.

Phase 1c of the vinta-django-orgs migration reparents the model onto
``AbstractOrganizationMembership``, which sets ``objects =
SingleOrganizationUnscopedManager()`` deliberately: a membership is *how* an
organization gets selected, so scoping the membership table to the selected
organization is circular. Our ``OrganizationMembershipManager`` therefore
inherits that manager rather than ``models.Manager`` or the scoped
``SingleOrganizationModelManager``.

Getting it wrong is silent rather than loud. Django builds the reverse accessors
from ``_default_manager.__class__``, so a scoped manager would make
``user.memberships`` and ``organization.memberships`` return nothing whenever no
organization is bound -- which is exactly the situation every membership lookup
that *decides* the organization runs in: listing a user's organizations for the
switcher, provisioning the first membership at signup, checking whether an
invitee is already a member. None of those would error; they would report "no
memberships" and the caller would take the gated branch.

The whole module therefore runs with **no organization bound**. That is the
contract under test, not an oversight.
"""

from django.db.models import Manager

import pytest
from model_bakery import baker
from organizations.managers import SingleOrganizationUnscopedManager
from organizations.querysets import SingleOrganizationQuerySet
from organizations.state import get_current_organization

from tenancy.managers import OrganizationMembershipManager
from tenancy.models import Organization, OrganizationMembership, OrganizationRole
from tenancy.querysets import OrganizationMembershipQuerySet
from users.models import User


class TestManagerWiring:
    """Static wiring. No database -- these are class-level facts."""

    def test_the_manager_inherits_the_unscoped_manager(self):
        assert issubclass(OrganizationMembershipManager, SingleOrganizationUnscopedManager)

    def test_objects_is_the_declared_default_manager(self):
        """``Meta.default_manager_name`` is spelled out on the model.

        Without it, manager creation order decides, and the mixin's
        ``original_manager`` -- declared on the abstract base, so with a lower
        creation counter -- would win.
        """
        assert OrganizationMembership._meta.default_manager_name == "objects"
        assert isinstance(OrganizationMembership._default_manager, OrganizationMembershipManager)

    def test_the_queryset_carries_the_package_scoping_methods(self):
        assert issubclass(OrganizationMembershipQuerySet, SingleOrganizationQuerySet)
        assert isinstance(
            OrganizationMembership.objects.get_queryset(), OrganizationMembershipQuerySet
        )

    def test_the_base_manager_does_not_scope(self):
        """``_base_manager`` is what ``save()``, ``refresh_from_db()`` and the
        cascade collector use; it is documented as a manager that must not filter
        rows away.

        ``SingleOrganizationModelMixin.Meta`` points it at ``original_manager``
        for exactly that reason, but that ``Meta`` is not in this model's
        inheritance chain (``AbstractOrganizationMembership`` lists
        ``TimeStampedModel`` first, and ``Options.base_manager`` stops at the
        first parent with a ``_meta``). The result is Django's plain, unscoped
        fallback manager -- which satisfies the same requirement. Pinned here
        because it is a subtle consequence of base-class order, not a choice
        anyone wrote down.
        """
        assert OrganizationMembership._meta.base_manager_name is None
        assert type(OrganizationMembership._base_manager) is Manager

    def test_the_scoping_methods_are_still_available(self):
        """Unscoped does not mean scope-blind: a caller that *does* want one
        organization still has ``filter_by_organization`` /
        ``for_current_organization`` on both the manager and its querysets."""
        assert hasattr(OrganizationMembership.objects, "filter_by_organization")
        assert hasattr(OrganizationMembership.objects, "for_current_organization")
        assert hasattr(OrganizationMembership.objects.all(), "filter_by_organization")


@pytest.mark.django_db
class TestMembershipLookupsWorkWithNoOrganizationBound:
    def test_nothing_is_bound(self):
        """The premise. Without this assertion every test below could pass for
        the wrong reason -- because something bound an organization first.

        Read from ``organizations.state`` (the package's own binding) rather than
        from ``common.organization_context``: the package's managers are what
        would do the scoping, and they consult the package's contextvar."""
        assert get_current_organization() is None

    def test_user_memberships_returns_rows_with_no_organization_bound(self):
        """The reverse accessor renamed by the base class, and the single most
        important query in this module: it is how a multi-organization user's
        switcher list is built, before any organization has been selected."""
        user = baker.make(User)
        first = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization), is_active=True
        )
        second = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization), is_active=True
        )

        assert get_current_organization() is None
        assert set(user.memberships.all()) == {first, second}

    def test_organization_memberships_returns_rows_with_no_organization_bound(self):
        organization = baker.make(Organization)
        membership = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization
        )

        assert get_current_organization() is None
        assert list(organization.memberships.all()) == [membership]

    def test_the_default_manager_returns_rows_across_organizations(self):
        first = OrganizationMembership.objects.create(
            user=baker.make(User), organization=baker.make(Organization)
        )
        second = OrganizationMembership.objects.create(
            user=baker.make(User), organization=baker.make(Organization)
        )

        found = set(OrganizationMembership.objects.filter(pk__in=[first.pk, second.pk]))
        assert found == {first, second}

    def test_filter_by_organization_still_narrows_on_request(self):
        organization = baker.make(Organization)
        mine = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization
        )
        OrganizationMembership.objects.create(
            user=baker.make(User), organization=baker.make(Organization)
        )

        assert list(OrganizationMembership.objects.filter_by_organization(organization)) == [mine]


@pytest.mark.django_db
class TestDomainMethodsSurviveTheReparenting:
    """The three domain methods keep working on the new base.

    ``billing_recipients`` still reads ``role`` / ``is_billing_owner`` -- Phase 3
    is what turns it into a permission-shaped query, and Phase 1c changes no
    authorization behavior.
    """

    def test_active_for_user(self):
        user = baker.make(User)
        active = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization), is_active=True
        )
        OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization), is_active=False
        )

        assert list(OrganizationMembership.objects.active_for_user(user)) == [active]

    def test_occupying_a_seat(self):
        organization = baker.make(Organization)
        occupied = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, is_active=True
        )
        OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, is_active=False
        )

        assert list(OrganizationMembership.objects.occupying_a_seat([organization.id])) == [
            occupied
        ]

    def test_billing_recipients(self):
        organization = baker.make(Organization)
        admin = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        billing_owner = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_billing_owner=True,
            is_active=True,
        )
        OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        assert set(OrganizationMembership.objects.billing_recipients(organization.id)) == {
            admin,
            billing_owner,
        }
