"""``OrganizationMembership``'s surrogate primary key, after the composite one.

Phase 1c of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
unwinds ``pk = SafeCompositePrimaryKey("user", "organization")`` back to a
surrogate ``id``, because Django cannot hang a ``ManyToManyField`` off a
composite-primary-key model and the package's ``groups`` / ``permissions`` are
exactly that.

Two things have to hold afterwards, and they pull in opposite directions:

* the ORM round-trip (``save`` -> ``refresh_from_db`` -> ``delete``) has to work
  on the surrogate key, which is what a composite PK previously provided; and
* ``uniq_membership_user_organization`` -- the unique constraint the five
  raw-SQL composite PROTECT FKs bind to -- has to survive, so ``(user,
  organization)`` is still a membership's logical identity even though it is no
  longer its primary key.

The DB-level half of the second point (that the PROTECT FKs still fire) is
proved separately, in ``calendar_integration/tests/test_membership_protect_fk.py``.
"""

import django.db.transaction
from django.db import IntegrityError, connection

import pytest
from model_bakery import baker

from tenancy.models import Organization, OrganizationMembership
from users.models import User


@pytest.mark.django_db
class TestMembershipSurrogatePrimaryKey:
    def test_primary_key_is_the_surrogate_id_field(self):
        """The model's declared primary key is ``id``, not a composite."""
        assert OrganizationMembership._meta.pk.name == "id"
        assert OrganizationMembership._meta.pk.get_internal_type() == "BigAutoField"

    def test_the_database_agrees_the_primary_key_is_id(self):
        """Pinned against the real table, not only against Django's state.

        ``0024_unwind_membership_composite_pk`` splits state from database
        (``SeparateDatabaseAndState``) precisely because Django would otherwise
        record the change without performing it, so "the ORM thinks the pk is
        ``id``" is not evidence about the column.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                WHERE c.conrelid = 'organizations_organizationmembership'::regclass
                  AND c.contype = 'p'
                ORDER BY a.attnum
                """
            )
            primary_key_columns = [row[0] for row in cursor.fetchall()]

        assert primary_key_columns == ["id"]

    def test_membership_round_trips_through_save_refresh_and_delete(self):
        user = baker.make(User)
        organization = baker.make(Organization)

        membership = OrganizationMembership.objects.create(
            user=user, organization=organization, is_active=True
        )

        assert isinstance(membership.pk, int)
        assert membership.pk == membership.id

        # save() on an existing row must UPDATE, not fall through to an INSERT.
        membership.is_active = False
        membership.save()

        membership.refresh_from_db()
        assert membership.is_active is False
        assert OrganizationMembership.objects.filter(pk=membership.pk).count() == 1

        fetched = OrganizationMembership.objects.get(pk=membership.pk)
        assert fetched == membership

        membership.delete()
        assert not OrganizationMembership.objects.filter(
            user=user, organization=organization
        ).exists()

    def test_two_memberships_in_the_same_organization_get_distinct_ids(self):
        organization = baker.make(Organization)
        first = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization
        )
        second = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization
        )

        assert first.pk != second.pk


@pytest.mark.django_db(transaction=True)
class TestUniqMembershipUserOrganizationSurvives:
    """``uniq_membership_user_organization`` still rejects a duplicate pair.

    ``transaction=True`` so the ``IntegrityError`` surfaces at the failing
    statement rather than at test teardown.
    """

    def test_duplicate_user_organization_pair_is_rejected(self):
        user = baker.make(User)
        organization = baker.make(Organization)
        OrganizationMembership.objects.create(user=user, organization=organization)

        with pytest.raises(IntegrityError, match="uniq_membership_user_organization"):
            with django.db.transaction.atomic():
                OrganizationMembership.objects.create(user=user, organization=organization)

    def test_the_constraint_still_exists_under_its_original_name(self):
        """Named explicitly because the five raw-SQL PROTECT FKs are bound to
        *this* constraint by name-independent oid, but a rename would drop and
        recreate it and take them with it."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT contype, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'organizations_organizationmembership'::regclass
                  AND conname = 'uniq_membership_user_organization'
                """
            )
            row = cursor.fetchone()

        assert row is not None, "uniq_membership_user_organization is gone"
        contype, definition = row
        assert contype == "u"
        assert definition == "UNIQUE (user_id, organization_id)"

    def test_the_same_user_may_still_join_a_second_organization(self):
        """The constraint is on the pair, not on the user."""
        user = baker.make(User)
        first = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization)
        )
        second = OrganizationMembership.objects.create(
            user=user, organization=baker.make(Organization)
        )

        assert first.pk != second.pk
        assert OrganizationMembership.objects.filter(user=user).count() == 2
