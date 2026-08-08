# Tracking — Payment Provider Selection

- **Feature**: Payment Provider Selection
- **Plan**: [ai-plans/2026-08-08-PAYMENT_PROVIDER_SELECTION_IMPLEMENTATION_PLAN.md](2026-08-08-PAYMENT_PROVIDER_SELECTION_IMPLEMENTATION_PLAN.md)
- **Started**: 2026-08-08
- **Last updated**: 2026-08-08
- **Feature flag**: none — deliberate exception recorded in the plan's **Guiding Decisions** and **Risk & Rollout Notes**. Contingent on no organization having a paid subscription; revisit before Phase 4 if that changes.

> **This file is the conductor's.** Sub-agents: read it, do not rewrite it. The conductor updates it after each phase.

## Run options

```yaml
run_options:
  pause_between_phases: false
  generate_inline_comments: true
  full_test_suite: true
  use_worktree: true
  commit_strategy_resolved: stacked-branches
  worktree_path: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-payment-provider-selection
  worktree_branch: plan-payment-provider-selection
  worktree_summary: /Users/hugobessa/Workspaces/vinta-schedule/.vinta-ai-workflows/worktrees/plan-payment-provider-selection.yaml
  sandbox_tier: enforced
agent_models:
  reviewer: 3
  fixer: 2
  worktree_prep: 1   # used: claude-haiku-4-5
  integrate: 1
```

## Plan-level decisions resolved during this run

- **2026-08-08 — `set_payment_provider` repoint guard: none.** Staff repoint succeeds even when the org holds an active subscription at the old provider. Recorded in the plan's **Guiding Decisions** (**Pin mutability** row) and its **Open Questions** table. **Phase 2 must implement no active-subscription check**, and must carry a test asserting the absence so a later reviewer does not reinstate it.

## Completed phases

### Phase 1 — Add provider credential and default settings ✅

- **Branch**: `plan/payment-provider-selection/phase-1` — base `plan-payment-provider-selection`
- **Models**: implementer Tier 1 (claude-haiku-4-5); reviewer Tier 3 (claude-sonnet-5); fixer Tier 2→Sonnet (4 files)
- **Commits**: `a8c6754` (implementation), `b0e6098` (review fixes)

Added `STRIPE_PUBLISHABLE_KEY`, `MERCADOPAGO_PUBLIC_KEY`, and `DEFAULT_PAYMENT_PROVIDER` (validated at import against the provider slug list, raising `ImproperlyConfigured` on a bad value) across all six layers: `settings/base.py`, `.env.example`, `.env.docker.example`, `render.yaml`, all five CI job blocks in `.github/workflows/main.yml`, and `ai-tools/AGENTS.md`. Added the `payment-provider: 120/min` throttle scope that Phase 3's unauthenticated endpoint will use.

**Structural decision worth carrying forward**: settings cannot import `payments/constants.py` — its `TextChoices` classes evaluate `gettext()` at class-body time, which touches `django.conf.settings` while settings are still mid-import. Confirmed reproducible by the reviewer, not hypothetical. So the provider slugs now live in a new Django-import-free leaf module **`payments/provider_slugs.py`** (`STRIPE`, `MERCADOPAGO`, `PAYMENT_PROVIDER_SLUGS`), which `settings/base.py` imports directly and which `PaymentProviders` binds its member values to. **Later phases: import slugs from `payments.constants.PaymentProviders` as usual — nothing changed for app code. Do not merge `provider_slugs.py` back into `constants.py`.**

**Review**: one BLOCKER — the phase's acceptance criterion (`check --deploy` fails on a bad provider) had zero coverage; the test asserted the tautology `ImproperlyConfigured is not None` and carried a comment falsely claiming CI covered it. One SHOULD-FIX — the "sync" test compared its own hand-copied literal, not the real tuple, so the settings list could drift silently. Both fixed by the leaf-module refactor plus a real out-of-process test (`subprocess` → `manage.py check --deploy` with `DEFAULT_PAYMENT_PROVIDER=nonsense`, asserting non-zero exit + `ImproperlyConfigured`). Acceptance proved by hand: `docker compose run --rm -e DEFAULT_PAYMENT_PROVIDER=nonsense api uv run python manage.py check --deploy` → exit 1, `ImproperlyConfigured`.

**Gates**: ruff check + format clean; `makemigrations --check` no changes; `check --deploy` exit 0 (pre-existing dev warnings only); full suite `5162 passed`; mypy — zero new errors (pre-existing errors confirmed unchanged via stash/unstash).

### Phase 2 — Pin the provider on the BillingProfile ✅

- **Branch**: `plan/payment-provider-selection/phase-2` — base `plan/payment-provider-selection/phase-1`
- **Models**: implementer Tier 2→Sonnet; reviewer Tier 3 (claude-sonnet-5); fixer Tier 2→Sonnet
- **Commits**: `<impl>` (implementation), `8d37db7` (review fixes)

Added `BillingProfile.payment_provider` (`CharField(choices=PaymentProviders, blank=True, default="")`) plus migrations `0016` (AddField) and `0017` (backfill every existing row to `stripe`, reversible, idempotent). `record_payment_method` pins on the first confirmed instrument; `set_payment_provider` is the audited staff repoint lever, surfaced on `BillingProfileAdmin.save_model`. `PaymentProviderNotConfiguredError` added to `payments/exceptions.py` — **defined but deliberately unused until Phases 3 and 4 raise it**. `di_core/containers.py` now injects `audit_service` into `subscription_service`.

**Decisions made during Phase 2 that Phases 3 and 4 must honor:**

1. **The pin write is a conditional UPDATE, not read-then-write.** `BillingProfile.objects.filter(organization=..., payment_provider="").update(...)` — only a row still unpinned when the UPDATE runs matches, so exactly one of two concurrent callers can win. Chosen over `select_for_update()` because its correctness is structural rather than dependent on transaction settings (relevant given the project's `ATOMIC_REQUESTS` trap). **Do not regress this to an attribute assignment + `save()`**; a test asserts the `update()` call and its zero-row result specifically.
2. **`payment_provider == ""` has two meanings, both resolving identically.** "Never paid" *and* "explicitly un-pinned by staff" — `set_payment_provider(org, "")` is a legitimate un-pin (the admin's empty select option), not an error. Phases 3 and 4 must treat empty as "fall back to `settings.DEFAULT_PAYMENT_PROVIDER`" without distinguishing the two.
3. **`set_payment_provider` takes an optional `actor`**, defaulting to the audit system actor; the admin passes `request.user` so the audit entry names the staff member who repointed. Non-admin callers are unaffected.
4. **No active-subscription guard**, per the user's decision — with a test asserting its absence against a genuinely `ACTIVE` subscription at the old provider.

**Review**: four SHOULD-FIX, all resolved — (a) the write-once pin was racy (two concurrent confirmations at different providers could both see an empty pin and the second would silently overwrite, with the discrepancy warning never firing); (b) clearing the admin field 500'd via an uncaught `UnknownPaymentProviderError`; (c) the admin surface shipped with zero test coverage; (d) the audit entry attributed every repoint to the system actor even when a staff user was right there. One reported BLOCKER — migration `0016` failing `ruff format` — was **rejected as a false positive**: `pyproject.toml` excludes `**/migrations/**`, and pre-existing migrations have byte-identical style.

**Gates**: ruff clean; `makemigrations --check` no changes; `check --deploy` exit 0; payments suite `838 passed`; full suite `5180 passed`; mypy zero new errors (379 pre-existing, verified unchanged via stash).

### Phase 3 — Provider credentials endpoints ✅

- **Branch**: `plan/payment-provider-selection/phase-3` — base `plan/payment-provider-selection/phase-2`
- **Models**: implementer Tier 2→Sonnet; reviewer Tier 3; fixer Tier 2→Sonnet

Shipped both endpoints per the plan's **API Design**: `GET /billing/payment-provider/` (authenticated, tenant-scoped) and `GET /billing/payment-provider/default/` (unauthenticated, throttled at `payment-provider` 120/min). New `payments/services/provider_credentials.py` (settings-only, no adapter import path), new `payments/services/payment_provider_resolver.py`, three serializers, and a regenerated `schema.yml`.

**Decisions Phase 4 must honor:**

1. **`PaymentProviderResolver` lives in its own module** (`payments/services/payment_provider_resolver.py`), not on `PaymentService` — deliberately, so Phase 3 didn't touch the class Phase 4 rewires. **Phase 4 must call this resolver for new-charge routing rather than reimplementing pin → default.** It is the single home of that rule.
2. **The two endpoints are mounted via `extra_patterns` + plain `path()`, not the DRF router.** Necessary: the router's static list-route hardcodes `mapping={'get': 'list'}`, which forces `self.action == "list"`, which makes drf-spectacular document the response as an **array** regardless of an explicit `responses=` override. Side effect: no `{format}`-suffix routes for these two endpoints, consistent with how `organizations`' `extra_patterns` already behave. Don't "restore" them to the router.
3. **`provider_credentials.py` collapses "unknown slug" and "configured-but-empty key" into `PaymentProviderNotConfiguredError`.** Sanctioned by the plan for this read-only module. **Phase 4 must NOT copy that collapsing into adapter resolution** — in `get_payment_adapter` / `get_subscription_adapter` the Unknown-vs-NotConfigured distinction is load-bearing for routing correctness ("bad data in the pin column" vs "provider unconfigured in this environment").

**Review**: one significant SHOULD-FIX — naming the org action `list` (to get the bare URL) made drf-spectacular document a single-object response as `type: array`, so generated SPA clients would have typed it `PaymentProvider[]` and broken on first use, defeating the plan's stated reason for typed per-provider fields. Fixed by splitting the unauthenticated endpoint into its own `APIView` and mounting both outside the router — which also removed a hand-rolled duplication of DRF's `action_map` internals in `get_authenticators()`. Also added the missing integration test for a pin naming a provider absent from the registry (409, and asserted **no** fallback to the default's credentials). Schema diff verified pure addition; `components:` untouched.

**Secret-leak check**: `TestNoSecretLeak` sets distinctive sentinels for `STRIPE_SECRET_KEY`, `MERCADOPAGO_ACCESS_TOKEN`, and both webhook secrets, and asserts none appear in either endpoint's body. The reviewer independently traced the path end to end rather than trusting the test.

**Gates**: ruff clean; `makemigrations --check` no changes; `check --deploy` exit 0; phase tests `21 passed`; full suite `5201 passed`; mypy zero new errors.

### Phase 4 — Route provider calls through the resolved provider ✅

- **Branch**: `plan/payment-provider-selection/phase-4` — base `plan/payment-provider-selection/phase-3`
- **Models**: implementer Tier 3 (Sonnet); reviewer **Tier 4 (Opus)** per the plan's `**Review models**` override, run **twice**; fixers Opus then Sonnet
- **Commits**: `7c86e73` (implementation), `309b175` (five BLOCKER fixes), `97424c6` (second-pass fixes)

`PaymentService` no longer takes the singular `payment_gateway` / `subscription_gateway`; the registries are the only adapter source. Rule A (existing row → its own stored provider) and Rule B (new row → `PaymentProviderResolver`) applied per call site. Two new migrations (`0018` repoint `Subscription.payment_provider`, `0019` backfill `Payment.payment_provider` on subscription charges).

**The Tier 4 reviewer was worth its cost — first pass found five BLOCKERs:**

1. **Adapter resolution broke the inbound webhook path.** Gating resolution on credentials made `verify_payment_webhook_signature` raise an uncaught 500 in any deployment without the (brand-new) public key — which is what both env examples ship. MercadoPago would have retried forever: no payment confirmations, no `record_payment_method`, no add-on activation, no dunning resolution. A path that works today, broken by this phase. Fixed by splitting resolution: `get_payment_adapter` / `get_subscription_adapter` are registry-only for the webhook path; `get_configured_*` variants assert credentials for outbound charge sites. **Do not merge these back together.**
2. **"Configured" was gated on the wrong setting.** The check read the *publishable* keys while the adapters authenticate outbound calls with `STRIPE_SECRET_KEY` / `MERCADOPAGO_ACCESS_TOKEN`. Chosen originally because gating on secrets broke green tests — a test-environment argument driving a production rule. Fixed with an `is_configured` property on each adapter, reading the credential it actually uses, plus `MERCADOPAGO_ACCESS_TOKEN` added across all six env layers (it was in **zero** CI job blocks and neither env example).
3. **A hardcoded `mercadopago` upstream made the pin inert on the whole subscription path.** `create_subscription_for_organization` stamped every org's only `Subscription` with a literal, so Rule A drove MercadoPago for every org including Stripe-pinned ones — sending a Stripe card token to MercadoPago while `create_payment` for the same org resolved Stripe. Two providers at once, which **Non-goals** forbids.
4. **The subscription webhook poisoned the pin**, permanently stamping `mercadopago` onto any unpinned org's first confirmed charge.
5. **Recurring subscription charges created `Payment` rows with no provider**, unroutable for later refund or status check.

**Second Tier 4 pass** found no BLOCKERs and two findings worth fixing now: the staff repoint lever was structurally inert on the subscription path (a repoint updated `BillingProfile` but never `Subscription`, so the SPA would mint a token for one provider while the upgrade drove the other), fixed by re-resolving and restamping when `subscription.external_id` is empty — there is no provider-side state to protect then, and Rule A still wins once there is; and both migration reverses were lossy in a way that would have destroyed the very evidence the plan's **Risk & Rollout Notes** rollback step tells you to check, now scoped to only the rows each forward pass touched, following `0009`'s meta-stamp precedent.

**Gates**: ruff clean; `makemigrations --check` no changes; `check --deploy` exit 0; payments suite `913 passed`; full suite **`5255 passed`** (independently re-run by the conductor at `5252` before the last fix commit); mypy zero new errors.

## Remaining phases

_(none — all four phases complete)_

## Deferred phases

_(none — no cross-repo phases, and this plan declares no feature flag, so there is no flag-removal phase)_
