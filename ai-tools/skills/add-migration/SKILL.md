---
name: add-migration
description: Author a Django migration in the Vinta Schedule API. Couples with the `migration-author` sub-agent. Handles standard `makemigrations` flow, lock-aware schema changes on hot tables, raw-SQL DB code (functions / procedures / triggers / views / materialized views) through the bespoke framework at `common/raw_sql_migration_managers.py`, and the reverse path. Use whenever a model change, FK change, index change, or DB-defined-code change must ship. Skip for pure code refactors that don't touch the database.
---

# Add Migration

Two migration surfaces:

1. **Standard Django** — `makemigrations` for model changes. This skill covers the lock-aware schema part.
2. **Raw-SQL framework** — for DB-defined code (functions, procedures, triggers, views, materialized views) routed through `common/raw_sql_migration_managers.py`. **Don't use this skill** for those — go straight to [create-postgres-view](../create-postgres-view/SKILL.md), [create-postgres-function](../create-postgres-function/SKILL.md), or the trigger / procedure variants. They own the framework specifics.

Background: [AGENTS.md → Architecture → Raw SQL](../../AGENTS.md#raw-sql-functions-procedures-triggers-views-materialized-views) for the framework's contract; [AGENTS.md → Multi-Tenancy](../../AGENTS.md#multi-tenancy) for `OrganizationModel` constraints on new columns + indexes.

## Decision questions

1. **What's the change?** Column add / drop / type / null / default; index add / drop; FK add / drop; new table; rename; raw SQL function / view / etc.; data migration.
2. **Is the target table hot?** Hot tables are those receiving non-trivial write traffic (calendar events, bundle relationships, bookings, organizations members). Schema changes on hot tables need lock-aware operations.
3. **Is the model `OrganizationModel`-derived?** New columns / indexes need to consider the org dimension (lead with `organization_id` in composite indexes, include `organization` in unique constraints).
4. **Is the operation reversible?** Every migration needs a meaningful reverse, or an explicitly-justified `RunPython.noop`.
5. **Does the change cascade into raw-SQL objects?** If you bump a function the recurrence DB framework depends on, the depending objects need rebuilding in the same chain.

## Checklist

### Standard model change

1. Edit the model.
2. Generate the migration:
   ```bash
   docker compose run --rm api uv run python manage.py makemigrations <app>
   ```
3. **Read the generated file.** Migrations are auto-generated but commit-reviewed. Look for:
   - For tenant-scoped FKs: both the `<name>_fk` concrete column AND the `<name>` `ForeignObject` are present.
   - For non-null new columns: `db_default=...` is set, or the migration is a deliberate three-step (add nullable → backfill → make non-null) on multiple migrations.
   - Unique / index constraints lead with `organization` where the model is tenant-scoped.
4. **For hot tables — add concurrent index ops**. Replace `AddIndex` with `AddIndexConcurrently`:
   ```python
   from django.contrib.postgres.operations import AddIndexConcurrently

   class Migration(migrations.Migration):
       atomic = False                            # required for CONCURRENTLY ops
       dependencies = [...]
       operations = [
           AddIndexConcurrently(
               model_name="event",
               index=models.Index(
                   fields=["organization", "start_time"],
                   name="calendar_event_org_start_idx",
               ),
           ),
       ]
   ```
5. **Adding non-null column on existing table:**
   - Small table (< 100k rows) → either pattern is fine.
   - Hot / large table → use `db_default` (Django ≥5) so Postgres backfills at the schema level without a table rewrite:
     ```python
     migrations.AddField(
         model_name="event",
         name="needs_review",
         field=models.BooleanField(db_default=False),
     )
     ```
   - When `db_default` doesn't fit (computed default, conditional), use the two-phase approach across separate migrations: add nullable + backfill in a `RunPython` data migration + then make non-null.
6. Run the migration, confirm reverse works:
   ```bash
   docker compose run --rm api uv run python manage.py migrate <app>
   docker compose run --rm api uv run python manage.py migrate <app> <previous_migration>
   docker compose run --rm api uv run python manage.py migrate <app>
   ```
7. Run the [outer gate](../../AGENTS.md#outer-gate).

### Raw-SQL framework (functions / views / materialized views / triggers / procedures)

**Don't author these here.** Use the dedicated skills:

- Views + materialized views → [create-postgres-view](../create-postgres-view/SKILL.md).
- Functions + procedures + triggers → [create-postgres-function](../create-postgres-function/SKILL.md).

Both follow the same framework contract documented in [AGENTS.md → Architecture → Raw SQL](../../AGENTS.md#raw-sql-functions-procedures-triggers-views-materialized-views): versioned `NNNN.sql` files, manager registered in `__init__.py`, Django migration calls `manager.migration()` (new) or `manager.migrate(old, new)` (bump), next-numbered file on every update.

When a model-shape migration *also* cascades into a raw-SQL bump (function depends on a renamed column, view projects a dropped field, generated column depends on a function), author both in the same migration chain and dispatch to `migration-author`.

### Lock-aware operation reference

| Operation | Lock taken | Production-safe? | Action |
|---|---|---|---|
| Add nullable column | `ACCESS EXCLUSIVE` (very brief) | yes | proceed |
| Add column with `db_default` (Django ≥5) | brief metadata-only | yes | proceed |
| Add non-null column without `db_default` | rewrite | no on hot tables | use three-phase or `db_default` |
| `CREATE INDEX` (non-concurrent) | `SHARE` (blocks writes) | no on hot tables | use `CREATE INDEX CONCURRENTLY` via `AddIndexConcurrently` |
| `ALTER COLUMN TYPE` (same-family) | rewrite usually | no on hot tables | avoid; use two-phase add-new-column |
| `DROP COLUMN` | brief metadata + rewrite at vacuum | safer | proceed |
| Rename column | `ACCESS EXCLUSIVE` (brief) but clients break | no | two-phase (add new + dual-write + cutover + drop old) across separate deploys |
| `ALTER TABLE ... NOT NULL` (Postgres < 16) | full scan | no on hot tables | `ADD CONSTRAINT CHECK ... NOT VALID` + `VALIDATE CONSTRAINT` |
| `DROP INDEX` | brief; consider `CONCURRENTLY` | yes | use `RemoveIndexConcurrently` for hot tables |

### Data migrations

`RunPython` for data fixes that can't be done in SQL. Rules:

- Iterate using **batched** queryset slicing (never `.all()` then `for x in qs`). Pattern:
   ```python
   qs = Event.objects.filter(...).order_by("id")
   batch_size = 500
   last_id = 0
   while True:
       batch = list(qs.filter(id__gt=last_id)[:batch_size])
       if not batch:
           break
       for obj in batch:
           ...
       Event.objects.bulk_update(batch, ["field"])
       last_id = batch[-1].id
   ```
- For tenant-scoped models, the manager raises on missing org filter. Iterate per-org:
   ```python
   for org_id in Organization.objects.values_list("id", flat=True):
       qs = Event.objects.filter(organization_id=org_id, ...)
       # batched loop
   ```
- **Reverse:** if the data migration is meaningful to reverse, write `reverse_code`. If not (one-shot fix), `migrations.RunPython.noop` with a one-line docstring explaining why.

### Reverse path

Every migration declares a reverse. Auto-generated migrations get sensible reverses by default. Hand-written + `RunPython` migrations must:

- Restore the schema state Postgres expects from the prior migration.
- For raw-SQL managers: call `manager.migrate("<new>", "<prev>")` — the framework handles the SQL.
- Never leave orphans (dropped trigger but kept function, dropped column but kept index).

`RunPython.noop` is allowed when the reverse is meaningless (one-way data backfill, etc.) but each use needs a docstring justifying it.

## Pitfalls

- **`RunSQL` for DB-defined code.** Bypasses the raw-SQL framework. Untracked DB objects, no version history, no reverse path.
- **Overwriting an existing version file.** History is the audit trail — always next-numbered.
- **Auto-generating a migration, then hand-editing it without re-running `makemigrations --check`.** The generator and your edit must converge. CI runs the check and will fail otherwise.
- **Adding a non-null column on a hot table without `db_default`.** Rewrites the table — production downtime.
- **`CREATE INDEX` without `CONCURRENTLY` on a hot table.** Blocks writes for the duration.
- **Renaming a column in a single migration.** Clients reading the old name during deploy break. Use two-phase (add new + dual-write + cutover + drop old) across separate deploys.
- **Forgetting to bump dependent views / materialized views / indexes** when a function they call changes signature.
- **Tenant-scoped data migration that iterates `Model.objects.all()`** — manager raises. Iterate per-org.
- **Skipping `makemigrations --check` after committing.** CI runs it; broken migrations fail the build.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
docker compose run --rm api uv run python manage.py migrate <app>                 # apply forward
docker compose run --rm api uv run python manage.py migrate <app> <prev>          # apply reverse
docker compose run --rm api uv run python manage.py migrate <app>                 # re-apply forward
```

Spot-checks:
- [ ] Operation class matches the safety needs of the target (CONCURRENTLY on hot indexes, `db_default` on non-null on hot tables).
- [ ] Reverse runs cleanly — orphans absent.
- [ ] For raw-SQL changes: framework manager used, not `RunSQL`; next-numbered file.
- [ ] Dependent objects (views, materialized views, indexes) updated in the same chain when an upstream function signature changes.
- [ ] Migration docstring mentions any non-obvious choice (deferred constraint, two-phase, `noop` reverse).
- [ ] If a data migration: batched, tenant-aware, reverse opted in or `noop` justified.

When in doubt: dispatch the `migration-author` sub-agent (`ai-tools/agents/migration-author.yaml`). That's exactly its job.
