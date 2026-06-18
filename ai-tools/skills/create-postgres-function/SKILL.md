---
name: create-postgres-function
description: Create or version a Postgres FUNCTION (or PROCEDURE, or TRIGGER) in the Vinta Schedule API through the bespoke raw-SQL framework at `common/raw_sql_migration_managers.py`. Use for DB-side computation that ORM expressions can't model: timezone conversions, recurring-event occurrence calculation, aggregate functions, complex CASE-driven derivations, custom operators. Skip for one-shot computations that belong in Python services and for pgvector / extension functions installed via DDL (use a stock migration for those).
---

# Create Postgres Function

Framework contract: [AGENTS.md → Architecture → Raw SQL](../../AGENTS.md#raw-sql-functions-procedures-triggers-views-materialized-views). Timezone-aware comparison rule: [AGENTS.md → Calendar Integration → Timezones](../../AGENTS.md#timezones).

This skill covers function-specific shape: volatility class (`IMMUTABLE` / `STABLE` / `VOLATILE`), language (`plpgsql` / `sql`), ORM wiring at `<app>/database_functions.py`, signature-change cascades, trigger pair. Examples to study: `convert_naive_utc_to_timezone`, `calculate_recurring_events`, `get_event_occurrences_json` under `calendar_integration/migrations/sql/functions/`.

## Decision questions

1. **Function, procedure, or trigger?**
   - **FUNCTION** (most common) — returns a value or table. Callable from SQL + Django ORM via custom database functions (`<app>/database_functions.py`).
   - **PROCEDURE** — side-effects-only, can `COMMIT` / `ROLLBACK` internally. Rare in this codebase.
   - **TRIGGER** — fires on INSERT / UPDATE / DELETE / TRUNCATE. Body is a function that returns `TRIGGER`. Always paired with a `FunctionMigrationManager` for the function plus a `TriggerMigrationManager` for the trigger binding.
2. **Volatility class?**
   - `IMMUTABLE` — same input → same output, always. Allows index expressions. Example: `convert_naive_utc_to_timezone`.
   - `STABLE` — same result within a single statement. Allows index expressions in some cases.
   - `VOLATILE` (default) — anything else. Cannot be used in index expressions.

   Pick the most restrictive that's truthful. Wrong choice produces incorrect query results or broken indexes.
3. **Language?** `plpgsql` (procedural, most common), `sql` (single-expression), `plpython3u` (rare; requires the extension). Stick to `plpgsql` unless the function is a one-liner SELECT — those go in `sql`.
4. **Will any object reference this function?** Generated columns, views, materialized views, indexes, other functions, triggers. Track them — version bumps must cascade.
5. **Does the function operate on tenant-scoped data?** If it filters by tenant, take the tenant id as an explicit argument — never read it from a session variable. If it doesn't, document why (utility function called from already-scoped queries).

## Checklist

### New function

For a function `calculate_business_hours_overlap` in app `calendar_integration`:

1. **Create the directory + first version:**
   ```
   calendar_integration/migrations/sql/functions/calculate_business_hours_overlap/
   ├── __init__.py
   └── 0001.sql
   ```

2. **Write the function body in `0001.sql`:**
   ```sql
   CREATE OR REPLACE FUNCTION calculate_business_hours_overlap(
       window_start TIMESTAMPTZ,
       window_end TIMESTAMPTZ,
       business_open TIME,
       business_close TIME
   ) RETURNS INTERVAL AS $$
   DECLARE
       overlap_seconds NUMERIC := 0;
       day_cursor DATE := window_start::DATE;
   BEGIN
       IF window_end <= window_start THEN
           RETURN INTERVAL '0';
       END IF;

       WHILE day_cursor <= window_end::DATE LOOP
           overlap_seconds := overlap_seconds + GREATEST(
               0,
               EXTRACT(EPOCH FROM (
                   LEAST(window_end, (day_cursor + business_close)::TIMESTAMPTZ)
                   - GREATEST(window_start, (day_cursor + business_open)::TIMESTAMPTZ)
               ))
           );
           day_cursor := day_cursor + INTERVAL '1 day';
       END LOOP;

       RETURN (overlap_seconds || ' seconds')::INTERVAL;
   END;
   $$ LANGUAGE plpgsql IMMUTABLE;
   ```

   Notes:
   - `CREATE OR REPLACE FUNCTION` is the standard form. The framework supports `CREATE FUNCTION` too, but `OR REPLACE` simplifies the "no-op when re-applied" semantics during local development.
   - The signature (argument types + name) is the function's identity in Postgres. Changing it = new function, not an update — see "Update existing function" below.
   - Pick the volatility class deliberately. `IMMUTABLE` lets the function appear in `Meta.indexes` expressions.
   - Test with a wide range of inputs before declaring `IMMUTABLE` / `STABLE`.

3. **Register the manager in `__init__.py`:**
   ```python
   from common.raw_sql_migration_managers import FunctionMigrationManager


   class CalculateBusinessHoursOverlapFunctionMigrationManager(FunctionMigrationManager):
       name = "calculate_business_hours_overlap"


   __all__ = ["CalculateBusinessHoursOverlapFunctionMigrationManager"]
   ```

4. **Create the Django migration:**
   ```bash
   docker compose run --rm api uv run python manage.py makemigrations calendar_integration --empty --name add_calculate_business_hours_overlap_fn
   ```

   ```python
   from django.db import migrations

   from calendar_integration.migrations.sql.functions.calculate_business_hours_overlap import (
       CalculateBusinessHoursOverlapFunctionMigrationManager,
   )


   class Migration(migrations.Migration):
       dependencies = [
           ("calendar_integration", "<previous_migration>"),
       ]

       operations = [
           CalculateBusinessHoursOverlapFunctionMigrationManager(
               app_path="calendar_integration",
               version="0001",
           ).migration(),
       ]
   ```

5. **Wire it into the ORM via `<app>/database_functions.py`:**
   ```python
   from django.db.models import DurationField, Func


   class CalculateBusinessHoursOverlap(Func):
       function = "calculate_business_hours_overlap"
       output_field = DurationField()
   ```

   Then in a manager / queryset method:
   ```python
   Calendar.objects.annotate(
       business_overlap=CalculateBusinessHoursOverlap(
           "event__start_time",
           "event__end_time",
           Value(datetime.time(9, 0)),
           Value(datetime.time(17, 0)),
       )
   )
   ```

6. **Apply + reverse + re-apply:**
   ```bash
   docker compose run --rm api uv run python manage.py migrate calendar_integration
   docker compose run --rm api uv run python manage.py migrate calendar_integration <previous>
   docker compose run --rm api uv run python manage.py migrate calendar_integration
   ```

   Reverse for version `0001` runs `DROP FUNCTION IF EXISTS calculate_business_hours_overlap;` (from the manager's `drop_command_template`).

   **Caveat:** `DROP FUNCTION IF EXISTS <name>;` without an argument list fails when multiple overloads exist. If you ship two functions sharing a name (different signatures), use a custom `drop_command_template` on a subclass that includes the signature.

### Update existing function

For bumping `calculate_business_hours_overlap` from `0001` → `0002`:

1. **Add `0002.sql`** with the new body. Never overwrite `0001.sql`.

2. **Signature compatibility:**
   - Same argument list + same return type → `CREATE OR REPLACE FUNCTION` works in-place. No rebuild needed for callers / views / generated columns.
   - Changed signature (added / removed / reordered / retyped arguments) → Postgres treats this as a different function. The old function persists until dropped. You must:
     1. Drop the old function explicitly in `0002.sql` (or in a separate migration).
     2. Rebuild every dependent object (views, generated columns, triggers, indexes, other functions calling it) in the same migration chain.
     3. The reverse path must restore both the old function AND every rebuilt dependent.

3. **Migration calls `migration()` with `version="0002"`:** same shape as the version-bump in [create-postgres-view](../create-postgres-view/SKILL.md).

### Trigger

A trigger needs **two** managers — a function returning `TRIGGER`, and the trigger binding itself:

1. Function lives at `<app>/migrations/sql/functions/<fn>/0001.sql` returning `RETURNS TRIGGER`.
2. Trigger lives at `<app>/migrations/sql/triggers/<trg>/0001.sql` with `CREATE TRIGGER <trg> AFTER INSERT ON <table> FOR EACH ROW EXECUTE FUNCTION <fn>();`.
3. Register both managers (`FunctionMigrationManager` + `TriggerMigrationManager`).
4. The Django migration lists both operations, in order — function first, trigger second. Reverse drops trigger first, then function (the framework's reverse path runs operations bottom-up, so list the trigger AFTER the function in `operations`).

## Pitfalls

- **Declaring `IMMUTABLE` for a function that reads from session state or another table.** Postgres caches results; you get stale data. Test with multiple distinct inputs before promoting.
- **`RunSQL` for the function instead of the framework.** Untracked object; no reverse path.
- **Changing the signature of a function used by an index expression.** Postgres rejects the migration. Drop dependent indexes first (in the same chain) and rebuild after.
- **Trigger function returning `NULL` for `AFTER` triggers** — fine, ignored. Returning `NULL` from a `BEFORE` trigger silently cancels the row write. Easy debugging trap.
- **Function names that collide with built-ins or extension functions.** `convert_*`, `get_*`, `calc_*` are common — namespace with a project prefix when the name is generic (`vinta_*`, app prefix).
- **Forgetting to wire `<app>/database_functions.py`.** Migration applies, but the ORM has no idiomatic way to call it. Code falls back to `raw()` queries.
- **Dropping a function the recurrence framework depends on.** `calculate_recurring_events` and friends are load-bearing — changing them affects every event query through the generated columns. Audit downstream impact + rebuild dependent objects in the same chain.
- **Overwriting `0001.sql`** in a bump instead of adding `0002.sql`. Audit trail dies.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Migration runs forward, reverse, forward
docker compose run --rm api uv run python manage.py migrate calendar_integration
docker compose run --rm api uv run python manage.py migrate calendar_integration <prev>
docker compose run --rm api uv run python manage.py migrate calendar_integration

# Function exists in catalog
docker compose run --rm -e DJANGO_SETTINGS_MODULE=vinta_schedule_api.settings.local api uv run python -c "
from django.db import connection
with connection.cursor() as cur:
    cur.execute('SELECT proname, prosrc FROM pg_proc WHERE proname = %s', ['calculate_business_hours_overlap'])
    for row in cur.fetchall():
        print(row[0], '— defined')
"

# Function-specific test
docker compose run --rm api uv run pytest <app>/tests/test_<fn>.py -vs
```

Spot-checks:
- [ ] Directory at `<app>/migrations/sql/{functions,procedures,triggers}/<name>/`.
- [ ] `__init__.py` registers the right manager subclass; `name` matches the SQL identifier.
- [ ] `0001.sql` (or next-numbered) holds the full `CREATE [OR REPLACE] FUNCTION ...` body.
- [ ] Volatility class chosen deliberately (`IMMUTABLE` / `STABLE` / `VOLATILE`).
- [ ] Tenant-scoped functions take the tenant id as an explicit argument.
- [ ] Django migration uses `manager(...).migration()`; reverse confirmed.
- [ ] Wired into `<app>/database_functions.py` if ORM consumers exist.
- [ ] Dependent objects (views, generated columns, indexes, triggers) bumped in the same chain when the signature changes.
- [ ] Trigger pair: function + trigger files both registered, listed in the right order.

When in doubt: dispatch the `migration-author` sub-agent.
