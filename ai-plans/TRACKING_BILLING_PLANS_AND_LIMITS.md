# Tracking — Billing Plans and Limits

- **Feature**: Billing Plans and Limits
- **Plan**: @ai-plans/2026-07-18-BILLING_PLANS_AND_LIMITS_IMPLEMENTATION_PLAN.md
- **Spec**: @ai-plans/2026-07-18-BILLING_PLANS_AND_LIMITS_SPEC.md
- **Plan id**: `BILLING_PLANS_AND_LIMITS` (kebab: `billing-plans-and-limits`)
- **Started**: 2026-07-18
- **Last updated**: 2026-07-19

## Feature flag

**None.** The plan deliberately declares no feature flag — see the "No feature flag" row in the plan's **Guiding Decisions**. The rollout switch is the plan catalog itself: every organization is seeded onto an `unlimited` plan whose `PlanLimit.limit_value` is NULL, so enforcement code runs but cannot block. Rollback for any organization is a `change_plan` back to `unlimited`, no deploy.

Consequence: there is no flag-removal phase. Instead **every enforcement phase carries a test asserting an `unlimited` organization sees unchanged behavior** — that test set is the equivalent of a flag-off suite.

## Run options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config default |
| `generate_inline_comments` | `true` | **user override** (config default `false`) |
| `full_test_suite` | `true` | **user override** (config default `false`) |
| `use_worktree` | `true` | config default |
| `commit_strategy_resolved` | `stacked-branches` | user answer (`commit_strategy: ask`) |
| `worktree_path` | `.claude/worktrees/plan-billing-plans-and-limits` | prepare-worktree |
| `worktree_branch` | `plan-billing-plans-and-limits` | prepare-worktree |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-billing-plans-and-limits.yaml` | prepare-worktree |
| `sandbox_tier` | `enforced` | re-probed on 2026-07-19 resume (`sandbox-exec` present) |

`WORKROOT` = `.claude/worktrees/plan-billing-plans-and-limits`

## Agent models

Implementer per-phase from the plan's `**Suggested AI model**:` line. Others from `.vinta-ai-workflows.yaml` `agent_models` (reviewer 3, fixer 2, worktree_prep 1, integrate 1), with per-phase `**Review models**:` overrides taking precedence.

## Environment notes

- Phases 1, 2, 2b, 3, 4 are **merged to `origin/main`** (PRs #189–#193). `origin/main` = `bd58606`.
- Phases 3 and 4 targeted `main` directly rather than stacking, since their predecessors had already merged. From Phase 5 on, each phase branches off `origin/main` for the same reason — the stack has collapsed into `main`.
- **Local `main` in the primary checkout is stale** (`591aed9`, behind `origin/main`). Harmless for this run — all work happens in the worktree — but worth a `git pull` before any local work there.
- The two forked docker volumes (`..._dbdata`, `..._floci_data`) exist and are in use.
- **Baselines as of `origin/main`**: full suite **3969 passed**; `mypy` **308 errors / 58 files**; `ruff` clean; `makemigrations --check` clean.

## Completed phases

### Phase 1 — Move billing ownership to the organization ✅

- **Status**: merged · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/189
- **Models**: implementer Tier 3, reviewer Tier 4, fixer Tier 2→3 (stepped up, >3 files)

`BillingProfile` moved from a `User` primary key to an `Organization` one — the spec's named one-way door, landed before any money can flow. `Subscription` gained `organization`, `plan`, `billing_state`, `billing_interval`, period, grace, `plan_external_id`, `payment_provider`. The dead seam was repaired: the phantom `membership` annotation and the `AttributeError`-raising `Subscription.plan` property are gone.

Three BLOCKERs, all fixed: a null `payer.email` that MercadoPago hard-400s (every payment path would have failed live while the suite stayed green); an unguarded reverse 1:1 that would 500-loop the webhook once Phase 4 gave every org a subscription — a Phase 1 change that only detonates on Phase 4 data; and `request.organization is None` for a memberless user → `IntegrityError` → 500.

**Decisions**: `contact_*` fields added to `BillingProfile` (user decision, plan amended); write actions gated on `IsOrganizationAdmin`; minimal `BillingPlan` pulled forward from Phase 3; `Subscription` carries both `status` (provider-reported) and `billing_state` (internal).

**Carried forward — needs human action:**
- ⚠️ **Client-breaking**: billing-profile `id` changes source from `user_id` to `organization_id`. Same name and type, so **no schema diff** — a client that persisted the old id silently reads a different entity. Needs `handoff-to-client`.
- ⚠️ **Verify in each environment** that `payments_billingprofile` and `payments_subscription` were empty before this deployed. Nobody queried a live database.
- `MissingBillingProfileError`'s default message still says "User does not have a billing profile" — cosmetic.

### Phase 2 — Authenticate provider webhooks and make them idempotent ✅

- **Status**: merged · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/190
- **Models**: implementer Tier 3, reviewer Tier 4

Both webhook endpoints had no authentication, no permissions, and no signature verification, while later phases make billing state depend on them. Added real MercadoPago `x-signature` HMAC verification (fails closed on empty secret), a `ProviderWebhookEvent` idempotency ledger unique on `(provider, route, external_event_id)`, a provider registry replacing hardcoded factories, and filled the five empty status-mapping dicts that were writing raw provider strings into `choices`-constrained columns.

**Security invariant anyone extending this must preserve**: MercadoPago's signature covers only `data.id`, `x-request-id`, and `ts` — **not the body**. That is tolerable only because status is always re-fetched from the provider API by id and never trusted from the body. If a future change reads a decision out of the webhook body, an attacker controls it.

Two BLOCKERs: the ledger key was derived from the **unsigned top-level `id`**, so an attacker could hold `data.id` fixed (signature still valid) and mutate the top-level id per replay — the ledger never deduped and the handler re-ran unbounded. Signature verification was correct and the ledger was correct; their *composition* was broken. And no timestamp tolerance, so a captured triple verified forever — the original tests signed with a `ts` 2.5 years stale and passed.

- **Baseline**: 3756 passed. Regression test for BLOCKER 1 was **proven** by reintroducing the vulnerability and watching it fail.

### Phase 2b — Stripe adapter behind the provider abstraction ✅

- **Status**: merged · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/191
- **Models**: implementer Tier 3

Nothing routes any organization to Stripe (that is Phase 9). This phase validated the abstraction against a real second provider — and the abstraction was wrong in ways only a second provider could expose. `BasePaymentAdapter.receive_update` called the adapter's own id hook and then **overwrote the result** with a hardcoded `payload["data"]["id"]`; the subscription base never called its hook at all. For any provider whose ids are not at `data.id` — exactly Stripe — the old code 500'd the webhook.

Interface changes Stripe forced: `Plan.billing_interval` required, `refund() -> RefundResult`, `check_refund_status(refund)`, `verifies_full_body: bool`. The third **fixed a real pre-existing bug** — MercadoPago's implementation looked up a *refund* id through the *payment* endpoint.

Review found the adapter was written against an obsolete Stripe API (`Invoice.subscription`, `Invoice.payment_intent`, `PaymentIntent.charges` are all gone in the pinned `2026-06-24.dahlia`). Every Stripe subscription webhook would have bailed and still been `mark_processed`-ed, permanently burning the delivery. **The tests passed the whole time** because every fixture was hand-written to the same obsolete shape the implementation assumed.

**Carried forward:**
- ⚠️ **No Stripe credentials exist in this environment.** Fixtures are derived from SDK type annotations, not captured from live events. This phase establishes the interface generalizes; it does **not** establish the Stripe adapter works. First real test is Phase 9's sandbox run.
- Specifically unconfirmed: whether `Invoice.payments` is populated by default or needs `expand=["latest_invoice.payments"]`.

### Phase 3 — Plan catalog, limits, and entitlements ✅

- **Status**: merged · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/192
- **Models**: implementer Tier 3

The catalog every later phase reads. Added `PlanLimit` (keyed `(plan, resource_key)`, carrying `limit_value` / `kind` / `overage_unit_price`) and `PlanEntitlement`, plus `LimitedResource` / `LimitKind` / `Entitlement` in `payments/billing_constants.py`, and a seed migration creating `unlimited` and `free`. Deleted `OrganizationTier`, `SubscriptionPlan`, `Organization.tier`, and the dead `organization_subscription_plan_factory.py`.

**The seed migration is this feature's kill switch** — there is no feature flag. `unlimited` carries a `PlanLimit` row for every `LimitedResource` with `limit_value = NULL` and `is_default_for_new_organizations = True`, so enforcement code from Phase 6 on runs but cannot block.

Review finding: seeding used `get_or_create(defaults=...)`, which applies `defaults` **only on creation**. A pre-existing `unlimited` plan with `is_default_for_new_organizations=False` would silently keep it — new orgs would not land on unlimited and enforcement would be live for them, with nothing raising. For a migration whose whole job is to be the safety net, filling gaps is not enough; it has to converge. Now `update_or_create` throughout.

- `mypy` went **down** to 308/58 from 322/59 (deleted dead code), with no `type: ignore` or `noqa` added anywhere in the diff.

**Carried forward:**
- ⚠️ **`free` plan values are placeholders** (5 members, 3 resource calendars, 2 groups, 1 bundle calendar, 5 availability windows, 1 webhook subscription, 0 partner-API system users, 50 event occurrences at $0.05 overage). Product supplies real numbers before Phase 14 — which is deferred for exactly this reason.
- `billing_day` is derived from `current_period_start.day`, so a cycle starting on the 29th–31st yields a value most providers reject for monthly recurrence. Clamping is a **Phase 9** decision.

### Phase 4 — Place every organization on a plan ✅

- **Status**: merged · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/193
- **Models**: implementer Tier 3, reviewer Tier 4

Establishes the core invariant: **every organization always has exactly one active plan — there is no plan-less state.** Every billing-root organization gets a `Subscription`; reseller children never hold one and pool against their root. Existing orgs backfilled onto `unlimited`, never `free`.

Shipped `SubscriptionPlanLimit` / `SubscriptionEntitlement` (per-subscription copies written on creation and plan change; catalog edits never propagate; `is_overridden=True` rows survive plan changes), `SubscriptionService.create_subscription_for_organization` / `change_plan`, `is_billing_root` / `billing_root_filter` / `resolve_billing_root` in `payments/services/subscription_service.py`, `OrganizationMembership.is_billing_owner` (field only), and the backfill migration.

Two review rounds, six BLOCKERs. Round 1's four shared one root cause: **"is this a billing root?" was defined twice and the definitions disagreed** — the backfill said *parent is null*, `resolve_billing_root` said *parent is null OR `can_invite_organizations`*. A nested reseller fell in the gap and its whole subtree resolved to a root with no subscription. Also: Django admin was an unhooked fourth creation path; the cycle guard returned an arbitrary node (and its test asserted `result.pk in (a, b)`, passing while the invariant was broken); and this phase broke Phase 3's rollback via `on_delete=PROTECT`.

Round 2 found that **fixing BLOCKER 1 built the trigger for a gap BLOCKER 4's fix did not cover** — `is_billing_root` now includes `can_invite_organizations`, a flag toggleable on an existing org through admin, but `save_model` returned early on `change=True`. Flipping it on a subscription-less child instantly produced a billing root with no subscription. Neither review could have caught this alone; it existed only in the interaction between two fixes. Also: the `is_overridden` clear was provably one-way (unchecking was a silent no-op), which turns every temporary support grant permanent.

- Migration chain verified from a **fresh schema**, not an incrementally-migrated one.

**Carried forward:**
- `payments/services/payment_service.py` `create_subscription` is an unreconciled second creation path — its unconditional `objects.create` would raise `IntegrityError` on the OneToOne now that every root has a `Subscription`. Tests-only today, so latent. Marked in-code for **Phase 9**.
- Migration `0009` imports `billing_root_filter` from a live service module rather than freezing it. A future rename of `can_invite_organizations` or `parent` would retroactively change a historical migration's behavior. Noted in AGENTS.md.

## Current phase

**Phase 5 — Effective limits and usage counting** (implementer Tier 4, reviewer Tier 4, fixer Tier 3)

Base: `origin/main` (`bd58606`) · Branch: `plan/billing-plans-and-limits/phase-5`

The engine every enforcement phase calls. Note for the implementer: Phase 5's changes list in the plan says to create `resolve_billing_root`, but **Phase 4 already shipped it** (along with `is_billing_root` and `billing_root_filter`) in `payments/services/subscription_service.py`, with `BillingRootCycleError` in `payments/exceptions.py`. Reuse them; do not write a second definition — a duplicated billing-root predicate is exactly the Phase 4 BLOCKER that cost two review rounds.

**Review round 1 applied.** Three BLOCKERs, all with a test confirmed failing before the fix:

1. **`OverLimitError` committed the request transaction.** `common/exception_handlers.py` returned a `Response` without `rest_framework.views.set_rollback()`, so under `ATOMIC_REQUESTS` the swallowed exception *committed* everything a guarded service wrote before the check — the invitation row, the `is_active` flip, the audit entries Phase 6a writes ahead of its guard. Proven through a real request, not a direct handler call.
2. **`_count_availability_windows` counted recurrence-derived rows.** Editing one occurrence of a recurring window *inserts* an `AvailableTime` row (`create_recurring_available_time_exception` → `create_available_time`); so does a series split. An org that made 3 recurring windows and edited 3 occurrences read as 6 — a lockout *below* real usage, which goal 6 forbids.
3. **Plan downgrade granted an infinite ceiling.** `_sync_limits` deleted rows the new plan omits; `get_effective_limit` reads an absent row as *unlimited*. Each correct alone; composed, downgrading onto a plan that omits a resource uncapped it. Same shape as Phase 2's BLOCKER.

**Decision on BLOCKER 3** (deviates slightly from the reviewer's suggestion — recorded because it changed a shipped test): the catalog never expresses "not included" by omission, it uses an explicit `limit_value=0` row (the seeded `free` plan does exactly that for `public_api_system_users`). Omission therefore means *incomplete plan*, which is the same data-gap condition the fail-open exists for. So `_prune_stale_limits` now deletes only **retired** resource keys — ones no longer in `LimitedResource` — and retains + logs a `LimitedResource` member the new plan omits. `test_downgrade_removes_stale_non_overridden_rows_absent_from_new_plan` was split into three tests reflecting this; the entitlements half is unchanged, since entitlements fail *closed* and deleting there really is a revocation.

**Carried forward:**
- ⚠️ **Residual gap in the availability counter.** When the edited occurrence is the *first* one, `recurrence_manager` truncates the master and creates a replacement series row with **no link back to it** — indistinguishable from a genuinely new window in the current schema. That case still over-counts by one. Closing it needs a column, not a filter. Documented on `AvailableTimeQuerySet.only_user_authored`.
- The add-on aggregate has no period/expiry filter, so a one-time add-on raises the ceiling forever. Owned by the add-on **purchase** phase (9), which is what introduces one-time purchases. Marked in-code.
- `SystemUser.organization` is nullable, so an org-less system user is invisible to that counter and entirely unmetered. Pinned by a test so whoever makes the column non-nullable revisits it deliberately.
- `LimitCheckResult.current_usage` is now `int | None`; it is `None` on the unlimited path, where usage is deliberately **not counted**. Phase 6a's "no change in query count for an unlimited org" test depends on this.
- `check_limit(..., exclude_invitation_id=...)` exists for the accept path and **Phase 6a must pass it** — accepting is net zero on seats, and without it an org can never fill its last seat.
- `UsageSnapshot` remains deferred; rationale recorded in `payments/services/billing_dataclasses.py`.

**Gates after the fixes**: suite **4039 passed** (was 4016); `mypy` **305 errors / 57 files** (under the 308/58 cap); `ruff check` + `format --check` clean; `makemigrations --check` clean (**no new migration** — the new managers are not `use_in_migrations`); `check --deploy` unchanged at the same 5 pre-existing dev-settings warnings.

## Remaining phases

| Phase | Title | Impl | Reviewer | Fixer |
|---|---|---|---|---|
| 6a | Enforce pre-paid limits: seats and invitations | 3 | — | — |
| 6b | Enforce pre-paid limits: calendars, groups, bundles, availability | 3 | 4 | — |
| 6c | Enforce pre-paid limits: webhook subscriptions and API system users | 2 | — | — |
| 7 | Meter event occurrences | 4 | 4 | — |
| 8 | Enforce the post-paid allowance | 3 | — | — |
| 9 | Upgrade, add-on purchase, and proration | 3 | 4 | — |
| 10 | Grace, dunning, and the restricted transition | 3 | — | — |
| 11 | Restricted enforcement and sync pause | 3 | 4 | — |
| 12 | Usage API and approaching-limit warnings | 2 | — | — |
| 13 | Cycle close, overage charge, and reconciliation | 4 | 4 | 3 |

## Deferred phases

**Phase 14 — Roll organizations onto real plans.** Deferred by user decision at run start.

Gated on signal this run cannot produce:
1. Phases 1–13 merged.
2. Phase 13 reconciliation running clean against the Stripe sandbox for at least one simulated cycle.
3. **The real plan limit values and paid tier numbers from product** — the plan's **Open Questions** item 4. Phase 3 seeds only `unlimited` and a placeholder `free`.

Executing it without (3) would seed invented tiers and migrate organizations onto limits nobody agreed to — the exact outcome spec objective 4 sets at zero.

No cross-repo phases in this plan. No flag-removal phase (no flag declared).
