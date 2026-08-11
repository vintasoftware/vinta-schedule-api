# Tracking — Billing API Contract Hardening

**This file is the conductor's.** Sub-agents: read it, do not rewrite it. Report your
results back in your final message; the conductor writes them here.

Plan: `ai-plans/2026-08-10-BILLING_API_CONTRACT_HARDENING_IMPLEMENTATION_PLAN.md`

## run_options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config + standing user preference (chain phases straight through) |
| `generate_inline_comments` | `true` | user answered "Yes" |
| `use_worktree` | `true` | config |
| `full_test_suite` | `false` (quick / scoped) | config |
| `commit_strategy_resolved` | `stacked-branches` | user answered (config was `ask`) |

## Resolved topology

| Value | Resolution |
|---|---|
| `WORKROOT` | `/Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-billing-api-contract-hardening` |
| `BASE_BRANCH` | `plan-billing-api-contract-hardening` (off `origin/main` @ f349b53) |
| `SANDBOX_TIER` | `none` — see below |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-billing-api-contract-hardening.yaml` |
| main checkout | `/Users/hugobessa/Workspaces/vinta-schedule` — **read-only for this run** |

**`SANDBOX_TIER = none`.** `sandbox-exec` is installed, but the conductor session is
rooted in the main checkout and claude-code runs subagents in-process, so the write
guard could not be enabled mid-session. Prevention degrades to the reactive backstop:
run `git -C <main checkout> status --short` after every phase and treat any new
modification to a tracked file as a stray write to be reverted and re-applied in the
worktree. The only expected untracked entries in main are the three `ai-plans/*.md`
files that were already there before this run.

## Agent models

| Role | Tier | Source |
|---|---|---|
| implementer | per phase | plan's `**Suggested AI model**` line |
| reviewer | 3 | `agent_models.reviewer`; **Phase 3 overrides to Tier 4** |
| fixer | 2 | `agent_models.fixer` |
| worktree_prep | 1 | ran inline (delegate was declined) |
| integrate | 1 | `agent_models.integrate` |

## Phases

| # | Title | Impl tier | Status | Branch | PR |
|---|---|---|---|---|---|
| 1 | Machine-readable error codes | 2→3 (sonnet) | ✅ done | `plan/billing-api-contract-hardening/phase-1` | [#249](https://github.com/vintasoftware/vinta-schedule-api/pull/249) |
| 2 | Constrain `document_type` | 2→3 (sonnet) | ✅ done | `plan/billing-api-contract-hardening/phase-2` | [#250](https://github.com/vintasoftware/vinta-schedule-api/pull/250) |
| 3 | Grace recovery via retry-payment | 3 (reviewer 4) | ✅ done | `plan/billing-api-contract-hardening/phase-3` | [#251](https://github.com/vintasoftware/vinta-schedule-api/pull/251) |

| 4 | Collect the outstanding balance on retry | 3 (reviewer 4) | ✅ done | `plan/billing-api-contract-hardening/phase-4` | [#252](https://github.com/vintasoftware/vinta-schedule-api/pull/252) |

**Four phases complete.** Final full-suite run on phase-4: **5391 passed, 0 failed.**

Phase 4 was added *after* Phase 3 shipped, on evidence from a live Stripe probe.

## Phase notes

### Phase 1 — Machine-readable error codes ✅

Commit `dd5bfae`, 9 files, +516/−91. PR [#249](https://github.com/vintasoftware/vinta-schedule-api/pull/249).

**What landed.** `code` + `as_error_body()` promoted onto `BillingError`; codes on the
four subclasses; three new branches in `vinta_exception_handler`, each calling
`set_rollback()`; per-view `try/except` deleted from `change_plan` and
`AddOnViewSet.create`; `BILLING_ERROR_BODY_SERIALIZER` (inline serializer, named
`BillingErrorBody` in the schema) documenting 400/409 on both actions; `schema.yml`
regenerated.

**Deliberate widening beyond the Touch List.** `PaymentProviderNotConfiguredError`'s
handler branch now returns `as_error_body()` instead of a hand-built
`{"detail": ...}` — required by the plan's **API Design** table, which says it "gains
`code` via the base". One pre-existing assertion in
`payments/tests/test_over_limit_rollback.py:234` was updated to match. Still a strict
literal-dict comparison; not loosened.

**`OverLimitError` verified frozen.** Reviewer read all three consumers
(`vinta_exception_handler` 402 branch, `public_api/extensions.py`,
`public_api/middlewares.py`) and confirmed none changed. The new frozen-body test
asserts a literal dict, not one re-derived from the object — checked for
self-comparison specifically, since a vacuous assertion there is the phase's worst
silent failure.

**Review: clean.** Zero BLOCKERs, zero SHOULD-FIXs. One NIT — a drive-by docstring
rewrite on `AddOnPurchaseRequestSerializer` that wasn't on the touch list. Harmless
(it corrected already-stale text); no fixer dispatched.

**Reviewer also confirmed** the committed `schema.yml` byte-for-byte matches a fresh
`manage.py spectacular` run, and flagged a pre-existing repo gap (not introduced
here): `test_schema_contract.py` generates the schema in-process, so it cannot catch a
stale committed `schema.yml`, and CI's `backend-schema` pre-commit hook is skipped
project-wide (`.github/workflows/main.yml:105-106`).

**PR comments:** 7/8 inline. The 8th targeted `payments/exceptions.py:305`
(`OverLimitError.as_error_body`), which is unchanged and therefore outside the diff —
GitHub can't anchor there. Posted as a top-level comment instead.

### Phase 2 — Constrain `document_type` ✅

Commit `1356d26` (amended once for the fix below), 10 files, +221/−7. PR
[#250](https://github.com/vintasoftware/vinta-schedule-api/pull/250), based on phase-1.

**What landed.** Nine-member `DocumentTypes` TextChoices; `choices=DocumentTypes` on
the model with `max_length` held at 50; migration `0021`, `choices`-only, verified
forward → reverse → forward; `ChoiceField` on the serializer;
`DOCUMENT_TYPES_MAPPING` extended with identity entries for `SSN`/`EIN`/`PASSPORT` and
its stale "no other provider to alias from/to" comment replaced; `schema.yml`
regenerated.

**BLOCKER found and fixed.** The implementer resolved a drf-spectacular enum-name
collision by adding `"PolicyDocumentTypeEnum"` to `ENUM_NAME_OVERRIDES` — which
**renamed the `legal` app's already-published `DocumentTypeEnum` component**. Every
generated client referencing it would have broken, for an app this plan does not
touch. The reviewer didn't just assert it was avoidable: it copied the worktree to a
scratch dir, re-ran `spectacular --fail-on-warn` with the override keyed to the
*existing* name, and showed the collision resolves with zero warnings while
`BillingProfileDocumentTypeEnum` auto-resolves needing no override at all. Fixer
applied it and amended the commit. Verified independently by the conductor: Phase 2's
`schema.yml` diff is now purely additive — one `BillingProfileDocumentTypeEnum` added,
`DocumentTypeEnum` preserved, `PolicyDocumentTypeEnum` absent from the schema.

**Lesson worth carrying:** resolve schema-component name collisions by pinning the
incumbent, never by renaming it. A component name is a client contract.

**Also verified by the reviewer.** The legacy-value read-back test is non-vacuous (it
writes via `objects.create()` and pre-asserts on the model instance, and DRF's
`ChoiceField.to_representation` genuinely doesn't validate on read). The
mapping-agreement test compares both directions, not a subset. Repo-wide fixture audit
clean. NIT only: three `document_type="DL"` `baker.make` calls exist, not the two the
implementer reported — all three verified harmless.

**Full suite run during the fix:** 5344 passed.

### Phase 3 — Grace recovery via retry-payment ✅

Commit `ab45ae4` (amended three times), 13 files, +1295/−78. PR
[#251](https://github.com/vintasoftware/vinta-schedule-api/pull/251), based on phase-2.
Tier 4 reviewer per the plan's `**Review models**` override — it earned the escalation.

**What landed.** `POST /billing/subscription/retry-payment/`; `retry_payment` on
`SubscriptionService` under a `select_for_update()` row lock; two new 409 errors;
`PaymentService.update_subscription_payment_token` wrapper; the namespaced
idempotency key; `schema.yml` regenerated. No migration. `change-plan`'s no-op
untouched, per **Non-goals**.

**Two BLOCKERs, neither the one the plan predicted.** The plan escalated this phase
fearing a cross-namespace idempotency collision. That risk turned out to be
structurally impossible (disjoint prefixes). The real defects were:

1. **Downgrade-originated GRACE was accepted** — `_schedule_downgrade` produces GRACE
   with `pending_plan` set and no failed charge. Charging there bills the
   still-active *higher* plan while the webhook's `confirm_plan_change` syncs the
   *lower* plan's limits: payer pays high, receives low. Reviewer proved it with a
   runnable probe. Fixed by sharing `DunningService`'s existing predicate.
2. **Repeat retries could double-charge** — the row lock serializes but does not
   deduplicate, because `retry_payment` writes nothing locally by design. Reviewer's
   probe produced two charges under two keys.

**The second fix had to be redone, and this is the lesson of the phase.** The
reviewer's suggested remedy — gate on the dunning retry bucket — was implemented as
specified and flagged by the fixer. On inspection it was disqualifying:
`MIN_DUNNING_RETRY_INTERVAL` is **20 hours** and `last_dunning_attempt_at` is stamped
by the ladder too, so the ladder tick that prompts the payer to update their card
would lock them out of the endpoint that exists to let them do it — and a declined
replacement card would block trying a second one, for 20 hours.

**User decided** (via `AskUserQuestion`): dedup on the client's namespaced
`idempotency_key`, keep the row lock for serialization. Same key → provider collapses
to one charge; different key → deliberate second attempt, allowed, because it is
indistinguishable from "my new card was declined, here is another". Accepted tradeoff:
no server-side backstop against a client that regenerates keys for one user intent —
which is what an idempotency key is for, and the contract `change-plan` already
relies on.

**A green test proved nothing here — twice.** The original non-collision test compared
against a string the test itself built; the reviewer disproved it by mutating the
production key to be byte-identical and watching the suite stay green. Both BLOCKER
fixes were required to demonstrate red-then-green, and did.

**Other review findings, all fixed:** key asserted only against a fake (now pinned at
the real MercadoPago SDK boundary, `x-idempotency-key`); no permission test for a
money-moving endpoint (the docstring claiming coverage was false); no RESTRICTED case
at the HTTP layer; stale class docstring leaking into the published schema as the new
operation's description.

**Deliberately NOT fixed — carry this forward.** `retry_failed_charge` is a "move onto
a plan" operation, not a "collect the outstanding balance" one. On Stripe it becomes
`Subscription.modify` with a same-amount price whose prorations may net to ~0, without
touching the past-due invoice. No test exercises either adapter's money semantics. The
plan's **Non-goals** fence off adapter work and this needs a real provider test
account, not a code change. **If it doesn't actually charge, this phase ships the same
silent-200 defect it exists to remove, one layer down.** Surfaced in the PR body.

**Conductor-applied cleanup:** five stale doc references to the now-module-level
helpers (`DunningService._is_downgrade_grace` etc.) that would have sent readers to
methods that no longer exist.

**Verified independently by the conductor:** `dunning_service.py`'s 170-line diff is a
pure method→module-function move with identical bodies plus call-site renames —
behaviorally inert. No stale references remain. Stash list contains only pre-existing
entries from other branches (a fixer had a `git stash` mishap mid-run and recovered).

### Phase 4 — Collect the outstanding balance on retry ✅

Added after Phase 3 shipped. PR [#252](https://github.com/vintasoftware/vinta-schedule-api/pull/252), 2 commits, 16 files.

**Why.** Phase 3's endpoint passed a 5364-test suite and collected **$0.00**. A probe
against real Stripe test mode (Test Clock, real adapter methods, genuine renewal
failure) showed the same-amount price move prorating to zero, raising a $0.00 invoice,
marking it paid, and flipping the subscription to `active` while the $49 stayed open.
That $0 invoice emits `invoice.paid`, which is in `RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES` —
a false-recovery risk, not merely a no-op.

**What landed.** New `pay_outstanding_invoice` adapter primitive (Stripe: pay every
open/uncollectible invoice oldest-first with per-invoice idempotency keys and an
explicit `payment_method`; MercadoPago: typed `collection_not_supported` refusal);
`PaymentService` wrapper on Rule A routing; `retry_payment` repointed;
`NoOutstandingBalanceError`; a zero-amount guard so no payment of zero can resolve
dunning. `retry_failed_charge` and `dunning_service` byte-unchanged.

**Two BLOCKERs the probe could not see — the lesson of this phase.** The probe verified
money moved *at Stripe*; it never checked that **our** system notices. The Tier 4
reviewer found the recovery never completed:

1. The webhook resolved the payment off `latest_invoice`, but paying an older invoice
   creates nothing new — so `latest_invoice` was the ladder's $0 proration invoice,
   which has no PaymentIntent. Lookup returned `None`, no `Payment` row, payer rode
   GRACE → RESTRICTED with $49 already taken. Phase 3's failure inverted.
2. `payments.data[0]` picked the dead card's *failed* `InvoicePayment` (both entries
   are present after a paid-after-failure; Stripe documents no ordering), mapping to
   `pending` — neither approved nor failed — so nothing happened.

Both reproduced red-then-green.

**Also fixed:** `limit=1` unordered paid the newest open invoice, leaving the real
past-due one open; `uncollectible` invoices produced spurious 409s; `PaymentMethod.attach`
was never called (a fresh Elements `pm_...` would error) and the customer's
`invoice_settings` default still pointed at the dead card; MercadoPago's refusal was an
unhandled 500; `record_payment_method` fired on $0, granting `has_payment_method`;
`Decimal(None)`/`Decimal("")` could 500 the webhook into an infinite provider retry.
The implementer separately found `confirm_plan_change` carries its **own** unconditional
GRACE→ACTIVE transition, so guarding `resolve_payment_success` alone left the hole open.

**Verified live after every change:** $49.00 collected, previously-open invoice `paid`,
none left open, `charge.succeeded $49.00`.

**Open, deliberately not bundled.** The Stripe dunning ladder now always runs to
RESTRICTED: `retry_failed_charge` collects $0.00 and the new guard correctly refuses to
call that recovery. Previously the $0 falsely recovered payers for free. Pointing the
ladder at `pay_outstanding_invoice` changes behaviour for every payer — needs its own
decision.

### Infrastructure fix during Phase 1

The Phase 1 implementer surfaced a **worktree provisioning bug of mine**, not a code
defect: the forked DB name
(`vinta_schedule_api_wt_plan_billing_api_contract_hardening`, 57 chars) plus Django's
`test_` prefix plus pytest-xdist's `_gwN` suffix exceeded Postgres's 63-char
identifier limit. Every xdist worker truncated to the *same* physical database and
raced on `CREATE`/`DROP DATABASE`, surfacing as a confusing `ImproperlyConfigured`
rather than a name-length error — so `pytest -n auto` was silently broken for any
DB-backed test. The implementer worked around it by running suites sequentially.

Fixed between Phase 1's review and integrate: DBs renamed to `vsa_wt_billing_hardening`
in both the worktree's compose Postgres and the host Postgres, env files repointed,
`pytest -n auto` re-verified green (36 passed). Recorded in the worktree summary and
`WORKTREE.md`. Phases 2 and 3 get working parallel test runs.
