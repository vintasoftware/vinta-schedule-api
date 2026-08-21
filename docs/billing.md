# Billing

The billing engine — subscriptions, plans, limits, entitlements, provider adapters
(Stripe, MercadoPago), dunning, cycle close, metering, usage summaries — is not
implemented in this repository. It lives in
[vinta-django-billing](https://github.com/vintasoftware/vinta-django-billing)
(the `vinta_billing` package), pinned in `pyproject.toml`. `payments/` is this
project's **configuration** of that engine, not a second copy of it.

This is the result of
`ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md`,
which moved a host-owned billing engine (originally specified in
`BILLING_PLANS_AND_LIMITS`, `BILLING_USAGE_SUMMARY_AND_LEDGER`,
`PAYMENT_PROVIDER_SELECTION` and `BILLING_API_CONTRACT_HARDENING`) onto the package
across six phases. The full history — including two authorization/correctness bugs
found and fixed in the package along the way — is in
`ai-plans/TRACKING_MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING.md`.

## What `payments/` actually contains

```
payments/
├── apps.py                     # wires the seams' imports/registrations at process start
├── tasks.py                    # thin Celery wrappers over vinta_billing.jobs, kept at
│                                # their payments.tasks.* dotted path so the beat schedule
│                                # (celerybeat_schedule.py) didn't need to change
├── notification_contexts.py    # vintasend @register_context functions for the dunning
│                                # ladder's in-app/email notifications
├── billing_plans_catalog.py    # the LIVE plan catalog + seeder (see "Two copies of the
│                                # catalog, on purpose" below)
├── seams/                      # see next section
├── management/commands/
│   └── reconcile_billing_period.py  # host-owned, tenancy-binding: re-runs cycle
│                                     # close's reconciliation for a named closed
│                                     # period on demand, for finance
├── migrations/                 # includes 0023/0024, the one-time table-move migration
│                                # (payments_* -> vinta_billing_*), and earlier migrations
│                                # this project wrote before the move
└── tests/                      # host-wiring tests only -- see "What is and isn't tested
                                 # here" below
```

Everything else — models, managers, querysets, serializers, REST views, provider
adapters, the state machine, dunning, cycle close, metering — is `vinta_billing.*`.

## The seams (`payments/seams/`)

A generic billing package cannot know this product's resources, its reseller
hierarchy, or how it sends notifications. `vinta_billing`'s settings
(`VINTA_BILLING` in `vinta_schedule_api/settings/base.py`) ask for exactly those
things as pluggable objects. Each one lives in its own module:

| Seam | What it configures |
|---|---|
| `resource_keys.py` | The thirteen resource/entitlement key **string constants**. Zero imports, deliberately — see below. |
| `resources.py` | Registers the eight resources / five entitlements against `vinta_billing.registry`, with a counter function per resource (seat counting, calendar counts, metered occurrences, ...). |
| `hierarchy.py` | `ResellerHierarchy` — this project's parent/child reseller shape (`Organization.parent`, `can_invite_organizations`), so a subscription can pool usage across a reseller's subtree. |
| `notifier.py` | Bridges the package's notification calls to the DI-built vintasend `NotificationService`. |
| `occurrences.py` | The metered-occurrence source — reads `calendar_integration.CalendarEvent` to tell the engine what a "billable occurrence" is. |
| `seats.py` | The two seat-limit checks that must exclude a pending invitation from its own count, built on the package's `usage_extra_resolver`. |
| `audit.py` | Receives `vinta_billing.signals.payment_provider_repointed` and writes this project's audit trail. |
| `resync.py` | Receives `vinta_billing.signals.billing_restriction_lifted` and resumes calendar sync for the pooled subtree. |

`payments/seams/dispatch.py` and `VINTA_BILLING["JOB_DISPATCHER"]` existed briefly
and were removed in Phase 2. **They stay removed, and not for the original reason.**
A `JOB_DISPATCHER` receives the package's job *function*, so any implementation
general enough to serialize it has to funnel every job through one generic task
that re-imports it by dotted path — which is what the deleted seam did, and which
calls `vinta_billing.jobs.<job>` directly, bypassing `payments/tasks.py`'s
per-subscription wrappers and the `organization_context` binding they carry. The
four `dispatch=` lambdas in `payments/tasks.py` are what keep the fan-out on tasks
that bind an organization; the setting would take it off them.

### Why `resource_keys.py` is a separate, zero-import module

`resources.py` is the registration site, so it imports live models from
`calendar_integration`, `organizations`, `public_api` and `webhooks` to build its
counters. A module in any of those four apps that wants a resource key back — e.g.
`organizations/models.py` wanting `WHITE_LABEL_BRANDING` to gate branding — would
form a direct import cycle if it imported `resources.py`. `resource_keys.py` holds
only the thirteen key strings, with no imports at all, so any app can import a key
symbol from it without risk.

### Registry keys, not enums

`LimitedResource` and `Entitlement` used to be `TextChoices` enums. They are gone:
a billing library cannot own the closed set of things *this specific product*
sells, so they became **registrations** against `vinta_billing.registry` instead —
`payments/seams/resources.py` is their definition site, and
`payments/seams/resource_keys.py` holds the plain string constants a call site
should use instead of a literal.

## Two copies of the catalog, on purpose

`payments/billing_plans_catalog.py` (head state — read by
`payments/tests/billing_fixtures.reseed_billing_plans` and the root `conftest.py`
whenever a `transaction=True` test's flush destroys the seeded rows) and
`payments/migrations/0007_seed_billing_plans.py` (a frozen data migration) both
seed the `unlimited` / `free` plans, and they are **allowed to diverge**. `0007`
freezes its own copy of the resource/entitlement keys and `LimitKind` values as
plain string literals rather than importing them — a data migration has to keep
meaning what it meant when it was written, and `free`'s ceilings are explicit
placeholders awaiting real product numbers. See that migration's module docstring
for the full reasoning, and `AGENTS.md`'s note on data migrations under
"Raw SQL: Functions, Procedures, Triggers, Views, Materialized Views".

## What is and isn't tested here

`payments/tests/` covers **host wiring only**: the eight resource counters, the
reseller pooling rules, the REST contract, the plan seed migration, tenancy. It
does not re-test the engine's internals (the billing state machine, provider
adapters, the dunning ladder, cycle close, plan change) — `vinta-django-billing`'s
own suite (850+ tests across the py3.11–3.14 × Django 5.2/6.0/6.1 matrix, plus a
Postgres environment for the row locks) covers those, and duplicating them here
would mean every engine fix has to be written and verified twice.

Two modules had drifted across that line, both because the package genuinely had
no equivalent at the time, and both were deleted when 0.6.0 gained one:
`test_dunning_retry_tolerance.py` and `test_cycle_close_concurrency.py`. The test
to ask about a candidate is whether any host wiring is in its call path — a test
that builds package models with `baker` and calls a package service directly is
the package's to own, wherever it happens to live.

## Upgrading the pin

1. Check the package's `HISTORY.md` for behavior changes between the installed
   version and the target.
2. Bump the `vinta-django-billing` constraint in `pyproject.toml`, then
   `uv sync` (or `uv lock --refresh-package vinta-django-billing` if the resolver
   sticks to a cached version).
3. Run the full suite. A regression here means either a host seam needs updating
   for a changed package contract, or the package shipped a real bug — see
   **Package gaps** for the standing process (fix upstream, release, bump the pin;
   don't subclass or monkeypatch around a package defect in the host).
4. Regenerate `schema.yml` (`make update_schema`) if the package's REST surface
   changed, and diff it — an unexpected path or shape change is a signal to read
   the package's changelog more carefully before merging.

## Package gaps found during the migration — all closed upstream

Six gaps were identified while migrating onto the package. None of them were
worked around by subclassing or monkeypatching; all six are now fixed in
`vinta-django-billing` itself, which is the standing process this project
follows.

Released in **0.5.0**, and severe enough to be worth remembering:

- `IsBillingManager.has_object_permission` was a no-op against a billing-root
  object (it read `obj.organization`, which a root `Organization` doesn't have),
  so a child-org admin could change a reseller root's plan, buy add-ons against
  it, or cancel its subscription.
- The Stripe subscription-billing adapter read `Invoice.billing`, a field
  `stripe==15.3.1`'s SDK doesn't have (it has `Invoice.payments`), so
  `invoice.paid` webhooks never resolved a payment and dunning was never cleared
  for Stripe subscription charges.

Released in **0.6.0**, each with the host-side change the fix allowed:

1. **`vinta_billing.jobs` didn't consult `VINTA_BILLING["SERVICE_CONTAINER"]`.**
   It resolved services from the package's own `services.container` cache, so a
   test that DI-overrode a Stripe adapter with an empty key still hit the real
   `STRIPE_SECRET_KEY`. The four sweeps now resolve through `SERVICE_CONTAINER`,
   and `payments/tasks.py`'s `@inject` / `Provide[...]` plumbing — which existed
   only to hand each job a DI-built service by hand — was deleted with this bump.
   `VINTA_BILLING["JOB_DISPATCHER"]` stays unset on purpose — see **The seams**
   above for why it would take the fan-out off the organization-binding tasks.
2. **The error-to-HTTP-status map and the package's own viewset annotations
   disagreed** about `PaymentProviderNotConfiguredError`: the table said 503, the
   shipped OpenAPI annotations said 409. This project had sided with the
   annotations, hardcoding 409 in `common/exception_handlers.py`. 0.6.0 settled
   it at **503** — a deployment fault the caller cannot fix by sending a
   different request, and the same status as its two sibling faults — corrected
   the annotations, and added a package test that regenerates the schema and
   fails if any declared status contradicts the table. The host override is gone;
   `common/exception_handlers.py` now takes every status from the table.
3. **No concurrency test for `CycleCloseService.close_subscription`'s row lock.**
   The package's suite ran on SQLite, which has no row locks — Django drops
   `SELECT ... FOR UPDATE` there rather than raising, so a two-thread test would
   have passed against a lock that was never taken. 0.6.0 added a Postgres test
   environment and a real two-thread test that also proves the loser *blocks*
   rather than failing fast. The host's stand-in
   `payments/tests/test_cycle_close_concurrency.py` was deleted with this bump:
   it drove `CycleCloseService` directly with package models and asserted a
   package-owned guarantee, with no host wiring in the path.
4. **No direct test of `SubscriptionService.retry_payment` / `retry_failed_charge`.**
   In particular the money-path guard — a `CollectionNotSupportedError` from any
   non-MercadoPago provider must re-raise rather than falling back into
   `change_subscription_plan`, which a live Stripe probe showed collects **$0.00**
   with only an INFO log to notice by. 0.6.0 covers both methods directly, so the
   host's stand-in `payments/tests/services/test_dunning_retry_tolerance.py` was
   deleted with this bump.

If you hit a seventh gap: report and fix it in the package (with a test and a
`HISTORY.md` entry), release, then bump the pin here. Don't subclass or
monkeypatch around it in `payments/` — that is exactly the duplication this
migration was meant to end.
