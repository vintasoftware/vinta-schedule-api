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

from common.organization_services import Memberships, Organizations, memberships, organizations
from organizations.models import Organization, OrganizationMembership
from users.models import User


class TestTheDeclarations:
    """Asserted on the *declarations*, not on the runtime resolution.

    ``organizations.model`` and ``memberships.organization_model`` both come
    from ``get_organization_model()``, which reads ``ORGANIZATION_MODEL`` and
    only ``issubclass``-checks the declared ``model_class`` against it. So they
    answer ``Organization`` whatever ``model_class`` names, as long as it is a
    superclass -- a settings lookup dressed up as a declaration check. The class
    attributes are what a wrong declaration actually changes, so they are what
    is pinned (the same way ``common/tests/test_organization_context.py`` pins
    ``ProjectOrganizationState.model_class``).
    """

    def test_the_organization_service_declares_our_organization_model(self):
        assert Organizations.model_class is Organization
        # Weaker (it is the settings lookup), but it pins that the declaration
        # and ``ORGANIZATION_MODEL`` have not been pointed at different classes.
        assert organizations.model is Organization

    def test_the_membership_service_declares_our_membership_model(self):
        assert Memberships.model_class is OrganizationMembership
        assert memberships.model is OrganizationMembership

    def test_the_membership_fk_our_organization_model_is_derived_from_points_at_it(self):
        """``MembershipService`` takes no organization argument on purpose.

        It reads ``model_class``'s ``organization`` foreign key and refuses to
        construct if the target is not ``ORGANIZATION_MODEL``. That is only safe
        while the foreign key really does point at our model, so the field is
        asserted rather than the derived attribute -- which would answer
        ``Organization`` from settings regardless.
        """
        assert OrganizationMembership._meta.get_field("organization").related_model is Organization
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
