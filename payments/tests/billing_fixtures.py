"""Putting the seeded plan catalog back after a transactional test destroyed it.

``@pytest.mark.django_db(transaction=True)`` flushes every table on teardown, and
``flush`` does not replay data migrations -- so the rows
``payments/migrations/0007_seed_billing_plans.py`` wrote are gone for the rest of that
worker's session. pytest-django orders every transactional test *after* the
non-transactional ones, so the damage is confined to the transactional tests that run
later; those are the ones that need this.

Seeds **head state** (``payments.billing_plans_catalog``), not the migration's frozen
copy, for the same reason ``vinta_orgs.testing`` gives for the group seeders: a data
migration is entitled to stop describing what the code now expects, and a test fixture
is not.
"""

from payments.billing_plans_catalog import seed_billing_plans


def reseed_billing_plans() -> None:
    """Recreate the ``unlimited`` and ``free`` plans. Idempotent, and cheap."""
    seed_billing_plans()
