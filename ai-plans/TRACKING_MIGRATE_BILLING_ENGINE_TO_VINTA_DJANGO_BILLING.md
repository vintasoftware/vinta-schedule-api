# Tracking — Migrate the Billing Engine to vinta-django-billing

**This file is the conductor's.** Sub-agents: read it, do not rewrite it. Report your
results back in your final message; the conductor writes them here.

Plan: `ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md`

Started: 2026-08-18 · Last updated: 2026-08-18

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
| 0 | Install the package and write the seams | 3 / sonnet | ⏳ pending | — |
| 1 | Install the app, move the rows, shim the host modules | 4 / opus | ⏳ pending | — |
| 2 | Point the host's own entry points at the package | 3 / sonnet | ⏳ pending | — |
| 2b | vinta-django-billing gap release | — | 🚫 deferred — cross-repo (`vintasoftware/vinta-django-billing`) | — |
| 3 | Consumer imports: `calendar_integration` and `webhooks` | 2 / sonnet (>3 files) | ⏳ pending | — |
| 4 | Consumer imports: `public_api`, `accounts`, `common`, root fixtures | 2 / sonnet (>3 files) | ⏳ pending | — |
| 5 | Triage the host billing test suite | 3 / sonnet | ⏳ pending | — |
| 6 | Delete the shims and close out | 1 / haiku | ⏳ pending | — |

No feature flag is declared by this plan, so there is no flag-removal phase.

## Completed phases

_none yet_

## Deferred phases

- **Phase 2b** — cross-repo, and conditional besides ("skipped entirely if no gap
  appears"). If a phase hits a package gap, it stops and reports rather than working
  around it host-side; the fix, the 0.4.0 release and the pin bump happen in
  `vintasoftware/vinta-django-billing`.
