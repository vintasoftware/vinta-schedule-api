# Migrate the Billing Engine to vinta-django-billing — Implementation Plan

No `..._SPEC.md` sibling, deliberately. This plan moves already-specified behaviour
onto a package; the behaviour's specs are the four billing plans already in
`ai-plans/` (`BILLING_PLANS_AND_LIMITS`, `BILLING_USAGE_SUMMARY_AND_LEDGER`,
`PAYMENT_PROVIDER_SELECTION`, `BILLING_API_CONTRACT_HARDENING`). Nothing new is
being decided about what billing does — only about where the code lives.

## 1. Goals

1. `vinta-django-billing` 0.3.0 becomes the billing engine of record. Every model,
   service, adapter, serializer and view the host duplicates is deleted from
   `payments/` and imported from `vinta_billing` instead.
2. `payments/` survives as the host's *configuration* of that engine: the resource
   and entitlement registry, the reseller hierarchy, the notifier, the occurrence
   source, the Celery bridge, the plan catalog, and the provider settings — the
   five seams and two data modules a library cannot own.
3. The billing rows move from `payments_*` to `vinta_billing_*` under a single
   host-owned migration that runs unattended in every environment, including a
   fresh test database.
4. Every route this project serves for billing sits under `/billing`. The two provider
   webhooks move from `/payments/{id}/…` to `/billing/payments/{id}/…`. Nothing else
   moves, and no client-facing path changes. *(Amended 2026-08-19 — see "The `/payments`
   prefix, and what actually moves" below.)*

**Non-goals:**

- No new billing features, no pricing changes, no new provider.
- No change to the GraphQL public API's entitlement gates beyond the import path.
- **No client-facing URL changes.** The only paths that move are the two inbound
  provider webhooks, which no client calls.
- No feature-flag infrastructure. See **Guiding Decisions**.
- No change to Render env var names or to the Celery beat schedule.
- Not migrating `organizations`, `webhooks` or `calendar_integration` onto anything.
  They change imports only.

### Amendment 2026-08-19 — the `/payments` prefix, and what actually moves

This plan originally described one deliberate behaviour change: the payments viewset
moving from `/payments` to `/billing`, "which the single client adopts in the same
release". The destination is right; the reason given for it is wrong, and the wrong
reason hid a real risk.

`PaymentsViewSet` is a bare `ViewSet` — no `queryset`, no `serializer_class` — whose
entire surface is two `@action`s, `payment_update` and `subscription_payment_update`.
Both are **inbound provider webhooks**. The published `schema.yml` confirms it: the only
four paths under `/payments` are those two actions and their `{format}` variants. **No
client calls `/payments`.** Stripe and MercadoPago do. So there is no client adoption to
schedule and no `handoff-to-client` to run; what looked like a client change is a
provider-integration change.

That distinction matters because provider-facing URLs are not ours to move freely.
`MercadoPagoSubscriptionAdapter.create_subscription` builds `notification_url` via
`reverse("Payments-subscription-payment-update", …)` and bakes it into the MercadoPago
**preapproval**, which lives as long as the subscription and is notified on every
recurring charge. Moving that path once a preapproval exists breaks recurring-payment
notifications silently — the provider keeps charging, the host never hears, and
subscriptions drift until dunning fires on customers who have paid. (Stripe is
unaffected: neither Stripe adapter reverses a URL, its endpoint being dashboard-configured.)

**It is safe here, now, and only now**, for two independent reasons: this project has no
webhooks registered with any provider yet (confirmed by the requester), and
`vinta-django-billing` 0.3.0's `PaymentsViewSet.__init__` required three services with no
defaults, so those routes raised `TypeError` on the first request and have never served
one in any deployment.

**The move is therefore made in the package, in 0.4.0**, where the same
`get_extra_patterns()` already hardcodes `billing/payment-provider…`; leaving the
webhooks under `payments/` was an inconsistency in the package's own URL surface. Both
webhooks now serve `^billing/payments/{pk}/…`. Reverse names are unchanged, so no host
`reverse()` call site or test changes.

Consequences for the rest of this plan:

- **Section 4's path-move table is replaced.** The client-facing routes all keep their
  paths; the two webhooks move under `billing/`.
- **Phase 2 changes no client-visible behaviour**, so its `handoff-to-client` step is
  removed. It gains one obligation instead: mount `get_extra_patterns()` alongside
  `get_routes()`, since from 0.4.0 the webhooks no longer come out of the router.
- **`Risk & Rollout Notes`' "The URL change" entry is replaced** by a standing constraint:
  once a webhook is registered with a provider, its path must never move again.
- No phase in this plan is user-visible.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Who owns the engine** | The package. The host keeps only what the package's seams ask for. The host's copy diverged from the package's by 2,848 diff lines across 46 modules — most of it the generalisation work — and keeping both means every future billing fix is written twice and diverges again. |
| **`payments` app survives** | The app label stays. `organizations/migrations/0028_seed_permission_groups.py` depends on `("payments", "0022_capability_permissions")`, and `celerybeat_schedule.py` names tasks by the `payments.tasks.*` dotted path. Keeping the label keeps both valid and keeps the migration history replayable. |
| **Table move mechanism** | **Copy forward, then drop** — not `AlterModelTable`. The package's `0001_initial` creates `vinta_billing_*` with its own index and constraint names; a host migration depending on it copies the rows in FK order, advances the sequences, then drops `payments_*` inside `SeparateDatabaseAndState`. See **Risk & Rollout Notes** for why the rename lost. |
| **Data at risk** | None in production — staging only. The migration is still written to preserve and to reverse, because it also has to run against staging and because a reversible migration is the rollback lever this plan has instead of a flag. |
| **Transitional re-export shims** | Phase 1 leaves `payments/models.py`, `payments/exceptions.py`, `payments/billing_constants.py` and `payments/services/*.py` as one-line re-exports of the package equivalents, so the 58 consumer files keep importing and every phase stays independently mergeable. Phase 6 deletes them; the end state has no shim. A re-export does **not** register a `payments.Subscription` model — Django registers models by their defining module's app — so `makemigrations` correctly sees `payments` as model-free from Phase 1 onward. |
| **Feature flag** | **None.** Every phase is a pure refactor, which the flag rule exempts. *(Amended 2026-08-19: Phase 2 used to be the exception, on the strength of a client-facing URL change that turned out not to exist. The two provider webhooks do move, but a flag cannot gate a URL conf and there is no client to stage the change for.)* Rollback is `git revert` plus the reverse migration. No flag is declared, so this plan has no flag-removal phase. |
| **Route ownership** | The package's. `vinta_billing.routing.get_routes()` / `get_extra_patterns()` replace `payments/routes.py`. Every `basename` is identical, so `reverse()` names and therefore host tests do not change. *(Amended 2026-08-19: the only literal paths that move are the two provider webhooks, `/payments/{id}/…` → `/billing/payments/{id}/…`, done in the package for prefix consistency. From 0.4.0 those webhooks come out of `get_extra_patterns()` rather than the router, so both must be mounted.)* |
| **DI ownership** | The host's. `di_core/containers.py` keeps constructing every billing service, importing the classes from `vinta_billing.*`. `vinta_billing.services.container` stays unused here. Every `@inject` / `Provide[...]` call site in `calendar_integration`, `public_api` and `webhooks` is untouched. |
| **Provider credentials** | Env var names do not change. `settings/base.py` keeps reading `STRIPE_SECRET_KEY`, `MERCADOPAGO_ACCESS_TOKEN`, `DEFAULT_PAYMENT_PROVIDER` and assembles `VINTA_BILLING["PROVIDERS"]` / `["DEFAULT_PROVIDER"]` from them. Render env groups and CI are untouched, so no deploy-ordering hazard. |
| **Who may manage billing** | The package's permission-backed seams, selected explicitly: `BILLING_MANAGER_PREDICATE = "vinta_billing.permissions.member_holding_manage_billing"` and `BILLING_RECIPIENTS = "vinta_billing.recipients.members_holding_manage_billing"`. Safe to select on day one *here specifically*, because `organizations.0028` already seeds `organization_admin` and `organization_billing_owner` with the grant — the package defaults to the permissive predicates precisely because a project without that seed would 403 every endpoint and leave the dunning ladder with no recipients. |
| **Test suite split** | Host tests that exercise engine internals (state machine, provider adapters, dunning ladder, cycle close, plan change) are deleted — the package's 714-test suite covers them on the py3.11–3.14 × Django 5.2/6.0/6.1 matrix. Host tests that exercise host wiring (the eight resource counters, entitlement gates in `public_api`, REST contract, plan seeds, tenancy) are retargeted and kept. |
| **Package gaps** | Fixed in the package and released. A gap found mid-migration becomes a `vinta-django-billing` change, a 0.4.0 release and a pin bump (**Phase 2b**), not a host-side subclass or monkeypatch. |
| **Phase granularity** | Bundled. Related concerns ride one phase — the five seams together, the two service-heavy consumers together, the four thin consumers together. |

## 3. Data Model Changes

No field changes. The package's `vinta_billing.base_models.BaseModel` was ported
field-for-field from `common.models.BaseModel`, and neither app sets `db_table`, so
the twenty tables differ from the host's in name only.

### 3.1 The twenty tables that move

`payments_<name>` → `vinta_billing_<name>`, copied in this order so every FK target
exists before its referents:

1. `billingaddress`, `billingplan`
2. `planlimit`, `planentitlement`
3. `billingprofile` (→ `billingaddress`)
4. `subscription` (→ `billingplan`, organization)
5. `paymentmethod`, `subscriptionplanlimit`, `subscriptionentitlement`
6. `payment` (→ `billingprofile`, `subscription`)
7. `subscriptionaddon` (→ `subscription`, `payment`), `refund` (→ `payment`)
8. `paymentstatusupdate`, `subscriptionstatusupdate`, `refundstatusupdate`
9. `providerwebhookevent`, `meteredoccurrence`, `limitwarningnotification`
10. `billingperiodsummary`, then `billingperiodresourceusage` (→ `billingperiodsummary`)

Each copy is `INSERT INTO vinta_billing_x SELECT ... FROM payments_x` with an
explicit column list — not `SELECT *`, whose correctness depends on column
ordering — followed by `setval` on the new table's identity sequence.
`MeteredOccurrence.event_id` is a soft reference to `calendar_integration`, not an
FK, so nothing outside these twenty tables participates.

### 3.2 The `manage_billing` permission

`organizations/migrations/0028_seed_permission_groups.py` creates the
`payments.subscription` content type, creates `manage_billing` on it, and grants it
to `organization_admin` and `organization_billing_owner`. After the move the
permission lives on `vinta_billing.subscription` (the package's
`0002_manage_billing_permission`). The move migration therefore also:

1. re-grants the `vinta_billing.manage_billing` permission to both groups,
2. revokes and deletes the `payments.manage_billing` permission and its content type.

Head state follows in the same phase: `organizations/permission_catalog.py:95` and
the help text at `organizations/serializers.py:590` name `vinta_billing.manage_billing`.
`organizations/migrations/0028` itself is frozen and keeps naming `payments` — it
describes the world as it was, and the move migration depends on it.

### 3.3 Registry keys replace two `TextChoices`

`payments.billing_constants.LimitedResource` (8 members) and `Entitlement`
(5 members) stop being enums and become registrations against
`vinta_billing.registry`. The stored values are the same strings, so no data
changes. The model fields take their `choices` from a callable, which Django
serializes by reference, so registering a resource never produces a migration.

## 4. API Design

No new endpoints, no payload changes, and no client-facing path changes. Two paths move,
both inbound provider webhooks that no client calls:

| Before | After | Reverse name |
|---|---|---|
| `/api/payments/{id}/payment-update/{provider}/` | `/api/billing/payments/{id}/payment-update/{provider}/` | `Payments-payment-update` (unchanged) |
| `/api/payments/{id}/subscription-payment-update/{provider}/` | `/api/billing/payments/{id}/subscription-payment-update/{provider}/` | `Payments-subscription-payment-update` (unchanged) |

The move happens in `vinta-django-billing` 0.4.0, not host-side. Because the reverse names
do not change, no host `reverse()` call site and no host test changes.

Every other billing route (`billing-profile`, `billing/plans`, `billing/usage`,
`billing/usage/periods`, `billing/usage/occurrences`, `billing/subscription`,
`billing/add-ons`, and the two `billing/payment-provider/` patterns) already has an
identical regex in both `payments/routes.py` and `vinta_billing/routes.py`.

`schema.yml` is regenerated in Phase 2. Expect the path change, changed operation
`tags`, and enum component names that now resolve through
`vinta_billing.constants.*` — see the **Open Questions** entry on serializer drift.

## 5. Phased Rollout

### Phase 0 — Install the package and write the seams

**Goal**: the host can describe its billing world in the package's vocabulary.
Ships no user-visible value on its own: `vinta_billing` is not in `INSTALLED_APPS`
yet, so nothing imports the seams and nothing changes behaviour. Scaffolding is
needed because every later phase reads these five objects out of settings.

**Feature flag**: none — see **Guiding Decisions**.

Changes:

1. `pyproject.toml`: add `vinta-django-billing>=0.3,<0.4`, upper-bounded for the
   reason the package's own pin on `vinta-django-orgs` is. Refresh `uv.lock`.
2. New `payments/seams/resources.py`: register the eight resources against
   `vinta_billing.registry.resources`. Each `counter` is the corresponding
   `_count_*` function lifted from
   [entitlement_service.py:139-296](../payments/services/entitlement_service.py#L139-L296),
   rewritten against `vinta_billing.counting.UsageContext` /
   `count_by_organization` / `merge_breakdowns` instead of the local
   `_group_counts_by_organization` / `_merge_breakdowns`. Register the five
   entitlements against `vinta_billing.registry.entitlements`. Keys and labels are
   byte-identical to the current `TextChoices` members.
3. New `payments/seams/hierarchy.py`: `ResellerHierarchy(ParentFieldHierarchy)` with
   `parent_field = "parent"` and `root_flag_field = "can_invite_organizations"`,
   which is exactly the shape
   [subscription_service.py:75](../payments/services/subscription_service.py#L75)
   computes by hand today.
4. New `payments/seams/notifier.py`: an adapter satisfying
   `vinta_billing.notifications.Notifier`, delegating to the vintasend
   `NotificationService` the DI container already builds — the package's protocol
   was modelled on that method's exact signature, so the adapter resolves the
   service and forwards. `payments/notification_contexts.py` stays where it is and
   keeps its `@register_context` decorators, since the package passes
   `context_name` / `context_kwargs` straight through.
5. New `payments/seams/occurrences.py`: an `OccurrenceSource` over
   `calendar_integration.CalendarEvent`, lifted from `MeteringService`'s current
   occurrence walk.
6. New `payments/seams/dispatch.py`: a `Dispatch` callable that enqueues via Celery
   `.delay`, so the package's per-subscription fan-out lands on the existing queue.
7. `vinta_schedule_api/settings/base.py`: add the `VINTA_BILLING` dict — `HIERARCHY`,
   `NOTIFIER`, `OCCURRENCE_SOURCE`, `METERED_RESOURCE_KEY = "event_occurrences"`,
   `JOB_DISPATCHER`, `BILLING_MANAGER_PREDICATE`, `BILLING_RECIPIENTS`,
   `GRACE_PERIOD_DAYS`, `USAGE_WARNING_THRESHOLD`, `DEFAULT_CURRENCY`, `SITE_DOMAIN`,
   and `PROVIDERS` / `DEFAULT_PROVIDER` assembled from the existing env vars.

Spec use-case: shared scaffolding — no use-case yet.

Tests:

- **Unit**: `payments/tests/seams/test_resources.py` — all eight resources register;
  keys match the current `LimitedResource.values` exactly; each counter returns the
  same breakdown as its `_count_*` predecessor for a two-organization fixture.
- **Unit**: `payments/tests/seams/test_hierarchy.py` — parentless org is a root; a
  child with `can_invite_organizations=True` is its own root; a plain child resolves
  to its reseller; a parent cycle does not hang.
- **Unit**: `payments/tests/seams/test_settings.py` — `VINTA_BILLING` resolves every
  dotted path via `vinta_billing.conf.get_object_from_setting`, and `PROVIDERS`
  carries a key per member of `PAYMENT_PROVIDER_SLUGS`.
- **Integration**: the existing `payments` suite still passes untouched, proving the
  new modules are inert.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../ai-tools/skills/plan-feature/resources/ai-models.yaml)).
Multi-file, and the counters carry the pooled-subtree and invitation-exclusion
subtleties that make the difference between a correct limit and an off-by-one on a
customer's ceiling.

**Reusable skills**: none — no model, migration, endpoint or env var.

Acceptance: `docker compose run --rm api uv run pytest payments/ -n auto` is green,
`manage.py makemigrations --check` reports no changes, and
`python -c "from vinta_billing.registry import resources; print(len(list(resources)))"`
prints 8.

---

### Phase 1 — Install the app, move the rows, shim the host modules

**Goal**: the billing tables and the billing models are the package's. The host's
duplicated engine modules stop existing as implementations and become one-line
re-exports, so nothing outside `payments/` has to change yet.

**Feature flag**: none. Rollback is the migration's reverse path plus a revert.

**Sizing**: ~700 LoC written (migration, shims, tests) against ~16,000 LoC deleted.
The deletions are whole-file removals of code that now lives in the package; read
the migration and the shim list, not the deletion hunks.

Changes:

1. `settings/base.py`: add `"vinta_billing"` to `INSTALLED_APPS`.
   `payments/apps.py`'s `ready()` imports `payments.seams.resources` so registration
   happens before the first limit check or admin render.
2. New `payments/migrations/0023_move_billing_to_vinta_billing.py`, depending on
   `("vinta_billing", "0002_manage_billing_permission")` and
   `("organizations", "0028_seed_permission_groups")`:
   - `RunPython` copying the twenty tables in the order in **Data Model Changes**,
     with explicit column lists and `setval` per sequence;
   - `RunPython` re-granting `vinta_billing.manage_billing` to `organization_admin`
     and `organization_billing_owner`, then deleting the `payments` permission and
     content type;
   - `SeparateDatabaseAndState` — state: `DeleteModel` × 20; database:
     `DROP TABLE payments_*`. The reverse recreates and copies back.
3. Delete the twenty-odd host engine modules: `models.py`, `managers.py`,
   `querysets.py`, `virtual_models.py`, `entitlement_cache.py`, `constants.py`,
   `exceptions.py`, `filtersets.py`, `pagination.py`, `provider_slugs.py`,
   `serializers.py`, `views.py`, `billing_views.py`, `admin.py`, and everything under
   `services/` except what Phase 0 replaced.
4. Leave transitional re-export shims at `payments/models.py`,
   `payments/exceptions.py`, `payments/billing_constants.py`, `payments/constants.py`
   and `payments/services/*.py`, each a single `from vinta_billing.… import …` line
   with a comment naming Phase 6 as its removal.
5. `organizations/permission_catalog.py` and `organizations/serializers.py`: name
   `vinta_billing.manage_billing`.

Spec use-case: no SPEC — preserves every use-case in the four existing billing plans.

Tests:

- **Integration**: `payments/tests/test_table_move_migration.py` — `migrate` from
  zero lands twenty populated-or-empty `vinta_billing_*` tables and no `payments_*`;
  seeded rows survive with identical PKs; sequences allocate above the highest
  copied PK; both groups hold `vinta_billing.manage_billing` and nothing holds the
  old one; the reverse path restores `payments_*` with the same row counts.
- **Integration**: the whole existing suite, unchanged, still green through the
  shims — that is the regression proof for this phase.
- **Unit**: `payments/tests/test_shims.py` — each shim re-exports the package object
  identically (`payments.models.Subscription is vinta_billing.models.Subscription`),
  and `django.apps.apps.get_app_config("payments").get_models()` is empty.

**Suggested AI model**: Tier 4. Twenty-table data move, sequence handling, a
permission re-grant across content types, and a reverse path that has to be real.

**Review models**: reviewer Tier 4 — a wrong column list or a missed `setval`
silently corrupts billing rows or collides PKs on the next insert, and the failure
surfaces long after the migration is green. Fixer left on the project default.

**Reusable skills**: `add-migration` (couples with the `migration-author` agent).

Acceptance: `migrate` runs unattended from an empty database and from a staging
snapshot; row counts per table are equal before and after; `migrate payments 0022`
reverses cleanly; full suite green.

---

### Phase 2 — Point the host's own entry points at the package

**Goal**: the host's tasks, DI container, error rendering, schema enums and URL conf
address the package directly. *(Amended 2026-08-19: this phase no longer moves any URL.
It is a pure route-ownership swap with no behaviour change.)*

**Feature flag**: none — nothing user-visible changes.

Changes:

1. `payments/tasks.py`: the four beat entry points become thin wrappers over
   `vinta_billing.jobs.meter_event_occurrences`, `process_dunning`,
   `check_approaching_limits`, `close_billing_periods`, plus the four
   per-subscription jobs the dispatcher fans out to. Task dotted paths stay
   `payments.tasks.*`, so `vinta_schedule_api/celerybeat_schedule.py` is untouched.
2. `di_core/containers.py`: import the service classes from `vinta_billing.services.*`
   and `PaymentProviders` from `vinta_billing.constants`. Provider registry, resolver
   and factory wiring keep their current shape.
3. `common/exception_handlers.py`: import from `vinta_billing.exceptions`, and
   delegate to `vinta_billing.exception_handling.billing_error_status` rather than
   re-deriving the status map.
4. `settings/base.py`: repoint `ENUM_NAME_OVERRIDES` at
   `vinta_billing.constants.BillingInterval.choices` and
   `vinta_billing.constants.PaymentProviders.choices`; import
   `PAYMENT_PROVIDER_SLUGS` from `vinta_billing.provider_slugs`.
5. `vinta_schedule_api/urls.py`: replace the two `payments.routes` imports with
   `vinta_billing.routing.get_routes()` / `get_extra_patterns()`. Delete
   `payments/routes.py`. **Mount both** — from 0.4.0 the package's `get_routes()` no
   longer lists `PaymentsViewSet`, because the two provider webhooks are bound in
   `get_extra_patterns()` with `re_path` instead of coming out of the router. Mounting
   `get_routes()` alone silently drops both webhooks. Keep the router under the `api:`
   namespace and the extra patterns unnamespaced, exactly as today.
6. Regenerate `schema.yml` (`make update_schema`). **Expect no path changes** — see the
   2026-08-19 amendment. A moved path in the diff means something is wrong.

Spec use-case: no SPEC — preserves `BILLING_API_CONTRACT_HARDENING`'s surface, with
the one path change this plan's **Goals** names.

Tests:

- **Integration**: `payments/tests/views/test_route_surface.py` — every billing
  `reverse()` name still resolves, and every client-facing path is byte-identical to
  today's; the two provider webhooks resolve at their new `/billing/payments/{pk}/…`
  paths under their unchanged reverse names, proving they survived the move out of the
  router into `get_extra_patterns()`; the two `billing/payment-provider/` patterns
  resolve unchanged.
- **Integration**: `payments/tests/tasks/` retargeted — each beat task calls its
  `vinta_billing.jobs` counterpart, and the dispatcher enqueues rather than running
  inline.
- **Unit**: `common/tests/test_exception_handlers.py` — each billing exception still
  renders its current status code and body.

**Suggested AI model**: Tier 3. Multi-file orchestration across tasks, DI, settings
and URL conf, with the schema regeneration to verify.

**Reusable skills**: none. `add-migration` is not needed here, and `handoff-to-client`
was removed by the 2026-08-19 amendment — no client-visible surface changes in this
phase, so there is nothing to hand off.

Acceptance: `/api/billing/` responds exactly as `/api/payments/` did, the beat
schedule resolves all four tasks, `schema.yml` regenerates with the path change as
its only structural diff, full suite green.

---

### Phase 2b — vinta-django-billing gap release *(parallel lane, conditional)*

**Goal**: any seam the host needs and the package lacks is fixed in the package, not
worked around in the host. Runs alongside Phases 1–4 in the
`vintasoftware/vinta-django-billing` repo. Skipped entirely if no gap appears.

**Feature flag**: not applicable — different repository.

Changes:

1. The gap, in `vinta_billing/`, with a test and a `HISTORY.md` entry.
2. `bump-my-version bump minor` → 0.4.0, tag `main` after merge, publish.
3. Host: bump the pin to `>=0.4,<0.5` and refresh `uv.lock`.

**Deploy ordering**: the package publishes to PyPI *before* the host phase that
depends on it merges. A host phase blocked on a gap waits on this lane.

Spec use-case: shared scaffolding — no use-case yet.

Tests: the package's own suite (704 default / 714 swapped) plus a host test
exercising the new seam.

**Suggested AI model**: Tier 3 — depends on the gap, but package changes carry a
public API and a changelog.

**Reusable skills**: none in this repo.

Acceptance: the host installs the released version from PyPI and the gap's host test
passes.

---

### Phase 3 — Consumer imports: `calendar_integration` and `webhooks`

**Goal**: the two consumers with production billing call sites import the package
directly; their shims stop being load-bearing.

**Feature flag**: none — import rewrite, no behaviour change.

Changes:

1. `calendar_integration/`: `services/calendar_event_service.py`,
   `services/calendar_service.py`, `services/calendar_group_service.py`,
   `services/calendar_bundle_service.py`, `services/calendar_sync_service.py`,
   `services/calendar_webhook_service.py`, `services/availability_service.py`,
   `services/calendar_service_context.py`, `tasks/calendar_sync_tasks.py`,
   `mutations.py` — `payments.*` → `vinta_billing.*`. `LimitedResource.X` becomes the
   registry key string, referenced through a host constant so call sites keep a
   symbol rather than a literal.
2. `webhooks/services/webhook_service.py`: same rewrite.
3. Both apps' tests follow.

Spec use-case: no SPEC — preserves the limit-enforcement use-cases in
`BILLING_PLANS_AND_LIMITS`.

Tests: the two apps' existing suites, retargeted and green — including
`test_postpaid_enforcement.py`, `test_calendar_limits.py`,
`test_availability_limit_concurrency.py` and
`test_webhook_subscription_limits.py`, which are the ones that would catch a
mis-mapped resource key.

**Suggested AI model**: Tier 2. Mechanical rewrite against an established pattern,
across more than three files.

**Reusable skills**: none.

Acceptance: `grep -rn "from payments" calendar_integration/ webhooks/` returns
nothing; both suites green.

---

### Phase 4 — Consumer imports: `public_api`, `accounts`, `common`, root fixtures

**Goal**: the remaining consumers import the package. After this phase the shims
have no callers.

**Feature flag**: none.

Changes:

1. `public_api/`: `views.py`, `services.py`, `middlewares.py`, `mutations.py` and
   the seven test modules.
2. `accounts/account_adapters.py` and its tests.
3. `common/exception_handlers.py` — any import Phase 2 left.
4. Root `conftest.py`: `BillingPlan` / `PlanEntitlement` / `PlanLimit` /
   `NoDefaultBillingPlanError` from `vinta_billing`, and the reseed fixture.
5. `scripts/one_off/2026-08-05-repair-untruncated-recurring-parents/`.

Spec use-case: no SPEC — preserves the entitlement gates specified in
`BILLING_PLANS_AND_LIMITS` and the public-API surface in `PUBLIC_API_DOCS`.

Tests: those apps' suites retargeted and green, `test_entitlement_gates.py` and
`test_system_user_limits.py` in particular.

**Suggested AI model**: Tier 2. Same rewrite, thinner call sites.

**Reusable skills**: none.

Acceptance: `grep -rn "from payments\." --include=*.py . | grep -v "^./payments/"`
returns nothing outside the shims' own tests.

---

### Phase 5 — Triage the host billing test suite

**Goal**: the host stops re-testing the package's engine and keeps the tests that
prove the host's own wiring.

**Feature flag**: none.

**Sizing**: almost entirely deletion — ~15,000 LoC removed, a few hundred rewritten.
The reviewable artefact is the keep/drop table in the PR body, not the diff.

Changes:

1. Delete the engine-internals modules under `payments/tests/`, roughly 15,000 LoC:
   the provider-adapter tests (`services/payment_adapters/`,
   `services/subscription_adapters/`), `services/test_dunning_service.py`,
   `services/test_cycle_close.py`, `services/test_plan_change.py`,
   `services/test_grace_recovery.py`, `services/test_payment_services.py`,
   `services/test_provider_registry.py`, `test_dunning_schedule.py`,
   `test_cycle_close_idempotency.py`, `test_cycle_close_concurrency.py`,
   `test_exceptions.py`, `test_over_limit_error.py`, `views/test_payment_webhooks.py`,
   `test_admin.py`, and the migration tests superseded by Phase 1's.
2. Keep and retarget the host-wiring modules: `services/test_usage_counters.py`,
   `services/test_metering_tenancy.py`, `services/test_pooled_limits.py`,
   `services/test_limit_concurrency.py`, `services/test_restricted_enforcement.py`,
   `test_prepaid_resource_coverage.py`, `test_prepaid_surface_contracts.py`,
   `test_reseller_restriction.py`, `test_schema_contract.py`, `test_settings.py`,
   `test_plan_seed_migration.py`, `test_dunning_recipients.py`, the `views/` contract
   tests, and `management/commands/`.
3. Fold what Phase 0 and Phase 1 added into the kept layout.

Spec use-case: no SPEC — coverage bookkeeping.

Tests: this phase *is* tests. The gate is that every deleted module has a named
counterpart in the package's suite, recorded in the PR body as a two-column list.

**Suggested AI model**: Tier 2 for the deletions; step up to Tier 3 for the
keep/drop judgement on the four modules that mix both concerns
(`test_metering_reconciliation.py`, `services/test_metering_service.py`,
`services/test_entitlement_service.py`, `services/test_subscription_service.py`).

**Review models**: reviewer Tier 4 — the failure mode here is silent loss of
coverage on a host-specific seam, which no test can catch by construction. The
review is the control.

**Reusable skills**: none.

Acceptance: full suite green; every deleted module named against its package
counterpart; the eight resource counters, the reseller pooling rules and the REST
contract all still have host tests.

---

### Phase 6 — Delete the shims and close out

**Goal**: `payments/` contains only configuration. No module in the repo re-exports
the package.

**Feature flag**: none.

Changes:

1. Delete `payments/models.py`, `payments/exceptions.py`, `payments/constants.py`,
   `payments/billing_constants.py` and the `payments/services/*.py` shims.
   `payments/services/` goes away entirely.
2. Retarget `payments/billing_plans_catalog.py` and
   `payments/tests/billing_fixtures.py` at registry keys and
   `vinta_billing.constants.LimitKind`. The frozen copy inside
   `payments/migrations/0007_seed_billing_plans.py` is **not** touched — a data
   migration keeps meaning what it meant, which is that module's whole premise.
3. Final `payments/` inventory: `apps.py`, `seams/`, `tasks.py`,
   `notification_contexts.py`, `billing_plans_catalog.py`, `migrations/`, `tests/`.
4. Docs: `AGENTS.md`'s app inventory and billing section, `README.md`, and a
   `docs/` note on where the engine now lives and how to upgrade the pin.

Spec use-case: no SPEC — cleanup.

Tests: full suite green with no shim in the tree.

**Suggested AI model**: Tier 1. Mechanical deletion and doc edits.

**Reusable skills**: `deslop-comments` on the comments Phases 0–2 introduce.

Acceptance: `grep -rn "from vinta_billing" payments/ | grep -v seams/ | grep -v tests/`
returns only `tasks.py` and `billing_plans_catalog.py`; `find payments -name "*.py"
-not -path "*/migrations/*" -not -path "*/tests/*"` lists at most ten files; full
suite green.

## 6. Risk & Rollout Notes

**No feature flag.** Recorded in **Guiding Decisions**; the rollback levers are the
per-phase revert and Phase 1's reverse migration. Every phase is independently
mergeable and independently reversible, which is the property that replaces the flag.

**Why the table rename lost.** `AlterModelTable` inside `SeparateDatabaseAndState`
renames `payments_*` in place — no copy, no sequence work — but it collides with
`vinta_billing.0001_initial`, which creates the same tables. Ordering the rename
first requires `migrate --fake-initial` at deploy time, and pytest-django builds the
test database with a plain `migrate`, so the test suite would break in every
environment that has no deploy script. Ordering it second means dropping the freshly
created tables and inheriting `payments_*`-prefixed index and constraint names,
which later package migrations would fail to find. The copy runs unattended
everywhere and leaves the package's own index names in place. Its cost is a table
copy, which is acceptable at staging volume and would need revisiting at production
volume — noted in **Open Questions**.

**Locks.** The copy holds ordinary write locks on twenty small tables inside one
transaction. No `ACCESS EXCLUSIVE` on a hot table, no partitioned table involved, no
index rebuild on `calendar_integration`.

**Sequences.** Every copied table's identity sequence is advanced past the highest
copied PK. A missed `setval` does not fail the migration — it fails the *next insert*,
in production, with a duplicate key. Phase 1's test asserts allocation, not just row
counts.

**Permissions.** Group grants move in the same transaction as the rows. If the
migration is reverted, the reverse re-grants the `payments` permission; a
half-reverted state where neither permission is granted would 403 every billing
endpoint for admins, which the reverse-path test covers.

**Backfill.** None beyond the copy. The copy is idempotent only in the sense that it
runs once — it is guarded by the migration record, not by an upsert.

**The URL change — rescoped 2026-08-19.** No client-facing path moves, so no
`handoff-to-client` runs and no phase in this plan is user-visible. The two provider
webhooks do move, `/payments/{id}/…` → `/billing/payments/{id}/…`, in the package's 0.4.0
rather than host-side; see the amendment under **Goals**.

**Provider callback URLs — a standing constraint that outlives this plan.** This move is
safe only because nothing is registered yet. `MercadoPagoSubscriptionAdapter` bakes
`notification_url` into the MercadoPago **preapproval**, which is notified on every
recurring charge for the life of the subscription; `MercadoPagoPaymentAdapter` does the
same per payment. Once a preapproval exists, changing that path breaks recurring
notifications **silently** — the provider keeps charging, the host never hears, and
subscriptions drift until dunning fires on customers who have paid. After the first
provider registration, treat these two paths as frozen: add an alias, never move them.

**Deploy ordering.** Phase 1 must be deployed before Phase 2 (the app must be
installed before the URL conf imports its routes) and before Phases 3–4 (the shims
must exist before the consumers stop needing them — they exist from Phase 1 to
Phase 6, so this is automatic). Phase 2b, if it happens, publishes to PyPI before
the host phase that needs it merges.

**Rollback.** Phase 6 → revert. Phase 5 → revert. Phases 2–4 → revert. Phase 1 →
revert the commit and run `migrate payments 0022`, which copies back and recreates.
Phase 0 → revert; nothing depends on it.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Do the package's serializers produce byte-identical responses to the host's, or does the 117-line divergence in `serializers.py` reach the wire? | Diff the regenerated `schema.yml` in Phase 2 and treat any response-shape change as a Phase 2b package fix, not a client change. | Eng, during Phase 2 |
| At production volume, is a twenty-table copy still acceptable? | Yes at current staging volume. Re-derive before the first production deploy that carries real billing rows; if it is not, the rename-plus-`--fake-initial` path is documented above and can be swapped in. | Eng, before production |
| Does `payments` remain the right app label once it holds only configuration? | Keep it. Renaming costs another content-type migration and invalidates `organizations.0028`'s dependency for no functional gain. Revisit only if the label confuses newcomers. | Product/Eng |
| Should `payments/seams/` live in `payments` at all, or in `common`? | `payments`. The seams are billing configuration and the app already exists; moving them to `common` would make `common` depend on `calendar_integration` through the occurrence source. | Eng |

## 8. Touch List

**Phase 0**
- `@pyproject.toml`, `@uv.lock`
- `@payments/seams/__init__.py`, `@payments/seams/resources.py`, `@payments/seams/hierarchy.py`, `@payments/seams/notifier.py`, `@payments/seams/occurrences.py`, `@payments/seams/dispatch.py`
- [settings/base.py](../vinta_schedule_api/settings/base.py)
- `@payments/tests/seams/test_resources.py`, `@payments/tests/seams/test_hierarchy.py`, `@payments/tests/seams/test_settings.py`

**Phase 1**
- `@payments/migrations/0023_move_billing_to_vinta_billing.py`
- [settings/base.py](../vinta_schedule_api/settings/base.py), [payments/apps.py](../payments/apps.py)
- Deleted: `payments/models.py` (as implementation), `managers.py`, `querysets.py`, `virtual_models.py`, `entitlement_cache.py`, `filtersets.py`, `pagination.py`, `provider_slugs.py`, `serializers.py`, `views.py`, `billing_views.py`, `admin.py`, `services/` (except the Phase 0 seams)
- Shimmed: `payments/models.py`, `payments/exceptions.py`, `payments/constants.py`, `payments/billing_constants.py`, `payments/services/*.py`
- [organizations/permission_catalog.py](../organizations/permission_catalog.py), [organizations/serializers.py](../organizations/serializers.py#L590)
- `@payments/tests/test_table_move_migration.py`, `@payments/tests/test_shims.py`

**Phase 2**
- [payments/tasks.py](../payments/tasks.py), [di_core/containers.py](../di_core/containers.py), [common/exception_handlers.py](../common/exception_handlers.py), [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py), [settings/base.py](../vinta_schedule_api/settings/base.py), `schema.yml`
- Deleted: `payments/routes.py`
- `@payments/tests/views/test_route_surface.py`

**Phase 2b (cross-repo — `vintasoftware/vinta-django-billing`)**
- `vinta_billing/<gap>.py`, `HISTORY.md`, `pyproject.toml`, `vinta_billing/__init__.py`
- Host: `@pyproject.toml`, `@uv.lock`

**Phase 3**
- `calendar_integration/services/*.py` (8 modules), `calendar_integration/tasks/calendar_sync_tasks.py`, `calendar_integration/mutations.py`, `calendar_integration/tests/**`
- `webhooks/services/webhook_service.py`, `webhooks/tests/test_webhook_subscription_limits.py`

**Phase 4**
- `public_api/{views,services,middlewares,mutations}.py` and `public_api/tests/**`
- `accounts/account_adapters.py`, `accounts/tests/**`
- [conftest.py](../conftest.py)
- `scripts/one_off/2026-08-05-repair-untruncated-recurring-parents/{script,test_script}.py`

**Phase 5**
- Deletions and retargets across `payments/tests/**`

**Phase 6**
- Deleted: the five shim modules and `payments/services/`
- [payments/billing_plans_catalog.py](../payments/billing_plans_catalog.py), `payments/tests/billing_fixtures.py`
- `@AGENTS.md`, `@README.md`, `@docs/`
