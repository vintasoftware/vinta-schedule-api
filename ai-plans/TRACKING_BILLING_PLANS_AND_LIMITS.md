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

### Phase 5 — Effective limits and usage counting ✅

- **Status**: reviewed clean (3 review rounds), PR open
- **Models**: implementer Tier 4; reviewer Tier 4 on all three rounds; fixer Tier 3→4 on rounds 1–2 (stepped up: >8 files plus a design decision), Tier 3 on round 3
- **Branch**: `plan/billing-plans-and-limits/phase-5` · **Base**: `origin/main` (`bd58606`)
- **Commits**: `8c42486` tracking, `bd62f64` implementation, `65689f7` round 1, `8e70340` round 2, `6159122` round 3

The engine every enforcement phase calls. Phase 5's changes list in the plan says to create `resolve_billing_root`, but **Phase 4 already shipped it** (along with `is_billing_root` and `billing_root_filter`) in `payments/services/subscription_service.py`, with `BillingRootCycleError` in `payments/exceptions.py`. The implementation reuses them rather than defining a second predicate — a duplicated billing-root predicate is exactly the Phase 4 BLOCKER that cost two review rounds.

**Review round 1 applied.** Three BLOCKERs, all with a test confirmed failing before the fix:

1. **`OverLimitError` committed the request transaction.** `common/exception_handlers.py` returned a `Response` without `rest_framework.views.set_rollback()`, so under `ATOMIC_REQUESTS` the swallowed exception *committed* everything a guarded service wrote before the check — the invitation row, the `is_active` flip, the audit entries Phase 6a writes ahead of its guard. Proven through a real request, not a direct handler call.
2. **`_count_availability_windows` counted recurrence-derived rows.** Editing one occurrence of a recurring window *inserts* an `AvailableTime` row (`create_recurring_available_time_exception` → `create_available_time`); so does a series split. An org that made 3 recurring windows and edited 3 occurrences read as 6 — a lockout *below* real usage, which goal 6 forbids.
3. **Plan downgrade granted an infinite ceiling.** `_sync_limits` deleted rows the new plan omits; `get_effective_limit` reads an absent row as *unlimited*. Each correct alone; composed, downgrading onto a plan that omits a resource uncapped it. Same shape as Phase 2's BLOCKER.

**Decision on BLOCKER 3** (round 2 — the round-1 fix did *not* close it, and this again changed shipped tests): retaining the stale row is only safe when that row is finite, and the dominant real state is the opposite. Every organization is on `unlimited`, whose rows are all `limit_value=None`, and `None` reads as unlimited — so the retained row reproduced the original infinite ceiling byte for byte. The invariant is now **in code**, not only in a seed-data test: `subscription_service.assert_plan_is_complete` refuses any plan missing a `LimitedResource` row on both paths that place a subscription on a plan (`change_plan`, `create_subscription_for_organization`), raising `IncompleteBillingPlanError` before any write; `BillingPlan.clean` and `PlanLimitInlineFormSet` raise the same condition at authoring time, so a support admin sees it in the admin instead of an end user seeing it mid-request. Materializing the gap as `limit_value=0` was rejected: it blocks an organization on a resource nobody agreed to restrict (goal 6). With completeness guaranteed, `_prune_stale_limits` deletes only **retired** keys, which nothing can consult. Tests changed: the fixture and every `TestChangePlan` plan now build *complete* plans via `make_complete_plan`; the round-1 `test_downgrade_does_not_turn_an_omitted_limit_into_an_unlimited_ceiling` was replaced by a NULL-row case (the one round 1 missed), a finite-row case, a creation-path case, and admin/model `clean` cases.

**Carried forward:**
- ⚠️ **Residual gap in the availability counter.** Editing **or cancelling** the *first* occurrence of a series truncates the master and creates a replacement series row with **no link back to it** — the branch never reads `is_cancelled`, so both operations behave identically — and it **compounds**: each subsequent first-occurrence edit or cancel on the resulting series adds another unlinked row. The over-count is once per operation and **unbounded**, not "by one". Closing it needs a column, not a filter; size the schema change accordingly. Documented on `AvailableTimeQuerySet.only_user_authored`.
- The add-on aggregate has no period/expiry filter, so a one-time add-on raises the ceiling forever. Owned by the add-on **purchase** phase (9), which is what introduces one-time purchases. Marked in-code.
- `SystemUser.organization` is nullable, so an org-less system user is invisible to that counter and entirely unmetered. Pinned by a test so whoever makes the column non-nullable revisits it deliberately.
- `LimitCheckResult.current_usage` is now `int | None`; it is `None` on the unlimited path, where usage is deliberately **not counted**. Phase 6a's "no change in query count for an unlimited org" test depends on this.
- **Phase 6a's accept path must call `EntitlementService.check_seat_limit_for_invitation_accept(invitation)`**, not `check_limit`. Accepting is net zero on seats; without the exclusion an org can never fill its last seat. It is a named entry point precisely so an omission is a missing *call* rather than a missing kwarg — and `exclude_invitation_id` now raises (`InapplicableInvitationExclusionError`) if passed for any resource but `organization_members`, where no counter reads it.
- `UsageSnapshot` remains deferred; rationale recorded in `payments/services/billing_dataclasses.py`.

**Review round 3** found **no BLOCKERs**. It verified the round-2 guard fires before `transaction.atomic` opens, that all three live creation paths (`organizations/services.py:105`, `organizations/admin.py:159`, `public_api/mutations.py:920`) route through the guarded method, that `skip_limit_coverage_validation` is set in exactly one place and `BaseModel` never calls `full_clean` on save (so the admin opt-out cannot leak), and that `IncompleteBillingPlanError` inherits `BillingError` rather than `ValueError` — so it cannot be flattened into a validation message or misrendered as the over-limit contract. Two SHOULD-FIXes applied:

1. **An incomplete plan could not be retired through the admin.** The coverage check ran on every POST, so flipping `is_active=False` on a broken plan was blocked until every missing row was added — with `extra = 0`, N manual clicks during an incident. The check is now skipped for a saved plan being deactivated, and `PlanLimitInline.get_extra` pre-renders one blank row per gap. A test proves the exemption is one-directional: *activating* an incomplete plan is still rejected.
2. **Coverage erosion introduced by round 2's own fixture change.** `make_complete_plan` carries `organization_members` at `limit_value=0`, so the overridden-row test's `.exists()` assertion passed whether or not the override survived. It now captures `limit_value` before the change and asserts it unchanged — confirmed failing when `is_overridden` handling is neutralised.

One NIT was **declined with evidence**: moving `from di_core.containers import container` to module scope binds to `None`, because the container is only wired in `DICoreConfig.ready()` after test collection imports the module. Two tests broke; the repo's root `conftest.py:136-141` uses the same deferred pattern. Kept inside the fixture with the reason written down, per AGENTS.md's carve-out.

**Final gates** (re-run independently by the orchestrator, not relayed): suite **4054 passed** (round 2: 4051; round 1: 4039; original: 4016; `origin/main` baseline 3969); `mypy` **305 errors / 57 files** — *below* the 308/58 baseline, with **zero `type: ignore` / `noqa` added across all four commits**; `ruff check` + `format --check` clean (479 files); `makemigrations --check` clean — **no migration in the whole phase after `0010`**; `check --deploy` unchanged at 5 pre-existing dev-settings warnings; main checkout clean; no AI co-author trailers.

### Phase 6a — Enforce pre-paid limits: seats and invitations ✅

- **Status**: reviewed clean (2 review rounds), PR open
- **Models**: implementer Tier 3; reviewer Tier 4 both rounds; fixer Tier 2→3 (stepped up for scope)
- **Branch**: `plan/billing-plans-and-limits/phase-6a` · **Base**: `main`
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/195
- **Commits**: `463015b` implementation, `e0360c0` round 1, `46f0c67` tracking, `c4524d8` round 2

Developed stacked on phase-5; **#194 merged mid-review**, so the PR targets `main`. `main` held only the merge commit beyond this branch (empty content diff), so no rebase was needed.

The first phase that can block a real user. Guards `invite_user_to_organization`, `accept_invitation`, the invite branch of `provision_tenant_for_user`, and `reactivate` (moved into `OrganizationService.reactivate_membership` — the plan puts enforcement in the service layer, and the viewset version had no `bypass_limits`). `OverLimitError` renders byte-identically through REST and GraphQL, asserted by comparing the two responses to each other.

**Two BLOCKERs, one of each failure mode** — and both lived outside the code the phase set out to change:

1. **Seat limits wedged signup with a 500.** The guard on `provision_tenant_for_user` was correct, but its two callers are allauth adapters, and **allauth headless mounts as plain Django views, not DRF** — so `common/exception_handlers` never ran. Under `ATOMIC_REQUESTS` the 500 rolled back the email-verification write too, so the address stayed unverified and every retry failed identically; social signup rolled back the whole user row. Both adapters now fall through to the membership-less gated branch they already use for uninvited users, with the invitation left pending so the user is recoverable.
2. **Resending an invitation was a false block.** The guard counted the invitation being resent, so an org at its exact ceiling could never resend — in precisely the state where it matters (seats just filled, one invite never arrived). The implementer had recorded this as an accepted corner case; review disagreed and was right. The exclusion machinery already existed one method over.

**A reviewer finding that was wrong, and the fixer caught it.** Round 2 reported the same outside-the-transaction bug in `provision_tenant_for_user` that was real in `accept_invitation`. It does not exist there — that method carries a method-level `@transaction.atomic()` the other lacks. The fixer stash-tested it, found its new test green against pre-fix code, applied the harmless clarifying move anyway, and said plainly the test is not a regression guard. Recorded because **reviewer output needs verifying too, not just implementer output.**

Also fixed: `accept_invitation` marked the invitation accepted outside its transaction (a window where a seat double-counts, permanently if that write fails); an invitation-write `IntegrityError` was reported to users as "you already have a membership"; three endpoints that can return 402 did not declare it; and the unlimited-path query budget was loose enough to absorb the next regression (now pinned to an exact count).

**Gates** (re-run independently): suite **4094 passed** (phase-5 base 4054; +40); `mypy` **305 / 57** at baseline with **zero suppressions added**; `ruff` clean; `makemigrations --check` clean (no model changes); `schema.yml` verified in sync by regenerating to an empty diff. Each guard confirmed by deletion; the concurrency test races genuinely with a working negative control.

### Phase 6b — Enforce pre-paid limits: calendars, groups, bundles, availability ✅

- **Status**: reviewed clean (3 rounds), PR open
- **Models**: implementer Tier 3; reviewer Tier 4 all three rounds; fixer Tier 4 rounds 1–2, Tier 3 round 3
- **Branch**: `plan/billing-plans-and-limits/phase-6b` · **Base**: `main` (developed on 6a; **#195 merged mid-review**, content diff to `main` empty so no rebase needed)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/196
- **Commits**: `64cf6c0` implementation, `1397738` round 1, `1e44a7a` tracking, `bf1058a` round 3

Guards `create_resource_calendar`, `create_group`, `create_bundle_calendar`, `create_available_time` / `bulk_create_availability_windows` / `batch_modify_available_times`, and the bulk room-import writer. **The reviewer's prediction was right**: the unmetered path did survive in the bulk sync writer, exactly where the plan said to look.

**Two BLOCKERs, and once again one of each failure mode** — unmetered creation and false block:

1. **The headroom split let unlimited RESOURCE calendars through by *promotion*.** It classified resources as "already imported" by querying `Calendar` on `(organization, external_id)` with **no `calendar_type` predicate**, while the counter it guards counts only live `RESOURCE` rows. The write loop puts `calendar_type=RESOURCE` in `update_or_create`'s `defaults`, so a matched PERSONAL row — created by `import_account_calendars` **on the identical key** — was retyped into the counted set having consumed nothing; and when every discovered room already had a row, the split returned before `check_limit` was ever called. **For Microsoft this is a total bypass, not an edge case**: `get_account_calendars` and `get_calendar_resources` both enumerate `client.list_calendars()` and emit the same `external_id` space, so importing an account's calendars and then the rooms retyped every one of them, unmetered, without limit.
2. **A net-zero availability batch was refused at the ceiling.** `delta=create_count` ignored the batch's `delete` operations, so an org at its exact `availability_windows` ceiling could never edit availability by replacement — reachable from both the REST batch serializer and the GraphQL mutation, and the same defect as 6a's resend-at-the-ceiling.

**The recurring root cause of this plan is now explicit: two predicates that are supposed to mean the same thing, written twice.** It has produced a defect four times. The split predicate now lives on `CalendarQuerySet` as the *exact complement* of the `live_of_type` the usage counter counts with, and a property test asserts that equivalence over every `(calendar_type, visibility)` combination rather than a few examples. The batch's delete credit is likewise computed with `only_user_authored` — the counter's own predicate — so deleting a derived row (a recurrence exception) earns no capacity for freeing something that occupied none.

The rule is stated once, in the queryset: **"will this write raise the counter?"** — not "does a row exist?". That also makes soft-deleted rows *free* (the upsert leaves `visibility` untouched, so they stay uncounted), which the reviewer's suggested `calendar_type=RESOURCE` predicate would have false-blocked; a deliberate deviation, derived in the docstring.

Also fixed: the split is read **inside** the guard lock (`EntitlementService.lock_billing_root`, so a bulk writer whose delta comes from a read can lock before reading); a truncated import now ends `PARTIAL` rather than `SUCCESS` plus an advisory string on an error column (migration `0041`, choices-only, reverses clean); the warning caps the skipped ids it names instead of writing a multi-hundred-KB string; and the executor returns what it imported rather than what the provider reported.

**One SHOULD-FIX was answered rather than applied.** The `FOR UPDATE` lock is held across the whole import loop and is **not narrowed here** — `check_limit(lock=True)`'s contract is that each authorized write commits in the transaction that holds the lock, not that the whole import must be one transaction; a chunked version that re-locked and re-checked headroom per chunk would preserve the invariant too. Justified in place instead of implemented: the loop is database-only (the provider call happens before the lock, every Celery dispatch on commit) and runs from a background task. **Follow-up (not scheduled to a phase): chunk `_execute_organization_calendar_resources_import`'s write loop** so a large reseller import doesn't hold the billing root's subscription lock for the whole run.

**Test gaps the review named, now closed**: every guard is exercised at `limit_value=NULL` as well as at a finite ceiling (the "no feature flag — `unlimited` is the switch" rule); the batch guard has false-block-direction tests (update-only, delete-only, one-for-one replacement); `bulk_create_availability_windows` is tested at delta>1; the "unlimited full sync" test drives `import_organization_calendar_resources` end to end and asserts `SUCCESS` **and** an empty `error_message`; and one test builds `CalendarService()` through DI and authenticates, so the container wiring is actually asserted rather than assumed by hand-built contexts.

Both BLOCKER regressions were **confirmed failing against pre-fix code** by reverting each fix in place (8 failures for the promotion split, including the property test; the replacement-at-the-ceiling test for the batch).

**Review round 3** found **no BLOCKERs** and declared the phase merge-safe, after verifying the split predicate by hand across all twelve `(calendar_type, visibility)` combinations — including `UNLISTED`, a third state nobody had raised, which lands correctly on both sides — and confirming the two changed test assertions are arithmetically right rather than fitted. Two SHOULD-FIXes applied:

1. **The lock fix had been applied on the calendar side and not the availability side.** The batch's delete-credit was read *before* any lock, and when `delta == 0` `check_limit` was never called, so **no lock was taken at all**. Two concurrent `[delete X, create]` batches both computed `delta=0`, both skipped the guard, and under READ COMMITTED the loser's delete silently affected zero rows — both creates landed, one over the ceiling. Reproduced with a real threaded test (4 rows against a ceiling of 3) before fixing.
2. A duplicated `external_id` in one discovery inflated the charge, producing a false partial cap.

Also: the "exact complement of `live_of_type`" docstring was factually wrong (`PERSONAL/ACTIVE` is in neither set) and is now stated as the complement of *newly entering* it — the one place in this phase where a misleading predicate comment is load-bearing.

**Gates** (re-run independently by the orchestrator): suite **4148 passed** (6a base 4094; +54); `mypy` **305 errors / 57 files** at baseline with **zero `type: ignore` added** (the only `noqa` are `S106` on test-only dummy credentials, matching five existing precedents); `ruff check` + `format --check` clean (486 files); `makemigrations --check` clean with `0041` applied; `check --deploy` unchanged.

⚠️ **Orchestration hazard worth remembering**: one gate run silently executed in the **main checkout** instead of the worktree, because the shell's cwd had reverted and background commands do not persist `cd`. It reported 3969 passed / mypy 308-58 — the *pre-Phase-5* baseline — while looking perfectly green. Caught only because those numbers are compared against recorded baselines rather than checked for "no failures". **Pin `cd <WORKROOT>` in every gate command.**

**Out of scope, spotted while fixing**: `CalendarQuerySet.update()` (`calendar_integration/querysets.py`) raises `AttributeError: 'CalendarQuerySet' object has no attribute '_meta'` — it reads `self._meta` where it means `self.model._meta`, so **any** `.update()` on a Calendar queryset is broken. Pre-existing, unrelated to this phase, not fixed here.

### Phase 6c — Enforce pre-paid limits: webhook subscriptions and API system users ✅

- **Status**: reviewed clean (3 rounds), PR open
- **Models**: implementer Tier 2; reviewer Tier 3→4 (stepped up for the security surface); fixer Tier 2→4 rounds 1–2, Tier 3 round 3
- **Branch**: `plan/billing-plans-and-limits/phase-6c` · **Base**: `plan/billing-plans-and-limits/phase-6b`
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/197
- **Commits**: `8c46f02` implementation, `bd07aed` rounds 1–2, `5796cc9` round 3

**Closes spec objective 1 for pre-paid resources.** All 7 `kind=prepaid` members of `LimitedResource` have a guarded creation path with a blocking test, machine-checked by `payments/tests/test_prepaid_resource_coverage.py`, which derives the prepaid set from the seeded plan's own `PlanLimit.kind` and asserts the registry bidirectionally. `event_occurrences` is the one deliberate exclusion (postpaid, Phase 8).

**The biggest finding was the first round's own fix.** The implementation changed `has_entitlement` to fail **open** on a missing subscription, after 106 test failures from calendar-integration fixtures that build orgs with no `Subscription`. It argued symmetry with `get_effective_limit`. **That symmetry is invalid**: for a numeric ceiling "we don't know" → NULL → unlimited, which coincides with what the rollout seeds; for a **boolean** gate "we don't know" → `True` grants **paid** features, strictly more than `free`. Rollout safety was already covered by the backfill migration, so the branch fired only when billing state was already broken — granting the most access to exactly those orgs. Ops deleting a `Subscription` to re-provision would hand a `free` org the entire partner API in that window. **Reverted; `entitlement_service.py` has zero net diff against 6b.** The fixtures were the defect and were fixed instead (autouse provisioning fixture, 20 modules opting out via `@pytest.mark.no_auto_subscription`).

Two more BLOCKERs: **the entitlement gate was bypassable** — the plan names `authenticate` as the chokepoint, but `_get_write_adapter_for_calendar` builds an adapter for the calendar's *owner*, so a Google-authenticated actor could write to a Microsoft calendar ungated; and **`create_system_user(organization=None)`**, an explicitly supported admin path, started 500ing.

**Two `@inject` landmines, neither billing-related, both invisible to the suite:**
1. **Silent no-op under `from __future__ import annotations`** — dependency_injector cannot read `Provide[...]` from a stringified annotation and returns the function unpatched. The branding guard passed every test while doing nothing.
2. **Admin autodiscovery beats DI wiring** — `@admin.register` constructs `SystemUserAdmin` before `DICoreConfig.ready()` wires the container, so its injected service was permanently `None`: **every** admin-created system user raised `ValueError`, independent of billing. A live production bug.

**⚠️ Follow-up: sweep the codebase for both `@inject` patterns.** Neither is detectable by tests.

Also: `resolve_branding` had to be **split** rather than gated — it also backs `validate_return_url`, so gating it wholesale would break the OAuth return flow for every downstream tenant of a reseller that downgraded off `white_label_branding`. And `_get_write_adapter_for_calendar` now **raises** rather than returning `None` (callers traced individually; the calendar webhook service is the one place where skipping the remote work is intended, and it catches explicitly — otherwise the provider gets a 500 and retries until the channel expires).

**Gate-integrity note.** The implementation regressed `mypy` 305/57 → 309/60 and reported "net new: zero". Its `git stash` verification left the new **untracked** test files on disk, so it measured the phase against itself. **Use `git stash -u` or a clean checkout of the base branch when establishing a baseline.**

**Gates** (re-run independently in the container): suite **4200 passed** (6b base 4148; +52); `mypy` **305 / 57** at baseline; `ruff` clean (493 files); `makemigrations --check` clean; `schema.yml` unchanged; 5 × `noqa: S106`, all test dummy credentials.

**Carried forward from 6c:**
- `ADVANCED_SCHEDULING` is a declared `Entitlement` with **no gate anywhere**. Recorded so it is not mistaken for covered.
- `SystemUserAdmin`'s changelist raises `ImproperlyConfigured: QuerySet must be filtered by 'organization'` — pre-existing, unrelated.
- The `@inject` sweep above.

### Phase 7 — Meter event occurrences ✅

- **Status**: reviewed clean, gates green, PR open
- **Models**: implementer Tier 4; reviewer Tier 4; fixer Tier 4
- **Branch**: `plan/billing-plans-and-limits/phase-7` · **Base**: `main` (rebased — 6b/6c merged mid-run)
- **Commits**: `64ea614` implementation, `ec758f1` BLOCKER fixes, `a05ddb9` mechanism correction

**Gates** (re-run independently, credentials cleared to match CI): suite **4262 passed** (base 4237; +25); `mypy` **305 / 57**, zero new errors by set diff against 6c; `ruff` clean (500 files); `makemigrations --check` clean; migration `0011` reverses.

One suite failure, investigated and **not** a Phase 7 regression: `test_without_the_lock_the_net_zero_race_overshoots` is a **Phase 6b** test — the negative control asserting the race overshoots when the lock is removed. It passed 3/3 in isolation and failed once under full parallel load. A control that asserts a race *reproduces* is inherently timing-dependent, so it will intermittently redden CI. **Recorded against #196; worth making deterministic or marking `flaky`.**

**Gate-integrity note (mine, not the phase's).** I reported a `mypy` regression to 315/58 that did not exist. Re-verification — three runs, one with `.mypy_cache` removed, plus a direct error-set diff — returned 305/57 with zero new errors. The 315 run was an outlier under container contention. **Corroborate a gate delta with a second run before acting on it**; the same contention produced three non-reproducible phantom test failures in another run this phase.

**A fabricated mechanism was caught and retracted.** The fixer claimed `truncate_parent` decrements COUNT by one. Reading `RecurrenceRuleSplitter` showed the arithmetic correct (`count=None` + `until` for the parent; `5-1=4` for the continuation). Challenged for persisted state rather than reasoning, it replied: *"My 'decrements COUNT by one' claim was fabricated — I saw COUNT=4 in the persisted parent and wrote a plausible-sounding cause instead of finding the real one."* The real cause is the pk-aliasing defect below.

⚠️ **The spec's highest-severity risk.** A double-count here is silent revenue drift or overcharging, and it is invisible until a customer disputes a bill. The unique constraint on `(organization, event_id, occurrence_start)` is what makes re-running a window harmless; `is_within_allowance` and `unit_price` are stamped at meter time so a later limit change cannot retroactively reprice already-metered occurrences.

#### Decision: what a bundle booking costs (binding on Phase 8)

**One bundle booking is billed as `1 + n_internal_children` occurrences.** Not changed in Phase 7; recorded here so Phase 8's guard counts the same number rather than re-deciding.

`CalendarBundleService` (`calendar_bundle_service.py:470-505`) creates a primary `CalendarEvent` plus, for each child calendar, either a full `CalendarEvent` (when `provider == INTERNAL`) or a `BlockedTime` (every other provider). The meter counts `CalendarEvent` rows, so the same user-facing action costs a different amount depending on which providers the bundle's children use: **1** occurrence across five Google calendars, **5** across five internal ones.

Rationale for leaving it:

- It is not arbitrary. An internal child event is a real, independently editable `CalendarEvent` that consumes the same storage and expansion cost as any other; a `BlockedTime` is not billable anywhere in this plan. Billing what exists is defensible and needs no special case.
- Suppressing child events at meter time means teaching the meter about `bundle_primary_event`, i.e. a billing-specific exception inside the one expansion this phase exists to keep single-definition. That is exactly the second-predicate shape that has produced a defect in every phase of this plan.
- Nobody is currently charged: every organization is on `unlimited` for the whole rollout, so the number is only visible in usage readouts until Phase 8.

**Revisit trigger.** If product decides a bundle booking should be one billable unit regardless of child providers, the change belongs in `MeteringService.expand_occurrence_identities` (skip `bundle_primary_event__isnull=False`), **and** the same predicate must be added to Phase 8's guard and Phase 12's usage readout in the same change — three places, or the meter and the guard disagree.

#### Phase 13 gating precondition: identity churn and series truncation

Widened from "first-occurrence split" to **all identity-churn paths**, plus one defect that is not identity churn at all and is materially larger. Both are characterised with measured numbers in `payments/tests/test_metering_reconciliation.py`.

Identity is `(series root pk, occurrence start time)`. The series-root half is durable and tested. **The start-time half is not**: re-timing an occurrence mints a new identity, so an edit applied to an already-metered stretch bills the moved occurrence again. Measured: re-timing one occurrence of a 5-occurrence month → **6** rows. Bounded by an already-metered window, and surfaced by `reconcile_period` as `orphaned` drift.

**Larger, and not previously known — a bulk modification does not truncate the parent series.** The rule *arithmetic* is correct: `RecurrenceRuleSplitter.split_at_date` returns a truncated parent rule (`count=None`, `until=<last occurrence before the split>`) and a continuation rule with the remaining count — 1 + 4 = 5 for a `COUNT=5` series split at its second occurrence. Verified directly against the splitter.

The defect is that **the truncation never reaches the database**. `copy.deepcopy` of a saved Django model preserves its `pk`, so both rules the splitter returns are aliases for the original row rather than the "new, unsaved instances" `recurrence_utils`' module docstring promises. In `RecurrenceManager.create_bulk_modification_generic` the parent is truncated first and `continuation_rule.save()` runs second; still carrying the original pk, it `UPDATE`s the **parent's** rule row and overwrites the `UNTIL` just written. The continuation is unharmed — it is built from an rrule *string* and gets a fresh rule row — so the clobber is pure collateral damage.

Persisted state, read back after splitting a `COUNT=5` weekly series at Monday #2: parent still points at rule id 1, now holding `COUNT=4, until=NULL`; continuation holds a **new** rule id 2 with `COUNT=4`. Parent yields Mondays 1-4, continuation yields Mondays 2-5.

**The open-ended case is worse.** An open-ended series has `count=None, until=None`, so the continuation rule is byte-for-byte the *original unbounded rule*. Saving it over the parent's row erases the truncation completely — the parent never stops, and the series is duplicated **indefinitely**, every future month, not merely across the split window. Verified: parent rule left at `count=NULL, until=NULL`, 9 billed in the month. This is the shape most standing meetings have.

Measured, with a `+30min` offset on the modification:

| Scenario | Real occurrences | Billed | `reconcile_period` |
|---|---|---|---|
| Modify, then sweep once (**no prior metering**) | 5 | **8** | `drift == 0`, `is_clean == True` |
| Sweep, then modify, then sweep | 5 | **9** | `drift == 1`, 1 `orphaned` |

Two things make this worse than the bound previously accepted:

1. **It does not need an already-metered window.** A single fresh sweep over-bills 8/5. The first-occurrence hazard cannot do this.
2. **Reconciliation is blind to it.** The recompute reads the same calendar, which also says eight, so it reports a clean period. In the already-metered case it reports `drift == 1` against a 4-row over-bill — an operator would materially underestimate it.

**It is an upstream recurrence defect, not a metering one.** `get_calendar_events_expanded` returns the same eight events, so the calendar genuinely contains them and the meter is faithful to what it is shown. The fix belongs in `CalendarEventService.create_recurring_event_bulk_modification`.

**Where this gates, precisely.** An earlier draft of this note said "Phase 8 must not enforce the post-paid ceiling until it lands." That is stricter than necessary and would stall the plan. Phase 8's guard is **inert on every current organization**: `unlimited` carries a NULL `event_occurrences` limit, and `check_postpaid_allowance` returns headroom for a NULL ceiling, so no code path can block on an over-counted number today. The real gate is the first moment a non-NULL `event_occurrences` limit reaches a live organization — **Phase 14**, already deferred, and **Phase 13**'s overage charge, which converts the same over-count into money. Both must wait for the recurrence fix. Phase 8 ships.

### Phase 8 — Enforce the post-paid allowance ✅

- **Status**: reviewed clean (1 round, 3 BLOCKERs fixed), gates green, ready to integrate
- **Models**: implementer Tier 3; reviewer Tier 4; fixer Tier 4
- **Branch**: `plan/billing-plans-and-limits/phase-8` · **Base**: `plan/billing-plans-and-limits/phase-7`
- **Commits**: `c9643ce` implementation, `0c7b705` BLOCKER + SHOULD-FIX fixes

**Gates** (implementer, then re-run independently with credentials cleared): suite **4316 passed** (base 4262; +54); `mypy` **305 / 57** at ceiling, zero new; `ruff` clean (502 files); `makemigrations --check` clean.

`has_payment_method(organization)` (billing-root-resolved) plus `check_postpaid_allowance(delta, lock)` gate the six creation surfaces named by the plan. Every current organization is on `unlimited` (NULL `event_occurrences`), so the guard is inert today — asserted by an unlimited-behaviour test on every path.

**Three BLOCKERs, all found by review and fixed:**

1. **The lock was taken before the unlimited early-return, so the phase was not inert.** `create_event` passes `lock=True`; the original `check_postpaid_allowance` took `SELECT ... FOR UPDATE` on the org's `Subscription` row *before* checking `is_unlimited`, inside the `@transaction.atomic` block that also holds the provider network write. Every event create for every org serialized org-wide behind a Google round-trip — for a NULL ceiling. The unlimited tests asserted only `201` and could not see it. Fixed by resolving the limit first and locking only when a real ceiling exists; a `CaptureQueriesContext` test now asserts no `FOR UPDATE` is issued on the unlimited path, with a finite-ceiling negative control.

2. **`delta` and `current_usage` were different units.** `current_usage` counts `MeteredOccurrence` rows (occurrences); `delta` counted `CalendarEvent` masters. A free org at 49/50 could create an open-ended daily series (`49 + 1 ≤ 50`) and accrue ~30 unbillable occurrences/month forever. Fixed for the single-event path by a two-stage guard: stage 1 keeps `delta=1` (exact for one-offs) before the provider write; stage 2, after insert and inside the transaction, expands the recurring master through **`MeteringService.occurrence_starts_of`** — the meter's own expansion, shared not copied — over `[now, current_billing_period_end)` and rolls back if the series does not fit. The bundle fan-out residual is recorded as a Phase 13 precondition (below).

3. **`billing_state != FREE` granted unbounded accrual to orgs whose payment failed.** Replaced with an explicit allow-list `PAYMENT_METHOD_BILLING_STATES = {ACTIVE}`. Load-bearing for Phase 10 — see the precondition below. Every `BillingState` is pinned to an assertion so a new state fails a test until someone decides for it.

The fixer went **narrower than the review** on BLOCKER 3, excluding `CANCELLED` ("a card was attached at some point" is the wrong tense for deciding whether overage can be billed — a cancelled org may have removed its instrument). Both of its independent judgment calls moved toward the safe side.

SHOULD-FIX cleared: registry test now derives the guarded set from an AST walk (was a same-file tautology); `bypass_limits` added to `create_event`; the guard now honours `_bypass_entitlement_limits`; `transfer_event` passes `_check_postpaid_allowance=False` (a move is not a creation); the over-allowance sync raise now logs org id + remedy and has a recovery test.

#### Phase 9/10 gating precondition: `has_payment_method` is a proxy

`EntitlementService.has_payment_method` is the whole basis on which an organization is allowed to accrue post-paid overage. **There is no payment-method record in the schema yet**, so it reads `Subscription.billing_state` as a stand-in, through an explicit allow-list: `PAYMENT_METHOD_BILLING_STATES = {ACTIVE}`. Everything else — `FREE`, `GRACE`, `RESTRICTED`, `CANCELLED`, and any state added later — resolves to `False`. `payments/services/entitlement_service.py` carries the per-state reasoning; `calendar_integration/tests/services/test_postpaid_enforcement.py::EXPECTED_HAS_PAYMENT_METHOD` pins every state to an assertion, and a new `BillingState` fails that test until somebody decides for it.

Two things are binding on the phases that touch this:

1. **Phase 9 must re-point the method at the real record, not widen the allow-list.** Once a payment instrument is actually persisted, `billing_state` stops being evidence of anything and the proxy must be deleted rather than extended.
2. **Phase 10 must keep `GRACE` resolving to `False`.** Phase 10 moves `ACTIVE → GRACE` **on a failed charge** while leaving the organization fully operational (only `RESTRICTED` is write-blocked, and that is Phase 11). If `GRACE` read as "has a payment method", an organization whose card just declined would get *unbounded, unbillable* accrual for the entire dunning window — making the dunning ladder the largest-bill path in the product. The original justification for including it ("the separate grace/restricted machinery is what blocks writes on that state") is true of `RESTRICTED` and false of `GRACE`.

The plan's deferred `TRIALING` state (`…IMPLEMENTATION_PLAN.md`, "no payment method to start") is the same trap from the other direction, and is why this is an allow-list: a newly-added state defaults to "no payment method", which is the safe direction, instead of silently granting accrual.

#### Residual: the guard counts occurrences for a single recurring master, masters for a bundle fan-out

`check_postpaid_allowance`'s `current_usage` is in `MeteredOccurrence` rows — **occurrences**. Phase 8's single-event path matches that unit: a recurring master is expanded, after insert and inside the creating transaction, through `MeteringService.occurrence_starts_of` — the meter's own expansion, shared rather than re-implemented — and the create is rolled back if the series does not fit. (It has to run after the insert because the expansion is a Postgres function keyed on the event's id; re-deriving it in Python from the rrule string would be the second-expansion defect this plan keeps producing.)

**The bundle fan-out still counts masters.** `_bundle_event_billable_units` returns `1 + n_internal_children` per the Phase 7 binding decision, and each child create runs with `_check_postpaid_allowance=False`, so a *recurring* bundle event is under-charged by exactly the same factor the single-event path used to be. Bounded and inert today (every organization is `unlimited`), but it is a **Phase 13 gating precondition** alongside the identity-churn and series-truncation defects above: an over-allowance number that becomes money must be in occurrence units on every path, not just the one.

## Current phase

**Phase 9 — Upgrade, add-on purchase, and proration** (implementer Tier 3, reviewer Tier 4)

Base: `plan/billing-plans-and-limits/phase-8` · Branch: `plan/billing-plans-and-limits/phase-9`

Spec objective 2: an org on the free plan can choose a paid plan, pay, and see its limits lift with no engineering intervention. New endpoints on a new path — purely additive surface, no feature flag.

**Binding on this phase (from Phase 8's precondition above):** Phase 9 introduces the first real payment-method record. When it does, `EntitlementService.has_payment_method` must be **re-pointed at that record and the `billing_state` proxy deleted** — not widened. Once an instrument is actually persisted, `billing_state` stops being evidence of anything. The per-state test (`EXPECTED_HAS_PAYMENT_METHOD`) must be updated in the same change so it pins the new source of truth.

**Watch for the plan's recurring failure shape** — two predicates that must mean the same thing. Proration maths is the obvious host for it here: the amount charged and the entitlement granted must derive from one plan-change computation, not two.

## Remaining phases

| Phase | Title | Impl | Reviewer | Fixer |
|---|---|---|---|---|
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
