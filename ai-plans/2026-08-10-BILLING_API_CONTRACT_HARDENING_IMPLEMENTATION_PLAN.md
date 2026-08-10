# Billing API Contract Hardening — Implementation Plan

## 1. Goals

1. Give every billing error a **stable machine-readable `code`** in its response body, documented in `schema.yml`, so the frontend stops matching on error strings.
2. Turn `BillingProfile.document_type` into a **constrained, schema-visible enum** so the generated client gets real types instead of a free string.
3. Make **grace recovery actually work**: ship `POST /billing/subscription/retry-payment/` so a payer in GRACE/RESTRICTED can attach a new instrument and drive a fresh charge.
4. Close the adjacent contract gap on `POST /billing/subscription/change-plan/`, whose **request** body is also undocumented (`payment_token` is accepted but absent from the schema).

**Non-goals:**

- **No change to `OverLimitError`'s response body.** It already carries `code` and is consumed byte-identically by the DRF handler, the GraphQL error extension (@public_api/extensions.py), and @public_api/middlewares.py. This plan makes it the *pattern*, not a target.
- **No proactive "update my payment method" endpoint.** `retry-payment` is GRACE/RESTRICTED only. Updating a card while ACTIVE is a different feature with a no-charge branch.
- **No token-less retry.** `payment_token` is required. A manual "try the same card again now" would duplicate the dunning ladder's scheduled retry.
- **No change to `change-plan`'s no-op semantics.** Re-requesting the settled plan stays a no-op; its row-lock/`already_settled` behavior is load-bearing for the concurrent-first-upgrade story.
- **No backfill of existing `document_type` values.** Django `choices` are not DB-enforced; stored rows are left untouched.
- **No new provider adapter work.** `update_subscription_payment_token` is already implemented and unit-tested on both adapters.
- **No feature flag.** See **Guiding Decisions**.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **`code` mechanism** | Promote `code` (snake_case class attribute) and `as_error_body()` onto the `BillingError` base in @payments/exceptions.py. This is not a new convention — `OverLimitError` already does exactly this (`code = "limit_exceeded"`, [exceptions.py:263](payments/exceptions.py#L263)), and three surfaces already consume its body. Promoting it means every future billing error carries a code by construction rather than by remembering. |
| **`OverLimitError` is frozen** | It overrides `as_error_body()` to keep its richer body (`resource`, `current_usage`, `limit`, `remedy`) **byte-identical**. A test asserts the exact dict. Changing it would silently alter the GraphQL error extension and the public_api middleware's 402 body at the same time. |
| **Flat 400 body** | `PaymentTokenRequiredError` moves from DRF's field-keyed `{"payment_token": ["..."]}` to flat `{"code": "payment_token_required", "detail": "..."}`. Breaking for anything reading `response.data["payment_token"]` — accepted, because the frontend matches on the string today and has to change either way. Consistency with the existing contract beats preserving a shape nobody consumes correctly. |
| **Central handler, not per-view** | All three errors render from `vinta_exception_handler` (@common/exception_handlers.py), not from `try/except` in each action. `UnconfirmedPlanChangeError`'s 409 moves out of `SubscriptionViewSet.change_plan` into the handler. Per-action mapping has to be remembered at every new billing write; the handler is remembered once. Every branch returning a `Response` **must** call `set_rollback()` first — the module docstring explains why, and the merged `PaymentProviderNotConfiguredError` branch is the precedent. |
| **`document_type` is constrained** | Evidence: `DOCUMENT_TYPES_MAPPING` in @payments/services/payment_adapters/mercadopago_payment_adapter.py is a fixed set (`CPF, CNPJ, DNI, CI, RUT, OTHER`) that the adapter sends as MercadoPago's `identification.type`; the Stripe subscription adapter passes `document_type=None`. It was only ever free-form because nothing enforced it. Note the repo's own fixtures use `"SSN"`, which is **not** in that mapping and would be forwarded to MercadoPago verbatim and rejected — the field is already being used in ways the provider cannot accept. |
| **Enum covers LatAm + US** | `CPF, CNPJ, DNI, CI, RUT, SSN, EIN, PASSPORT, OTHER`. Wider than today's MercadoPago-only mapping because Stripe is now a live provider (US payers are representable) — but the mapping stays a *translation seam*, not a promise that every value works at every provider. |
| **Enforced immediately** | New writes carrying an out-of-set value 400 from day one. Chosen over log-first: the contract in `schema.yml` should describe what the API accepts, and a documented enum the server silently ignores is the same defect this plan exists to remove. |
| **Retry idempotency is namespaced** | The client key becomes `retry-payment-{subscription_pk}-{client_key}`, structurally distinct from the dunning ladder's `dunning-retry-{pk}-{ordinal}`. **This is the load-bearing detail of Phase 3**: a user paying with a *new card* must never be deduplicated against the scheduled attempt that just failed on the *old* card. Sharing the bucket would silently swallow the payment the user believes they just made. |
| **No feature flag** | Consistent with the payment-provider plan's recorded exception: this repo has no feature-flag framework. Both behavior changes here were explicitly chosen to be immediate — flat error bodies with "API first, frontend follows", and `document_type` enforced at once. A flag would contradict both decisions. Rollback is a deploy revert; the `document_type` migration is `choices`-only and touches no data. |
| **Provider routing is inherited, not re-derived** | Both provider calls in `retry-payment` run through the merged Rule A path (the subscription's own stored `payment_provider`), via `retry_failed_charge` → `change_subscription_plan` and the adapter resolved for `update_subscription_payment_token`. Phase 3 must **not** resolve the provider from the organization's pin — a subscription with live provider-side state must be driven at the provider holding it. |

## 3. Data Model Changes

### 3.1 `DocumentTypes` TextChoices

New in @payments/billing_constants.py, beside `BillingState` / `BillingInterval`:

```python
class DocumentTypes(TextChoices):
    """Kind of tax/identity document on a ``BillingProfile``.

    Sent to MercadoPago as ``payer.identification.type`` (see
    ``DOCUMENT_TYPES_MAPPING``); ignored by Stripe, which takes no document type.
    Not every member is accepted by every provider -- this enum is the set the
    API accepts, and ``DOCUMENT_TYPES_MAPPING`` is the per-provider translation
    seam. A member valid here can still be refused by a specific provider.
    """
    CPF = ("CPF", _("CPF"))
    CNPJ = ("CNPJ", _("CNPJ"))
    DNI = ("DNI", _("DNI"))
    CI = ("CI", _("CI"))
    RUT = ("RUT", _("RUT"))
    SSN = ("SSN", _("SSN"))
    EIN = ("EIN", _("EIN"))
    PASSPORT = ("PASSPORT", _("Passport"))
    OTHER = ("OTHER", _("Other"))
```

### 3.2 `BillingProfile.document_type`

In @payments/models.py (currently `models.CharField(max_length=50)` at line 202):

```python
document_type = models.CharField(max_length=50, choices=DocumentTypes)
```

`max_length` stays 50 — no column alteration, so the migration is `choices`-only metadata. Existing rows are untouched (`choices` is not a DB constraint).

### 3.3 `BillingError.code` / `as_error_body()`

In @payments/exceptions.py, on the `BillingError` base:

```python
class BillingError(Exception):
    #: Stable, machine-readable discriminator in the rendered error body.
    #: Snake_case, never reworded once shipped -- clients branch on it.
    #: Subclasses rendered by ``vinta_exception_handler`` must override it.
    code: str = "billing_error"

    def as_error_body(self) -> dict:
        """The shared contract body every rendering surface emits."""
        return {"code": self.code, "detail": str(self)}
```

`OverLimitError` keeps its own `as_error_body()` override returning the existing six keys, unchanged.

## 4. API Design

### 4.1 Error bodies

| Error | Status | Body |
|---|---|---|
| `PaymentTokenRequiredError` | 400 | `{"code": "payment_token_required", "detail": "..."}` |
| `AddOnNotPurchasableError` | 400 | `{"code": "add_on_not_purchasable", "detail": "..."}` |
| `UnconfirmedPlanChangeError` | 409 | `{"code": "unconfirmed_plan_change", "detail": "..."}` |
| `OverLimitError` | 402 | unchanged — `{"code": "limit_exceeded", "detail", "resource", "current_usage", "limit", "remedy"}` |
| `PaymentProviderNotConfiguredError` | 409 | gains `code` via the base; body otherwise as merged |

### 4.2 `POST /billing/subscription/retry-payment/`

Auth and permissions identical to `change-plan`: `IsBillingOwnerOrAdmin`, tenant-scoped, `throttle_scope = "billing-write"` (inherited from `SubscriptionViewSet`, [billing_views.py:559](payments/billing_views.py#L559)).

Request:

```json
{"payment_token": "tok_...", "idempotency_key": "..."}
```

Both required. Response `200`: `SubscriptionSerializer`, re-fetched through the virtual-model queryset (same pattern as `change_plan`, which re-fetches to avoid an N+1 on `plan`/`add_ons`).

Errors:
- `409 {"code": "retry_payment_not_applicable", ...}` — subscription is not in GRACE or RESTRICTED.
- `409 {"code": "subscription_not_attached", ...}` — `external_id` is blank, so there is nothing at the provider to re-charge or re-instrument. Such an org has never paid; it belongs on `change-plan`'s first-upgrade path, not here.
- `400` — serializer validation.
- `429` — `billing-write` throttle.

**Semantics**: attach the new instrument (`update_subscription_payment_token`), then drive the charge (`retry_failed_charge`). The endpoint returns as soon as the provider accepts the *attempt*; the subscription is still GRACE at that point. Recovery to ACTIVE happens later, when the subscription-payment webhook confirms the charge and `DunningService.resolve_payment_success` runs — identical to every other provider-driven charge in this codebase. **The 200 does not mean "you are now active."**

### 4.3 `change-plan` request body (contract gap)

`ChangePlanRequestSerializer` accepts `payment_token`, but the `extend_schema` on `change_plan` does not declare it — the serializer docstring calls this "a deliberate deviation from the documented request shape." Phase 1 declares the serializer as the documented request so `payment_token` appears in `schema.yml`, and removes that caveat from the docstring.

## 5. Phased Rollout

### Phase 1 — Machine-readable error codes

**Goal**: every billing error returns a stable `code`, documented in `schema.yml`, so the frontend can branch on a discriminator instead of a message string.

**Feature flag**: none — see **Guiding Decisions**. This changes existing response bodies deliberately, with "API first, frontend follows" as the agreed ordering.

Changes:

1. @payments/exceptions.py: add `code` + `as_error_body()` to `BillingError` per **Data Model Changes**. Assign codes: `PaymentTokenRequiredError` → `payment_token_required`, `AddOnNotPurchasableError` → `add_on_not_purchasable`, `UnconfirmedPlanChangeError` → `unconfirmed_plan_change`, `PaymentProviderNotConfiguredError` → `payment_provider_not_configured`. `OverLimitError` keeps `limit_exceeded` and its own `as_error_body()` override.
2. @common/exception_handlers.py: add branches for `PaymentTokenRequiredError` (400), `AddOnNotPurchasableError` (400), `UnconfirmedPlanChangeError` (409). Each calls `set_rollback()` before returning, per the module docstring. Order the `isinstance` checks so the existing `OverLimitError` (402) and `PaymentProviderNotConfiguredError` (409) branches keep matching first and behave identically.
3. @payments/billing_views.py: delete the now-redundant `try/except` blocks in `change_plan` (the `PaymentTokenRequiredError` → `ValidationError` re-raise and the `UnconfirmedPlanChangeError` → 409 `Response`) and in `AddOnViewSet.create`. The handler owns these now — leaving both would mean the view wins and the handler branch is dead.
4. Same file: `extend_schema` on `change_plan` and `AddOnViewSet.create` documenting the 400 (and 409) bodies with `code`. Declare `ChangePlanRequestSerializer` as the documented request so `payment_token` appears (see **API Design**), and drop the "deliberate deviation" caveat from its docstring in @payments/serializers.py.
5. Regenerate `schema.yml` (`make update_schema`).

Spec use-case: item 1 of the request — document the two undocumented 400 error bodies.

Tests:
- **Unit** `payments/tests/test_exceptions.py` — every `BillingError` subclass rendered by the handler has a non-default `code`; **`OverLimitError.as_error_body()` returns its exact pre-existing six-key dict** (guards the GraphQL extension and public_api middleware against a silent change).
- **Integration** [payments/tests/views/test_billing_views.py](payments/tests/views/test_billing_views.py) — `change-plan` without a required token returns 400 with `code == "payment_token_required"`; add-on purchase of an unpriced resource returns 400 with `code == "add_on_not_purchasable"`; a second in-flight plan change returns 409 with `code == "unconfirmed_plan_change"`.
- **Regression** — the existing 402 over-limit tests and the GraphQL/public_api paths that read `as_error_body()` pass unchanged.
- **Contract** — assert `schema.yml` documents `code` on both 400 responses (the point of the phase is the *documented* contract, not just the runtime body).

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). Established pattern applied across a handful of files; the only subtlety is handler branch ordering and not disturbing `OverLimitError`.

**Reusable skills**: none — no clean match.

Acceptance: `POST /billing/subscription/change-plan/` omitting a required `payment_token` returns `400 {"code": "payment_token_required", ...}`, `schema.yml` documents that body, and `OverLimitError`'s 402 body is byte-identical to before the change.

---

### Phase 2 — Constrain `document_type`

**Goal**: the generated client gets an enum for `document_type` instead of a free string, and the API rejects values no provider can accept.

**Feature flag**: none — see **Guiding Decisions**.

Changes:

1. @payments/billing_constants.py: add `DocumentTypes` per **Data Model Changes**.
2. @payments/models.py: apply `choices=DocumentTypes` to `BillingProfile.document_type`. `max_length` unchanged.
3. Migration: `AlterField` on `payments_billingprofile.document_type`. `choices`-only — Django emits no DDL that rewrites the column. **No data migration**; existing rows keep whatever they hold.
4. @payments/serializers.py: `BillingProfileSerializer.document_type` becomes a `ChoiceField(choices=DocumentTypes.choices)` so writes are rejected at the serializer and the enum surfaces in `schema.yml`.
5. @payments/services/payment_adapters/mercadopago_payment_adapter.py: extend `DOCUMENT_TYPES_MAPPING` to cover the new members. Replace the stale comment ("there is currently no other provider to alias from/to" — Stripe is live now) with one stating that the mapping is a per-provider seam and that a value valid in `DocumentTypes` may still be refused by a given provider.
6. Fix fixtures using out-of-set values. `"SSN"` is now valid; audit for others.
7. Regenerate `schema.yml`.

Spec use-case: item 2 of the request — settle the `document_type` field.

Tests:
- **Unit** [payments/tests/test_models.py](payments/tests/test_models.py) — `DocumentTypes.values` and `DOCUMENT_TYPES_MAPPING`'s keys agree, so a member added to one but not the other fails loudly rather than silently forwarding an untranslated value to a provider.
- **Integration** [payments/tests/views/test_billing_profile_view_set.py](payments/tests/views/test_billing_profile_view_set.py) — creating/updating a `BillingProfile` with a valid member succeeds; with `"NOT_A_TYPE"` returns 400; **an existing row holding an out-of-set value still reads back over the API without error** (the deliberate consequence of not backfilling — a read must not start 500ing because of a write-side constraint).
- **Contract** — `schema.yml` shows `document_type` as an enum with all nine members.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). Enum + `AlterField` + serializer field, all with precedent; the read-back-of-legacy-value test is the one non-obvious piece.

**Reusable skills**: `add-migration` (the `choices`-only `AlterField` and its reverse).

Acceptance: `POST /billing-profile/` with `document_type: "NOT_A_TYPE"` returns 400, `schema.yml` exposes the nine-member enum, and a pre-existing profile row holding an unlisted value is still readable through the API.

---

### Phase 3 — Grace recovery via retry-payment

**Goal**: a payer in GRACE or RESTRICTED can submit a new card and actually get charged — today the frontend's resolve-payment flow calls `change-plan`, which returns 200 and does nothing.

**Feature flag**: none — brand-new endpoint at a new path; no existing caller reaches it.

Changes:

1. @payments/serializers.py: `RetryPaymentRequestSerializer` — `payment_token` (required, non-blank) and `idempotency_key` (required), mirroring `ChangePlanRequestSerializer`'s field definitions.
2. @payments/exceptions.py: `RetryPaymentNotApplicableError` (`code = "retry_payment_not_applicable"`) and `SubscriptionNotAttachedError` (`code = "subscription_not_attached"`), both `PaymentError`. Rendered as 409 via the Phase 1 handler.
3. @payments/services/subscription_service.py: new `retry_payment(subscription, payment_token, idempotency_key) -> Subscription`:
   - Re-read the subscription `select_for_update()` inside `transaction.atomic()`, matching `request_plan_change`'s lock discipline, so two concurrent retries cannot both drive the provider.
   - Raise `RetryPaymentNotApplicableError` unless `billing_state` is GRACE or RESTRICTED.
   - Raise `SubscriptionNotAttachedError` when `external_id` is blank. Note `retry_failed_charge` currently *logs and returns unchanged* in that case — acceptable for a background dunning tick, wrong for a user-facing request that would otherwise report success having done nothing. Do not change `retry_failed_charge`'s behavior for its existing caller.
   - Resolve the subscription adapter by **the subscription's own `payment_provider`** (Rule A — the merged provider-routing contract), call `update_subscription_payment_token(subscription, payment_token)`, then `retry_failed_charge(subscription, f"retry-payment-{subscription.pk}-{idempotency_key}")`.
   - Write nothing about the outcome locally. Success arrives via the subscription-payment webhook → `DunningService.resolve_payment_success` → ACTIVE, exactly like every other charge here.
4. @payments/billing_views.py: `retry_payment` action on `SubscriptionViewSet` — `@action(methods=["post"], detail=False, url_path="retry-payment", url_name="retry-payment")`, `IsBillingOwnerOrAdmin`, inheriting `throttle_scope = "billing-write"`. `@extend_schema` documenting the 200, both 409 codes, and 400.
5. Regenerate `schema.yml`.

Spec use-case: item 3 of the request — verify and fix grace recovery.

Tests:
- **Integration** `payments/tests/services/test_grace_recovery.py` (new) — **the phase's headline test**: a GRACE subscription → `retry_payment` with a new token → adapter receives `update_subscription_payment_token` then the charge → simulated approved subscription-payment webhook → subscription is ACTIVE, `grace_period_ends_at` and `last_dunning_attempt_at` cleared. Same for RESTRICTED, additionally asserting the post-recovery calendar resync is queued (`_trigger_resync_after_recovery` fires only when the prior state was RESTRICTED).
- **Integration** — the retry's idempotency key does **not** equal any `dunning-retry-{pk}-{ordinal}` the ladder would generate for the same subscription in the same window, so a new-card charge can never be deduplicated against the failed old-card attempt. Assert the key the adapter actually received.
- **Integration** [payments/tests/views/test_billing_views.py](payments/tests/views/test_billing_views.py) — 409 `retry_payment_not_applicable` from ACTIVE and from FREE; 409 `subscription_not_attached` when `external_id` is blank; 400 when `payment_token` is missing or blank; the 200 body reports the subscription **still in GRACE** (recovery is webhook-driven — a test that asserted ACTIVE here would be asserting a lie).
- **Regression** — a `change-plan` request re-affirming the settled plan still returns 200 as a no-op. This plan does not change that; the test records that the no-op is intentional and that `retry-payment` is the supported recovery path.
- **Unit** — the existing `retry_failed_charge` blank-`external_id` behavior (log and return) is unchanged for the dunning caller.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). New service method coordinating two provider round trips under a row lock, plus an integration test spanning endpoint → provider → webhook → state machine.

**Review models**: reviewer Tier 4 — this phase drives real charges against a payer already in a failed-payment state, and its worst failure is silent: an idempotency-key collision would make the user's new-card payment appear to succeed while the provider deduplicates it away against the old card's failed attempt. That cannot be caught by reading the diff shape.

**Reusable skills**: `create-rest-endpoint` (action wiring, permissions, `schema.yml` regeneration).

Acceptance: a GRACE subscription with an expired card recovers to ACTIVE through `POST /billing/subscription/retry-payment/` with a new token followed by the approved webhook, and the charge reaches the provider under a key distinct from the dunning ladder's.

---

### Phase 4 — Collect the outstanding balance on retry

**Added after Phase 3 shipped**, on evidence rather than suspicion. Phase 3's **Risk &
Rollout Notes** flagged that `retry_failed_charge` might be a "move onto a plan"
operation rather than a "collect the balance" one, and asked for verification against a
real provider account. That verification was run against Stripe test mode, driving the
real adapter methods with a Test Clock to produce a genuine renewal failure. Result:

| | before retry | after retry |
|---|---|---|
| $49 renewal invoice | `open` | **still `open`** |
| collected | $49.00 (first period) | $49.00 — **$0.00 from the retry** |
| Stripe subscription status | `past_due` | **`active`** |

The mechanism is exactly the predicted one: `_ensure_provider_plan` mints a *fresh*
Stripe Price at the *same* amount, so `Subscription.modify(proration_behavior=
"always_invoice")` produces offsetting proration line items (`-42.47` / `+42.47`) that
net to zero. Stripe raises a **$0.00 invoice**, finalizes it, marks it paid, and flips
the subscription to `active`. The past-due invoice is never touched.

Worse than a no-op: that $0.00 invoice emits `invoice.paid`, and
`RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES` in the Stripe subscription adapter contains
`invoice.paid` — so the event reaches the subscription-payment path. Whether it
resolves the dunning state (a **false recovery**: payer marked ACTIVE, nothing
collected, balance outstanding forever) or errors out because a $0 invoice has no
PaymentIntent was not established, and this phase must make the question moot rather
than answer it.

**Goal**: `retry-payment` actually collects the outstanding balance, and no
zero-amount invoice can ever resolve a dunning state.

**Feature flag**: none — `retry-payment` has no frontend caller yet (Phase 3 shipped
behind that fact deliberately), so this corrects an endpoint nobody has pointed at.

Changes:

1. @payments/services/subscription_adapters/base.py: new abstract
   `pay_outstanding_invoice(subscription, idempotency_key) -> None` — "collect the
   balance that put this subscription into dunning, now". Distinct from
   `change_subscription_plan`, which moves a subscriber onto a plan and only charges a
   proration as a side effect.
2. @payments/services/subscription_adapters/stripe_subscription_adapter.py: implement
   it — locate the subscription's open/unpaid invoice and `stripe.Invoice.pay(...)`,
   forwarding `idempotency_key`. This is Stripe's actual "collect now" operation.
3. @payments/services/subscription_adapters/mercadopago_subscription_adapter.py:
   implement it as an explicit, documented **refusal** (`PaymentAdapterError`).
   MercadoPago has no invoice to pay: `preapproval.update(status="authorized")`
   re-authorizes the series and lets MP charge on its own schedule, so it is very
   likely to have the same defect — but that is *unverified*, no MercadoPago test
   credentials were available, and no organization is routed to MercadoPago today. A
   loud failure beats shipping a second silent no-op on a money path. The docstring
   carries the probe recipe so whoever enables MercadoPago verifies it first.
4. @payments/services/payment_service.py: `pay_outstanding_invoice` wrapper resolving
   the adapter by the subscription's own `payment_provider` (Rule A), matching the
   `update_subscription_payment_token` wrapper Phase 3 added.
5. @payments/services/subscription_service.py: `retry_payment` calls
   `pay_outstanding_invoice` instead of `retry_failed_charge`. **`retry_failed_charge`
   itself is unchanged** — it keeps its current meaning for the dunning ladder, whose
   semantics are out of scope here.
6. @payments/exceptions.py: `NoOutstandingBalanceError` (`code =
   "no_outstanding_balance"`), 409 via the Phase 1 handler — a subscription in
   GRACE/RESTRICTED with nothing actually owed at the provider.
7. Zero-amount guard: a `$0` invoice payment must never resolve a dunning state.
   Defense in depth — even with Phase 4's fix, a $0 `invoice.paid` can arise from any
   proration and must not clear GRACE.

Spec use-case: closes the verification item Phase 3's **Risk & Rollout Notes** left open.

Tests:
- **Unit** — Stripe adapter pays the open invoice with the namespaced idempotency key;
  raises when there is no open invoice; MercadoPago's refusal is explicit and typed.
- **Integration** — `retry_payment` on a GRACE subscription drives
  `update_subscription_payment_token` **then** `pay_outstanding_invoice`, in that
  order, and never `change_subscription_plan`.
- **Integration** — a zero-amount subscription payment does **not** resolve dunning;
  the subscription stays GRACE. This is the regression that makes the false-recovery
  question moot.
- **Regression** — `retry_failed_charge` and the dunning ladder are untouched.

**Suggested AI model**: Tier 3. Adapter work on a money path with an established
pattern to follow, plus a webhook-path guard.

**Review models**: reviewer Tier 4 — same rationale as Phase 3, strengthened by
evidence: the first attempt at this endpoint shipped something that looked correct,
passed a full suite, and collected nothing. Reading the diff is not sufficient here.

Acceptance: re-running the Stripe probe shows the past-due invoice `paid` and the
retry collecting the outstanding amount; a zero-amount invoice payment leaves a GRACE
subscription in GRACE.

## 6. Risk & Rollout Notes

**No feature flag** — deliberate, justified in **Guiding Decisions**. There is therefore no flag-removal phase. Rollback is a deploy revert for Phases 1 and 3; Phase 2 additionally reverses a `choices`-only migration that touches no data.

**Deploy ordering (agreed: API first, frontend follows).** Phase 1 changes `PaymentTokenRequiredError`'s body from `{"payment_token": [...]}` to flat `{"code", "detail"}`. Any frontend branch reading `response.data["payment_token"]` stops matching the moment Phase 1 deploys, and the existing string heuristics stop matching too. **Confirm before merging Phase 1 that the frontend degrades to a generic error message rather than crashing on the missing key.** That check is the one thing standing between this and a visible regression on a payment screen.

**Phase 2 rejects writes that previously succeeded.** Any client sending a `document_type` outside the nine members gets a 400 from deploy. The enum was widened past MercadoPago's set specifically to reduce that surface, but it is not zero. If you want certainty, query production for `SELECT DISTINCT document_type FROM payments_billingprofile` before merging and widen the enum to cover whatever is genuinely in use — a step this plan recommends but does not require, since the field is small and the frontend is the only writer.

**Migration safety.** Phase 2's migration is `AlterField` with only `choices` changed — metadata, no table rewrite, no lock of consequence on a table holding one row per organization. Reverse restores the unconstrained field.

**Phase 3 charges real money.** The endpoint is reachable by any billing owner/admin of an org in GRACE, throttled at `billing-write` (30/min). The idempotency namespace is what prevents a duplicate charge on retry and what prevents a *swallowed* charge against the dunning bucket — both directions matter, and both are asserted. The endpoint returns before the charge is confirmed; the frontend must poll or await the subscription state rather than treating 200 as recovery.

**What this plan does not fix.** `change-plan` re-affirming the settled plan remains a silent 200 no-op. That is intentional (its `already_settled` guard is load-bearing for concurrent first-upgrades), but it means the *current* frontend flow keeps silently doing nothing until it migrates to `retry-payment`. If that window is unacceptable, the alternative — making `change-plan` handle the grace case too — was considered and set aside; revisit before Phase 3 ships if the frontend cannot migrate promptly.

## 7. Open Questions

| Question | Recommended default |
|---|---|
| Should `RetryPaymentNotApplicableError` distinguish "wrong state" from "no attached subscription", or collapse to one code? | **Keep both.** They call for different frontend copy: one means "nothing to pay right now", the other means "you have never paid — start a plan". Collapsing them would push the frontend back to inferring from `detail`, which is exactly the defect this plan removes. |
| Does the frontend need a `code` on DRF's own validation errors (400 from serializer field validation)? | **No** for this plan. Those are field-keyed and self-describing; adding codes to DRF's built-in validation is a much larger surface. Revisit if the frontend reports string-matching those too. |
| Should `retry-payment` return `202 Accepted` rather than `200`, given recovery is asynchronous? | **200**, matching `change-plan`, which is equally asynchronous and already returns 200 with the not-yet-updated subscription. Consistency within the billing surface beats strict HTTP semantics here. Worth revisiting only if the whole surface moves to 202. |
| Should a successful retry clear `last_dunning_attempt_at` so the ladder doesn't fire a redundant scheduled retry right after? | **Not in this plan.** The webhook path already clears it via `resolve_payment_success`. A redundant scheduled retry in the gap would carry the ladder's own bucket key and be deduplicated by the provider. Flagged because it is worth confirming in staging. |

## 8. Touch List

**Phase 1 — error codes**
- [payments/exceptions.py](payments/exceptions.py) — `code` + `as_error_body()` on `BillingError`; codes on four subclasses; `OverLimitError` override
- [common/exception_handlers.py](common/exception_handlers.py) — three new branches, each with `set_rollback()`
- [payments/billing_views.py](payments/billing_views.py#L627) — remove redundant `try/except` in `change_plan` and `AddOnViewSet.create`; `extend_schema` request/response docs
- [payments/serializers.py](payments/serializers.py) — drop the "deliberate deviation" caveat from `ChangePlanRequestSerializer`
- `schema.yml` — regenerated
- @payments/tests/test_exceptions.py *(new)*
- [payments/tests/views/test_billing_views.py](payments/tests/views/test_billing_views.py) — code assertions

**Phase 2 — document_type**
- [payments/billing_constants.py](payments/billing_constants.py) — `DocumentTypes`
- [payments/models.py](payments/models.py#L202) — `choices=DocumentTypes`
- @payments/migrations/00XX_alter_billingprofile_document_type.py *(new)*
- [payments/serializers.py](payments/serializers.py#L203) — `ChoiceField`
- [payments/services/payment_adapters/mercadopago_payment_adapter.py](payments/services/payment_adapters/mercadopago_payment_adapter.py#L35) — extend mapping, replace stale comment
- `schema.yml` — regenerated
- [payments/tests/test_models.py](payments/tests/test_models.py), [payments/tests/views/test_billing_profile_view_set.py](payments/tests/views/test_billing_profile_view_set.py)

**Phase 3 — retry-payment**
- [payments/serializers.py](payments/serializers.py) — `RetryPaymentRequestSerializer`
- [payments/exceptions.py](payments/exceptions.py) — `RetryPaymentNotApplicableError`, `SubscriptionNotAttachedError`
- [payments/services/subscription_service.py](payments/services/subscription_service.py#L600) — `retry_payment`
- [payments/billing_views.py](payments/billing_views.py#L559) — `retry_payment` action
- `schema.yml` — regenerated
- @payments/tests/services/test_grace_recovery.py *(new)*
- [payments/tests/views/test_billing_views.py](payments/tests/views/test_billing_views.py) — endpoint error cases
