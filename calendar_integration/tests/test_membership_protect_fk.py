"""The raw-SQL composite PROTECT FK still fires after the membership PK swap.

``CalendarOwnership.membership`` is a Django ``ForeignObject`` and therefore
carries no database constraint of its own; PROTECT semantics come from a
hand-written composite foreign key added in
``calendar_integration/migrations/0026_calendarownership_membership_protect_fk``::

    ALTER TABLE calendar_integration_calendarownership
      ADD CONSTRAINT calownership_membership_protect_fk
      FOREIGN KEY (membership_user_id, organization_id)
      REFERENCES organizations_organizationmembership (user_id, organization_id)
      ON DELETE NO ACTION
      DEFERRABLE INITIALLY DEFERRED;

PostgreSQL binds such a foreign key to one *specific* unique index. Phase 1c of
the vinta-django-orgs migration drops ``OrganizationMembership``'s composite
``PRIMARY KEY (user_id, organization_id)`` and puts a surrogate ``id`` back, and
the whole reason that is cheap is the claim that this FK -- and its four
siblings -- were bound to ``uniq_membership_user_organization`` rather than to
the primary key, so nothing needs rebinding. **This module is where that claim
is checked rather than asserted.**

``DEFERRABLE INITIALLY DEFERRED`` is why the assertions below look the way they
do: the referential check runs at COMMIT, not at statement time (see 0026's
docstring for why -- a non-deferrable RESTRICT would abort organization
deletion, whose Python-side cascade collector is blind to the ForeignObject). So
the delete itself succeeds and the error arrives when the surrounding atomic
block commits, which is what ``transaction=True`` plus an explicit
``transaction.atomic()`` block reproduces.
"""

from django.db import IntegrityError, connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarOwnership
from tenancy.models import Organization, OrganizationMembership
from users.models import User


CONSTRAINT_NAME = "calownership_membership_protect_fk"


def _make_ownership() -> tuple[OrganizationMembership, CalendarOwnership]:
    organization = baker.make(Organization)
    membership = OrganizationMembership.objects.create(
        user=baker.make(User), organization=organization
    )
    calendar = baker.make(Calendar, organization=organization)
    ownership = CalendarOwnership.objects.create(
        organization=organization,
        calendar_fk=calendar,
        membership_user_id=membership.user_id,
    )
    return membership, ownership


@pytest.mark.django_db(transaction=True)
class TestMembershipProtectForeignKeySurvivesThePrimaryKeySwap:
    def test_deleting_a_membership_with_a_live_ownership_is_refused(self):
        """The PROTECT guarantee itself: the row cannot go while it is referenced."""
        membership, ownership = _make_ownership()
        # Captured up front: ``Model.delete()`` clears the instance's pk in
        # Python before the transaction is asked to commit, so reading
        # ``membership.pk`` after the raise would silently query ``pk=None``.
        membership_pk = membership.pk

        with pytest.raises(IntegrityError, match=CONSTRAINT_NAME):
            with transaction.atomic():
                membership.delete()

        # Nothing was lost: the failed COMMIT rolled the whole block back.
        assert OrganizationMembership.objects.filter(pk=membership_pk).exists()
        assert CalendarOwnership.original_manager.filter(pk=ownership.pk).exists()

    def test_the_guard_is_the_constraint_and_not_the_test_setup(self):
        """Proves the test above can actually go red.

        Same delete, with the referencing ownership removed first, must succeed.
        Without this, ``test_deleting_a_membership_with_a_live_ownership_is_refused``
        would still pass if the delete were failing for some unrelated reason,
        or if the fixture were not really wiring the ownership to the membership.
        """
        membership, ownership = _make_ownership()
        membership_pk = membership.pk

        with transaction.atomic():
            ownership.delete()
            membership.delete()

        assert not OrganizationMembership.objects.filter(pk=membership_pk).exists()

    def test_deleting_membership_and_ownership_together_still_commits(self):
        """The reason the constraint is DEFERRABLE: an organization delete
        cascades to both rows in one transaction, in an order Django chooses and
        that this constraint cannot see. Deferring the check to COMMIT is what
        lets that succeed while a membership-only delete still fails."""
        membership, _ownership = _make_ownership()
        organization = membership.organization
        membership_pk = membership.pk
        organization_pk = organization.pk

        organization.delete()

        assert not OrganizationMembership.objects.filter(pk=membership_pk).exists()
        assert not Organization.objects.filter(pk=organization_pk).exists()

    def test_the_constraint_is_bound_to_the_unique_constraint_not_the_primary_key(self):
        """The load-bearing fact behind "no rebind was needed".

        ``pg_constraint.conindid`` records which unique index a foreign key was
        bound to. All five membership PROTECT FKs must name
        ``uniq_membership_user_organization``; any of them naming
        ``organizations_organizationmembership_pkey`` would mean the primary-key
        swap in ``tenancy/migrations/0024_unwind_membership_composite_pk`` had
        pulled the index out from under it.

        Scoped by ``confrelid`` + ``contype = 'f'`` alone -- no ``conname LIKE
        '%protect_fk'`` filter. A name pattern would hide any future composite
        FK targeting this table under a different name, which is exactly the
        case that would silently (and wrongly) bind to the PK index instead of
        the unique constraint. Dropping the filter also surfaces the two plain
        Django-managed FKs Phase 1c's ``groups``/``permissions`` M2Ms added
        (the through tables' FK to the membership side) -- correctly bound to
        the PK, unlike the five hand-written composite ones, and accounted for
        below so this is a complete inventory rather than a re-narrowed one;
        asserting every FK found (five pinned by name plus a count-and-target
        check on the rest) also catches an unexpected eighth.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.conname, i.relname
                FROM pg_constraint c
                JOIN pg_class i ON i.oid = c.conindid
                WHERE c.confrelid = 'organizations_organizationmembership'::regclass
                  AND c.contype = 'f'
                ORDER BY c.conname
                """
            )
            bindings = dict(cursor.fetchall())

        protect_fk_bindings = {
            name: target for name, target in bindings.items() if name.endswith("_protect_fk")
        }
        assert protect_fk_bindings == {
            "bookingpolicy_membership_protect_fk": "uniq_membership_user_organization",
            "calmgmttoken_membership_protect_fk": "uniq_membership_user_organization",
            "calownership_membership_protect_fk": "uniq_membership_user_organization",
            "evattendance_membership_protect_fk": "uniq_membership_user_organization",
            "externaleventcr_resolved_by_protect_fk": "uniq_membership_user_organization",
        }

        # The two M2M through-table FKs are the "unexpected sixth" (here,
        # eighth) this broader query is meant to catch if a future composite
        # FK bound to the wrong index without carrying the `_protect_fk`
        # suffix. Asserted separately, by their actual (autogenerated) names,
        # rather than folded into the dict above -- their names are not
        # something this test should pin, only their count and target index.
        other_bindings = {
            name: target for name, target in bindings.items() if name not in protect_fk_bindings
        }
        assert len(other_bindings) == 2
        assert set(other_bindings.values()) == {"organizations_organizationmembership_pkey"}
