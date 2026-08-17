"""All five raw-SQL composite PROTECT FKs still fire after the membership PK swap.

``OrganizationMembership`` traded its composite ``(user, organization)`` primary
key for a surrogate ``id`` (organizations ``0023``). Five migrations in this app
declare, in raw SQL::

    FOREIGN KEY (<name>_user_id, organization_id)
    REFERENCES organizations_organizationmembership (user_id, organization_id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED

PostgreSQL binds each of those to one specific unique index. The whole reason the
primary-key swap was cheap is that they are bound to the
``uniq_membership_user_organization`` *constraint*, which was kept, rather than
to the primary-key index, which was dropped and rebuilt over a different column.
That claim is worth more than an argument, so this module checks it two ways:

1. **Structurally** -- ``pg_constraint.conindid`` names the index each foreign
   key is actually bound to. If any of the five were bound to the primary key,
   the ``DROP CONSTRAINT`` in ``0023`` would have failed outright; this asserts
   the surviving state directly rather than inferring it from a migration that
   did not error.
2. **Behaviourally** -- deleting a membership that a live row references still
   raises, per constraint.

The sibling modules ``test_ownership_protect_fk.py``,
``test_attendance_protect_fk.py`` and ``test_management_token_protect_fk.py``
cover three of these five in much more depth (orphan NULLs, cascade behaviour,
partial uniques). This module deliberately does not repeat them: it covers all
five at the one property the primary-key swap could have broken.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import Group, Permission
from django.db import IntegrityError, connection, transaction

import pytest
from model_bakery import baker

from calendar_integration.factories import create_calendar_ownership, create_event_attendance
from calendar_integration.models import (
    BookingPolicy,
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    ExternalEventChangeRequest,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization, OrganizationMembership
from users.models import User


#: constraint name -> the table that carries it. All five, enumerated as
#: literals: deriving them from ``pg_constraint`` would make the count
#: self-fulfilling, and "there are five" is itself part of what this pins (an
#: earlier revision of the plan said two, then three).
PROTECT_FK_CONSTRAINTS = {
    "calownership_membership_protect_fk": "calendar_integration_calendarownership",
    "evattendance_membership_protect_fk": "calendar_integration_eventattendance",
    "calmgmttoken_membership_protect_fk": "calendar_integration_calendarmanagementtoken",
    "externaleventcr_resolved_by_protect_fk": ("calendar_integration_externaleventchangerequest"),
    "bookingpolicy_membership_protect_fk": "calendar_integration_bookingpolicy",
}

MEMBERSHIP_TABLE = "organizations_organizationmembership"
MEMBERSHIP_UNIQUE_CONSTRAINT = "uniq_membership_user_organization"


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def member_user(organization) -> User:
    user: User = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=organization)
    return user


@pytest.fixture
def membership(organization, member_user) -> OrganizationMembership:
    return OrganizationMembership.objects.get(user=member_user, organization=organization)


@pytest.fixture
def calendar(organization) -> Calendar:
    return baker.make(Calendar, organization=organization)


@pytest.fixture
def event(organization) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        title="Protect FK Event",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
    )


def _composite_membership_foreign_keys() -> dict[str, tuple[str, str]]:
    """``{constraint_name: (referencing_table, bound_index_name)}`` from PostgreSQL."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.conname, c.conrelid::regclass::text, i.relname
            FROM pg_constraint c
            JOIN pg_class i ON i.oid = c.conindid
            WHERE c.contype = 'f'
              AND c.confrelid = %s::regclass
              AND array_length(c.conkey, 1) = 2
            """,
            [MEMBERSHIP_TABLE],
        )
        return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}


@pytest.mark.django_db
class TestTheFiveConstraintsSurvivedThePrimaryKeySwap:
    def test_all_five_still_exist_on_their_original_tables(self):
        found = _composite_membership_foreign_keys()

        for constraint_name, table in PROTECT_FK_CONSTRAINTS.items():
            assert constraint_name in found, (
                f"{constraint_name} is gone. It was created against "
                f"{MEMBERSHIP_UNIQUE_CONSTRAINT}; dropping or renaming that "
                "constraint takes the foreign key with it."
            )
            assert found[constraint_name][0] == table

    def test_every_one_binds_to_the_unique_constraint_not_the_primary_key(self):
        """The property that made the primary-key swap free.

        A two-column foreign key cannot rebind to the new single-column
        primary key, so this assertion cannot fail by that specific
        mechanism today. Its real value is as a rename tripwire: it fails if
        ``uniq_membership_user_organization`` is ever renamed or replaced,
        which would silently detach these five foreign keys from the
        constraint they depend on without anything else here noticing.
        """
        found = _composite_membership_foreign_keys()

        for constraint_name in PROTECT_FK_CONSTRAINTS:
            assert found[constraint_name][1] == MEMBERSHIP_UNIQUE_CONSTRAINT


@pytest.mark.django_db
class TestEachConstraintStillBlocksAMembershipDelete:
    """One referencing row per constraint, then a membership delete that must raise.

    The constraints are ``DEFERRABLE INITIALLY DEFERRED``, so they normally fire
    at COMMIT rather than at the failing statement. Rather than pay for
    ``transaction=True`` (which disables the rolled-back test transaction and
    truncates every table afterwards), each test forces immediate checking with
    ``SET CONSTRAINTS ALL IMMEDIATE`` before the delete, so the violation still
    raises synchronously, inside the fast, rolled-back transaction.
    """

    def test_calendar_ownership_blocks_the_delete(self, organization, member_user, calendar):
        create_calendar_ownership(calendar=calendar, user=member_user)
        membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with (
            pytest.raises(IntegrityError, match="calownership_membership_protect_fk"),
            transaction.atomic(),
        ):
            membership.delete()

    def test_event_attendance_blocks_the_delete(self, organization, member_user, event):
        create_event_attendance(event=event, user=member_user)
        membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with (
            pytest.raises(IntegrityError, match="evattendance_membership_protect_fk"),
            transaction.atomic(),
        ):
            membership.delete()

    def test_calendar_management_token_blocks_the_delete(self, organization, member_user, calendar):
        CalendarManagementToken.objects.create(
            organization=organization,
            calendar_fk=calendar,
            membership_user_id=member_user.id,
            token_hash=hash_long_lived_token(generate_long_lived_token()),
        )
        membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with (
            pytest.raises(IntegrityError, match="calmgmttoken_membership_protect_fk"),
            transaction.atomic(),
        ):
            membership.delete()

    def test_external_event_change_request_resolved_by_blocks_the_delete(
        self, organization, member_user, event
    ):
        ExternalEventChangeRequest.objects.create(
            organization=organization,
            event_fk=event,
            kind="update",
            status="approved",
            provider="google",
            resolved_by_user_id=member_user.id,
        )
        membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with (
            pytest.raises(IntegrityError, match="externaleventcr_resolved_by_protect_fk"),
            transaction.atomic(),
        ):
            membership.delete()

    def test_booking_policy_blocks_the_delete(self, organization, member_user):
        BookingPolicy.objects.create(
            organization=organization,
            membership_user_id=member_user.id,
        )
        membership = OrganizationMembership.objects.get(user=member_user, organization=organization)

        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with (
            pytest.raises(IntegrityError, match="bookingpolicy_membership_protect_fk"),
            transaction.atomic(),
        ):
            membership.delete()


@pytest.mark.django_db
class TestTheManyToManyThroughTablesBindToThePrimaryKey:
    """The two relations the primary-key swap was performed *for*.

    ``groups`` and ``permissions`` are ordinary many-to-many fields, so their
    through tables carry a plain single-column foreign key to
    ``organizations_organizationmembership.id``. Asserted separately from the
    five composite ones above so a future reader does not mistake them for a
    sixth and a seventh PROTECT FK.
    """

    def test_the_through_tables_reference_the_surrogate_primary_key(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.conname, i.relname
                FROM pg_constraint c
                JOIN pg_class i ON i.oid = c.conindid
                WHERE c.contype = 'f'
                  AND c.confrelid = %s::regclass
                  AND array_length(c.conkey, 1) = 1
                """,
                [MEMBERSHIP_TABLE],
            )
            single_column_foreign_keys = dict(cursor.fetchall())

        assert len(single_column_foreign_keys) == 2
        assert set(single_column_foreign_keys.values()) == {f"{MEMBERSHIP_TABLE}_pkey"}

    def test_deleting_a_membership_clears_its_group_and_permission_rows(
        self, organization, member_user
    ):
        """The through-table rows -- not just the membership row -- must be
        gone after the delete, proving the CASCADE actually ran rather than
        merely that the membership itself is gone."""
        membership = OrganizationMembership.objects.get(user=member_user, organization=organization)
        membership.groups.add(Group.objects.create(name="through-table-group"))
        membership.permissions.add(baker.make(Permission))

        assert membership.groups.count() == 1
        assert membership.permissions.count() == 1
        groups_through_table = OrganizationMembership.groups.through
        permissions_through_table = OrganizationMembership.permissions.through

        membership.delete()

        assert not OrganizationMembership.objects.filter(pk=membership.pk).exists()
        assert not groups_through_table.objects.filter(
            organizationmembership_id=membership.pk
        ).exists()
        assert not permissions_through_table.objects.filter(
            organizationmembership_id=membership.pk
        ).exists()


@pytest.mark.django_db
class TestUniqMembershipUserOrganizationSurvives:
    """The constraint the five raw-SQL FKs above bind to, still enforced.

    This class used to live beside the model in
    ``organizations/tests/test_membership_pk.py``. It moved here because
    ``uniq_membership_user_organization`` is not just "the" membership
    uniqueness rule -- the whole reason it survived the primary-key swap
    (rather than being replaced by the base class's own
    ``unique_together``) is that the five composite ``PROTECT`` foreign keys
    in this module are bound to it by name. Its invariant belongs with its
    reason to exist.

    Unlike the delete-blocking tests above, this constraint is an ordinary
    (non-deferrable) ``UniqueConstraint``: it fires at the failing
    ``INSERT``, not at ``COMMIT``, so no ``SET CONSTRAINTS ALL IMMEDIATE``
    is needed to observe it inside the fast, rolled-back test transaction.
    """

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
        assert MEMBERSHIP_UNIQUE_CONSTRAINT in names

    def test_the_same_user_may_still_join_a_second_organization(self):
        user = baker.make(User)
        OrganizationMembership.objects.create(user=user, organization=baker.make(Organization))
        OrganizationMembership.objects.create(user=user, organization=baker.make(Organization))

        assert OrganizationMembership.objects.filter(user=user).count() == 2
