"""``payments/migrations/0024_move_billing_to_vinta_billing.py`` -- the twenty-table
move onto ``vinta-django-billing``, and its reverse.

Every assertion here reads the *database this test session is running against*,
which pytest-django built by running ``migrate`` from an empty database. That is
the property the whole migration has to hold: it runs unattended, with no
``--fake-initial`` and no operator step, in every environment including CI's
throwaway database. A test that stubbed the migration out would not be testing
that.

Three failures this file exists to catch, none of which announces itself:

* **A missed ``setval``.** Copying rows keeps their primary keys but does not move
  the identity sequence, so the next ``INSERT`` collides on a key that already
  exists -- in production, long after the migration reported success. Row counts
  cannot see this, so ``TestSequencesAllocateAboveTheCopiedRows`` inserts real
  rows and reads the keys they get.
* **A wrong column list.** ``payments_*`` and ``vinta_billing_*`` hold the same
  columns in *different orders*, so a copy that leaned on position rather than
  name would land plausible values in the wrong columns.
  ``test_the_seeded_catalog_survived_with_its_values_intact`` pins values, not
  just counts.
* **A grant left behind.** The ``manage_billing`` permission moved content types.
  A group still pointing at the old one holds a permission on a model that no
  longer exists, and every billing endpoint 403s for its members.
"""

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection

import pytest
from vinta_billing.models import BillingAddress, BillingPlan, PlanEntitlement, PlanLimit

from payments.billing_constants import Entitlement, LimitedResource


#: The twenty tables, as ``0024`` names them.
EXPECTED_TABLES = {
    "billingaddress",
    "billingperiodresourceusage",
    "billingperiodsummary",
    "billingplan",
    "billingprofile",
    "limitwarningnotification",
    "meteredoccurrence",
    "payment",
    "paymentmethod",
    "paymentstatusupdate",
    "planentitlement",
    "planlimit",
    "providerwebhookevent",
    "refund",
    "refundstatusupdate",
    "subscription",
    "subscriptionaddon",
    "subscriptionentitlement",
    "subscriptionplanlimit",
    "subscriptionstatusupdate",
}

GROUPS_HOLDING_MANAGE_BILLING = {"organization_admin", "organization_billing_owner"}


def _table_names(prefix: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name LIKE %s",
            [f"{prefix}%"],
        )
        return {row[0].removeprefix(prefix) for row in cursor.fetchall()}


def _count(table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{table}"')  # noqa: S608
        return cursor.fetchone()[0]


@pytest.mark.django_db
class TestTheTablesMoved:
    def test_migrate_from_zero_lands_the_twenty_vinta_billing_tables(self):
        assert _table_names("vinta_billing_") == EXPECTED_TABLES

    def test_and_leaves_no_payments_table_behind(self):
        """Not "some are gone" -- none. A survivor would be a table nothing reads
        and nothing maintains, holding a stale copy of live billing data."""
        assert _table_names("payments_") == set()

    def test_the_seeded_catalog_survived_with_its_values_intact(self):
        """``0007_seed_billing_plans`` writes the ``unlimited`` plan and a row per
        limited resource / entitlement *before* ``0024`` runs, so these rows made
        the trip. Values, not just counts: a positionally-shifted copy would keep
        the count and corrupt the content.
        """
        plan = BillingPlan.objects.get(slug="unlimited")

        assert plan.is_active is True
        assert plan.is_default_for_new_organizations is True
        assert {limit.resource_key for limit in plan.limits.all()} == set(LimitedResource.values)
        assert all(limit.limit_value is None for limit in plan.limits.all())
        assert {row.entitlement_key for row in plan.entitlements.all()} == set(Entitlement.values)
        assert all(row.is_enabled for row in plan.entitlements.all())

    def test_the_copied_rows_kept_their_primary_keys(self):
        """The copy carries ``id`` explicitly rather than letting the destination
        allocate a new one. Anything holding a billing primary key -- a provider's
        records, an operator's runbook, a ``BillingPeriodSummary.payment_id`` --
        still resolves.
        """
        plan = BillingPlan.objects.get(slug="unlimited")

        assert PlanLimit.objects.filter(plan_id=plan.pk).count() == len(LimitedResource.values)
        assert PlanEntitlement.objects.filter(plan_id=plan.pk).count() == len(Entitlement.values)


@pytest.mark.django_db
class TestSequencesAllocateAboveTheCopiedRows:
    """The silent one. A missed ``setval`` leaves the identity sequence at 1 while
    the table already holds rows with those keys, and nothing fails until the next
    insert -- with a duplicate-key error, in whichever environment writes first.
    Counting rows cannot see it; allocating one can.
    """

    def test_a_new_billing_plan_gets_a_key_above_every_copied_one(self):
        highest = BillingPlan.objects.order_by("-pk").first()
        assert highest is not None, "no copied rows, so this test would prove nothing"

        created = BillingPlan.objects.create(
            slug="sequence-probe",
            name="Sequence probe",
            monthly_price=0,
            currency="BRL",
        )

        assert created.pk > highest.pk

    def test_the_same_holds_for_a_table_the_seed_migrations_left_empty(self):
        """``billingaddress`` has no seeded rows, so its sequence was ``setval``-ed
        against an empty table. ``setval(..., 0 + 1, is_called=False)`` is what
        keeps that legal -- a sequence cannot be set to 0."""
        assert BillingAddress.objects.count() == 0

        created = BillingAddress.objects.create(
            street_name="Rua A",
            street_number="10",
            city="Sao Paulo",
            state="SP",
            country="BR",
            zip_code="01000-000",
        )

        assert created.pk >= 1

    def test_two_consecutive_inserts_do_not_collide(self):
        first = BillingAddress.objects.create(
            street_name="Rua B",
            street_number="20",
            city="Sao Paulo",
            state="SP",
            country="BR",
            zip_code="01000-001",
        )
        second = BillingAddress.objects.create(
            street_name="Rua C",
            street_number="30",
            city="Sao Paulo",
            state="SP",
            country="BR",
            zip_code="01000-002",
        )

        assert second.pk > first.pk


@pytest.mark.django_db
class TestManageBillingMovedContentType:
    def test_both_groups_hold_the_permission_on_the_new_content_type(self):
        for group_name in GROUPS_HOLDING_MANAGE_BILLING:
            group = Group.objects.get(name=group_name)
            labels = {
                f"{permission.content_type.app_label}.{permission.codename}"
                for permission in group.permissions.select_related("content_type")
            }
            assert "vinta_billing.manage_billing" in labels

    def test_nothing_holds_the_old_permission_because_it_no_longer_exists(self):
        assert not Permission.objects.filter(
            content_type__app_label="payments", codename="manage_billing"
        ).exists()

    def test_the_payments_subscription_content_type_is_gone(self):
        """Left behind, it would keep answering ``ContentType.objects.get(...)``
        lookups for a model Django no longer has, which is how a stale grant comes
        back."""
        assert not ContentType.objects.filter(app_label="payments", model="subscription").exists()

    def test_the_catalog_and_the_database_name_the_same_permission(self):
        """``organizations.permission_catalog`` is what every runtime check reads;
        ``organizations.0028`` (frozen, still naming ``payments``) is what seeded
        the row. ``0024`` is the bridge, and this is the assertion that it did not
        leave the two disagreeing."""
        from organizations.permission_catalog import MANAGE_BILLING

        assert MANAGE_BILLING == "vinta_billing.manage_billing"
        app_label, codename = MANAGE_BILLING.split(".")
        assert Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).exists()


@pytest.mark.no_auto_subscription
@pytest.mark.no_billing_catalog_reseed
@pytest.mark.timeout(300)
@pytest.mark.django_db(transaction=True)
class TestTheReversePath:
    """``migrate payments 0022`` is this plan's rollback lever -- there is no
    feature flag -- so it is exercised for real here, against this session's
    database, rather than asserted structurally.

    The reverse also unapplies ``vinta_billing.0002`` and ``0001``. That is not
    incidental: ``0023``'s ``run_before`` edge makes the package's initial
    migration depend on it, and it has to, because ``0023``'s own reverse re-adds
    the thirteen constraint and index names ``vinta_billing_*`` would otherwise
    still be holding. Postgres namespaces those per schema, not per table.

    The forward re-apply in ``finally`` is what keeps this test from poisoning
    every test that runs after it in the same xdist worker.
    """

    def test_reverse_restores_payments_then_forward_restores_vinta_billing(self):
        seeded = BillingAddress.objects.create(
            street_name="Reversible",
            street_number="1",
            city="Sao Paulo",
            state="SP",
            country="BR",
            zip_code="01000-000",
        )
        before = {table: _count(f"vinta_billing_{table}") for table in EXPECTED_TABLES}
        assert before["billingaddress"] >= 1
        assert before["billingplan"] >= 1

        try:
            call_command("migrate", "payments", "0022_capability_permissions", verbosity=0)

            assert _table_names("vinta_billing_") == set(), (
                "reversing past 0023 must take the package's tables with it, or "
                "re-adding the constraint names would collide"
            )
            assert _table_names("payments_") == EXPECTED_TABLES
            after_reverse = {table: _count(f"payments_{table}") for table in EXPECTED_TABLES}
            assert after_reverse == before

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT street_name, zip_code FROM payments_billingaddress WHERE id = %s",
                    [seeded.pk],
                )
                assert cursor.fetchone() == ("Reversible", "01000-000")

                cursor.execute(
                    "SELECT count(*) FROM auth_group g "
                    "JOIN auth_group_permissions gp ON gp.group_id = g.id "
                    "JOIN auth_permission p ON p.id = gp.permission_id "
                    "JOIN django_content_type ct ON ct.id = p.content_type_id "
                    "WHERE ct.app_label = 'payments' AND p.codename = 'manage_billing'"
                )
                assert cursor.fetchone()[0] == len(GROUPS_HOLDING_MANAGE_BILLING)
        finally:
            call_command("migrate", verbosity=0)

        assert _table_names("payments_") == set()
        assert {table: _count(f"vinta_billing_{table}") for table in EXPECTED_TABLES} == before
        assert BillingAddress.objects.filter(pk=seeded.pk).exists()
