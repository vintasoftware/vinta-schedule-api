---
name: create-postgres-view
description: Create or version a Postgres VIEW or MATERIALIZED VIEW in the Vinta Schedule API through the bespoke raw-SQL framework at `common/raw_sql_migration_managers.py`. Use when adding read-side projections that ORM queries can't express efficiently, denormalized aggregates, multi-tenant flattening for reporting, or a materialized cache for expensive computations. Skip for purely-ORM-derived projections (use querysets / annotations / virtual models instead) and for DBA-managed views maintained outside the migration system.
---

# Create Postgres View

Framework contract: [AGENTS.md → Architecture → Raw SQL](../../AGENTS.md#raw-sql-functions-procedures-triggers-views-materialized-views). Multi-tenancy rule for projections that touch tenant-scoped tables: [AGENTS.md → Multi-Tenancy](../../AGENTS.md#multi-tenancy).

This skill covers the view-specific shape — `vw_*` / `mv_*` naming, `WITH NO DATA` + UNIQUE INDEX for `REFRESH CONCURRENTLY`, `CREATE OR REPLACE VIEW` limits, consumer wiring.

## Decision questions

1. **View or materialized view?**
   - VIEW — query runs every time. Right for cheap projections, when freshness must be exact.
   - MATERIALIZED VIEW — snapshot. Refresh manually or via a job. Right when source query is expensive and downstream tolerates staleness.
2. **Does the view touch tenant-scoped tables?** If yes, the view definition itself must include the `organization_id` column on the projected rows so consumers can filter. Don't strip it.
3. **Will any other DB object reference this view?** (Functions, generated columns, other views, materialized views, indexes.) Track them — version bumps must cascade.
4. **Does an existing Django ORM expression already do this?** Annotations, `Subquery`, `RawSQL`, virtual models, custom manager methods. Prefer those when feasible. A view earns its keep when it's reused across multiple queries OR feeds drf-spectacular / GraphQL types as a flat source.
5. **For materialized views — what's the refresh strategy?** `REFRESH MATERIALIZED VIEW name` (blocks reads). `REFRESH MATERIALIZED VIEW CONCURRENTLY name` (requires a `UNIQUE INDEX`, no read block). Pick the latter for anything user-facing.

## Checklist

### New view

For a view `vw_organization_calendar_summary` in app `calendar_integration`:

1. **Create the SQL directory + first version file:**
   ```
   calendar_integration/migrations/sql/views/vw_organization_calendar_summary/
   ├── __init__.py
   └── 0001.sql
   ```

2. **Write the view body in `0001.sql`:**
   ```sql
   CREATE OR REPLACE VIEW vw_organization_calendar_summary AS
   SELECT
       org.id AS organization_id,
       c.id AS calendar_id,
       c.name AS calendar_name,
       c.calendar_type,
       COUNT(e.id) AS event_count,
       MAX(e.end_time) AS last_event_end_time
   FROM organizations_organization org
   JOIN calendar_integration_calendar c
       ON c.organization_fk = org.id
   LEFT JOIN calendar_integration_calendarevent e
       ON e.calendar_fk = c.id
       AND e.organization_fk = org.id
   GROUP BY org.id, c.id, c.name, c.calendar_type;
   ```

   Notes:
   - **Always include `organization_id` (or `*_fk`)** on tenant-scoped rows in the projection. Downstream queries filter by it.
   - Use the **`*_fk` concrete columns** (`organization_fk`, `calendar_fk`) in the SQL — the `ForeignObject` virtual columns are ORM-only.
   - Name views with a `vw_` prefix so they're distinguishable from tables in `pg_dump` output.

3. **Register the manager in `__init__.py`:**
   ```python
   from common.raw_sql_migration_managers import ViewMigrationManager


   class OrganizationCalendarSummaryViewMigrationManager(ViewMigrationManager):
       name = "vw_organization_calendar_summary"


   __all__ = ["OrganizationCalendarSummaryViewMigrationManager"]
   ```

4. **Create the Django migration:**
   ```bash
   docker compose run --rm api uv run python manage.py makemigrations calendar_integration --empty --name add_organization_calendar_summary_view
   ```

   Edit the generated file:
   ```python
   from django.db import migrations

   from calendar_integration.migrations.sql.views.vw_organization_calendar_summary import (
       OrganizationCalendarSummaryViewMigrationManager,
   )


   class Migration(migrations.Migration):
       dependencies = [
           ("calendar_integration", "<previous_migration>"),
       ]

       operations = [
           OrganizationCalendarSummaryViewMigrationManager(
               app_path="calendar_integration",
               version="0001",
           ).migration(),
       ]
   ```

5. **Apply + reverse + re-apply:**
   ```bash
   docker compose run --rm api uv run python manage.py migrate calendar_integration
   docker compose run --rm api uv run python manage.py migrate calendar_integration <previous_migration>
   docker compose run --rm api uv run python manage.py migrate calendar_integration
   ```

   The reverse path runs `DROP VIEW IF EXISTS vw_organization_calendar_summary;` (from the manager's `drop_command_template`) because this is version `0001`.

### Update existing view

For bumping `vw_organization_calendar_summary` from `0001` → `0002`:

1. **Add the next-numbered SQL file** — never overwrite `0001.sql`:
   ```
   calendar_integration/migrations/sql/views/vw_organization_calendar_summary/
   ├── __init__.py
   ├── 0001.sql           # unchanged
   └── 0002.sql           # new version
   ```

   `0002.sql` is the full `CREATE OR REPLACE VIEW ...` body — not a delta. The framework reads it whole.

2. **Migration uses `migration()` again, with `version="0002"`:**
   ```python
   from calendar_integration.migrations.sql.views.vw_organization_calendar_summary import (
       OrganizationCalendarSummaryViewMigrationManager,
   )


   class Migration(migrations.Migration):
       dependencies = [
           ("calendar_integration", "<previous_migration_that_added_0001>"),
       ]

       operations = [
           OrganizationCalendarSummaryViewMigrationManager(
               app_path="calendar_integration",
               version="0002",
           ).migration(),
       ]
   ```

   Reverse path: the manager reads `0001.sql` and re-applies it. Good — no orphaned schema.

3. **`CREATE OR REPLACE VIEW` limitations.** Postgres rejects `CREATE OR REPLACE VIEW` if you remove / rename / change-type of an existing column. Two-phase: drop view → re-create (via two separate migrations on separate deploys if anything reads from it).

### Materialized view

Replace `ViewMigrationManager` with `MaterializedViewMigrationManager`. SQL uses `CREATE MATERIALIZED VIEW ... AS ... WITH NO DATA;` for the first version (refresh separately), then `REFRESH MATERIALIZED VIEW CONCURRENTLY ...` from app code or a Celery task.

```sql
-- 0001.sql for mv_organization_calendar_summary
CREATE MATERIALIZED VIEW mv_organization_calendar_summary AS
SELECT
    org.id AS organization_id,
    c.id AS calendar_id,
    COUNT(e.id) AS event_count
FROM organizations_organization org
JOIN calendar_integration_calendar c
    ON c.organization_fk = org.id
LEFT JOIN calendar_integration_calendarevent e
    ON e.calendar_fk = c.id
GROUP BY org.id, c.id
WITH NO DATA;

-- REQUIRED for REFRESH ... CONCURRENTLY
CREATE UNIQUE INDEX mv_organization_calendar_summary_pk
    ON mv_organization_calendar_summary (organization_id, calendar_id);
```

Notes:
- `WITH NO DATA` skips the initial populate — do it via `REFRESH MATERIALIZED VIEW` from a one-off script (see [add-one-off-script](../add-one-off-script/SKILL.md)) or a Celery task.
- The `UNIQUE INDEX` is **required** for `REFRESH MATERIALIZED VIEW CONCURRENTLY`. Without it, refreshes block reads.
- For tenant-scoped data, lead the unique index with `organization_id`.

## Consuming the view from Django

Two patterns:

1. **Unmanaged model** — define a model in `<app>/models.py` with `Meta.managed = False` + `Meta.db_table = "vw_..."`. Queryset works like any other model. Project pattern from `calendar_integration/models.py` — read existing unmanaged models first.

2. **Raw SQL via service** — for one-off reads, a service method calling `connection.cursor().execute(...)`. Avoid for multi-tenant unless the service receives the organization id explicitly.

Either way: the view's `organization_id` column flows through to queries; consumers filter by it.

## Pitfalls

- **`RunSQL` instead of the framework.** The DB ends up with an object the migration history doesn't know about. Reverses break. Use `ViewMigrationManager` / `MaterializedViewMigrationManager`.
- **Overwriting an existing `<n>.sql` file.** The audit trail dies. Always add the next-numbered file.
- **Joining tenant-scoped tables on the `*` (ForeignObject) virtual column.** That column doesn't exist in SQL — only the `*_fk` concrete column does. Join on `*_fk`.
- **Stripping `organization_id` from the projection.** The view becomes unusable for tenant-scoped reads.
- **Materialized view without a UNIQUE INDEX.** Forces blocking refreshes — unsuitable for user-facing data.
- **`REFRESH MATERIALIZED VIEW` in a request handler.** Blocks for the duration. Always run from a background job.
- **Forgetting dependent objects.** When a view's column shape changes, downstream views / functions / generated columns that reference it must be bumped in the same chain.
- **Calling `migration()` without the `app_path` / `version` kwargs.** Manager raises. Read `common/raw_sql_migration_managers.py` to confirm the constructor signature.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Forward apply
docker compose run --rm api uv run python manage.py migrate calendar_integration

# Reverse + re-apply
docker compose run --rm api uv run python manage.py migrate calendar_integration <prev>
docker compose run --rm api uv run python manage.py migrate calendar_integration

# Confirm the view exists + projected columns match
docker compose run --rm -e DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.local api uv run python -c "
from django.db import connection
with connection.cursor() as cur:
    cur.execute('SELECT column_name FROM information_schema.columns WHERE table_name = %s', ['vw_organization_calendar_summary'])
    for row in cur.fetchall():
        print(row[0])
"
```

Spot-checks:
- [ ] Directory under `<app>/migrations/sql/views/<name>/` (or `materialized_views/` for matviews).
- [ ] `__init__.py` registers a `ViewMigrationManager` / `MaterializedViewMigrationManager` subclass; `name` matches the view's SQL identifier.
- [ ] `0001.sql` (or next-numbered) holds the full `CREATE [OR REPLACE] VIEW ...` body.
- [ ] Django migration calls `manager(app_path=..., version=...).migration()` — not `RunSQL`.
- [ ] Tenant-scoped projection includes `organization_id`.
- [ ] Materialized view has a `UNIQUE INDEX` for `REFRESH ... CONCURRENTLY`.
- [ ] Reverse runs clean (no orphan dependencies).
- [ ] If consumed by ORM: unmanaged model added with `Meta.managed = False` + `db_table` matching the view.

When in doubt: dispatch the `migration-author` sub-agent. View bumps are exactly its job.
