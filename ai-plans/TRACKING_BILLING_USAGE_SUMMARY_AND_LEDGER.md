# Tracking — Billing Usage Summary & Occurrence Ledger

**Plan**: `ai-plans/2026-08-08-BILLING_USAGE_SUMMARY_AND_LEDGER_IMPLEMENTATION_PLAN.md`
**Plan id**: `billing-usage-summary-and-ledger`
**Started**: 2026-08-08
**Last updated**: 2026-08-08

**Feature flag**: none. The plan's **Guiding Decisions** record the reason — this repo has no flag framework and is pre-production. No flag-removal phase exists.

## Run options

```yaml
run_options:
  pause_between_phases: false
  generate_inline_comments: false
  full_test_suite: true          # overrides config default; Phase 1 touches enforcement counters app-wide
  use_worktree: true
  worktree_path: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-billing-usage-summary-and-ledger
  worktree_branch: plan-billing-usage-summary-and-ledger
  worktree_summary: .vinta-ai-workflows/worktrees/plan-billing-usage-summary-and-ledger.yaml
  sandbox_tier: enforced
  commit_strategy_resolved: stacked-branches

agent_models:                    # from .vinta-ai-workflows.yaml
  reviewer: 3                    # phases 1 and 2 override to 4; phase 5 to 3
  fixer: 2
  worktree_prep: 1
  integrate: 1
```

`BASE_BRANCH` = `plan-billing-usage-summary-and-ledger`. Phase 0 branches off it; every later phase branches off the previous phase's branch.

## Completed phases

### Phase 0 — Add the billing period statement models ✅

- **Branch**: `plan/billing-usage-summary-and-ledger/phase-0` (base: `main`) · **PR**: [#241](https://github.com/vintasoftware/vinta-schedule-api/pull/241)
- **Models**: implementer Sonnet (Tier 2, stepped up from Haiku because the phase touches ~6 files) · reviewer Sonnet (Tier 3) · no fixer needed
- **Commits**: `8a9f613` (models), `76f31b1` (plan touch-list correction)

Added `BillingPeriodSummary` and `BillingPeriodResourceUsage` to `payments/models.py`, plus `BillingPeriodSummaryQuerySet.for_organizations`, `BillingPeriodSummaryManager`, migration `0016_billing_period_summary`, read-only admin for both models, and `payments/tests/test_billing_period_summary_model.py` (10 tests). Pure scaffolding — nothing reads or writes the tables yet.

Verification: full repo suite 5168 passed; `makemigrations --check` clean; migration applies and reverses cleanly; `ruff` + `check --deploy` clean; `mypy` clean on all touched files (379 pre-existing errors elsewhere, unrelated, consistent with AGENTS.md noting mypy is not in CI).

Review returned no BLOCKER and no SHOULD-FIX. One NIT on branch naming, which is the plan-runner's stacked-branch scheme rather than an implementer choice — matches the four pre-existing worktrees, so no action taken.

Two things worth carrying forward:
- The implementer caught a real inconsistency in the plan: the **Touch List** still listed a `payments/factories.py` that **Data Model Changes** and the Phase 0 **Changes** list had already ruled out. Corrected in commit `76f31b1`. This app builds test objects with `model_bakery`.
- The migration-reversibility test drives `MigrationExecutor` directly, a new pattern for this repo (no `django_test_migrations` installed). The reviewer specifically re-ran the `payments/` suite under `pytest -n auto` to confirm it does not corrupt sibling xdist workers' databases. If a later phase adds migration tests, follow that file's `try/finally` restore-to-head structure.

### Phase 1 — Per-organization breakdown in the usage counters ✅

- **Branch**: `plan/billing-usage-summary-and-ledger/phase-1` (base: `phase-0`)
- **Models**: implementer Sonnet (Tier 3) · reviewer Opus (**Tier 4** override) · fixer Sonnet then Haiku (Tier 2)
- **Commits**: `0230252` (widen counters), `8a70508` (close BLOCKERs), `5bccddd` (close gate NIT)

`UsageCounter` widened from `-> int` to `-> dict[int, int]`. All eight counters group by `organization_id`; `_count_usage` returns `sum(breakdown.values())`, so the total is structurally the sum of the parts rather than a second query. New `EntitlementService.get_usage_breakdown()`. New helpers `_group_counts_by_organization` and `_merge_breakdowns`.

Verification: full suite **5172 passed, 0 failed** (5168 baseline + 4 new tests). All nine pre-existing enforcement test files show a **zero-line diff** — the phase's acceptance criterion, verified independently by the conductor, not taken on the agents' word.

**The Tier 4 review override paid for itself. Two BLOCKERs, both invisible to a normal reading:**

1. **The regression gate was a tautology.** It asserted `sum(breakdown.values()) == get_current_usage()` — but `get_current_usage` is now *defined as* `sum(breakdown.values())`, so it compared a value to itself and could never fail. A bug dropping every group but the first would have passed green. Replaced with breakdowns pinned to hand-written literals, and the second assertion is `total == sum(expected.values())` — deliberately **not** `sum(breakdown.values())`, which would have reintroduced the tautology in a new shape. Proven by breaking the grouping helper and watching it fail.
2. **Three resources had no test anywhere in the repo that could catch a grouping bug.** `bundle_calendars`, `webhook_subscriptions`, `public_api_system_users` each had exactly one row in one organization. The fixture now gives all eight resources ≥2 rows in one organization across ≥2 organizations.

Also fixed: a defensive `.order_by()` before `.values().annotate()`. Not a live bug — no source model declares `Meta.ordering` today, verified across all eight chains and their manager/queryset methods — but adding one later is an ordinary-looking change no reviewer would connect to billing, and it would silently mis-bill every customer with no failing test.

Two things a later phase should not undo:
- `Count("pk")` on `OrganizationMembership` resolves to `COUNT(user_id)` because its pk is a `SafeCompositePrimaryKey`, and adding `distinct=True` raises `ValueError` on composite keys. There is a comment saying so; do not "defensively" add `distinct`.
- The pinned-literal test is deliberately verbose. Its docstring explains why collapsing it back to a breakdown-vs-total comparison would be vacuous.

Carried into the plan's **Risk & Rollout Notes**: every usage count is now a hash aggregate rather than `COUNT(*)`, since `GROUP BY organization_id` cannot be satisfied from `metered_occ_sub_period_idx`. Bounded by pool size; noted rather than fixed.

### Phase 2 — Persist the statement at cycle close ✅

- **Branch**: `plan/billing-usage-summary-and-ledger/phase-2` (base: `phase-1`)
- **Models**: implementer Sonnet (Tier 3) · reviewer Opus (**Tier 4** override, three rounds) · fixer Sonnet
- **Commits**: `8e1250b`, `c1e73d5`, `6cb1600`, `1665ec5`, `0d351e2`

`CycleCloseService._persist_statement` writes one `BillingPeriodSummary` plus one `BillingPeriodResourceUsage` per `LimitedResource` member, ordered `reconcile_period` → `_charge_overage` → **persist** → `_roll_period`. Idempotent via `get_or_create` on `(subscription, billing_period_start)`; failure-isolated in a savepoint so a statement bug degrades history, never revenue. Full suite **5182 passed, 0 failed**.

**Three defects here would each have shipped silently. Two were caught only by the Tier 4 override.**

1. **The statement contradicted the invoice it explains.** The grouping applied `.for_organizations(pool)` computed at *close* time, while `_charge_overage` and `reconcile_period` filter by period only. A child promoted to its own billing root mid-period leaves the pool, so its already-billed rows vanish from the statement while still appearing on the invoice. Reproduced as a red gate against the pre-fix code: `assert 2 == 3`. The fix is that the statement must read byte-identically to what the charge summed — never the reverse.
2. **A dangling FK could have rolled back a completed charge.** `payment_id=payment.pk` assigns the raw column, and Django creates FK constraints on Postgres as `DEFERRABLE INITIALLY DEFERRED` — so the check lands at *outer commit*, after the savepoint closed and after the period rolled, taking the whole close with it including a charge the provider already took. Now assigns the instance. Note the guarantee is still not absolute: the descriptor validates type, not row existence; that case is unreachable today only because `_charge_overage` creates the row in the same transaction.
3. **The implementer found a real defect unprompted**: `get_usage_breakdown` anchors `event_occurrences` on `timezone.now()`, which at close time is never the closing period, so every statement would have recorded zero metered occurrences. Confirmed real by the reviewer.

Also fixed across review rounds: an `ERROR` log that told on-call the statement was recoverable by re-running close (it is not — the period has already rolled and the sweep guard never revisits it); a `not created` early return that left summaries with zero resource rows unrepairable; `overage_unit_price` picking an arbitrary lowest-pk price when a period has mixed stamped prices (now writes `None` plus a warning); `by_organization` int keys that changed type across a JSON round-trip; `total=0` where the contract requires `null`; and a test claiming to exercise a database `IntegrityError` that never issued SQL (now trips a real `CHECK (total >= 0)`).

Two false invariants were corrected in **docstrings** rather than code, because no stamped source exists for them:
- `limit_value` is as-of-close for **all eight** resources, including `event_occurrences`. Only `overage_unit_price` is stamped. `MeteredOccurrence` records `is_within_allowance` and `unit_price`, never the ceiling — a stamped ceiling would be a new column.
- The savepoint swallow is not unconditional; if `savepoint_rollback()` itself fails, Django marks the outer atomic for rollback anyway.

Known residual, accepted: `cycle_close_service` still imports module-private `_group_counts_by_organization` and `USAGE_COUNTERS` from `entitlement_service`. The two service methods it called privately were promoted to public pre-resolved entry points (`effective_limit_for_subscription`, `usage_breakdown_for_root`), which also let the pool resolve once per period instead of once per resource under the `SELECT ... FOR UPDATE`.

### Phase 3 — Enrich the current-usage summary ✅

- **Branch**: `plan/billing-usage-summary-and-ledger/phase-3` (base: `phase-2`)
- **Models**: implementer Sonnet (Tier 3) · reviewer Sonnet (Tier 3 default) · fixer Sonnet
- **Commits**: `2e2b71b`, `28b0c6e`, `89dc631`

`GET /billing/usage/` enriched additively with `billing_root_organization_id`, `plan`, `billing_period`, `estimated_overage_total`, and per-row `included_in_plan` / `add_on_quantity` / `by_organization`. Full suite **5194 passed, 0 failed**. No BLOCKERs in review.

**Query count went 63 → 21.** The endpoint previously resolved the billing root sixteen times and walked the subtree eight times per request. The first pass got it to 37; the review then found that 16 of those remaining 37 were duplicate `SubscriptionPlanLimit` / add-on lookups redoing per resource what the view had just batched two lines earlier. The query-count test now pins a ceiling, not just two SQL substrings, so the number cannot silently drift back.

Worth carrying forward:

- **Do not put a docstring on the `retrieve_usage` action.** drf-spectacular uses an action's docstring as the OpenAPI `description` in preference to the class's, which would silently drop the RESTRICTED / read-never-blocks documentation from the public schema. Implementation notes there stay regular comments. The implementer hit this and caught it before committing.
- **`estimated_overage_total` applies `.for_organizations(pool)` and Phase 2's statement deliberately does not.** These look inconsistent and are not: this is a live read scoped to the asking organization's current pool, while the closed-period statement must reproduce exactly what the charge summed, pool filter and all. Do not "align" them.
- **The ceiling arithmetic nearly got a second implementation.** The N+1 fix introduced `effective_limit_from_resolved`, which reproduced `_effective_limit_for_subscription`'s three fail-open branches line for line — the same hazard the plan's **Guiding Decisions** rejected for the usage counters in Phase 1. Refactored so `_effective_limit_for_subscription` delegates to it: one arithmetic, two ways to supply inputs. The lazy skip of the add-on aggregate on an unlimited limit is preserved and now has its own query-capture test.
- The `event_occurrences` counter still derives its own period from `timezone.now()`, so it and the view's period can straddle a boundary in a rare race. Out of scope to fix (Phase 1 counter signature); the comment says so honestly rather than claiming the race is closed.

## Current phase

**Phase 4 — Closed-period statement endpoints** (implementer Tier 2, no review override).

Branch `plan/billing-usage-summary-and-ledger/phase-4`, based on `phase-3`. Bundled per the chosen granularity: list + detail share a queryset, permission, and serializer tree.

## Remaining phases

| Phase | Title | Implementer | Review override |
|---|---|---|---|
| 3 | Enrich the current-usage summary | Tier 3 | — |
| 4 | Closed-period statement endpoints | Tier 2 | — |
| 5 | The occurrence ledger endpoint | Tier 3 | reviewer Tier 3 |

## Deferred phases

_None._ The plan has no cross-repo phases and no flag-removal phase.

## Notes

- The plan `.md` is untracked in the main checkout and was copied into the worktree. **Phase 0 commits it** alongside its own changes, so it lands with the first PR.
- Phase 4 is bundled (list + detail endpoints) per the plan's chosen granularity; that is intentional, not a phase that should be split.
- Phase 4's endpoints return empty lists until Phase 2 has run at least one cycle close in a real environment. That is expected and is documented in the plan's **Risk & Rollout Notes** as forward-only behavior, not a defect.
