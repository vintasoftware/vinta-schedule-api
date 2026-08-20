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
| 1 | Install the app, move the rows, shim the host modules | 4 / opus | ✅ done — reviewed, suite green | `plan/migrate-billing-engine-to-vinta-django-billing/phase-1` |
| 2 | Point the host's own entry points at the package | 3 / sonnet | ✅ done — reviewed, suite green | `plan/migrate-billing-engine-to-vinta-django-billing/phase-2` |
| 2b | vinta-django-billing gap release | — | 🚫 deferred — cross-repo (`vintasoftware/vinta-django-billing`) | — |
| 3 | Consumer imports: `calendar_integration` and `webhooks` | 2 / sonnet (>3 files) | ✅ done — reviewed clean | `plan/migrate-billing-engine-to-vinta-django-billing/phase-3` |
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

### Phase 1 — completed on 0.4.0 ✅ (suite green, pending review)

Resumed after `vinta-django-billing` 0.4.0 landed. The first attempt died mid-triage on a
dropped connection with 54 files uncommitted; the same agent was resumed with its context
intact rather than restarted, so the keep/drop reasoning stayed consistent.

Six commits: `33889f79` pin · `d8927f2f` seat API · `78070f42` REST layer · `b9cf8e4a`
reconnected seams · `c3c9fbe8` migration-replay safety · `3936729d` test triage.

**Suite: 6243 passed, 7 xfailed, 0 failed** — re-run independently by the conductor
(4m26s, exit 0), matching the implementer's report exactly. Baseline was 203 failed /
6045 passed. `ruff`, `makemigrations --check`, `check --deploy` and the full pre-commit
chain all clean. Main checkout clean, no AI trailers.

`uv lock --refresh-package vinta-django-billing` was needed — the resolver had 0.3.0
cached as latest and called the new floor unsatisfiable.

**No test module was deleted.** All 29 touched modules were retargeted, in five
categories: `mock.patch` targets naming a shim (4), `override_settings` on provider
credentials (7), historical migrations asking for `payments.BillingPlan` (4),
`payments.manage_billing` labels (10), services built bare (1). Phase 5 keeps the
engine-internals deletion decision, which is the right place for it — none of it was
needed for green.

**`URL_NAMESPACE` is `""`, superseding the pre-flight finding of `"api"`.** That finding
was right only while the webhooks came out of the namespaced router; 0.4.0 binds them in
unnamespaced `extra_patterns`. Verified: both reverse to `/billing/payments/…`.

**Migration re-proved end to end** against the dev database — forward, seeded graph,
reverse to `0022_capability_permissions`, forward again. Row counts, PKs and the
permission grant all restored; the next-insert test allocated above every copied PK
(3→4, 17→18, 11→12, 1→2). The reverse-path test now seeds the graph it measures, and was
shown to bite: skipping `planlimit` in the reverse copy fails it with
`{'planlimit': 0} != {'planlimit': 17}`.

**Review (opus, Tier 4 per the plan): no BLOCKERs.** The reviewer named the five places a
defect would have been invisible and showed each held — the migration's column lists
(re-derived from `information_schema` on both sides, so a wrong list hard-stops rather
than shipping green), the reverse-plan ordering, the exclusion guard actually being on,
seat net-zero, and the permissions seam. Eight SHOULD-FIX and seven NITs, all applied in
`7868058c`. Final suite: **6244 passed, 7 xfailed, 0 failed**.

Two of the fixes were required to be demonstrated rather than asserted:

- **A genuine vacuous gate.** Six tests in `test_provider_routing_migrations.py` called
  `use_providers(default_provider=...)`, which sets `VINTA_BILLING["DEFAULT_PROVIDER"]` —
  but the code under test is frozen migration `0018`, which reads the **top-level**
  `settings.DEFAULT_PAYMENT_PROVIDER`. They were asserting against the ambient `.env`.
  Proof before the fix: `assert 'stripe' == PaymentProviders.MERCADOPAGO`. A data
  migration keeps reading the setting it read when it was written; a helper that renames
  the setting has to keep both spellings in step.
- **The reverse path never allocated a PK.** The forward direction had that test
  precisely because a missed `setval` is invisible to row counts; the reverse did not.
  Proof: neutering the reverse `_advance_sequence` gives
  `duplicate key value violates unique constraint "payments_billingaddress_pkey"`.

Two dead shims (`filtersets.py`, `virtual_models.py` — no importers anywhere) were
deleted rather than carried to Phase 6.

### mypy regression — 565 new errors, invisible to every per-phase check

Measured by the conductor across the whole plan, which no individual phase could see:

| Tree | mypy |
|---|---|
| base branch `claude/billing-engine-vinta-django-933687` | 293 errors / 57 files |
| Phase 1 HEAD | **858 errors / 163 files** |

Every agent honestly reported "no new errors against my starting point" — but the
starting point moved each phase, so a steady drift went unreported. This is a real
regression introduced by Phases 0–1, and the project's stated type gate is
`docker compose run --rm api uv run mypy .`.

**One root cause.** 655 of the errors are `[attr-defined]` of the form
`Module "payments.models" has no attribute "Subscription"`. Phase 0 added `vinta_billing.*`
to mypy's `ignore_missing_imports` because the package ships no `py.typed` marker; mypy
therefore treats it as `Any`, and **a star import from an untyped module re-exports
nothing**. Phase 1's shims are star re-exports, so every consumer importing through a
shim lost its types.

The package is *fully annotated* and runs mypy in its own pre-commit gate — it simply
never declared itself typed. **Adding `py.typed` upstream is a one-line fix** that lets
the host drop the override and restores type checking across the whole billing surface.
Recorded as a 0.5.0 item. Phase 6 deletes the shims anyway, but the errors stand until
then and would mask any genuine new type error in the meantime.

### Two confirmed defects in published `vinta-django-billing`

Both reported by the implementer and **verified independently by the conductor**. Both
are live in 0.4.0 and neither is host-fixable in principle.

1. **Authorization bypass — `IsBillingManager.has_object_permission` is a no-op.** It
   reads `getattr(obj, "organization", None)`, but all three object-level checks in the
   package's own viewsets (`billing_views.py:509`, `:623`, `:864`) pass a **billing root**
   — an `Organization`, which has no `organization` field (confirmed:
   `Organization._meta` has no such field and `hasattr` is False). So it returns `None`
   and falls back to `has_permission`, which resolves the organization from the *request*.
   The package's own comments call this "the real gate". **Effect: a child-org admin can
   change a reseller root's plan and buy add-ons against it.**
   Upstream fix: when `obj` is itself an organization, check against `obj`.
2. **Stripe subscription billing never resolves a payment.** The package reads
   `Invoice.billing`; `stripe==15.3.1`'s `Invoice` has `payments` and **no** `billing`
   (confirmed by introspecting `stripe.Invoice.__annotations__` in the container). The
   same file still *expands* `latest_invoice.payments` while reading `billing`, and the
   docstrings say `payments` throughout — the signature of a bad find-and-replace during
   the extraction. **Effect: `get_payment_external_id_*` returns `None` for every Stripe
   subscription charge, so `invoice.paid` resolves no payment, dunning is never cleared,
   and customers who have paid keep being chased.**
   Held as seven `xfail(strict=True)` asserting the *correct* field, so they flip to a
   loud XPASS the moment the pin moves to a fixed release.

Two further gaps were reported but are lower severity: `BillingProfileAdmin.save_model`'s
`subscription_service` is never supplied by the package (worked around via host DI), and
there is no seam for the tenant-scoped view mixin or the service container, **so Phase 2
cannot mount `get_routes()` verbatim** — it must mount the host subclasses or the package
needs those settings.

### Superseded — the first Phase 1 attempt ❌ INCOMPLETE

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

## Plan amendment 2026-08-19 (final) — the webhooks move under `/billing`

Supersedes the "withdrawn" note below, which was written before the requester confirmed
no webhooks are registered with any provider yet.

Requester's goal: every route this project serves for billing sits under `/billing`. The
two provider webhooks were the only exception. With no live provider registrations, the
move is safe, so it is made — **in the package, not host-side**, because
`get_extra_patterns()` already hardcoded its other two endpoints under
`billing/payment-provider`, making `payments/` the package's own inconsistency.

Package commit `1938bf3` on PR #7:
`^payments/{pk}/…` → `^billing/payments/{pk}/…` for both webhooks, `trailing_slash`
handling and reverse names (`Payments-payment-update`,
`Payments-subscription-payment-update`) untouched. Verified by the conductor: 774 passed
/ 10 skipped, 784 swapped — both matching baseline. The PR description and `HISTORY.md`
both previously claimed the URLs were unchanged character for character; both are
rewritten.

**A second, independent reason the move is safe, and the one that generalises:** 0.3.0's
`PaymentsViewSet.__init__` required three services with no defaults — gap 1 — so every
request reaching `payments/…` raised `TypeError` before a provider notification could be
processed. No deployment anywhere has a webhook that ever worked at the old path, so no
adopter is broken either.

**Standing constraint recorded in the plan's Risk & Rollout Notes.** This window closes
the moment the first provider registration happens.
`MercadoPagoSubscriptionAdapter.create_subscription` bakes `notification_url` into the
MercadoPago preapproval, which is notified on every recurring charge for the life of the
subscription. After that, moving these paths breaks recurring notifications *silently* —
the provider keeps charging, the host never hears, subscriptions drift until dunning
fires on customers who have paid. From then on: add an alias, never move. Stripe is
unaffected; neither Stripe adapter reverses a URL.

**Host consequence for Phase 2**: mount `get_extra_patterns()` alongside `get_routes()`,
expect the two webhook paths to move in the regenerated `schema.yml`, and expect no other
path to change. Still no `handoff-to-client` — no client calls these.

## Superseded — plan amendment 2026-08-19 (first pass), the `/payments` → `/billing` move is withdrawn

Prompted by two review comments on package PR #7 asking whether the webhook patterns
should say `payments` or `billing`. Chasing the answer showed the plan's one deliberate
behaviour change rested on a false premise.

`PaymentsViewSet` is a bare `ViewSet` — no `queryset`, no `serializer_class` — carrying
only `payment_update` and `subscription_payment_update`, both inbound provider webhooks.
The shipped `schema.yml` lists exactly four `/payments` paths: those two actions and
their `{format}` variants. **No client calls `/payments`.** The plan's Goal 4 describes
the move as something "the single client adopts in the same release"; there is no such
client.

Moving it would instead break provider integrations. The MercadoPago adapter builds its
callback at charge time via `reverse("api:Payments-payment-update", ...)` and sends it as
`notification_url`, so the URL is stored provider-side per payment. Every retry against
an already-registered callback would strand.

Requester's decision: keep `payments`, drop Goal 4. The plan file is amended — Goals,
Non-goals, section 4's path table, the Feature-flag and Route-ownership rows, Phase 2's
goal / changes / tests / skills, and the Risk & Rollout "URL change" entry, which is
replaced by a standing warning that the webhook paths must never move. Package PR #7
needs no change; `payments` was already correct there.

Net effect on the run: **Phase 2 shrinks to a pure route-ownership swap with no
behaviour change, and its `handoff-to-client` step is gone.** No phase in this plan is
user-visible any more. Phase 2 must mount both `get_routes()` and `get_extra_patterns()`,
since from 0.4.0 the webhooks no longer come out of the router.

## Phase 2b (second lane) — `vinta-django-billing` 0.5.0, PR open

Requester chose "full 0.5.0, then Phase 2" at the gate after Phase 1. Branch
`fix/host-integration-seams` @ `95fa4d0`, PR:
https://github.com/vintasoftware/vinta-django-billing/pull/8
Version deliberately left at 0.4.0 — no bump, no tag, no publish.

| # | Item | Outcome |
|---|---|---|
| 1 | `IsBillingManager.has_object_permission` bypass | Fixed — checks the predicate against the object when the object is an organization, via `AbstractOrganization` so it survives a swapped `ORGANIZATION_MODEL` |
| 2 | Stripe `Invoice.billing` → `payments` | Fixed at all three call sites; the suite now pins the chain against `stripe.Invoice.__annotations__` rather than a remembered name |
| 3 | `py.typed` | Added; conductor verified it is inside both the built wheel and the sdist |
| 4 | `VIEW_MIXIN` + `SERVICE_CONTAINER` | Added, defaulting to current behaviour **by identity** |
| 5 | `BillingProfileAdmin.save_model` | Fixed through the same container seam |

Verified by the conductor: 809 passed / 10 skipped, 819 swapped, `py.typed` in wheel and
sdist, no `0.5.0` tag.

**The security fix was reproduced failing.** Swapping in 0.4.0's `permissions.py` turns
`tests/test_viewset_permissions.py` red at 6 failed / 6 passed; restoring gives 12 passed.
`cancel` answered **200 and genuinely cancelled the root's subscription** — worse than the
"change plan and buy add-ons" framing the conductor gave the implementer. Five endpoints
are affected, not three: `get_subscription(check_object_perms=True)` gates `change-plan`,
`cancel` **and** `retry-payment`.

**Two corrections the implementer made to the conductor's brief, both right:**

1. The mypy figures were internally inconsistent — "565 extra errors, 655 of them
   `[attr-defined]`" has the subset exceeding the total. Re-measured properly: base has
   **93** `[attr-defined]` of 293 total, Phase 1 HEAD has **655** of 858. So **562 of the
   565 new errors** are that one mechanism — a cleaner claim than the original. Corrected
   in `HISTORY.md` (`95fa4d0`) before the PR was opened.
2. `SERVICE_CONTAINER` needed a wider contract than `get_object_from_setting` alone: a
   `dependency_injector` container names its providers `entitlement_service`, not
   `get_entitlement_service`. `resolve_service` accepts both spellings, which is what
   removes the last host-side adapter.

**One adjacent defect fixed unasked, and worth having:** under `OrganizationMiddleware` an
unresolved organization arrives as `SimpleLazyObject(None)`, which is not `None` by
identity — so it passed every `is None` check in the package, **including the deliberately
fail-closed branch in `filter_queryset_by_organization`**. A request that should have been
refused built a query and 500'd instead.

### 0.5.0 RELEASED — 2026-08-19

PR #8 merged by the requester, who then asked the conductor to cut the release. Published
to PyPI; publish workflow `32313897986` succeeded.

The release blocker below was **still present at merge time** and was fixed before
tagging: `pyproject.toml` said `current_version = "0.3.0"` while `__init__.py` said
`0.4.0`. Left alone, `bump-my-version bump minor` would have computed `0.4.0`, failed its
search-and-replace, and tried to re-tag an existing version. Sequence actually run:

1. `b45be8b` — correct `current_version` to `0.4.0`.
2. `bump-my-version bump minor --dry-run` — confirmed `0.4.0 → 0.5.0`, both files, tag
   `0.5.0` unprefixed.
3. `6c0371f` — the real bump, which commits and tags.
4. Push `main`, then push tag `0.5.0` (the tag push is what triggers publication).

So `main` carries two commits beyond the merge: the drift fix and the bump.

**Verified against the published wheel downloaded from PyPI**, not from CI's green tick:
`vinta_billing/py.typed` ships; the authorization fix is present; the Stripe adapter has
**zero** remaining `"billing"` reads and four `"payments"`; `VIEW_MIXIN` and
`SERVICE_CONTAINER` are both in `conf.py`.

### ⚠️ Release blocker (resolved above) for whoever cuts 0.5.0

`pyproject.toml`'s `[tool.bumpversion] current_version` is **`0.3.0`** while
`vinta_billing/__init__.py` is **`0.4.0`** — pre-existing drift, not introduced here.
`bump-my-version bump minor` would compute 0.3.0 → 0.4.0, fail to find its search string,
and try to tag a version that already exists. **Correct `current_version` to `0.4.0`
before cutting the release.** Flagged in the PR body.

### What Phase 2 becomes once 0.5.0 is out

The implementer confirmed against the host's actual files that the host can then delete
`payments/views.py`, `payments/billing_views.py`, `payments/routes.py` and
`payments/seams/view_scoping.py` outright, plus `payments/admin.py`'s subclass — which the
brief had not counted. `payments/seams/permissions.py` goes too **if** the host accepts
`BILLING_MANAGER_PREDICATE` (already configured as `member_holding_manage_billing`) as the
object-level answer; that covers branch 1 of `IsBillingOwnerOrAdmin` exactly, and branch 2
— the acting-reseller-root subtree walk — is unreachable from any endpoint today by that
class's own docstring. To keep it, the host writes it as a `(user, organization) -> bool`
predicate rather than a permission class plus a `get_permissions` rewriter.

Host settings Phase 2 must add:
`"VIEW_MIXIN": "common.utils.view_utils.TenantScopedViewMixin"` and
`"SERVICE_CONTAINER": "di_core.containers.container"` (resolved lazily per view
construction, so `container.<provider>.override(...)` in tests is honoured).

### Phase 2 — Point the host's own entry points at the package ✅ (pending review)

Branch `plan/migrate-billing-engine-to-vinta-django-billing/phase-2`, based on phase-1.
Implementer sonnet (plan's Tier 3). Six commits: `f8412fa2` pin · `49bd55dc` beat tasks ·
`80c6d703` DI + exception handling · `0da368dd` routes + enum settings · `5b47ed70`
REST-layer deletion · `283366f1` Stripe xfails removed.

The implementer stopped once mid-phase waiting on its own background test run; it was
resumed with context intact rather than restarted.

**Suite: 6283 passed, 0 failed, 0 xfailed** — re-run independently by the conductor
(6m15s, exit 0). The seven Stripe `xfail(strict=True)` markers are gone: 0.5.0 fixed the
field they asserted, so they became ordinary passing tests. That is exactly what
`strict=True` was there to signal.

**The mypy regression is fully repaid: 858 → 294**, against a pre-migration baseline of
293/57. Removing `vinta_billing.*` from `ignore_missing_imports` — now that 0.5.0 ships
`py.typed` — recovered all 562 `[attr-defined]` errors. None of the remaining 294 are in
any file this phase touched.

**`schema.yml` diff is empty**, confirmed by the conductor against phase-1. Correct: the
webhook paths moved during Phase 1, so any movement here would have meant a mistake.

**Route surface verified independently by the conductor:**

```
Payments-payment-update              -> /billing/payments/7/payment-update/stripe/
Payments-subscription-payment-update -> /billing/payments/7/subscription-payment-update/stripe/
payment-provider                     -> /billing/payment-provider/
payment-provider-default             -> /billing/payment-provider/default/
Payments in get_routes() basenames?  False
```

That last line is the load-bearing one: the webhooks provably come from
`get_extra_patterns()`, not the router. A host that mounted only `get_routes()` would have
404'd both webhooks silently.

**0.5.0 paid off as estimated, and slightly better.** Deleted: `payments/routes.py`,
`views.py`, `billing_views.py`, `seams/view_scoping.py`, `seams/permissions.py`, **and
`admin.py`** — the last of which was not in the estimate; `SERVICE_CONTAINER` supplies the
service its `save_model` subclass existed for.

`payments/seams/permissions.py` was deleted after checking both branches of
`IsBillingOwnerOrAdmin` against the code rather than the docstrings: branch 1 is exactly
what the configured `BILLING_MANAGER_PREDICATE` computes, and branch 2's acting-reseller-
root walk needs the bound membership to differ from the object, which the header resolver
never produces. The reviewer confirmed both independently against
`vinta_billing/permissions.py` and `vinta_orgs/drf.py` — no authorization regression.

**Correction.** An earlier revision of this entry said `IsBillingOwnerOrAdmin` "itself was
kept — `public_api/capabilities.py` still uses it". That is **false**, and the conductor
propagated it from the implementer's report without checking. `public_api/capabilities.py:35`
only *mentions* the class in a docstring explaining why the subtree walk is
transport-neutral; there is no import and no `permission_classes` entry. Every other
reference in the repo is a docstring, a migration `help_text`, or a test.

So Phase 2 **orphaned** it: deleting `payments/seams/permissions.py` removed its last live
wiring, and `organizations/permissions.py:417-549` — roughly 130 lines including the
acting-reseller-root subtree walk — is now unreachable from every request path, REST,
GraphQL and admin alike. Reachable only from `payments/tests/test_reseller_root_billing.py`
and `organizations/tests/test_permissions_parity.py`.

**→ Phase 5 owns the decision**, since it is the keep/drop phase and
`test_reseller_root_billing.py` sits on its explicit *keep* list — a list written before
the thing it tests became unwired. Either delete the class with its tests, or keep it and
say plainly in its docstring that it is retained-but-unwired policy. What must not happen
is leaving tests that pass while proving nothing about production, which is how a future
reader concludes the policy is enforced when it is not.

**Review (sonnet): no BLOCKERs.** It independently traced the two silent-failure risks —
branch-2 unreachability through `vinta_orgs/drf.py`'s resolver rather than the docstring,
and that the beat tasks really dispatch instead of running inline. Three SHOULD-FIX and
two NITs; the actionable ones applied in `3b1aaa2f`. Suite re-verified by the conductor
after the fixes: **6283 passed, 0 failed** (3m19s, exit 0).

Fixes applied:

- **The dead dispatch seam is gone.** `payments/seams/dispatch.py` and
  `VINTA_BILLING["JOB_DISPATCHER"]` deleted, along with the settings test's assertion on
  the key. The only surviving mentions are a docstring in `payments/tasks.py` recording
  *why* it went, which is history rather than dead code.
- **A real bug in multi-tenancy safety tooling, fixed rather than routed around.**
  `common/organization_context_test_support.py`'s `_is_scoped_enough` caught only
  `(TypeError, ValueError)` around `str(query)`, but Django raises `EmptyResultSet` — a
  subclass of neither — for a structurally-empty `IN` clause, which a plain recurring
  event with no exceptions produces. The guard therefore *crashed* instead of reporting
  or passing. Demonstrated red (`django.core.exceptions.EmptyResultSet` escaping) then
  green. A safety check that fails for the wrong reason trains people to work around it,
  which is why this was worth fixing outside the phase's touch list.
- A stale pointer in `celerybeat_schedule.py` to a constant this phase moved.

Test count stayed at exactly 6283 for a stated reason: +1 regression test, −1 removed
`JOB_DISPATCHER` parametrize case.

### The plan predicted five seams; the answer is seven, and not a superset

Worth recording, because it is the clearest measure of how much the plan could not have
known in advance. Final `payments/seams/`: `resources`, `hierarchy`, `notifier`,
`occurrences`, `seats`, `audit`, `resync`.

- Four of the five planned seams survive.
- **`dispatch` was planned and turned out unnecessary** — the package's own job dispatcher
  bypasses host DI, so the tasks pass services explicitly instead.
- **`seats`, `audit` and `resync` were unplanned**, each covering a host behaviour the
  package signals rather than owns.
- **`permissions` and `view_scoping` existed only between Phase 1 and Phase 2**, purely as
  workarounds for package defects, and both were deleted once 0.5.0 fixed them upstream.

### Three declared deviations in Phase 2, all reviewed

1. **`JOB_DISPATCHER` is now bypassed, and `payments/seams/dispatch.py` is dead code.**
   `vinta_billing.jobs`' per-subscription default service resolution builds from the
   *package's* `services.container` cache and does not consult `SERVICE_CONTAINER`. Found
   empirically, not by reading: a DI-overridden Stripe adapter with an empty key was
   silently replaced by the real `STRIPE_SECRET_KEY` from `.env` — a test believing it
   used a fake while using the real credential. Each per-subscription task now resolves
   its service through the host's own `@inject` and passes it in, which the implementer
   argues is the package's documented extension point. **Reported as a package gap, not
   worked around.** The seam and its setting were left in place as dead code for a later
   phase to remove.
2. **A multi-tenancy test-harness bug worked around rather than fixed.**
   `common/organization_context_test_support.py`'s `_is_scoped_enough` re-compiles a query
   via `str(query)` and does not catch `EmptyResultSet`, which Django raises for a
   structurally-empty `IN` clause. Out of scope (shared tooling, not billing), so
   `organization_context` binding was restored around each per-subscription task to match
   pre-Phase-2 behaviour.
3. **`PaymentProviderNotConfiguredError` keeps a hardcoded 409** rather than delegating to
   `vinta_billing.exception_handling.billing_error_status`, which maps it to 503 — a
   deliberate departure from the phase's "delegate, do not re-derive" instruction. The
   argument: the package's own `SubscriptionViewSet.change_plan` / `cancel` docstrings
   promise 409 "mapped centrally", and this project has a committed, tested 409 contract.
   If the package's table and its own docstrings really disagree, this is a package bug
   and the divergence is a correction rather than a deviation.

### Phase 3 — Consumer imports: `calendar_integration` and `webhooks` ✅

Branch `plan/migrate-billing-engine-to-vinta-django-billing/phase-3`, based on phase-2.
Implementer sonnet (plan's Tier 2, stepped up for file count). Commits: `312f1c6b`
entitlement-key symbols · `746847c0` the 56-import rewrite.

**Review: no BLOCKER, no SHOULD-FIX, one NIT.** The reviewer verified the thing that
mattered — all 13 constant values byte-identical to the enum members they mirror, every
`register(...)` call using the constant, and no swapped resource pair across any call
site. A swapped pair (say `CALENDAR_GROUPS` for `BUNDLE_CALENDARS`) would enforce the
wrong ceiling and nothing would necessarily go red, which is why that check was the point
of the review rather than a formality.

It also made an observation worth keeping: because Phase 1's shims were `import *`
re-exports, these rewrites resolve to **literally the same class objects**, so no `except`
clause could have broken and no behaviour could have changed. That is the strongest
possible answer to "did the mapping drift".

**The registry-key constants live in `payments/seams/resources.py`** — the decision Phase 3
was handed, since the plan named the requirement without naming a home. Reasons: Phase 6
deletes `payments/billing_constants.py`, so it could not go there; the seam is already the
definition site after Phase 0's review; and it survives to the end. Phase 3 extended the
pattern from the eight resource keys to the five entitlement keys and rewired the
registrations to use the symbols, so a constant and its registered key cannot drift.

**→ Phases 4 and 6 use the same symbols.** Phase 4 needs `PARTNER_API`,
`WHITE_LABEL_BRANDING` and `ADVANCED_SCHEDULING` for `public_api` and `organizations`;
Phase 6 retargets `payments/billing_plans_catalog.py` and `payments/tests/billing_fixtures.py`.

Acceptance holds: the only `from payments` imports left in either app are
`payments.seams.resources` and `payments.seams.occurrences`, both host code. mypy unchanged
at 294.

**Chasing a flake found a vacuous test — the more serious problem.** Commit `de38b661`.

`payments/tests/seams/test_resources.py::TestCounterBreakdowns::test_event_occurrences`
failed once under `-n auto` and passed on rerun. The implementer called it pre-existing;
it is not — Phase 0 wrote that file. It guards `event_occurrences`, the one postpaid
resource customers are billed for, so it was sent for a fix rather than a rerun.

**The fixer found that the test could not catch the regression it exists to prevent**, and
the conductor reproduced this independently:

| | broken counter | correct counter |
|---|---|---|
| original test | **passed** — vacuous | passed |
| fixed test | **failed** — a real gate | passed |

"Broken counter" means `_count_event_occurrences` reading `subscription.current_period_start`
instead of `current_billing_period_start(subscription)` — precisely the bug that counter's
docstring says it was written to prevent. The original test could not see it, because a
freshly created subscription's stored column and the freshly computed period always
coincide. Freezing time alone would have removed the race and inherited the blind spot, so
the fix also stamps the stored period one cycle stale, forcing the two apart. That mirrors
the house pattern in `payments/tests/services/test_metering_service.py`.

**A correction to the conductor's own diagnosis.** This tracking file previously stated, as
fact, that the flake came from the test and the counter "straddling a period boundary under
contention". The fixer pushed back and is right: with a 30-day period, wall-clock drift
inside a single test cannot cross that boundary, so that mechanism does not explain the
observed failure. What is actually established is narrower and still worth the commit — a
real vacuity (proven above), and a structural race that is real in principle because both
sides call `timezone.now()` independently. **The cause of the one observed `-n auto` failure
remains unexplained**; database contention is the likelier candidate, since the fixer saw
unrelated contention errors across four apps when it ran suites concurrently. Recorded as
open rather than papered over: the test is now deterministic and non-vacuous either way.

### Phase 4 — Consumer imports: `public_api`, `accounts`, `common`, `organizations`, fixtures ✅

Branch `plan/migrate-billing-engine-to-vinta-django-billing/phase-4`, based on phase-3.
Implementer sonnet. Four per-app commits: `28663ff8` organizations · `652c8819` public_api ·
`af4b13cc` accounts · `92c72c3a` common + conftest + scripts.

**Scope was 36 files, 14 of them the `organizations/` app the plan never assigned to any
phase.** The conductor enumerated the file list rather than leaving it to be inferred: a
raw `from payments` grep returns 118 files, but 68 are `payments/**` itself (Phase 5's
triage and Phase 6's shims) and several are permanent `payments.seams.*` host imports.

**Acceptance met.** Outside `payments/`, the only surviving `from payments.` imports are
`payments.seams.*` — the registry-key symbols, the seat seam and the occurrence source,
all host code — plus two `payments.tests.billing_fixtures` imports deliberately left for
Phase 6. No shim import remains anywhere outside `payments/`.

*(A conductor error worth noting: the first acceptance grep run used `^\./payments/` as
its filter, but grep prints paths without the `./` prefix here, so the filter was a no-op
and appeared to show shim imports surviving. The implementer's `^payments/` form was
correct.)*

**Review: no BLOCKER.** It cross-checked every entitlement and resource symbol against the
registration site — `WHITE_LABEL_BRANDING`, `PARTNER_API`, `ORGANIZATION_MEMBERS`,
`PUBLIC_API_SYSTEM_USERS`, `AVAILABILITY_WINDOWS`, `CALENDAR_GROUPS`, both
`EXTERNAL_CALENDAR_*` — with no swap or typo anywhere. That was the point of the review:
this phase rewrote the gates deciding what a paying customer can do, and a swapped
entitlement silently opens or closes a feature for every tenant without turning a test red.

It also confirmed the three seat-seam calls in `organizations/services.py` are byte-for-byte
untouched (Phase 1 built that named entry point precisely because the bare-kwarg form fails
silently), that `reactivate_membership`'s direct `check_limit` kept its lock and exception
behaviour, and that `usage_extra_keys` declarations were not touched.

mypy unchanged at 294.

### A real import cycle, and why the structural fix waits for Phase 6

Retargeting `organizations/models.py` at the registry-key symbols created a genuine cycle:
`payments/seams/resources.py` imports `organizations.models` to build the
`organization_members` counter, so a module-level import back the other way is circular.
The previous shim import (`payments.billing_constants.Entitlement`, a plain `TextChoices`)
carried no such transitive weight. Fixed with a deferred import inside
`resolve_branding_for_display`, matching that file's existing pattern for
`di_core.containers.container`.

That works, but it treats a symptom. `resources.py` mixes two concerns: 37 call sites want
a **pure string constant**, while the module also imports live models from four apps to
build its counters. Any of those four apps' own `models.py` that later wants a resource key
hits the identical cycle and gets patched the same ad-hoc way.

**→ Phase 6 splits the constants into a leaf module** (`payments/seams/resource_keys.py`,
zero imports) that `resources.py` imports for registration, so consumers needing only a
symbol import the leaf and the cycle becomes structurally impossible.

The reviewer argued for Phase 6 over Phase 4 and the conductor agrees: Phase 6 is where the
risk becomes concrete, since `payments/billing_plans_catalog.py` is reachable from a
data-migration import path (`0007_seed_billing_plans`, `0009_backfill_unlimited_subscriptions`)
and AGENTS.md warns against tying a data migration's import graph to live model modules.
Phase 6 already retargets that file, so the split folds into work it is doing anyway rather
than reopening `resources.py` a third time.

### Phase 5 — Triage the host billing test suite ✅ (pending opus review)

Branch `plan/migrate-billing-engine-to-vinta-django-billing/phase-5`, based on phase-4.
Implementer sonnet. Three commits: `ca95b59e` deletions · `f5c941ca` retargets and splits ·
`9026f455` the `IsBillingOwnerOrAdmin` decision. **13 files changed, +109 / −4737.**

**Suite: 5906 passed, 0 failed** — re-run by the conductor (5m43s, exit 0). The count
reconciles **exactly**: 6283 − 377 removed = 5906. An exact reconciliation is the strongest
evidence available that nothing was lost by accident, since a deleted test cannot fail.

**Process note.** This implementer stalled twice waiting on background `pytest -n auto`
runs, advancing by one file across two resumes and ~315k tokens. The fix was to forbid it
from running the full suite at all — fast scoped runs only, with the conductor running the
suite. It finished promptly after that. Worth remembering: when an agent loops on a
long-running command, remove the command rather than resuming into the same trap.

### The plan's delete list was largely wrong, and that is this phase's real finding

The plan predicted ~15,000 LoC removed; the honest answer is 4,737. The implementer was
given one rule — *if you cannot name a counterpart in the package's suite, do not delete
it* — and applying it reversed most of the list:

| Plan said delete | Why it survived |
|---|---|
| `test_cycle_close_concurrency.py` | The package has **zero** concurrency tests — conductor verified (`grep ThreadPoolExecutor\|threading` over its `tests/` returns nothing). This is a real two-thread `select_for_update` race test against a real database. |
| `test_dunning_schedule.py` | Its docstring states its purpose: proving `@inject` on the Celery tasks resolves a working service. Also the only coverage anywhere for two real production bugs (`stripe.CardError` / `InvalidRequestError` swallowed in the dunning tick). |
| `services/test_payment_services.py` | Two tests drive `payments/seams/audit.py`'s receiver end to end — the audit-trail rollout's one billing write. |
| `services/test_grace_recovery.py` | Asserts `resync_organization_calendars_task.delay`, i.e. `payments/seams/resync.py`. Host seam, not engine. |
| `views/test_payment_webhooks.py` | Signature tampering, replay, idempotent duplicate delivery, "resolves off the payment row not the org's pin". The package's webhook tests cover the generic seam-mounting mechanism only. |
| `test_admin.py` | Drives the package's own admin classes deeper than the package's three admin tests — formset override detection, plan-limit coverage-gap validation. |
| "migration tests superseded by Phase 1's" | **No such tests exist.** The named modules test host data migrations `0009`, `0018`, `0019`, `0021`, unrelated to the table-move migration. The plan asserted a supersession that never happened. |

Eight modules were deleted, each against a named counterpart: the four provider-adapter
modules (diffed test-for-test), `services/test_dunning_service.py` (→ the package's
`test_billing_state_machine.py` + `test_dunning.py`), `test_exceptions.py`, and partial
class removals from `test_provider_registry.py` and `test_over_limit_error.py`.

**The four mixed modules.** `services/test_entitlement_service.py` was split — 14 tests
removed against `tests/test_entitlement_engine.py`, keeping `TestUsageCounters` (the eight
host counters), `TestSeatCountingOnTheAcceptPath`, `TestHasEntitlement` (the package has no
`has_entitlement` tests at all) and the row-lock tests. The other three were kept whole
after inspection: `test_metering_reconciliation.py` and `services/test_metering_service.py`
build every fixture through real `CalendarEventFactory` recurrence rather than the package's
stub source, so splitting would delete the only proof the host's `OccurrenceSource` seam
bounds an infinite series correctly; `services/test_subscription_service.py` carries
`IncompleteBillingPlanError` and retired-resource-key invariants tied to this host.

**`IsBillingOwnerOrAdmin`: kept, ratified as retained-but-unwired** in
`organizations/permissions.py`'s docstring. The argument: both surviving test modules
already call the permission directly and never claim an endpoint is gated by it, so nothing
passes while misrepresenting production. The opus review is checking whether a reader would
actually come away with that understanding.

**Two package gaps found, reported not worked around**: `vinta-django-billing` has no
concurrency test for `CycleCloseService.close_subscription`'s row lock, and no direct test
of `SubscriptionService.retry_payment`'s ordering and idempotency contract — both fully
package-owned code. Candidates for a future release alongside the 409-vs-503 status-map
contradiction and `vinta_billing.jobs` ignoring `SERVICE_CONTAINER`.

### Opus review found a BLOCKER: a counterpart claim that was plausible and false

This is the finding that justifies the plan putting a Tier-4 reviewer on this phase, and it
is worth stating precisely because it is the failure mode the whole phase was designed
around.

`test_dunning_service.py` was deleted on the claim that the package's `tests/test_dunning.py`
plus `tests/test_billing_state_machine.py` cover it. Both files exist — but
`tests/test_dunning.py:42` defines `class FakeSubscriptionService` whose `retry_failed_charge`
is a **no-op recorder**, and that fake is the *only* occurrence of `retry_failed_charge`
anywhere in the package's test suite. Conductor verified by grep. The package has zero real
tests of that method.

Four properties therefore had no enforcement in **either** codebase after the deletion:

- **The money-path guard.** For any provider other than MercadoPago,
  `CollectionNotSupportedError` must re-raise rather than fall back into
  `change_subscription_plan`. `vinta_billing/services/subscription_service.py:709-718`
  documents this across six paragraphs, including that a live Stripe probe proved the
  fallback "collects **$0.00**" with only an INFO log to notice by — and ships no test.
- `NoOutstandingBalanceError` swallowed in a `CELERY_TASK_ACKS_LATE` beat tick.
- Blank `external_id` handling on the beat path (distinct from the user-facing 409, which
  the deleted class docstring explicitly distinguished).
- The MercadoPago ladder ordering.

Plus `enter_grace` never touching `PaymentMethod`: the supposed substitute in
`test_postpaid_enforcement.py` sets `billing_state` **directly** rather than calling
`enter_grace`, so it would stay green if a future package version deactivated the card.

Restored into `payments/tests/services/test_dunning_retry_tolerance.py` (`37d0300b`), with
red-then-green proof — the guard was patched out of the installed package and the test gave
`DID NOT RAISE`, then the patch was reverted under an md5 check. One test was correctly
*not* restored: `test_charge_declined_…` genuinely is covered by the kept
`test_dunning_schedule.py` against the real DI stack.

**How the reviewer caught it, worth reusing:** it grepped the package's *source* for the
guard rather than only its tests, and diffed each deleted file against its counterpart
instead of trusting name matches. A "does a similarly named file exist?" check passes this
straight through — which is exactly what happened.

Six SHOULD-FIX items also applied: two add-on tests with no counterpart restored
(cross-resource leak, and two rows summing); `organizations/authorization.py`'s two
justifications repointed at `vinta_billing.permissions.IsBillingManager`, the class that
actually gates; four present-tense statements in `IsBillingOwnerOrAdmin`'s docstring
converted to past tense with the ratification moved to the top; three orphaned helpers
deleted; seven stale cross-references repointed.

**An unplanned find from the `SITE_DOMAIN` fix.** Adding the assertion that pins the
primary `VINTA_BILLING["SITE_DOMAIN"]` branch went red for an unrelated reason:
`vinta_schedule_api/settings/test.py` overrode the top-level `SITE_DOMAIN` but left
`VINTA_BILLING["SITE_DOMAIN"]` baked from `base`'s unset-env default, so under test settings
the primary branch read a stale value. Fixed with a one-line sync rather than writing an
assertion shaped around the bug. Same hazard class as the pre-flight `URL_NAMESPACE`
finding: wrong only against a live MercadoPago call, invisible to a green suite.

**A procedural finding against the conductor**, and fair: the phase's own named gate is "a
two-column keep/drop table **in the PR body**", and no PR existed when the review ran. The
table goes in the PR at integration, now carrying a corrected row for the dunning module.

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

## Phase 2b — ACTIVATED (was deferred)

The requester chose "fix the package first" at the Phase 1 failure gate on 2026-08-19,
which authorises cross-repo work this skill would otherwise defer. Phase 2b is therefore
running, not deferred.

Repo cloned to `/Users/hugobessa/Workspaces/vinta-django-billing` (public,
`vintasoftware/vinta-django-billing`, `main` @ `e6b2521`, version 0.3.0). Branch
`fix/host-integration-gaps`. Implementer opus — the plan suggests Tier 3 for this lane,
stepped up because all four items are public API design on a released library, and gap 4
in particular has a backward-compatibility trap (every existing registration declares no
`usage_extra` keys, so a strict check would break existing 0.3.0 projects on upgrade).

Scope: the four gaps recorded in the Phase 1 entry. The agent may also conclude gap 2 is
correct as shipped and that the host should adapt instead — that is an acceptable
outcome for that gap alone, with reasoning.

### Outcome — all four gaps fixed, PR open, release deliberately not taken

Commit `ed463c8` on `fix/host-integration-gaps`, pushed. PR:
https://github.com/vintasoftware/vinta-django-billing/pull/7
Suite verified by the conductor: **774 passed / 10 skipped**, **784** under the swapped
organization model, pre-commit clean, `__version__` still `0.3.0`.

| Gap | Resolution |
|---|---|
| 1 — viewsets unmountable | Every service argument defaults to `None` and falls back to `services.container`, resolved inside `__init__`. A DI host passing services by keyword never reaches a factory. `get_payment_provider_resolver()` was missing from the container and is new. |
| 2 — regex vs converter `url_path` | Solved by removing the coupling rather than picking a side: an `@action`'s `url_path` is baked in at import and can only be spelled one way, so *either* choice breaks half of all adopters. The two parameterised webhooks leave the router entirely and are bound with `re_path` in `get_extra_patterns()`. URLs and reverse names byte-identical. |
| 3 — no lazy `usage_extra` | `check_limit(usage_extra_resolver=...)`, mirroring the existing `delta_resolver`: mutually exclusive with `usage_extra`, called at most once, never on the unlimited path. |
| 4 — no place to reject a misrouted key | `ResourceDefinition.usage_extra_keys` plus a new `InapplicableUsageExtraError`, enforced across `check_limit` / `get_current_usage` / `get_usage_breakdown`. |

**The conductor's suggested shape for gap 4 was wrong and the implementer corrected it.**
`None` (undeclared, 0.3.0 behaviour) must stay distinct from `frozenset()` (declared
empty). Collapsing them would make the host's guard inexpressible: the motivating case is
a seat exclusion aimed at some *other* resource, and those resources read no keys at all,
so they have to be declarable as strictly empty. Consequence for Phase 1's resumption —
the host must pass `usage_extra_keys=frozenset()` on all seven resources other than
`organization_members`, or the guard stays off.

**One behaviour change for existing adopters**, called out in `HISTORY.md` and the PR:
`get_routes()` no longer lists `PaymentsViewSet`, so a project mounting `get_routes()`
without `get_extra_patterns()` loses its webhooks. **Phase 2 must mount both.**

### Two extra findings — real, but non-issues for this host

The implementer raised both as possible blockers. The conductor checked each against this
host and neither blocks the migration:

1. **Three throttle scopes are required** (`payment-webhook`, `payment-provider`,
   `billing-write`); DRF raises `ImproperlyConfigured` for a scope with no rate, so
   mounting the package routes without them means a 500 on every webhook and billing
   write. `vinta_schedule_api/settings/base.py:287` already declares all three with rates.
   Documented in the package README for other adopters; nothing to do here.
2. **`url_path=""` does not mean "no segment".** DRF treats it as falsy and substitutes
   the method name, so six actions really serve `/billing/subscription/retrieve_subscription/`,
   `/billing-profile/create_billing_profile/` and so on, not the paths their docstrings and
   OpenAPI summaries claim. The implementer inferred the host's frontend must be calling
   paths that do not exist. **It is not**: the host's own viewsets carry the identical
   `url_path=""`, and the shipped `schema.yml` already lists the suffixed paths. The two
   sides match exactly, so the Phase 2 route swap does not move them. A genuine
   docstring-versus-reality wart in both codebases; not a migration risk and not a client
   change.

**Release is gated on the requester.** `pyproject.toml`'s bumpversion config commits and
tags, and the publish workflow fires on unprefixed tags, so tagging *is* publishing to
PyPI — irreversible and outward-facing. The agent is instructed not to push, tag, bump or
publish. The conductor reviews, then asks before any of it happens.

Once 0.4.0 is out: bump the host pin to `>=0.4,<0.5`, refresh `uv.lock`, then resume
Phase 1 — remove the host REST layer that gap 1 forced it to keep, rebuild the seat-accept
path on the new `usage_extra_resolver`, and re-declare the invitation-exclusion guard
through the registry so the lost behaviour comes back.

## Deferred phases

- **Phase 2b** — cross-repo, and conditional besides ("skipped entirely if no gap
  appears"). If a phase hits a package gap, it stops and reports rather than working
  around it host-side; the fix, the 0.4.0 release and the pin bump happen in
  `vintasoftware/vinta-django-billing`.
