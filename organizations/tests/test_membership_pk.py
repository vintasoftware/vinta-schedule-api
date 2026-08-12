"""``OrganizationMembership``'s surrogate primary key, and what outlived it.

The composite ``(user, organization)`` primary key added in
``0013_organizationmembership_composite_pk`` is gone: Django cannot hang a
``ManyToManyField`` off a composite-PK model, and the package's abstract
membership base declares two (``groups`` and ``permissions``).

What did **not** go is ``uniq_membership_user_organization``. Five raw-SQL
composite foreign keys in ``calendar_integration`` reference
``(user_id, organization_id)`` and bind to that *constraint*, not to the primary
key -- which is what made the swap cheap. That they still fire is covered
separately, in ``calendar_integration/tests/test_membership_protect_fk.py``;
what is covered here is that the constraint itself is still enforced, and that
an ordinary membership round-trips on the surrogate key.
"""

from django.db import IntegrityError, transaction

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import User


@pytest.mark.django_db
class TestSurrogatePrimaryKey:
    def test_the_primary_key_is_a_single_integer_id(self):
        membership = OrganizationMembership.objects.create(
            user=baker.make(User), organization=baker.make(Organization)
        )

        assert OrganizationMembership._meta.pk.name == "id"
        assert isinstance(membership.pk, int)
        assert membership.pk == membership.id

    def test_a_membership_round_trips_through_save_refresh_and_delete(self):
        membership = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=baker.make(Organization),
            role=OrganizationRole.MEMBER,
        )
        pk = membership.pk

        membership.role = OrganizationRole.ADMIN
        membership.save()
        membership.refresh_from_db()
        assert membership.role == OrganizationRole.ADMIN
        assert membership.pk == pk

        assert OrganizationMembership.objects.get(pk=pk) == membership

        membership.delete()
        assert not OrganizationMembership.objects.filter(pk=pk).exists()

    def test_two_memberships_of_the_same_user_get_distinct_primary_keys(self):
        user = baker.make(User)
        first = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization)
        )
        second = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization)
        )

        assert first.pk != second.pk

    def test_the_groups_and_permissions_relations_exist_and_are_empty(self):
        """The whole reason the composite primary key had to go.

        They are declared and unused: nothing reads them until Phase 3, and
        every authorization decision still goes through ``role`` /
        ``is_billing_owner``.
        """
        membership = OrganizationMembership.objects.create(
            user=baker.make(User), organization=baker.make(Organization)
        )

        assert membership.groups.count() == 0
        assert membership.permissions.count() == 0


@pytest.mark.django_db(transaction=True)
class TestUniqMembershipUserOrganizationSurvives:
    """``transaction=True`` so the DB-level violation raises at the failing
    statement rather than at teardown."""

    def test_a_duplicate_user_organization_pair_is_still_rejected(self):
        user = baker.make(User)
        organization = baker.make(Organization)
        OrganizationMembership.objects.create(user=user, organization=organization)

        with pytest.raises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(user=user, organization=organization)

    def test_the_constraint_is_still_declared_under_its_original_name(self):
        """The name is load-bearing: five raw-SQL foreign keys were created
        against this constraint and PostgreSQL binds each to one specific
        index. Renaming it would drop them."""
        names = {constraint.name for constraint in OrganizationMembership._meta.constraints}
        assert "uniq_membership_user_organization" in names

    def test_the_same_user_may_still_join_a_second_organization(self):
        user = baker.make(User)
        OrganizationMembership.objects.create(user=user, organization=baker.make(Organization))
        OrganizationMembership.objects.create(user=user, organization=baker.make(Organization))

        assert OrganizationMembership.objects.filter(user=user).count() == 2
