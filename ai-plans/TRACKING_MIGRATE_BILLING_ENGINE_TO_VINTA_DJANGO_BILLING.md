# Tracking — Migrate the Billing Engine to vinta-django-billing

**This file is the conductor's.** Sub-agents: read it, do not rewrite it. Report your
results back in your final message; the conductor writes them here.

Plan: `ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md`

Started: 2026-08-18 · Last updated: 2026-08-19

## run_options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config + standing user preference (chain phases straight through) |
| `generate_inline_comments` | `false` | config |
| `use_worktree` | `true` | config |
| `full_test_suite` | `false` (quick / scoped) | config |
| `commit_strategy_resolved` | `stacked-branches` | user answered (config was `ask`) |

## Resolved topology

| Value | Resolution |
|---|---|
| `WORKROOT` | `/Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/billing-engine-vinta-django-933687` |
| `BASE_BRANCH` | `claude/billing-engine-vinta-django-933687` (@ 4b0c353b, = origin/main) |
| `SANDBOX_TIER` | `none` — `sandbox-exec` exists but the conductor session is rooted in the main checkout and claude-code runs subagents in-process, so the guard could not be installed mid-session. Prevention degrades to the review-phase stray-write backstop: `git -C <main checkout> status --short` after every implementer and every fixer. |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/billing-engine-vinta-django.yaml` |
| main checkout | `/Users/hugobessa/Workspaces/vinta-schedule` — **read-only for this run** |

The worktree was created by the harness, not by `prepare-worktree`. The user chose to
provision it in place rather than spend a second checkout, so the branch keeps its
harness name instead of `plan-…`. Phase branches stack on it normally.

## Worktree provisioning (delegate, haiku)

Provisioned in place on 2026-08-19. Summary:
`/Users/hugobessa/Workspaces/vinta-schedule/.vinta-ai-workflows/worktrees/billing-engine-vinta-django.yaml`

| Concern | Resolution |
|---|---|
| deps | **fresh `.venv` in the worktree**, not the configured symlink — `deps_change` is true (Phase 0 adds `vinta-django-billing`), and a symlinked venv would leak the new dependency into the main checkout |
| env | `.env` + `.env.docker` copied (never symlinked) and extended with `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, forked `DATABASE_URL` / `TEST_DATABASE_URL`, plus a placeholder `MERCADOPAGO_ACCESS_TOKEN` |
| dev DB | `vsa_wt_billing_engine` — 103 migrations applied cleanly, isolation confirmed against main's `vinta_schedule_api` |
| test DB | `test_vsa_wt_billing_engine` — 26 chars, so `test_` + name + xdist `_gwN` stays well under Postgres's 63-char identifier limit (the failure mode the hardening run hit) |
| compose | project `vinta-schedule_wt_billing-engine-vinta-django`; `dbdata`, `floci_data`, `virtualenv` all forked; ports stripped from every service |
| untracked runtime | `vinta_schedule_api/settings/local.py` copied from main |

Verified by the conductor after the delegate returned: `.venv` is a real independent
directory (not a symlink to main's), the main checkout is clean, and the worktree holds
nothing but this tracking file.

One overstatement in the delegate's report, corrected here: it said the fresh venv
"includes vinta-django-billing from Phase 0". It does not — Phase 0 has not run and the
package is not in `pyproject.toml` yet. Phase 0's own `uv sync` installs it. What
matters is that the venv is independent, and it is.

Teardown (do NOT run until the plan is merged — see the summary yaml for the exact commands):
`docker compose down -v` in the worktree, drop both forked databases, `git worktree remove`.

## Agent models

| Role | Tier | Model | Source |
|---|---|---|---|
| implementer | per phase | see phase rows | plan's `**Suggested AI model**` line |
| reviewer | 3 | sonnet | `agent_models.reviewer` — **overridden to 4 (opus) on Phases 1 and 5** by their `**Review models**` lines |
| fixer | 2 | haiku (sonnet when >3 files) | `agent_models.fixer` |
| worktree_prep | 1 | haiku | `agent_models.worktree_prep` |
| integrate | 1 | haiku | `agent_models.integrate` |

Runtime exposes anthropic only, so tier → model is 1/2 → haiku, 3 → sonnet, 4 → opus.

## Pre-flight findings (conductor)

Checked before Phase 0, so no phase discovers these the hard way:

- **`vinta-django-billing` 0.3.0 is on PyPI.** Releases: 0.1.0, 0.2.0, 0.3.0. The
  plan's `>=0.3,<0.4` pin resolves.
- **Every seam symbol the plan names exists in 0.3.0**, verified against the wheel:
  `registry.resources` / `registry.entitlements` (module-level singletons),
  `counting.UsageContext` / `count_by_organization` / `merge_breakdowns`,
  `notifications.Notifier`, `metering.OccurrenceSource`, `jobs.Dispatch` +
  the four sweeps and their four per-subscription jobs, `hierarchy.ParentFieldHierarchy`,
  `routing.get_routes` / `get_extra_patterns`, `conf.get_object_from_setting`,
  `permissions.member_holding_manage_billing`, `recipients.members_holding_manage_billing`,
  and migration `0002_manage_billing_permission`.
- **`VINTA_BILLING` rejects unknown keys loudly.** `conf._build_settings` raises
  `ValueError` on any key outside its defaults, so a typo in Phase 0's settings dict
  fails at first access rather than silently falling back. All thirteen keys the plan
  names are valid.
- **`URL_NAMESPACE` is a fourteenth valid key the plan does not mention, and its
  default is wrong for this host.** Phase 0 MUST set
  `VINTA_BILLING["URL_NAMESPACE"] = "api"`. The chain:

  - `vinta_billing/urls_helpers.py:14` reverses `"{URL_NAMESPACE}:{url_name}"`, default
    namespace `"billing"`.
  - The MercadoPago adapters are its only callers, and they reverse **router-generated**
    names — `namespaced("Payments-payment-update")` in
    `payment_adapters/mercadopago_payment_adapter.py:108` and
    `namespaced("Payments-subscription-payment-update")` in
    `subscription_adapters/mercadopago_subscription_adapter.py:149`.
  - `vinta_schedule_api/urls.py:58` mounts the shared router as
    `include((router.urls, "api"))`, so those names live under `api:`.
  - The host's own copies hardcode exactly that today:
    `payments/services/payment_adapters/mercadopago_payment_adapter.py:103` reverses
    `"api:Payments-payment-update"`.

  Left at the default, MercadoPago webhook-callback URL construction raises
  `NoReverseMatch` — and only when MercadoPago is actually exercised, so a green test
  suite proves nothing here. Stripe is unaffected; it does not reverse through this
  helper. Phase 0 sets the key; Phase 2 must keep the router mounted under `api:` or
  change both together.

  Note the two `billing/payment-provider/` extra patterns are mounted at
  `vinta_schedule_api/urls.py:61` with **no** namespace, unlike the router routes.
  Phase 2 swaps them for `vinta_billing.routing.get_extra_patterns()` and must preserve
  that asymmetry.

- **The plan's consumer phases miss `organizations/` entirely.** Phase 3 covers
  `calendar_integration` + `webhooks`; Phase 4 covers `public_api`, `accounts`,
  `common` and the root fixtures. But `organizations/` imports `payments` from five
  production modules — `services.py`, `models.py`, `permissions.py`, `admin.py`,
  `views.py` — plus nine test modules, and the plan's **Non-goals** explicitly expects
  it to "change imports only". Phase 4's own acceptance grep cannot pass while those
  fourteen files stand. Two further files are missed by name in Phase 4's list for the
  same reason: `public_api/extensions.py` and `public_api/admin/system_user.py`.

  **Resolution: fold all sixteen into Phase 4**, whose goal is already "the remaining
  consumers import the package" and whose acceptance criterion already demands exactly
  this. No scope is being added — the plan's acceptance gate is being made reachable.
  Recorded here rather than silently, because it changes Phase 4's file list.

  One coupling to watch: `organizations/tests/test_seat_enforcement.py` imports
  `payments.tests.billing_fixtures.reseed_billing_plans`, which Phase 6 retargets at
  registry keys. Phase 6 must keep that import working or update it.

- **Three migrations import host modules Phase 6 deletes.** Phase 6 says the frozen
  copy inside `payments/migrations/0007_seed_billing_plans.py` "is **not** touched — a
  data migration keeps meaning what it meant". That premise is right, but 0007 is not
  actually frozen: it *imports* the enums live. `grep "^from payments" payments/migrations/*.py`:

  | Migration | Imports | Phase 6 deletes it? |
  |---|---|---|
  | `0007_seed_billing_plans.py:23` | `payments.billing_constants` → `Entitlement`, `LimitedResource`, `LimitKind` | yes |
  | `0009_backfill_unlimited_subscriptions.py:30-31` | `payments.exceptions.MissingSeedBillingPlanError`, `payments.services.subscription_service.billing_root_filter` | yes (both) |
  | `0021_alter_billingprofile_document_type.py:5` | `payments.billing_constants.DocumentTypes` | yes |

  Deleting those modules breaks `migrate` from zero, which is how every test database
  is built — so this would surface as a total suite failure, not a subtle bug. The
  shims keep it alive from Phase 1 through Phase 5; **Phase 6 is where it bites.**

  **Resolution, split by what the symbol is:**
  - `LimitedResource` / `Entitlement` — 0007 iterates `.values` to seed a row per
    member, and the package has **no equivalent** (they become registry keys, by
    design). Freeze them as literal tuples inside 0007. This is what the plan's own
    premise asks for; it just assumed the freezing had already happened.
  - `DocumentTypes` (0021), `MissingSeedBillingPlanError` + `billing_root_filter`
    (0009) — all three **do** exist in the package (`vinta_billing.constants`,
    `vinta_billing.exceptions`, `vinta_billing.services.subscription_service`).
    Repoint those imports at `vinta_billing`; it is the same code at a new address.
    `DocumentTypes` may be frozen to literals instead if the reviewer prefers
    consistency with 0007 — either is defensible, since 0021 only feeds `choices`.

## Phases

| # | Title | Impl tier/model | Status | Branch |
|---|---|---|---|---|
| 0 | Install the package and write the seams | 3 / sonnet | ✅ done | `plan/migrate-billing-engine-to-vinta-django-billing/phase-0` |
| 1 | Install the app, move the rows, shim the host modules | 4 / opus | ❌ incomplete — blocked on package gaps | `plan/…/phase-1` (checkpoint `fc5fa1e6`, not pushed) |
| 2 | Point the host's own entry points at the package | 3 / sonnet | ⏳ pending | — |
| 2b | vinta-django-billing gap release | — | 🚫 deferred — cross-repo (`vintasoftware/vinta-django-billing`) | — |
| 3 | Consumer imports: `calendar_integration` and `webhooks` | 2 / sonnet (>3 files) | ⏳ pending | — |
| 4 | Consumer imports: `public_api`, `accounts`, `common`, root fixtures | 2 / sonnet (>3 files) | ⏳ pending | — |
| 5 | Triage the host billing test suite | 3 / sonnet | ⏳ pending | — |
| 6 | Delete the shims and close out | 1 / haiku | ⏳ pending | — |

No feature flag is declared by this plan, so there is no flag-removal phase.

## Completed phases

### Phase 0 — Install the package and write the seams ✅

Branch `plan/migrate-billing-engine-to-vinta-django-billing/phase-0`, based on
`claude/billing-engine-vinta-django-933687`. Implementer sonnet (plan's Tier 3),
reviewer sonnet (Tier 3), fixers sonnet + haiku. 14 files, +1401 / -2.

**What shipped.** `vinta-django-billing` 0.3.0 (MIT) pinned `>=0.3,<0.4` in
`pyproject.toml`; `uv.lock` refreshed; `vinta_billing.*` added to the mypy
`ignore_missing_imports` list because the package ships no `py.typed` marker. Five
seams under `payments/seams/`: `resources.py` (8 resources + 5 entitlements, each
counter a port of its `_count_*` predecessor onto `vinta_billing.counting`),
`hierarchy.py` (`ResellerHierarchy(ParentFieldHierarchy)`), `notifier.py`
(`NotificationServiceNotifier` over the DI-built vintasend service), `occurrences.py`
(`CalendarEventOccurrenceSource`), `dispatch.py` (Celery bridge that serializes a job's
dotted path through one generic task). `VINTA_BILLING` added to `settings/base.py` with
all 14 valid keys. 39 new tests. `vinta_billing` is deliberately NOT in `INSTALLED_APPS`
— that is Phase 1.

**Acceptance, verified by the conductor on the docker surface, not taken on report:**
`pytest payments/tests/ -n auto` → 1129 passed; `makemigrations --check` → `No changes
detected`; registry → 8 resources, 5 entitlements.

**Review — one BLOCKER, found by the conductor, missed by the reviewer.**
`seams/resources.py` originally imported `LimitedResource` / `Entitlement` from
`payments.billing_constants` and built every registration by iterating them. Byte-identity
was guaranteed, but it inverted section 3.3 (the enums are supposed to *stop* being the
source of truth), it depended on a module Phase 1 shims and Phase 6 deletes — with no
package equivalent to shim against and no phase owning the retarget — and it made the
registration tests self-comparing, so they could not go red. Fixed by making the seam the
definition site: literal keys and labels, one explicit `register(...)` per resource. The
tests now bridge two independent sides. **Proven, not asserted:** a deliberately typo'd
key turned three tests red (`No resource registered under 'organization_members'`), and
reverting turned them green.

The reviewer judged all 39 tests "not self-comparing". That holds for `TestCounterParity`
— which runs new and legacy counters over the same rows *and* pins literal breakdowns,
and is genuinely well built — but not for the registration tests it was lumped in with.

**Review — two SHOULD-FIX, both from the reviewer, both applied.** The two `.unscoped()`
calls in `occurrences.describe()` now carry their justification at the call site rather
than only in the module docstring (the protocol has no organization parameter and the ids
arrive pre-scoped, but AGENTS.md wants the bound visible where the read happens). And four
docstrings claiming the seams are not imported until Phase 1 were corrected — see the
carry-forward section, since the same false premise sits in the plan's Phase 1 body.

One NIT (a plain `RuntimeError` in `notifier.py` where a typed exception might fit) was
left alone; the repo has no single convention for that call-site shape.

### Phase 1 — Install the app, move the rows, shim the host modules ❌ INCOMPLETE

Branch `plan/migrate-billing-engine-to-vinta-django-billing/phase-1`, based on phase-0.
Implementer opus via the `migration-author` agent (plan's Tier 4). Committed as a
labelled checkpoint at `fc5fa1e6` — **62 files, +1976 / −11450 — NOT pushed, NOT
mergeable.** Full suite: **203 failed, 6045 passed** (independently re-run by the
conductor; the count matches the implementer's report exactly).

**The migration itself succeeded and is the salvageable part.** Forward and reverse both
proven against the real forked database, with row counts, PK identity, the transposed-
column trap, and the permission re-grant all verified in `psql`. Sequence allocation was
proven by neutering `_advance_sequence` and watching the gate go red with
`duplicate key value violates unique constraint`, then restoring it. Sequences are
`GENERATED BY DEFAULT AS IDENTITY`, and the sequence name is derived through
`pg_get_serial_sequence` rather than spelled.

**The plan's central premise about table naming is false, and this is why the phase grew
a second migration.** The plan's **Risk & Rollout Notes** argues the copy approach wins
because it "leaves the package's own index names in place". The package does not have its
own names: `vinta_billing/migrations/0001_initial.py` carries eleven `UniqueConstraint`
names and two `Index` names byte-identical to this app's, because the package's models
were ported field-for-field from these. Postgres namespaces constraint and index names per
**schema**, not per table, so `vinta_billing.0001_initial` fails outright wherever
`payments_*` still stands — in every environment, including a fresh test database.
Verified by the conductor against the wheel: all thirteen names match.

The fix is a new `0023_free_colliding_constraint_names.py` ordered with
`run_before = [("vinta_billing", "0001_initial")]`, which drops the thirteen; the move
itself becomes `0024`. The `run_before` edge is also what makes the reverse safe, since
it forces `vinta_billing` to unapply first. The reasoning is sound and the numbering
shift is a deviation the plan should absorb.

**Known defect in the checkpoint**: `test_table_move_migration.py::TestTheReversePath`
passes in isolation (the implementer's 81-passed run) but **fails under `pytest -n auto`**.
A migration round trip inside an xdist worker is not safe as written. The reverse-path
proof therefore does not currently hold under the suite that gates every phase.

**Four package gaps, all reported rather than worked around.** Two verified directly by
the conductor against the 0.3.0 wheel:

1. **BLOCKER — the package's viewsets cannot be mounted by a router.** All eight in
   `vinta_billing/billing_views.py` and `views.py` declare their service as a keyword-only
   constructor argument with **no default** (`def __init__(self, *args, entitlement_service:
   "EntitlementService", **kwargs)`) and pass only `*args, **kwargs` upward. DRF offers no
   per-registration `initkwargs`, so the package's own documented mounting raises
   `TypeError: … missing 1 required keyword-only argument`. The package already ships
   `vinta_billing/services/container.py` with `get_entitlement_service()` and friends —
   the obvious intended defaults, left unwired. **This also blocks Phase 2**, whose whole
   content is swapping `payments/routes.py` for `vinta_billing.routing.get_routes()`.
   The implementer kept the host REST layer pointed at the package's models rather than
   subclassing or monkeypatching, which is the right call under the plan's package-gap rule.
2. **Package viewsets use DRF's regex `url_path` form** (`(?P<provider>[^/.]+)`) while this
   host runs `DefaultRouter(use_regex_path=False)`. Mounting them needs the host router
   flipped to regex mode and `legal/views.py:88` converted with it.
3. **The seat API is gone.** `EntitlementService.check_seat_limit_for_invitation_accept`
   and `check_limit(exclude_invitation_id_resolver=...)` have no package equivalent;
   `organizations/services.py` calls both at three sites. `usage_extra` is the documented
   replacement channel, but it is eager where the host's resolver was lazy — roughly 75 of
   the 203 failures. A host-side fix exists (put the callable into `usage_extra` and
   resolve it inside the seam's own counter, which the engine only invokes on the
   finite-ceiling path), but it touches Phase 4's file, so the implementer left it.
4. **`InapplicableInvitationExclusionError`'s guard is lost.** The host raised it when
   `exclude_invitation_id` was passed for a non-seat resource — a silent-wrong-answer
   guard. The package forwards `usage_extra` opaquely and documents that it never reads
   it, so there is nowhere to raise from. The class is kept defined so the name survives;
   **the guard is not enforced.** Three tests assert it. This is a real behaviour
   regression, not just a test failure.

**A seam the plan never mentions was required.** `SubscriptionService` no longer accepts
`audit_service`; the package publishes a `payment_provider_repointed` signal instead. A
sixth seam, `payments/seams/audit.py`, receives it and writes the same entry. Without it
the provider-repoint audit trail disappears silently — exactly the failure mode the
audit-trail rollout was built to prevent.

**Where the 203 failures sit**, by the implementer's triage: ~75 the seat API (Phase 4),
~60 `mock.patch` targets that now resolve to a shim so the package module goes unpatched
(Phase 5), ~26 migration tests importing historical migrations against a now-model-free
`payments` (Phase 5/6), ~10 `override_settings` on provider keys the package now reads
from `VINTA_BILLING["PROVIDERS"]`, ~8 permission-label assertions, ~6 audit subject types.
None is a data-integrity or migration problem.

**Why this is a plan failure rather than an implementer failure.** Phase 1's acceptance
is "full suite green", and the plan's premise is that re-export shims make every consumer
keep working untouched. That premise does not survive contact with the package: the seat
API, the audit seam and the viewset constructors diverged enough that no shim can bridge
them. Reaching green from Phase 1 would mean absorbing most of Phase 4 and Phase 5 plus a
package release — a re-sequencing decision that belongs to the requester, not to the
phase. Escalation is not the answer either; this was already the plan's top tier, and the
report is correct rather than confused.

## Carry-forward into later phases

Discovered during Phase 0's review. Each one is a correction to a later phase's body,
recorded here because the phase that has to act on it has not run yet.

**→ Phase 1: `payments/seams/` imports need retargeting, and no phase currently owns it.**
`payments/seams/resources.py` imports `payments.models` (`MeteredOccurrence`,
`Subscription`) and `payments.services.subscription_service.current_billing_period_start`.
All three exist in the package (`vinta_billing.models`,
`vinta_billing.services.subscription_service`), but Phase 0 could not import them —
`vinta_billing` is not in `INSTALLED_APPS` until Phase 1, so a model import would raise
`AppRegistryNotReady`. Phase 1 is the first phase that *can* retarget them, and Phase 6
deletes the modules they currently point at. Phase 1 must do it; the plan's Phase 1 body
does not mention `payments/seams/` at all.

**→ Phase 1: the plan's stated reason for the `AppConfig.ready()` import is false.**
Phase 1's body says `payments/apps.py`'s `ready()` should import
`payments.seams.resources` "so registration happens before the first limit check or
admin render". The reviewer verified live, by inspecting `sys.modules` immediately after
`django.setup()`, that `di_core.apps.DICoreConfig.ready()` already calls
`container.wire(packages=INTERNAL_INSTALLED_APPS)`, which recursively imports every
submodule under `payments` — all five seam modules included — at every process start,
*before* `PaymentsConfig.ready()` runs at all. The same mechanism means
`payments/seams/dispatch.py`'s `@shared_task` is registered in Celery workers today,
ahead of `autodiscover_tasks()`.

The `ready()` import is still worth adding, because relying on DI wiring to import a
registry is accidental coupling — `container.wire` exists to find `@inject` call sites,
and nothing guarantees it keeps walking every submodule. But Phase 1 should add it as an
explicit, order-independent guarantee, not as the fix for a gap that does not exist, and
`Registry.register` tolerating the repeat is what makes the belt-and-braces safe.

**→ Phase 1: `payments/billing_constants.py` cannot be a pure re-export shim.** The plan
describes every Phase 1 shim as "a single `from vinta_billing.… import …` line". That
holds for four of the five, but not this one. Verified against the installed package:

| Host `billing_constants.py` | In `vinta_billing.constants`? |
|---|---|
| `BillingState`, `BillingInterval`, `DocumentTypes`, `ProviderWebhookRoute`, `LimitKind`, `LimitRemedy`, `LimitWarningLevel` | yes — re-export |
| `LimitedResource`, `Entitlement` | **no, deliberately** — section 3.3 turns them into registry keys |

So the shim re-exports seven names and must keep the real `LimitedResource` / `Entitlement`
class definitions for the consumers still importing them (`organizations/`,
`calendar_integration/`, `webhooks/`, `public_api/`, and migrations 0007 and 0021).
Phases 3 and 4 retarget those consumers at registry keys; Phase 6 then deletes the module.

`payments/constants.py` is the clean case — all four of its classes (`PaymentProviders`,
`PaymentStatuses`, `RefundStatuses`, `SubscriptionStatuses`) exist in the package.

**→ Phase 3: the "host constant" for registry keys needs a home.** Phase 3's body says
`LimitedResource.X` becomes "the registry key string, referenced through a host constant
so call sites keep a symbol rather than a literal", but never says where that constant
lives. Phase 3 has to choose one — most likely alongside the registrations in
`payments/seams/resources.py`, which is the definition site after Phase 0's review — and
Phases 4 and 6 must use the same one.

**→ Phase 2: `payments/seams/dispatch.py` needs no autodiscovery fix.** Phase 0's
docstring claimed the generic `payments.dispatch_billing_job` task is unreachable from a
worker until Phase 2 imports it from `payments/tasks.py`. Same false premise — it is
already registered. Phase 2 still owns pointing the beat wrappers at
`vinta_billing.jobs`, but not this.

## Deferred phases

- **Phase 2b** — cross-repo, and conditional besides ("skipped entirely if no gap
  appears"). If a phase hits a package gap, it stops and reports rather than working
  around it host-side; the fix, the 0.4.0 release and the pin bump happen in
  `vintasoftware/vinta-django-billing`.
