"""Give ``Audit`` a cross-repository identity, and let ``created_at`` be assigned.

``uid`` is what makes an audit record the *same* record in more than one
repository. Every write is now an ``INSERT ... ON CONFLICT (uid) DO UPDATE``
(see ``DjangoORMAuditRepository.bulk_add``), so re-persisting a record -- a
retried Celery task, a replica catching up, a re-run backfill -- converges on
the row that already exists instead of appending a copy. The ``id`` column
cannot serve that purpose: it is assigned per backend, so two copies of one
record hold different ids.

**Three operations for one field, on purpose.** ``AddField`` evaluates a default
ONCE and writes that single value to every existing row, so adding a unique
UUID column in one step would try to give every row the same uuid and fail on
the unique index (or, on an empty table, quietly work in dev and fail in an
environment that has rows). The standard split applies:

1. ``AddField`` the column nullable and non-unique -- no default to evaluate,
   so nothing collides.
2. ``RunPython`` fills a distinct uuid4 per existing row, in batches.
3. ``AlterField`` makes it ``NOT NULL`` and adds the ``UNIQUE`` index, which now
   has distinct values to index.

``created_at`` moves from ``auto_now_add=True`` to ``default=timezone.now``.
``auto_now_add`` overwrites any assigned value in ``pre_save``, which is
incompatible with replication in two ways: a replica could not reuse the emit
time the record already carries (every copy would be stamped with its own write
time, so the copies would never compare equal and the ``created_at`` windows a
sync runs under would not line up), and the timestamp would keep recording when
a Celery worker got around to the write rather than when the audited action
happened. State-level for existing rows -- Django never stores either form as a
database default, so ``sqlmigrate`` reports no DDL for this operation.

**Lock audit.** ``AddField`` of a nullable column with no default is metadata
only in Postgres 11+. The backfill runs in batches, each its own ``UPDATE``, so
no single statement holds row locks across the whole table. The ``AlterField``
takes ``ACCESS EXCLUSIVE`` for a ``SET NOT NULL`` plus a non-``CONCURRENTLY``
unique index build -- proportional to table size, and deliberate: this project
is pre-launch with no production data, matching the precedent set in
``audit/migrations/0002`` and ``calendar_integration/migrations/0045``.
"""

import uuid

import django.utils.timezone
from django.db import migrations, models


#: Rows updated per statement during the backfill. Bounds how long any single
#: UPDATE holds its row locks.
BACKFILL_BATCH_SIZE = 1000


def backfill_uids(apps, schema_editor):
    """Give every existing Audit row its own uuid7.

    Rows that predate the column all get timestamps from the moment the
    migration runs, so they carry no useful order relative to each other. That
    is fine: ``uid`` orders records only as a tiebreak behind ``created_at``,
    which these rows already have.

    Batched and restricted to ``uid IS NULL`` so the migration can be
    interrupted and re-run without either rewriting rows it already filled or
    holding locks on the whole table in one statement.
    """
    Audit = apps.get_model("audit", "Audit")
    while True:
        batch = list(Audit.objects.filter(uid__isnull=True)[:BACKFILL_BATCH_SIZE])
        if not batch:
            return
        for audit in batch:
            audit.uid = uuid.uuid7()
        Audit.objects.bulk_update(batch, ["uid"])


def noop_reverse(apps, schema_editor):
    """Nothing to undo — the column the backfill wrote is dropped by the reverse
    of the AddField above it."""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0002_alter_audit_managers_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="audit",
            name="uid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(backfill_uids, noop_reverse, elidable=True),
        migrations.AlterField(
            model_name="audit",
            name="uid",
            field=models.UUIDField(default=uuid.uuid7, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="audit",
            name="created_at",
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
        ),
    ]
