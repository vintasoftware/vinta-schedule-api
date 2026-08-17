"""Assigning ``None`` to an organization-bearing relation must not clear ``organization``.

Django's forward descriptor writes *every* local column of a relation when it is
assigned, which is what makes ``policy.calendar = calendar`` copy the calendar's
organization onto the policy. Assigning ``None`` takes the same path and writes
``None`` to both columns, so the row loses its organization --
``SafeRelationNullInitMixin`` rewrites that one case onto the concrete key
column(s) and leaves ``organization`` alone.

Two families of relation reach ``organization`` this way, and until now only one
was covered:

* ``OrganizationSafeForeignKey`` -- ``(<name>_fk, organization)``. Recognized by
  the package's ``get_organization_safe_relations`` through its ``<name>_fk``
  sibling. Covered.
* :class:`common.fields.OrganizationMembershipForeignKey` -- ``(<name>_user_id,
  organization)``. Its concrete column does *not* end in ``_fk``, so the package's
  helper never reported it and ``policy.membership = None`` went on nulling
  ``organization_id``.

The second used to surface as a NOT NULL ``IntegrityError`` on the next save. It
does not any more: ``SingleOrganizationModelMixin.save()`` fills an empty
``organization`` from the bound context, so the same assignment now silently
re-stamps the row with whatever organization happens to be bound -- which is a
cross-tenant row move, not an error. Hence the last test here.
"""

from __future__ import annotations

import pytest

from calendar_integration.models import BookingPolicy, Calendar, ExternalEventChangeRequest
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationMembership
from users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Org A")


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(name="Org B")


@pytest.fixture
def membership(organization: Organization) -> OrganizationMembership:
    user = User.objects.create_user(email="member@example.com")
    return OrganizationMembership.objects.create(user=user, organization=organization)


@pytest.fixture
def membership_policy(
    organization: Organization, membership: OrganizationMembership
) -> BookingPolicy:
    return BookingPolicy.objects.create(
        organization=organization,
        membership_user_id=membership.user_id,
        lead_time_seconds=3600,
    )


class TestAMembershipRelationKeepsItsOrganization:
    def test_nulling_it_on_a_persisted_row_leaves_organization_alone(
        self, organization, membership_policy
    ):
        membership_policy.membership = None

        assert membership_policy.organization_id == organization.id
        assert membership_policy.membership_user_id is None

    def test_the_row_is_still_in_its_own_organization_after_saving(
        self, organization, membership_policy
    ):
        membership_policy.membership = None
        # The check constraint wants exactly one target, so give it another one.
        membership_policy.is_organization_default = True
        membership_policy.save()

        membership_policy.refresh_from_db()
        assert membership_policy.organization_id == organization.id

    def test_a_foreign_binding_cannot_claim_the_row_through_the_null_assignment(
        self, organization, other_organization, membership_policy
    ):
        """The consequence the flip to ``SingleOrganizationModelMixin`` added: with
        ``organization_id`` cleared, ``save()`` fills it from the bound context
        instead of failing the NOT NULL. A row of ``organization`` saved while
        ``other_organization`` is bound must still belong to ``organization``.
        """
        membership_policy.membership = None
        membership_policy.is_organization_default = True

        with organization_context(other_organization):
            membership_policy.save()

        membership_policy.refresh_from_db()
        assert membership_policy.organization_id == organization.id

    def test_nulling_it_in_the_constructor_does_not_discard_the_organization(self, organization):
        policy = BookingPolicy(
            organization=organization, membership=None, is_organization_default=True
        )

        assert policy.organization_id == organization.id

    def test_it_covers_every_membership_relation_not_just_bookingpolicy(self, organization):
        """``resolved_by`` on ``ExternalEventChangeRequest`` is the same field class
        under a different name -- the fix keys on the relation's shape, not on a
        list of names.
        """
        request = ExternalEventChangeRequest(organization=organization)

        request.resolved_by = None

        assert request.organization_id == organization.id
        assert request.resolved_by_user_id is None


class TestASafeForeignKeyStillKeepsItsOrganization:
    """The behaviour that already worked, kept under test: the generalized lookup
    must not have dropped the ``<name>_fk`` family on its way to the membership one.
    """

    def test_nulling_a_safe_relation_leaves_organization_alone(self, organization):
        calendar = Calendar.objects.create(name="A's calendar", organization=organization)
        policy = BookingPolicy.objects.create(organization=organization, calendar=calendar)

        policy.calendar = None

        assert policy.organization_id == organization.id
        assert policy.calendar_fk_id is None

    def test_the_cleared_target_is_not_handed_back_from_the_relation_cache(self, organization):
        calendar = Calendar.objects.create(name="A's calendar", organization=organization)
        policy = BookingPolicy.objects.create(organization=organization, calendar=calendar)
        assert policy.calendar == calendar

        policy.calendar = None

        assert policy.calendar is None
