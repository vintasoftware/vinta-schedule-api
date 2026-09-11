# Deploying the billing-scope swap

This app exists because `BILLING_SCOPE_MODEL` points at
`billing_integration.OrganizationBillingScope` instead of
`vinta-django-billing`'s shipped `BillingScope`. The shipped scope addresses its
payer through a generic key (`content_type` + `object_id`); this one uses a real
foreign key, because every payer here is an organization. See the model
docstring for why that is worth an app of its own.

On a **new** database that costs nothing: Django skips the shipped model's
table, `vinta_billing.0004` builds every `scope_id` foreign key against this
app's table directly, and an ordinary `migrate` is all there is to it.

On a database that carries 0.7 billing data it costs one extra command, and
**this file is the reason why. Read it before deploying.**

## Why a plain `migrate` stops

`vinta_billing.0005_backfill_scopes` is the package's own 0.7-to-0.8 backfill:
one scope per organization, every billing row re-pointed at it. It creates those
scopes through the generic key, which this project's scope does not have, so it
refuses rather than guessing:

```
RuntimeError: vinta_billing has 4 organization(s) to migrate onto scopes, but
BILLING_SCOPE_MODEL points at a scope model with no generic key, so this
migration cannot name their payers. Backfill those scopes in your own data
migration and point the billing rows at them, then re-run with this one faked.
```

`0002_backfill_scopes_from_organizations` is that data migration, and it is
ordered `run_before` `0005` so the rows are already scoped by the time the
package looks. What it cannot do is stop `0005` from counting
`BillingProfile.organization_id` — that column is the table's primary key until
`vinta_billing.0006` renames it, so unlike the other four it cannot be cleared.
`0005` therefore still sees organizations to migrate, and still refuses. Faking
it is the documented answer and is what the error message itself asks for.

## The deploy

Three commands, in this order, in one maintenance window.

```bash
# 1. Everything up to and including this project's backfill. Pulls in
#    vinta_billing 0003 and 0004 as real dependencies along the way.
python manage.py migrate billing_integration 0002

# 2. Mark the package's backfill done. Step 1 did its work.
#    ONLY IF `showmigrations vinta_billing` lists `[ ] 0005_backfill_scopes`.
python manage.py migrate vinta_billing 0005_backfill_scopes --fake

# 3. Deploy and migrate normally.
python manage.py migrate
```

**Step 2 is conditional, and running it at the wrong time is worse than not
running it.** `migrate <app> <migration> --fake` means "migrate *to* this
migration", not "mark this migration applied" — so on a database where `0006`
is already applied it fake-*unapplies* it. The columns stay dropped, Django
records them as present, and the next deploy fails trying to drop them again.
Check first:

```bash
python manage.py showmigrations vinta_billing | grep 0005_backfill_scopes
```

Run step 2 only for `[ ]`. Steps 1 and 3 are safe to re-run at any time.

**Do not shorten this to `migrate vinta_billing 0005 --fake` followed by
`migrate`** either. On a database where `0004` is not applied yet, that fakes
`0004` too — a schema migration — and the `scope_id` columns are never created.

## Rolling back

```bash
python manage.py migrate vinta_billing 0005_backfill_scopes       # real: undoes 0006
python manage.py migrate vinta_billing 0004_add_scope_columns --fake  # 0005 never ran
python manage.py migrate billing_integration 0001                 # our reverse
```

The middle command is the mirror image of the deploy's step 2 and carries the
same warning: it is only correct once the first command has left `0005` as the
last applied `vinta_billing` migration.

The last step reads the organization back off each scope, restores the
`organization_id` columns and the `by_organization` breakdowns, and empties this
app's table. Verified by round-tripping forward → back → forward on a database
seeded with a reseller tree and its billing rows.

## When the fake steps go away

Both are there because `0005` treats "this row has an organization" as "this row
needs migrating", without checking whether it already has a scope. A release
that skips already-scoped rows — and that no-ops rather than raising in reverse
when the scope model has no generic key — makes both directions an ordinary
unattended `migrate`. Delete the fake steps from this file when that ships, and
raise the floor in `pyproject.toml`.

## What this does not cover

No environment has ever run 0.8 with the *shipped* scope model, so there is no
path here for adopting rows out of `vinta_billing_billingscope`. If one ever
does, it needs a different migration: copy the rows preserving primary keys, and
re-point the five `scope_id` foreign key constraints by hand — Django emits no
DDL for a swappable-model change, so nothing in the ordinary migration machinery
will do it.

## One behaviour change

`OrganizationBillingScope.organization` is `CASCADE`. Deleting an organization
now deletes its billing scope, and the scope's own cascades take its billing
rows with it. The shipped generic-key scope had no foreign key, so a deleted
organization left both behind — rows pointing at a scope whose payer no longer
existed. `PROTECT` was the obvious alternative and is worse: every organization
has a scope from its first save, so it would make every organization
permanently undeletable. See the model docstring.
