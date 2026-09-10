"""Move an existing installation off ``vinta_billing``'s shipped scope table.

Only an installation that ran ``vinta_billing`` 0.8.x *before*
``BILLING_SCOPE_MODEL`` was pointed at this app has anything to do here. On a
database built after the swap, ``0003_billingscope`` was skipped, there is no
shipped table, and this migration returns immediately.

Why it has to exist at all
--------------------------
Every scope relation in ``vinta_billing`` is declared
``to=settings.BILLING_SCOPE_MODEL``. Changing that setting therefore rewrites
what those *historical* migrations mean: Django's migration state now says the
five ``scope_id`` columns always pointed at this app's model, so the
autodetector sees nothing to do and emits no DDL. The database disagrees --
its foreign keys still reference ``vinta_billing_billingscope`` -- and nothing
in the normal migration machinery will ever reconcile the two. ``makemigrations
--check`` stays clean, a build from zero is correct, and only a real upgraded
database is wrong. That silence is the whole reason this file is hand-written.

Primary keys are preserved, deliberately
----------------------------------------
Each shipped scope is copied to the same ``id`` in this app's table. That is
what keeps the migration boring: the five ``scope_id`` columns and
``BillingPeriodResourceUsage.by_scope``'s JSON keys are already correct once
the rows exist under the same ids, so there is no mass ``UPDATE`` of live
billing foreign keys to get wrong. What is left is a copy and a constraint
swap.

The shipped table is left in place rather than dropped. It belongs to
``vinta_billing``'s ``0003``, dropping it would make this migration's reverse a
lie, and it costs one unreferenced table until the package's own migrations are
squashed past the swap.
"""

from django.db import migrations


SHIPPED_TABLE = "vinta_billing_billingscope"
OWN_TABLE = "billing_integration_organizationbillingscope"

#: ``meta`` key ``payments/0026`` mirrored ``can_invite_organizations`` into,
#: back when the flag had nowhere better to live. It has a real column now.
RESELLER_ROOT_META_KEY = "is_reseller_root"


def _shipped_table_exists(schema_editor) -> bool:
    return SHIPPED_TABLE in schema_editor.connection.introspection.table_names()


def _referencing_foreign_keys(cursor, target_table: str):
    """``[(table, column, constraint)]`` for every FK pointing at ``target_table``.

    Introspected rather than listed. The set is ``vinta_billing``'s to change,
    and a hardcoded list that fell behind would leave a constraint pointing at
    the old table with nothing to say so.
    """
    cursor.execute(
        """
        SELECT tc.table_name, kcu.column_name, tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND ccu.table_name = %s
        """,
        [target_table],
    )
    return cursor.fetchall()


def _repoint(cursor, from_table: str, to_table: str) -> None:
    """Re-aim every foreign key from ``from_table`` at ``to_table``.

    Deferrable-initially-deferred, matching what Django emits for its own
    foreign keys, so a transaction that touches both sides mid-flight is not
    rejected on the first statement.
    """
    for table, column, constraint in _referencing_foreign_keys(cursor, from_table):
        cursor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint}"')
        cursor.execute(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint}" '
            f'FOREIGN KEY ("{column}") REFERENCES "{to_table}" ("id") '
            f"DEFERRABLE INITIALLY DEFERRED"
        )


def forwards(apps, schema_editor):
    if not _shipped_table_exists(schema_editor):
        return

    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type = ContentType.objects.filter(
        app_label="organizations", model="organization"
    ).first()

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT COUNT(*) FROM "{OWN_TABLE}"')  # noqa: S608
        if cursor.fetchone()[0]:
            raise RuntimeError(
                f"{OWN_TABLE} already holds rows; refusing to copy the shipped "
                "scopes on top of them. This migration expects to be the first "
                "thing to write to that table."
            )

        if content_type is None:
            # No organization content type means nothing in the shipped table
            # can name an organization, so there is nothing this app can adopt.
            _repoint(cursor, SHIPPED_TABLE, OWN_TABLE)
            return

        # `object_id` is the organization's pk as a string -- the generic key
        # the shipped model addresses its payer through. Rows naming anything
        # else are not this project's to adopt, and there are none: every scope
        # here was created for an organization.
        cursor.execute(
            f"""
            INSERT INTO "{OWN_TABLE}"
                (id, created, modified, meta, scope_type, scope_key, label,
                 owner_id, parent_id, organization_id, is_reseller_root)
            SELECT s.id, s.created, s.modified, s.meta, s.scope_type, s.scope_key,
                   s.label, s.owner_id, s.parent_id, s.object_id::integer,
                   COALESCE((s.meta ->> %s)::boolean, false)
            FROM "{SHIPPED_TABLE}" s
            WHERE s.content_type_id = %s AND s.object_id ~ '^[0-9]+$'
            """,  # noqa: S608
            [RESELLER_ROOT_META_KEY, content_type.pk],
        )

        # Ids were preserved, so the sequence has to be moved past them or the
        # next insert collides.
        cursor.execute(
            f"""SELECT setval(pg_get_serial_sequence('{OWN_TABLE}', 'id'),
                   GREATEST((SELECT COALESCE(MAX(id), 1) FROM "{OWN_TABLE}"), 1))"""  # noqa: S608
        )

        # Flush the deferred constraint checks the insert queued before any
        # `ALTER TABLE`. Postgres refuses to alter a table with pending trigger
        # events, and the self-referential `parent_id` this just populated is
        # `DEFERRABLE INITIALLY DEFERRED` like every foreign key Django emits.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

        _repoint(cursor, SHIPPED_TABLE, OWN_TABLE)


def backwards(apps, schema_editor):
    """Point the foreign keys back and empty this app's table.

    The shipped rows were never deleted, so putting the constraints back is all
    it takes for them to be authoritative again -- the ids on the far side of
    each foreign key never changed.
    """
    if not _shipped_table_exists(schema_editor):
        return

    with schema_editor.connection.cursor() as cursor:
        _repoint(cursor, OWN_TABLE, SHIPPED_TABLE)
        cursor.execute(f'DELETE FROM "{OWN_TABLE}"')  # noqa: S608
        # Same reason as the forward pass: leave nothing deferred behind for
        # the next statement to trip over.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [
        ("billing_integration", "0001_initial"),
        # The shipped scopes have to be finished before they are copied: 0005
        # creates them, 0006 drops the organization columns they replaced, and
        # `payments/0026` mirrors the organization tree onto them.
        ("payments", "0026_mirror_organization_tree_onto_scopes"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
