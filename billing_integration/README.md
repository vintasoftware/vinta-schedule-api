# Deploying the billing-scope model swap

This app exists because `BILLING_SCOPE_MODEL` now points at
`billing_integration.OrganizationBillingScope` instead of `vinta-django-billing`'s
shipped `BillingScope`. On a **new** database that is unremarkable: Django skips
the shipped model's table and every scope relation is built against this one.

On a database that already ran `vinta_billing` 0.8.x it is not unremarkable, and
this file is the reason why. **Read it before deploying this change.**

## Why a normal `migrate` is not enough

Every scope relation in `vinta_billing` is declared
`to=settings.BILLING_SCOPE_MODEL`. Changing that setting therefore changes what
those *already-applied* migrations mean. Two consequences, both silent:

**1. Django emits no DDL.** The migration state now says the five `scope_id`
columns always pointed at this app's model, so the autodetector sees nothing to
do. `makemigrations --check` stays clean. The database still has its foreign
keys on `vinta_billing_billingscope`. Nothing in the ordinary migration
machinery ever reconciles the two — `0002_adopt_shipped_scopes` does it by hand.

**2. `migrate` refuses to start.** `vinta_billing.0004_add_scope_columns`
carries `swappable_dependency(settings.BILLING_SCOPE_MODEL)`, which now resolves
to this app. On an upgraded database `0004` is already applied and
`billing_integration.0001_initial` is not, so Django raises:

```
InconsistentMigrationHistory: Migration vinta_billing.0004_add_scope_columns
is applied before its dependency billing_integration.0001_initial
```

History cannot be reordered, so `0001` has to be in place *before* `migrate`
runs. This is the same difficulty Django documents for changing
`AUTH_USER_MODEL` mid-project, and it has the same shape of answer.

## The deploy

Three steps, in this order, in one maintenance window. Steps 1 and 2 are the
manual part; everything after is an ordinary deploy.

```bash
# 1. Create this app's table, without recording anything yet.
python manage.py sqlmigrate billing_integration 0001 > /tmp/scope.sql
psql "$DATABASE_URL" -f /tmp/scope.sql

# 2. Tell Django the table is there.
psql "$DATABASE_URL" -c "INSERT INTO django_migrations (app, name, applied) \
    VALUES ('billing_integration', '0001_initial', now());"

# 3. Deploy and migrate normally. This runs 0002, which copies the shipped
#    scopes across and re-aims the foreign keys.
python manage.py migrate
```

Step 3 is where the real work happens, and it is written to be dull: the copy
**preserves primary keys**, so the five `scope_id` columns and
`BillingPeriodResourceUsage.by_scope`'s JSON keys are already correct once the
rows exist. There is no mass `UPDATE` of live billing foreign keys.

## Rolling back

`migrate billing_integration 0001` reverses `0002`: it aims the foreign keys
back at `vinta_billing_billingscope` and empties this app's table. The shipped
rows were never deleted and the ids on the far side of each foreign key never
changed, so the old table is authoritative again the moment its constraints are
back. Verified by round-tripping forward → back → forward on a seeded copy.

Going back further means undoing steps 1 and 2 by hand, in reverse: revert the
setting, `DELETE FROM django_migrations WHERE app = 'billing_integration'`, then
`DROP TABLE billing_integration_organizationbillingscope`.

## What is deliberately left behind

`vinta_billing_billingscope` keeps its rows. Dropping it would make the reverse
above a lie, it belongs to `vinta_billing`'s `0003` rather than to this project,
and it costs one unreferenced table until the package squashes its migrations
past the swap. Drop it in a later, separate change once the rollback window has
closed.

## One behaviour change

`OrganizationBillingScope.organization` is `CASCADE`. Deleting an organization
now deletes its billing scope, and the scope's own cascades take its billing
rows with it. The shipped generic-key scope had no foreign key, so a deleted
organization left both behind — rows pointing at a scope whose payer no longer
existed. `PROTECT` was the obvious alternative and is worse: every organization
has a scope from its first save, so it would make every organization
permanently undeletable. See the model docstring.
