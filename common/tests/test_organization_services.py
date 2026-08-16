"""The package's service subclasses are bound to *this* project's models.

``vinta-django-orgs`` ``0.4.0`` deleted ``vinta_orgs.helpers`` and replaced its
free functions with generics a project specializes once. Both halves of that
specialization can go wrong quietly, which is why they are asserted here rather
than left to the first caller:

* ``model_class`` is a plain class attribute. Pointing it at the package's own
  concrete ``vinta_orgs.Organization`` instead of ours would still *run* --
  the services resolve ``ORGANIZATION_MODEL`` at runtime regardless -- and only
  the static types would be wrong.
* ``MembershipService`` derives its organization model from the membership
  model's ``organization`` foreign key, so the two services cannot drift apart.
  Pinning that here is what makes the missing constructor argument safe.

The resolution table itself belongs to the package and is exercised where this
project consumes it; the smoke test below only proves these instances are wired
to the right tables.
"""

from __future__ import annotations

import pytest
from model_bakery import baker

from common.organization_services import memberships, organizations
from organizations.models import Organization, OrganizationMembership
from users.models import User


class TestTheDeclarations:
    def test_the_organization_service_is_bound_to_our_organization_model(self):
        assert organizations.model is Organization

    def test_the_membership_service_is_bound_to_our_membership_model(self):
        assert memberships.model is OrganizationMembership

    def test_the_membership_service_derives_our_organization_model_from_the_fk(self):
        assert memberships.organization_model is Organization


@pytest.mark.django_db
class TestResolutionReachesOurTables:
    def test_a_sole_active_membership_resolves_without_a_selection(self):
        user = baker.make(User)
        organization = baker.make(Organization)
        membership = OrganizationMembership.objects.create(user=user, organization=organization)

        assert memberships.resolve_for_user(user) == membership
        assert memberships.resolve_organization_for_user(user) == organization

    def test_an_inactive_membership_resolves_to_nothing(self):
        """``is_active`` is the membership's soft delete.

        The permission backend filters on it inside its own lookup, so a
        resolver that ignored it would hand a caller a membership the backend
        refuses to grant anything for.
        """
        user = baker.make(User)
        organization = baker.make(Organization)
        OrganizationMembership.objects.create(user=user, organization=organization, is_active=False)

        assert memberships.resolve_for_user(user) is None
