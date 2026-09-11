"""The 0.7-to-0.8 upgrade, replayed with data in the database.

``billing_integration/0002_backfill_scopes_from_organizations`` only ever runs
on a database that carries 0.7 billing rows. CI builds every database from zero,
where it is a no-op, so nothing here was exercised until a staging deploy ran
it -- which is the failure this test exists to keep from happening twice:

    RuntimeError: vinta_billing has 4 organization(s) to migrate onto scopes,
    but BILLING_SCOPE_MODEL points at a scope model with no generic key

The shape under test is the one from ``billing_integration/README.md``: step the
graph back to before ``vinta_billing`` had scopes at all, seed an organization
tree and the billing rows that name it, then run the three commands the deploy
runs and check what came out.

The tree is not decoration. A reseller with two children, neither of which has a
billing row of its own, is the case where a flat backfill is wrong in two ways
at once -- no ``parent`` links, so each child becomes its own billing root and
stops pooling into the reseller's ceiling; and the reseller's usage breakdown,
which is keyed by *child*, comes out empty because those children never got
scopes. Both are silent, and both under-charge the paying root.
"""

import json

from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

import pytest

from common.testing.migration_replay import migration_replay, uninterruptible


#: Well clear of anything a fixture or another test allocates, so the rows this
#: seeds are unmistakably its own and can be deleted by range at the end.
RESELLER_ID = 990001
CHILD_A_ID = 990002
CHILD_B_ID = 990003
STANDALONE_ID = 990004

#: The state before ``vinta_billing`` knew what a scope was. ``payments.0024``
#: stays applied, so the billing tables are already ``vinta_billing_*`` and
#: still carry their ``organization_id`` columns -- exactly a 0.7 installation
#: that has taken the table move but not the scope re-key.
PRE_SCOPE_TARGET = "0002_manage_billing_permission"


def _seed_pre_scope_rows(plan_id: int) -> None:
    """Four organizations and the billing rows naming them, in raw SQL.

    Raw because the live models cannot express this state: they have
    ``scope_id``, and at this point in the graph the tables have
    ``organization_id`` instead.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO organizations_organization
                (id, created, modified, name, should_sync_rooms,
                 can_invite_organizations, parent_id, slug)
            VALUES
                (%s, now(), now(), 'Upgrade reseller',   false, true,  NULL, 'upgrade-reseller'),
                (%s, now(), now(), 'Upgrade child A',    false, false, %s,   'upgrade-child-a'),
                (%s, now(), now(), 'Upgrade child B',    false, false, %s,   'upgrade-child-b'),
                (%s, now(), now(), 'Upgrade standalone', false, false, NULL, 'upgrade-standalone')
            """,
            [
                RESELLER_ID,
                CHILD_A_ID,
                RESELLER_ID,
                CHILD_B_ID,
                RESELLER_ID,
                STANDALONE_ID,
            ],
        )
        cursor.execute(
            """
            INSERT INTO vinta_billing_subscription
                (created, modified, meta, status, billing_state, billing_interval,
                 current_period_start, current_period_end, external_id, plan_external_id,
                 payment_provider, pending_billing_interval,
                 plan_change_pending_confirmation, plan_id, organization_id)
            VALUES
                (now(), now(), '{}', 'active', 'current', 'monthly', now(),
                 now() + interval '30 days', 'upgrade-sub-reseller', '', 'stripe', '',
                 false, %s, %s),
                (now(), now(), '{}', 'active', 'current', 'monthly', now(),
                 now() + interval '30 days', 'upgrade-sub-standalone', '', 'stripe', '',
                 false, %s, %s)
            """,
            [plan_id, RESELLER_ID, plan_id, STANDALONE_ID],
        )
        cursor.execute(
            """
            INSERT INTO vinta_billing_billingperiodsummary
                (created, modified, meta, billing_period_start, billing_period_end,
                 plan_slug, plan_name, billing_interval, currency, overage_total,
                 charged, reconciliation_unmetered, reconciliation_orphaned,
                 closed_at, subscription_id, organization_id)
            SELECT now(), now(), '{}', now() - interval '30 days', now(),
                   'upgrade-probe', 'Upgrade probe', 'monthly', 'usd', 0, false, 0, 0,
                   now(), s.id, %s
            FROM vinta_billing_subscription s
            WHERE s.external_id = 'upgrade-sub-reseller'
            RETURNING id
            """,
            [RESELLER_ID],
        )
        summary_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO vinta_billing_billingperiodresourceusage
                (created, modified, meta, resource_key, summary_id, by_organization)
            VALUES (now(), now(), '{}', 'upgrade_probe', %s, %s::jsonb)
            """,
            [
                summary_id,
                # The reseller's own usage plus its two children's. Only the
                # reseller has a billing row, so a backfill that makes scopes
                # for billing rows alone drops two thirds of this.
                json.dumps({str(RESELLER_ID): 5, str(CHILD_A_ID): 3, str(CHILD_B_ID): 2}),
            ],
        )


def _run_the_documented_deploy() -> None:
    """The three commands from ``billing_integration/README.md``, verbatim.

    Step 2 is conditional for the reason the README gives: ``migrate
    vinta_billing 0005 --fake`` means "migrate *to* 0005", so on a database
    where ``0006`` is already applied it fake-*unapplies* it -- the columns stay
    dropped, Django records them as present, and the next ``migrate`` fails
    trying to drop them again.
    """
    call_command("migrate", "billing_integration", "0002", verbosity=0)
    if not _package_backfill_is_recorded():
        call_command("migrate", "vinta_billing", "0005_backfill_scopes", fake=True, verbosity=0)
    call_command("migrate", verbosity=0)


def _package_backfill_is_recorded() -> bool:
    return ("vinta_billing", "0005_backfill_scopes") in MigrationRecorder(
        connection
    ).applied_migrations()


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestUpgradingADatabaseThatAlreadyHasBillingRows:
    def test_every_organization_gets_a_scope_with_its_tree_and_usage_intact(self):
        from vinta_billing.models import BillingPlan

        from billing_integration.models import OrganizationBillingScope

        plan = BillingPlan.objects.create(
            slug="upgrade-backfill-probe",
            name="Upgrade backfill probe",
            monthly_price=0,
            currency="BRL",
        )

        try:
            call_command("migrate", "vinta_billing", PRE_SCOPE_TARGET, verbosity=0)
            _seed_pre_scope_rows(plan.pk)

            _run_the_documented_deploy()

            scopes = {
                scope.organization_id: scope
                for scope in OrganizationBillingScope.objects.filter(
                    organization_id__in=[RESELLER_ID, CHILD_A_ID, CHILD_B_ID, STANDALONE_ID]
                )
            }
            assert set(scopes) == {RESELLER_ID, CHILD_A_ID, CHILD_B_ID, STANDALONE_ID}, (
                "the two children have no billing rows of their own, and still "
                "need scopes: the reseller's usage breakdown is keyed by them"
            )

            # The tree, mirrored off `Organization.parent`.
            assert scopes[RESELLER_ID].parent_id is None
            assert scopes[CHILD_A_ID].parent_id == scopes[RESELLER_ID].pk
            assert scopes[CHILD_B_ID].parent_id == scopes[RESELLER_ID].pk
            assert scopes[STANDALONE_ID].parent_id is None

            # ...and the root flag, off `can_invite_organizations`. Without it
            # `ResellerHierarchy` gives every scope its own billing root.
            assert scopes[RESELLER_ID].is_reseller_root is True
            assert [
                scopes[pk].is_reseller_root for pk in (CHILD_A_ID, CHILD_B_ID, STANDALONE_ID)
            ] == [
                False,
                False,
                False,
            ]

            # The key `build_scope_key` would produce, so the first ordinary
            # save after the upgrade does not silently rewrite it.
            assert scopes[RESELLER_ID].scope_key == f"organizations.organization:{RESELLER_ID}"
            assert scopes[RESELLER_ID].label == "Upgrade reseller"

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT external_id, scope_id FROM vinta_billing_subscription "
                    "WHERE external_id LIKE 'upgrade-sub-%' ORDER BY external_id"
                )
                assert cursor.fetchall() == [
                    ("upgrade-sub-reseller", scopes[RESELLER_ID].pk),
                    ("upgrade-sub-standalone", scopes[STANDALONE_ID].pk),
                ]

                cursor.execute(
                    "SELECT by_scope FROM vinta_billing_billingperiodresourceusage "
                    "WHERE resource_key = 'upgrade_probe'"
                )
                # Django points psycopg's jsonb loader at the identity
                # function so `JSONField` can apply its own decoder, so a raw
                # cursor hands back the undecoded text.
                assert json.loads(cursor.fetchone()[0]) == {
                    str(scopes[RESELLER_ID].pk): 5,
                    str(scopes[CHILD_A_ID].pk): 3,
                    str(scopes[CHILD_B_ID].pk): 2,
                }, "every key the breakdown had before must survive the re-key"
        finally:
            # `uninterruptible`: see `common.testing.migration_replay`. The
            # whole worker's database depends on this finishing.
            with uninterruptible():
                _run_the_documented_deploy()
                # Explicitly, child-first: Django's `CASCADE` is resolved in
                # Python by the collector, so the raw statements this test used
                # to seed with have to be unwound the same way.
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM vinta_billing_billingperiodresourceusage "
                        "WHERE resource_key = 'upgrade_probe'"
                    )
                    cursor.execute(
                        "DELETE FROM vinta_billing_billingperiodsummary "
                        "WHERE plan_slug = 'upgrade-probe'"
                    )
                    cursor.execute(
                        "DELETE FROM vinta_billing_subscription "
                        "WHERE external_id LIKE 'upgrade-sub-%%'"
                    )
                    cursor.execute(
                        "DELETE FROM billing_integration_organizationbillingscope "
                        "WHERE organization_id >= %s",
                        [RESELLER_ID],
                    )
                    cursor.execute(
                        "DELETE FROM organizations_organization WHERE id >= %s", [RESELLER_ID]
                    )
                BillingPlan.objects.filter(pk=plan.pk).delete()
