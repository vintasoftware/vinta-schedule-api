# Payment Provider Selection — Implementation Plan

## 1. Goals

1. Expose the **system default payment provider and its public credentials** on an unauthenticated, throttled REST endpoint, so a frontend can build a payment form before (or without) a session.
2. Expose the **organization's payment provider and its public credentials** on an authenticated, tenant-scoped REST endpoint, resolving to the org's pinned provider when it has one and the system default otherwise.
3. **Pin a provider to an organization** the first time a payment instrument is confirmed for it, and keep that pin for every future charge.
4. **Route every provider call through the resolved provider** instead of the single hardcoded MercadoPago gateway that `PaymentService` injects today.
5. Make `stripe` the system default and the explicit pin for every existing `BillingProfile`.

**Non-goals:**

- **No provider choice exposed to users.** Neither endpoint lets an org admin pick or switch providers. The per-org endpoint returns exactly one provider. Repointing an org is a staff action only.
- **No public GraphQL surface.** Partner integrations do not get these endpoints in this plan.
- **No multi-provider-per-org.** An org has at most one pin; it does not hold instruments at two providers simultaneously by design.
- **No provider-side plan catalog work.** `Subscription.plan_external_id` is already per-subscription, so nothing in `BillingPlan` needs a per-provider external id.
- **No new tokenization / card-attach flow.** The pin is still written off a confirmed charge, exactly as `PaymentMethod` is today.
- **No migration of an existing org from one provider to another** (moving live subscriptions across providers). The staff override repoints future charges; it does not port provider-side state.
- **No feature flag.** See **Guiding Decisions**.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Pin storage** | New nullable `BillingProfile.payment_provider` `CharField(choices=PaymentProviders)`. `BillingProfile` is already the one-to-one billing root keyed on `organization` and already the thing every charge path loads before hitting a gateway (`PaymentService.create_payment` raises `MissingBillingProfileError` without it). A dedicated table would add a manager, admin, and migration for a single scalar; a field on `Organization` would push billing concerns onto the tenant root. Nullable rather than defaulted so "never pinned" stays distinguishable from "pinned to the default". |
| **What pins** | `SubscriptionService.record_payment_method` — the same call, on the same evidence bar, that writes `PaymentMethod`. That method is documented as running only from the webhook path once a charge is reported `APPROVED`, never from a request that merely *attempts* to attach an instrument. Pinning anywhere earlier (subscription creation) would pin on abandoned checkouts. |
| **Pin mutability** | Write-once from application code: `record_payment_method` sets it only when currently null. No API surface mutates it. Staff repoint through Django admin or an explicit `SubscriptionService.set_payment_provider` service method, which writes through `AuditService` like the other billing business writes. `set_payment_provider` repoints **unconditionally** — it does not refuse an org holding an active subscription at the old provider. Rationale: the lever exists precisely for the migrate-a-customer-off-a-provider case, and a guard would block it in exactly that scenario. The tradeoff is accepted knowingly: repointing under a live subscription strands a provider-side subscription that no charge path will drive afterwards, and unwinding it is the operator's manual responsibility. The audit entry records the pre-repoint provider so the stranded state is traceable. |
| **System default** | `settings.DEFAULT_PAYMENT_PROVIDER`, read from env, validated against `PaymentProviders` at import so a typo fails the deploy rather than every checkout. Changing it is a deploy — acceptable, since it matches how `MERCADOPAGO_ACCESS_TOKEN` / `STRIPE_SECRET_KEY` already live and it should change roughly never. Value: `stripe`. |
| **Backfill** | Data migration writes `stripe` into every existing `BillingProfile.payment_provider`. No organization has a paid subscription yet, so this is a no-op in effect — but it makes every existing row explicitly pinned, which means a future change to `DEFAULT_PAYMENT_PROVIDER` cannot silently move an existing org onto a different provider. |
| **Adapter resolution: two rules, not one** | Operations against an **existing row** resolve the adapter from that row's own stored provider — `Payment.payment_provider`, `Subscription.payment_provider`, `Refund.payment.payment_provider`. A charge made through MercadoPago must be refunded, status-checked, and cancelled through MercadoPago; the org's *current* pin is irrelevant and using it would send a MercadoPago external id to Stripe. Operations creating a **new** row resolve from the org: pin first, `DEFAULT_PAYMENT_PROVIDER` when unpinned. This split is the load-bearing part of the routing phase. |
| **Unresolvable provider fails loudly** | A pin naming a provider that is absent from the registry, or whose credentials are unconfigured, raises a typed `PaymentProviderNotConfiguredError` (HTTP 409 at the view boundary). It never falls back to the default: a card token minted for provider A is meaningless at provider B, and the org's stored instrument lives at A, so a "helpful" fallback would produce a confusing decline at best and a charge against the wrong instrument at worst. |
| **Public credentials are typed per provider** | The response carries explicit named fields per provider in the OpenAPI schema (`stripe.publishable_key`, `mercadopago.public_key`, …) rather than an opaque dict, so the SPA gets real codegen types. Cost: adding a provider key is an API change. Accepted — providers are added roughly never, and `schema.yml` is already regenerated per change. |
| **No feature flag** | The repo has no feature-flag framework (flags are ad-hoc booleans). No organization has a paid subscription today, so the routing refactor's blast radius on real money is zero at merge time, and the "off" branch would be dead weight guarding a path nobody is on. Rollback is a deploy revert. Recorded here as a deliberate exception to the default-flag rule, not an oversight. |
| **Unauthenticated default endpoint** | `AllowAny` + `ScopedRateThrottle` under a new `payment-provider` scope, following `PaymentsViewSet`'s precedent. The payload is publishable keys and a provider slug — values that ship to every browser that loads a payment form. It carries no plan, price, org, or usage data, so it leaks nothing a rendered checkout page wouldn't. |
| **No `BillingProfile` → default** | The per-org endpoint returns the system default (not 404) for an org with no `BillingProfile`. A frontend hitting this endpoint is about to render a form to *create* the first payment; a 404 would force it to call the unauthenticated endpoint as a fallback for the most common case. |
| **`_serialize_payment` provider bug** | `PaymentService._serialize_payment` currently stamps `payment_provider=self.payment_gateway.provider` onto the serialized payload regardless of the row's own `payment_provider`. Harmless while one gateway exists; wrong the moment two do. Fixed in the routing phase by reading `payment.payment_provider`. |

## 3. Data Model Changes

### 3.1 `BillingProfile.payment_provider`

In [payments/models.py](payments/models.py#L186-L209):

```python
class BillingProfile(BaseModel):
    ...
    #: The payment provider this organization is pinned to, written once by
    #: ``SubscriptionService.record_payment_method`` when the organization's
    #: first payment instrument is confirmed. Null means "never paid" and
    #: resolves to ``settings.DEFAULT_PAYMENT_PROVIDER``. Once set, every new
    #: charge and subscription for this organization goes through this
    #: provider -- the instrument on file lives there and nowhere else.
    #: Repointing is a staff action (``SubscriptionService.set_payment_provider``),
    #: not something any API surface exposes.
    payment_provider = models.CharField(
        max_length=50, choices=PaymentProviders, blank=True, default=""
    )
```

`blank=True, default=""` rather than `null=True` — matches the `external_id` / `plan_external_id` convention already used across this module for "not yet known" string columns, and avoids the null-vs-empty-string ambiguity in serializer and filter code.

Migration: `AddField` on `payments_billingprofile`. Low-cardinality table (one row per organization), no lock concern.

### 3.2 Backfill migration

Separate `RunPython` migration setting `payment_provider = PaymentProviders.STRIPE` for every row where it is `""`. Reverse operation sets it back to `""`. Idempotent — re-running matches nothing.

### 3.3 Type plumbing

New module `payments/services/provider_credentials.py`:

```python
@dataclass(frozen=True)
class PublicProviderCredentials:
    """The non-secret, browser-safe half of a provider's credentials.

    Deliberately separate from the adapter's constructor arguments: the adapter
    holds the *secret* key (``STRIPE_SECRET_KEY`` / ``MERCADOPAGO_ACCESS_TOKEN``)
    and must never be a source these values are read through, so that no
    refactor can accidentally serialize a secret onto a response.
    """
    provider: str
    stripe_publishable_key: str | None = None
    mercadopago_public_key: str | None = None
```

## 4. API Design

### 4.1 `GET /billing/payment-provider/default/`

Auth: none (`AllowAny`). Throttle: `ScopedRateThrottle`, scope `payment-provider`.

Response `200`:

```json
{
  "provider": "stripe",
  "stripe": { "publishable_key": "pk_live_..." },
  "mercadopago": null
}
```

Only the object matching `provider` is populated; the others are `null`. Errors: `503` when the resolved default provider has no public credentials configured (a deployment error, surfaced rather than returning a form the browser cannot submit); `429` on throttle.

### 4.2 `GET /billing/payment-provider/`

Auth: `IsAuthenticated` + `TenantScopedViewMixin`. Returns the same shape as the default endpoint, with `provider` resolved as: `request.organization.billing_profile.payment_provider` when non-empty → else `settings.DEFAULT_PAYMENT_PROVIDER`.

Errors: `403` when there is no active organization on the request (matching `_require_organization` in [payments/billing_views.py](payments/billing_views.py#L60-L67)); `409` (`PaymentProviderNotConfiguredError`) when the org is pinned to a provider that is not in the registry or has no configured public credentials — the frontend cannot render a working form and must not be handed a different provider's keys; `503` for the same condition on the *default* fallback path.

## 5. Phased Rollout

### Phase 1 — Add provider credential and default settings

**Goal**: `settings.DEFAULT_PAYMENT_PROVIDER`, `settings.STRIPE_PUBLISHABLE_KEY`, and `settings.MERCADOPAGO_PUBLIC_KEY` exist and are validated everywhere the app runs. No behavior change — nothing reads them yet.

**Feature flag**: none — pure configuration scaffolding, no reachable behavior.

Changes:

1. [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py#L582-L598): add `STRIPE_PUBLISHABLE_KEY` and `MERCADOPAGO_PUBLIC_KEY` (both `default=""`, alongside the existing secret-key block), and `DEFAULT_PAYMENT_PROVIDER = config("DEFAULT_PAYMENT_PROVIDER", default=PaymentProviders.STRIPE)`. Validate the value is a member of `PaymentProviders` at import and raise `ImproperlyConfigured` otherwise — a bad slug must fail the deploy, not every checkout.
2. Same file, `DEFAULT_THROTTLE_RATES`: add `"payment-provider": "120/min"` next to `payment-webhook`, with a comment explaining it covers the unauthenticated provider-credentials read (cheap, no outbound call, so a higher ceiling than the webhook scope).
3. @.env.example and @.env.docker.example: the three new vars, with comments matching the existing fail-closed style. Note explicitly that the publishable/public keys are browser-safe and intentionally *not* secrets.
4. @render.yaml: three entries in the payments `envVarGroups` block alongside `STRIPE_SECRET_KEY`.
5. @.github/workflows/main.yml: fake values for the three vars in all five job env blocks that already carry the payment vars.
6. @AGENTS.md: env-var section entries for all three.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `payments/tests/test_settings.py` — `DEFAULT_PAYMENT_PROVIDER` is a valid `PaymentProviders` member; an invalid value raises `ImproperlyConfigured`.

**Suggested AI model**: Tier 1 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). Mechanical config plumbing with exact precedent in the adjacent `STRIPE_SECRET_KEY` block.

**Reusable skills**: `add-env-var` (covers all six layers this phase touches).

Acceptance: `docker compose run --rm api uv run python manage.py check --deploy` passes with the three vars set, and fails with `ImproperlyConfigured` when `DEFAULT_PAYMENT_PROVIDER=nonsense`.

---

### Phase 2 — Pin the provider on the BillingProfile

**Goal**: an organization's provider is stored and set the first time a payment instrument is confirmed for it. Nothing reads the pin yet, so no charge changes path.

**Feature flag**: none — see **Guiding Decisions**.

Changes:

1. [payments/models.py](payments/models.py#L186-L209): add `BillingProfile.payment_provider` per **Data Model Changes**.
2. Migration adding the column; separate migration backfilling every existing row to `stripe` with a working reverse.
3. [payments/services/subscription_service.py](payments/services/subscription_service.py#L931-L968): `record_payment_method` writes `billing_profile.payment_provider = provider` when it is currently empty, in the same transaction as the `PaymentMethod` `get_or_create`. Never overwrites a non-empty value — an org that somehow gets a confirmed instrument at a second provider keeps its original pin, and the discrepancy is logged at `warning` so it surfaces rather than silently repointing future charges.
4. Same module: new `set_payment_provider(organization, provider)` — the staff repoint lever. Validates the slug against `PaymentProviders`, writes through `AuditService` like the other billing business writes (the audit entry records the previous provider), and refuses a provider that is absent from the payment registry. **No active-subscription guard** — the repoint succeeds even when the org holds a live subscription at the old provider; see the **Pin mutability** row in **Guiding Decisions**.
5. [payments/admin.py](payments/admin.py): surface `payment_provider` on the `BillingProfile` admin as an editable field routed through `set_payment_provider`, so an admin edit is audited rather than a bare `save()`.
6. New `PaymentProviderNotConfiguredError(PaymentError)` in [payments/exceptions.py](payments/exceptions.py#L53-L64), next to `UnknownPaymentProviderError`, with a docstring distinguishing the two: `Unknown` = slug is not a provider at all; `NotConfigured` = a real provider the deployment has no usable credentials for.

Spec use-case: "an org that started paying with a provider keeps that provider for future charges" — the persistence half.

Tests:
- **Unit**: `payments/tests/services/test_payment_services.py` — `record_payment_method` sets the pin on first call; a second call with a different provider leaves it unchanged and logs; `set_payment_provider` rejects an unknown slug, writes an audit entry naming the previous provider, and **succeeds even when the org holds an active subscription at the old provider** (asserting the deliberate absence of a guard, so a future reviewer doesn't "fix" it back in).
- **Integration**: `payments/tests/test_models.py` — the backfill migration sets every pre-existing profile to `stripe` and reverses cleanly.
- **Regression**: existing `payments/tests/` suite passes unchanged — nothing reads the new column yet, so every current charge path must behave identically.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). Field + two migrations + a guarded write in an existing service, all against established patterns.

**Reusable skills**: `add-model` (field + admin conventions); `add-migration` (backfill with a reverse path).

Acceptance: confirming an organization's first payment writes `stripe` (or whichever provider handled it) into `BillingProfile.payment_provider`, a second confirmation at a different provider does not change it, and every existing payments test passes untouched.

---

### Phase 3 — Provider credentials endpoints

**Goal**: a frontend can fetch the provider slug plus its public credentials, both for the system default (no session) and for the current organization.

**Feature flag**: none — brand-new endpoints at new paths; no existing code reads or writes them.

Changes:

1. New `payments/services/provider_credentials.py`: `PublicProviderCredentials` dataclass and a `resolve_public_credentials(provider: str) -> PublicProviderCredentials` function reading from settings, raising `PaymentProviderNotConfiguredError` when the matching key is empty. Reads settings directly, never the adapter — the adapter holds secrets, and this module must have no path to them.
2. New `PaymentProviderResolver` in [payments/services/payment_service.py](payments/services/payment_service.py) (or a small sibling module): `resolve_for_organization(organization) -> str` implementing pin → default, and `resolve_default() -> str`. Single place both endpoints and Phase 4's routing call, so the resolution rule cannot drift between read and write paths.
3. [payments/serializers.py](payments/serializers.py): `StripePublicCredentialsSerializer`, `MercadoPagoPublicCredentialsSerializer`, and `PaymentProviderSerializer` composing them as nullable nested fields plus the `provider` slug. Plain `serializers.Serializer` (no virtual model) — nothing here is DB-backed.
4. New `PaymentProviderViewSet` in [payments/views.py](payments/views.py): a `ViewSet` with a `list` action (`GET /billing/payment-provider/`, `IsAuthenticated` + `TenantScopedViewMixin`) and a `default` action (`GET /billing/payment-provider/default/`, `AllowAny`, `authentication_classes = ()`, `ScopedRateThrottle` scope `payment-provider`). `PaymentProviderNotConfiguredError` maps to 409 on the org action and 503 on the default action. `@extend_schema` on both, following the existing annotations in this module.
5. [payments/routes.py](payments/routes.py): register under `billing/payment-provider` with basename `BillingPaymentProvider`.
6. Regenerate `schema.yml`.

Spec use-case: "endpoint returning the default provider + public credentials" and "endpoint returning the org's provider + public credentials". Bundled per the agreed granularity — they share one serializer, one resolver, and one viewset, so splitting them would put ~20 LoC in the second PR.

Tests:
- **Unit**: `payments/tests/services/test_provider_credentials.py` — `resolve_public_credentials` returns only the matching provider's block; raises `PaymentProviderNotConfiguredError` on an empty key; `PaymentProviderResolver` returns the pin when set and the default when empty.
- **Integration**: `payments/tests/views/test_payment_provider_views.py` — default endpoint reachable with no credentials at all (200, correct slug); default endpoint 429s past the throttle; org endpoint returns the pin for a pinned org, the default for an unpinned one, and the default for an org with no `BillingProfile`; org endpoint 403s with no active organization; org endpoint 409s for an org pinned to a provider whose public key is unset; **no response from either endpoint contains any value from `STRIPE_SECRET_KEY`, `MERCADOPAGO_ACCESS_TOKEN`, or either webhook secret** — asserted explicitly, since this is the one way this phase could do real damage.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). Serializer + viewset + route wiring with dense precedent in `billing_views.py`, plus one small resolver.

**Reusable skills**: `create-rest-endpoint` (viewset, permissions, route registration, `schema.yml` regeneration).

Acceptance: `GET /billing/payment-provider/default/` returns `{"provider": "stripe", "stripe": {"publishable_key": ...}, "mercadopago": null}` without a session, and `GET /billing/payment-provider/` returns a pinned org's own provider — with the secret-leak assertion green.

---

### Phase 4 — Route provider calls through the resolved provider

**Goal**: every charge, refund, status check, and subscription operation goes through the provider that owns it, instead of the single hardcoded MercadoPago gateway. This is the phase that makes the pin mean something.

**Feature flag**: none — see **Guiding Decisions**. Rollback is a deploy revert.

Changes:

1. [payments/services/payment_service.py](payments/services/payment_service.py#L55-L87): drop the `payment_gateway` / `subscription_gateway` singular injections. The two registries stay and become the only adapter source. Update [di_core/containers.py](di_core/containers.py#L60-L125) to stop passing them to `PaymentService` — the standalone `payment_gateway` / `subscription_gateway` providers remain, since the registries are built from them.
2. Same file — **existing-row resolution**. Each of these resolves its adapter from the row's own stored provider, not from the org's pin:
   - `process_payment` and `check_payment_status` → `payment.payment_provider`
   - `create_refund` and `check_refund_status` → `refund.payment.payment_provider`
   - `process_subscription`, `change_subscription_plan`, `cancel_subscription` → `subscription.payment_provider`
   - `update_subscription_plan` → gains an explicit `provider: str` parameter; the sole caller in [payments/services/subscription_service.py](payments/services/subscription_service.py#L586) has the subscription in hand.
3. Same file — **new-row resolution**. `create_payment` and `create_subscription` resolve via `PaymentProviderResolver.resolve_for_organization(organization)` and stamp the result onto `Payment.payment_provider` / `Subscription.payment_provider`, replacing the current `self.payment_gateway.provider` / `self.subscription_gateway.provider`. `create_subscription_plan` gains an explicit `provider: str` parameter, passed by its caller at [subscription_service.py:586](payments/services/subscription_service.py#L586) from the subscription being created.
4. [payments/services/payment_service.py](payments/services/payment_service.py#L159-L179): `_serialize_payment` reads `payment.payment_provider` instead of `self.payment_gateway.provider` — the latent bug named in **Guiding Decisions**.
5. `get_payment_adapter` / `get_subscription_adapter` raise `PaymentProviderNotConfiguredError` (not a bare `KeyError`) when the slug is a real provider whose credentials are unset, keeping `UnknownPaymentProviderError` for genuinely unknown slugs. The webhook views' existing handling of `UnknownPaymentProviderError` must keep working unchanged.
6. [payments/services/cycle_close_service.py](payments/services/cycle_close_service.py#L256): confirm its `create_payment` call needs no signature change (it passes an organization, so resolution happens inside). Adjust if not.

Spec use-case: "an org that started paying with a provider keeps that provider for future charges" — the routing half.

Tests:
- **Unit**: `payments/tests/services/test_payment_services.py` — a payment row stamped `mercadopago` is refunded and status-checked through the MercadoPago adapter even when its org's pin says `stripe` (the single most important assertion in this phase); `create_payment` for a `stripe`-pinned org stamps and drives Stripe; an unpinned org drives `DEFAULT_PAYMENT_PROVIDER`; an org pinned to an unconfigured provider raises `PaymentProviderNotConfiguredError` and creates no `Payment` row.
- **Unit**: `payments/tests/services/test_provider_registry.py` — the DI wiring still resolves both registries after `PaymentService` stops taking the singular gateways.
- **Integration**: `payments/tests/views/test_billing_views.py` and `payments/tests/views/test_payment_webhooks.py` — existing coverage passes with the routing in place; add a webhook case where an org pinned to one provider receives a delivery for a payment made at the other, asserting the webhook resolves off the payment row.
- **Integration**: `payments/tests/services/test_plan_change.py` and `test_dunning_schedule.py` — plan-change and dunning flows drive the subscription's own provider.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](.claude/skills/plan-feature/resources/ai-models.yaml)). Multi-file refactor across ~15 call sites in two services with a per-call-site correctness rule (row provider vs org pin) that a mechanical find-replace gets wrong.

**Review models**: reviewer Tier 4 — this phase rewires every money-moving path in the codebase, and the failure mode it guards against (resolving a refund or status check through the wrong provider) is silent at the type level and only shows up against a live provider. The independent review runs on the most capable model. Fixer left on the project default.

Acceptance: with an org pinned to `stripe` and a historical `Payment` row stamped `mercadopago`, a new charge drives the Stripe adapter and a refund of the old payment drives the MercadoPago adapter, in the same test run; `grep -n "self.payment_gateway\|self.subscription_gateway" payments/services/payment_service.py` returns nothing.

## 6. Risk & Rollout Notes

**No feature flag** — a deliberate exception, justified in **Guiding Decisions**: no organization has a paid subscription, the repo has no flag framework, and the off-branch would guard a path with no traffic. Consequently there is **no dedicated flag-removal phase** in this plan. If paid organizations exist by the time Phase 4 is ready, revisit this decision before merging that phase — the argument is entirely contingent on the zero-paying-org fact, and it expires the day that changes.

**Migration safety.** `payments_billingprofile` holds one row per organization and is not on a hot query path. `AddField` with a string default takes a brief `ACCESS EXCLUSIVE` lock on a small table; no rewrite concern. The backfill is a separate migration so it can be re-run or reversed without touching the schema change. Both have working reverse operations.

**Deploy ordering.** Phase 1 must be deployed with real values for `STRIPE_PUBLISHABLE_KEY` and `MERCADOPAGO_PUBLIC_KEY` in Render **before** Phase 3 ships, or the default endpoint returns 503 to every caller. Phase 3's endpoints are read-only and additive, so they can ship well ahead of Phase 4. Phase 4 is the only phase that changes money-moving behavior.

**Rollback.**
- Phase 1: revert; nothing reads the settings.
- Phase 2: revert the code; the column can stay (nothing reads it) or be dropped via the reverse migration.
- Phase 3: revert; the endpoints disappear. Confirm no frontend has shipped against them first.
- Phase 4: revert the deploy. Rows already stamped with a provider stay correct, because the pre-Phase-4 code resolves everything through MercadoPago and the reverted code reads `payment_provider` off nothing. **Verify before rollback** that no Stripe-provider `Payment` or `Subscription` rows were created in the window — those rows would be unservicable by the reverted code, which would try to drive them through MercadoPago. This is the single genuinely irreversible risk in the plan and the reason Phase 4 carries a Tier 4 reviewer.

**Credential hygiene.** The one way this feature causes real harm is serializing a secret onto a public, unauthenticated response. Mitigated structurally: `provider_credentials.py` reads settings directly and has no reference to any adapter or secret setting, and Phase 3 carries an explicit test asserting no secret value appears in either response body.

## 7. Open Questions

| Question | Recommended default |
|---|---|
| Should the per-org endpoint also report *whether* the provider is pinned vs. defaulted (e.g. an `is_pinned` boolean)? | **No** for v1 — the frontend renders the same form either way, and exposing it invites the SPA to build provider-switching UI that the API does not support. Add it when a real consumer asks. |
| Does MercadoPago's checkout need more than a public key (e.g. `locale`, `site_id`)? | Unknown until someone builds the MercadoPago form. `PublicProviderCredentials` is a dataclass with per-provider fields, so adding one is a serializer field plus a `schema.yml` regeneration. Ship with the public key alone. |
| ~~Should `set_payment_provider` refuse to repoint an org with an active subscription at the old provider?~~ | **Resolved 2026-08-08: no guard.** Repoint is unconditional — see the **Pin mutability** row in **Guiding Decisions** for the rationale and the accepted tradeoff. Phase 2 implements no active-subscription check. |
| Who is allowed to call `set_payment_provider` — Django staff only, or a support role? | Django `is_staff` via admin for now. Revisit if a support tooling surface appears. |

## 8. Touch List

**Phase 1 — settings**
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py#L582-L598) — three new settings, one new throttle scope
- [.env.example](.env.example#L39-L45) — three vars
- [.env.docker.example](.env.docker.example#L37-L43) — three vars
- [render.yaml](render.yaml#L177-L183) — three envVarGroup entries
- [.github/workflows/main.yml](.github/workflows/main.yml#L96-L98) — three fake vars × five job env blocks
- [AGENTS.md](AGENTS.md) — env-var documentation
- @payments/tests/test_settings.py *(new)*

**Phase 2 — pin**
- [payments/models.py](payments/models.py#L186-L209) — `BillingProfile.payment_provider`
- @payments/migrations/00XX_billingprofile_payment_provider.py *(new)*
- @payments/migrations/00XX_backfill_billingprofile_payment_provider.py *(new)*
- [payments/services/subscription_service.py](payments/services/subscription_service.py#L931-L968) — `record_payment_method` pins; new `set_payment_provider`
- [payments/exceptions.py](payments/exceptions.py#L53-L64) — `PaymentProviderNotConfiguredError`
- [payments/admin.py](payments/admin.py) — audited `payment_provider` edit
- [payments/tests/services/test_payment_services.py](payments/tests/services/test_payment_services.py) — pin tests
- [payments/tests/test_models.py](payments/tests/test_models.py) — backfill test

**Phase 3 — endpoints**
- @payments/services/provider_credentials.py *(new)*
- [payments/services/payment_service.py](payments/services/payment_service.py) — `PaymentProviderResolver`
- [payments/serializers.py](payments/serializers.py) — three new serializers
- [payments/views.py](payments/views.py) — `PaymentProviderViewSet`
- [payments/routes.py](payments/routes.py#L12-L38) — route registration
- `schema.yml` — regenerated
- @payments/tests/views/test_payment_provider_views.py *(new)*
- @payments/tests/services/test_provider_credentials.py *(new)*

**Phase 4 — routing**
- [payments/services/payment_service.py](payments/services/payment_service.py#L55-L540) — registry-only resolution across every gateway call site
- [payments/services/subscription_service.py](payments/services/subscription_service.py#L586) — pass `provider` to plan create/update
- [payments/services/cycle_close_service.py](payments/services/cycle_close_service.py#L256) — verify `create_payment` call
- [di_core/containers.py](di_core/containers.py#L115-L125) — drop singular gateways from `PaymentService`
- [payments/tests/services/test_payment_services.py](payments/tests/services/test_payment_services.py) — routing tests
- [payments/tests/services/test_provider_registry.py](payments/tests/services/test_provider_registry.py) — DI wiring
- [payments/tests/views/test_payment_webhooks.py](payments/tests/views/test_payment_webhooks.py) — cross-provider webhook case
- [payments/tests/services/test_plan_change.py](payments/tests/services/test_plan_change.py), [payments/tests/test_dunning_schedule.py](payments/tests/test_dunning_schedule.py) — provider-aware flows
