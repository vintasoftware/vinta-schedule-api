# Billing Plans and Limits — Implementation Plan

Spec: @ai-plans/2026-07-18-BILLING_PLANS_AND_LIMITS_SPEC.md. This plan translates that spec into phases; it does not re-derive requirements. Where this plan closes one of the spec's **Open questions**, the resolution is recorded in **Guiding Decisions** below.

## 1. Goals

1. **Billing is owned by the organization, not the user.** `BillingProfile` is keyed on `Organization`, `Subscription` resolves to an organization, and the currently-dead seam between the payments module and the organization tree works and is tested.
2. **Every organization always has exactly one active plan**, from creation, with no plan-less state.
3. **No unmetered creation path exists** for any limited resource, across the internal REST surface, the public GraphQL API, and background sync and import paths.
4. **A free organization can self-serve upgrade end to end** — choose a paid plan, pay, and observe its limits lift, with no manual intervention.
5. **Post-paid event usage is metered per occurrence, idempotently**, and charges at cycle close reconcile against the provider with no unexplained drift.
6. **No organization is blocked as a consequence of the rollout itself.**

**Non-goals:**

- Invoice PDFs, tax documents, tax calculation.
- Refund and credit-note flows beyond what @payments/services/payment_service.py already exposes.
- Trials. Deferred to v1.1 — see **Open Questions**.
- Coupons and discounts. Deferred to v1.1 — see **Open Questions**.
- Historical usage analytics, trend charts, mid-cycle overage estimates. v1 shows current usage against current limits.
- Per-organization custom *pricing*. Per-organization limit *overrides* are in scope; the charged amount always derives from the catalog plan plus add-ons.
- Materializing recurring-event occurrences as rows. The computed-occurrence model in @calendar_integration/models.py stays as it is.
- Changing the tenancy contract — `OrganizationModel`, `OrganizationForeignKey`, the `X-Organization-Id` resolution in @common/utils/view_utils.py, and the tenant-safe queryset layer are hands-off.
- Changing existing resource-creation API request/response shapes. The only new behavior is an over-limit rejection.
- Retroactive billing for usage predating this work.
- Self-serve plan authoring. Plans are seeded and administered internally.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Billing ownership** | `BillingProfile` drops its `user` primary key (@payments/models.py:45) and becomes a `OneToOneField(Organization, primary_key=True)`. Both `organizations_organizationtier` and `organizations_subscriptionplan` are empty in every environment and no code assigns `Organization.tier`, so a destructive migration carries no data risk. This is the spec's named one-way door and it lands in Phase 1, before any money flows. |
| **Plan catalog shape** | A new `BillingPlan` in `payments`, with `PlanLimit` rows keyed `(plan, resource_key)` carrying `limit_value`, `kind` (prepaid/postpaid), and `overage_unit_price`. Rows rather than JSONB or per-resource columns because the limited set is broad and still moving, effective-limit resolution has to aggregate in SQL across a reseller subtree, and adding a limited resource should be a data row rather than a migration. |
| **Per-subscription limit copy** | On subscription creation or plan change, `PlanLimit` rows are copied into `SubscriptionPlanLimit`. Catalog edits do **not** propagate to existing subscriptions — an org keeps what it was sold, and a catalog typo cannot silently lower limits for everyone at once. Support fixes a stuck organization by raising its `SubscriptionPlanLimit` row, which is why there is no support-facing enforcement bypass. |
| **Limits overridable, price is not** | Admins may edit `SubscriptionPlanLimit`. The charged amount always comes from the catalog plan plus add-ons, keeping the spec's Negative-scope ban on bespoke pricing intact. |
| **Effective limit** | `SubscriptionPlanLimit.limit_value` + active add-on quantity, resolved at the reseller root. A child organization in a reseller tree never holds its own subscription; usage sums across the whole subtree against the root's pooled limit. |
| **Enforcement layer** | Service layer, not viewsets. `CalendarEvent` alone has six distinct entry points (REST at @calendar_integration/views.py:888, the token surface at @calendar_integration/token_views.py:154, three GraphQL mutations, and the bulk sync writer at @calendar_integration/services/calendar_sync_service.py:1168). Guarding viewsets would leave the sync path unmetered, which spec objective 1 forbids. |
| **Enforcement bypass** | An explicit `bypass_limits: bool = False` kwarg on the guarded service methods. Only management commands and one-off repair scripts pass `True`. Calendar sync and import stay **enforced** — they are named enforcement points in the spec. Rejected: bypassing by staff/superuser (a staff user in the normal UI would silently escape limits) and a thread-local context manager (ambient state that leaks across Celery task boundaries). |
| **No feature flag** | The project has no feature-flag module — no waffle, no django-flags, nothing. Rather than build one for this feature alone, the rollout switch is the plan itself: every organization is seeded onto an `unlimited` plan whose `PlanLimit` rows have `limit_value = NULL`, meaning no ceiling. Enforcement code runs from Phase 6 onward but cannot block anyone until an org is migrated onto a real plan, per-org and reversibly. This is the spec's own stated rollback ("place every organization on a plan with no limits"). **Consequence: this plan has no flag-removal phase.** Each enforcement phase instead carries a test asserting an org on the `unlimited` plan sees byte-for-byte pre-feature behavior. |
| **Occurrence metering** | Occurrences are computed, never stored (`calculate_recurring_events` and friends, wired in @calendar_integration/migrations/0004_add_recurring_models_db_functions.py). A Celery beat task sweeps each organization's elapsed window using those existing SQL functions and inserts `MeteredOccurrence` rows with a unique constraint on `(organization, event, occurrence_start)`. The constraint is what makes re-runs and overlapping windows idempotent — the spec requires an occurrence be billed at most once, ever. |
| **Pre-paid concurrency** | The count and the check must be inseparable. Each check takes a `SELECT ... FOR UPDATE` on the resolved billing-root `Subscription` row inside the caller's transaction, so two racing creates serialize on one row per organization. Scoped to the subscription row rather than the resource table to keep contention off hot paths. |
| **Second payment provider** | Stripe. The abstraction is shaped against MercadoPago (@payments/services/payment_adapters/mercadopago_payment_adapter.py) *and* Stripe together rather than against an imagined generic provider. Stripe's subscription, usage-metering, proration, and dunning primitives are the closest match to this spec, so generalizing toward the more capable provider is the right direction. |
| **Webhook authenticity** | The payments webhook endpoints at @payments/views.py:36 and @payments/views.py:61 currently have no `authentication_classes`, no `permission_classes`, and no signature verification — anyone can POST a payment-succeeded event. Since this feature makes organization state depend on those webhooks, provider signature verification plus a `ProviderWebhookEvent` idempotency table land in Phase 2, before anything reads them. |
| **Restricted state** | Blocks create/update/delete on `OrganizationModel` subclasses and pauses background calendar sync. Allows authentication, all reads, exports, and every billing action needed to resolve the payment. Already-scheduled events continue to fire. Reseller children follow the root into Restricted. |
| **Billing owner** | An `is_billing_owner` `BooleanField` on `OrganizationMembership`, mirroring the existing `is_active` precedent at @organizations/models.py:272. `OrganizationRole` stays a flat MEMBER/ADMIN enum so `is_admin`, `IsOrganizationAdmin`, `update_role`, and the last-active-admin guard are all untouched. |
| **Grace and proration** | Grace window is a per-plan field with one global settings default, tunable without a deploy. Downgrade takes no cash refund; the plan change applies at the next cycle boundary while the lower limits are enforced immediately behind a grace window. |
| **Notifications** | Approaching-limit warnings go through the existing in-app notification pipeline (`send_pending_notifications`, @vinta_schedule_api/celerybeat_schedule.py:10). The dunning ladder goes by email — a restricted org's billing owner may never log in. |

## 3. Data Model Changes

All new models live in `payments` and are **not** `OrganizationModel` subclasses. They hold a plain `organization` FK instead: the tenant-safe queryset layer at @organizations/querysets.py:53 raises `ImproperlyConfigured` unless `organization` appears in the WHERE clause, and billing code legitimately needs cross-organization reads (sweeping every subscription at cycle close, summing usage across a reseller subtree). Making them `OrganizationModel` would force `original_manager` escapes at nearly every call site. This is called out explicitly so a reviewer does not read it as an oversight.

### 3.1 `BillingProfile` — repointed to Organization

@payments/models.py:44. Replace the `user` primary key with:

```python
organization = models.OneToOneField(
    "organizations.Organization",
    primary_key=True,
    on_delete=models.CASCADE,
    related_name="billing_profile",
)
contact_first_name = models.CharField(max_length=255)
contact_last_name = models.CharField(max_length=255, blank=True)
contact_email = models.EmailField()
contact_phone = models.CharField(max_length=50, blank=True)
```

`contact_first_name` and `contact_email` are required; `contact_last_name` and `contact_phone` are optional. These four fields carry the payer identity the payment gateway needs (MercadoPago hard-400s on a null payer email) now that billing is organization-owned rather than user-owned — there is no longer a `User` to source a name/email/phone from. This is distinct from the `is_billing_owner` membership flag described under **Data Model Changes** and gated in Phase 9, which governs who may *manage* billing rather than what identity is sent to the gateway.

The `user` property on `Payment` (@payments/models.py:103) and `BillingAddress` (@payments/models.py:39) become `organization`. `BillingProfileSerializer` (@payments/serializers.py:56) and its `create`/`update` overrides at lines 81 and 92 resolve the organization from `request.organization` rather than `request.user`.

### 3.2 New `BillingPlan`

Supersedes `OrganizationTier` (@organizations/models.py:84, a bare `name`) and `SubscriptionPlan` (@organizations/models.py:172, whose `__str__` references a nonexistent `name` field).

```python
class BillingPlan(BaseModel):
    slug = models.SlugField(max_length=100, unique=True)   # "unlimited", "free", "pro"
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True, db_index=True)
    is_default_for_new_organizations = models.BooleanField(default=False)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    annual_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3)
    grace_period_days = models.PositiveIntegerField(null=True, blank=True)  # null -> settings default
```

A partial unique constraint enforces at most one row with `is_default_for_new_organizations=True`.

### 3.3 New `PlanLimit` and `PlanEntitlement`

```python
class PlanLimit(BaseModel):
    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    limit_value = models.PositiveIntegerField(null=True, blank=True)  # NULL == unlimited
    kind = models.CharField(max_length=20, choices=LimitKind)         # prepaid | postpaid
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    class Meta:
        constraints = [UniqueConstraint(fields=["plan", "resource_key"], name="uniq_plan_limit_resource")]


class PlanEntitlement(BaseModel):
    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="entitlements")
    entitlement_key = models.CharField(max_length=100, choices=Entitlement)
    is_enabled = models.BooleanField(default=False)
```

`LimitedResource`, `LimitKind`, and `Entitlement` are `TextChoices` in a new `payments/billing_constants.py`. `LimitedResource` members, per the closed set:

| `resource_key` | Kind | Counted from |
|---|---|---|
| `organization_members` | prepaid | active `OrganizationMembership` + pending `OrganizationInvitation` |
| `resource_calendars` | prepaid | `Calendar` where type is a resource/room |
| `calendar_groups` | prepaid | `CalendarGroup` |
| `bundle_calendars` | prepaid | bundle-type `Calendar` |
| `availability_windows` | prepaid | availability-window rows |
| `webhook_subscriptions` | prepaid | `webhooks` subscription rows |
| `public_api_system_users` | prepaid | `public_api` system users |
| `event_occurrences` | postpaid | `MeteredOccurrence` in the current cycle |

`Entitlement` members: `external_calendar_google`, `external_calendar_microsoft`, `partner_api`, `white_label_branding`, `advanced_scheduling`.

Counting pending invitations toward the seat limit is deliberate: without it an org can hold unlimited outstanding invitations and blow past its seat ceiling the moment they are accepted.

### 3.4 `Subscription` — repaired and org-linked

@payments/models.py:58. `membership: "SubscriptionPlan"` at line 69 is a bare type annotation with no field and no migration, so `Subscription.plan` (line 74) raises `AttributeError` — which the `except ObjectDoesNotExist` at line 78 does not catch. Replace with real fields:

```python
organization = models.OneToOneField(
    "organizations.Organization", on_delete=models.CASCADE, related_name="subscription"
)
plan = models.ForeignKey(BillingPlan, on_delete=models.PROTECT, related_name="subscriptions")
billing_state = models.CharField(max_length=20, choices=BillingState, default=BillingState.FREE, db_index=True)
billing_interval = models.CharField(max_length=10, choices=BillingInterval, default=BillingInterval.MONTHLY)
current_period_start = models.DateTimeField()
current_period_end = models.DateTimeField(db_index=True)
grace_period_ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
plan_external_id = models.CharField(max_length=255, blank=True)
payment_provider = models.CharField(max_length=50, choices=PaymentProviders)
```

`external_id` (line 62) gains `db_index=True` — @payments/services/payment_service.py:211 filters on it today with no index.

`BillingState`: `FREE`, `ACTIVE`, `GRACE`, `RESTRICTED`, `CANCELLED`. The spec's lifecycle diagram is the authority on the transitions.

`OrganizationTier` and `SubscriptionPlan` in `organizations` are deleted in Phase 3; `Organization.tier` (@organizations/models.py:101) goes with them. Its only read site is @organizations/graphql.py:15.

### 3.5 New `SubscriptionPlanLimit`

The per-subscription copy. Same shape as `PlanLimit` plus `subscription` FK and `is_overridden`:

```python
class SubscriptionPlanLimit(BaseModel):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=LimitKind)
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    is_overridden = models.BooleanField(default=False)

    class Meta:
        constraints = [UniqueConstraint(fields=["subscription", "resource_key"], name="uniq_sub_limit_resource")]
```

`SubscriptionEntitlement` mirrors this for booleans.

### 3.6 New `SubscriptionAddOn`

```python
class SubscriptionAddOn(BaseModel):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="add_ons")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    quantity = models.PositiveIntegerField()
    is_recurring = models.BooleanField()
    is_active = models.BooleanField(default=True, db_index=True)
    external_id = models.CharField(max_length=255, blank=True)
    purchase_idempotency_key = models.CharField(max_length=255, unique=True)
```

`purchase_idempotency_key` unique is what makes a retried purchase neither grant capacity twice nor charge twice.

### 3.7 New `MeteredOccurrence`

```python
class MeteredOccurrence(BaseModel):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE)
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="metered_occurrences")
    event_id = models.BigIntegerField()
    occurrence_start = models.DateTimeField()
    billing_period_start = models.DateTimeField(db_index=True)
    is_within_allowance = models.BooleanField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["organization", "event_id", "occurrence_start"],
                name="uniq_metered_occurrence",
            )
        ]
        indexes = [models.Index(fields=["subscription", "billing_period_start"])]
```

`event_id` is a soft reference (`BigIntegerField`, not an FK) on purpose: deleting an event must not delete the record that it was billed. An occurrence is billed at most once, ever, and that has to survive the event's deletion.

### 3.8 New `ProviderWebhookEvent`

```python
class ProviderWebhookEvent(BaseModel):
    provider = models.CharField(max_length=50, choices=PaymentProviders)
    external_event_id = models.CharField(max_length=255)
    payload = models.JSONField()
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["provider", "external_event_id"], name="uniq_provider_webhook_event")
        ]
```

### 3.9 `OrganizationMembership.is_billing_owner`

@organizations/models.py:226 gains `is_billing_owner = models.BooleanField(default=False, db_default=False, db_index=True)`, exactly mirroring the `is_active` field at line 272 and its migration `0005_organizationmembership_is_active.py`.

### 3.10 Type plumbing

New `payments/services/billing_dataclasses.py`: `EffectiveLimit(resource_key, limit_value, kind, current_usage, overage_unit_price)`, `UsageSnapshot(organization_id, limits: list[EffectiveLimit], billing_state)`, `LimitCheckResult(allowed, resource_key, current_usage, ceiling, remedy)`. New exception `OverLimitError` in `payments/exceptions.py` carrying those four fields so every surface renders the same structured error.

The `Plan` / `CreatedPlan` dataclasses at @payments/services/dataclasses.py gain `interval` and `trial_days` fields for the Stripe adapter.

## 4. API Design

### 4.1 Over-limit error — the shared contract

Every guarded surface raises `OverLimitError`, rendered identically by REST and GraphQL. HTTP 402 Payment Required rather than 403, so clients can distinguish "you may not" from "you have run out".

```json
{
  "detail": "Organization is at its limit for organization members.",
  "code": "limit_exceeded",
  "resource": "organization_members",
  "current_usage": 10,
  "limit": 10,
  "remedy": "purchase_add_on"
}
```

`remedy` is one of `purchase_add_on`, `upgrade_plan`, `add_payment_method`, `resolve_billing`. A DRF exception handler entry plus a Strawberry error extension keep the two surfaces byte-identical.

### 4.2 Billing endpoints (internal REST)

Registered in a new `payments/routes.py` block alongside the existing `Payments` and `BillingProfile` basenames.

| Method / path | Purpose |
|---|---|
| `GET /billing/usage/` | Current usage against effective limits, per resource, plus `billing_state`. Use-case 8. |
| `GET /billing/plans/` | Active catalog plans with limits and entitlements. |
| `GET /billing/subscription/` | The org's subscription: plan, state, period, add-ons. |
| `POST /billing/subscription/change-plan/` | Upgrade or downgrade. Body `{plan_slug, billing_interval, idempotency_key}`. |
| `POST /billing/add-ons/` | Purchase capacity. Body `{resource_key, quantity, is_recurring, idempotency_key}`. |
| `DELETE /billing/add-ons/{id}/` | Cancel a recurring add-on at period end. |
| `POST /billing/subscription/cancel/` | Cancel; runs to end of paid cycle. |

Purchase actions require an active membership that is admin **or** `is_billing_owner`, or an acting reseller root — a new `IsBillingOwnerOrAdmin` permission in @organizations/permissions.py alongside `IsOrganizationAdmin` at line 91.

### 4.3 Webhook endpoints — hardened

@payments/views.py:36 and :61 keep their paths and response shapes. They gain `authentication_classes = ()` made explicit, a provider signature check, and idempotent recording into `ProviderWebhookEvent` before dispatch. The `provider` URL kwarg — captured but never read today, since the adapter resolves statically from DI — starts selecting the adapter.

## 5. Phased Rollout

Phases are bundled by closely-related use-case per the granularity decision. No phase declares a feature flag; the `unlimited` plan is the switch, so every enforcement phase instead ships a test asserting an org on the `unlimited` plan behaves exactly as before.

---

### Phase 1 — Move billing ownership to the organization

**Goal**: `BillingProfile` and `Subscription` belong to an organization, and the dead code between payments and organizations executes. Ship value: none user-visible; this is the one-way door the spec says must land before money flows.

**Feature flag**: none — no reachable behavior change. The billing-profile REST surface is the only live consumer and it moves with the model.

Changes:
1. @payments/models.py: `BillingProfile.user` → `organization` OneToOne primary key. `BillingAddress.user` property (line 39) and `Payment.user` property (line 103) become `organization`.
2. @payments/models.py:58: replace the `membership` annotation with the real `organization`, `plan`, `billing_state`, `billing_interval`, period, grace, `plan_external_id`, and `payment_provider` fields from **Data Model Changes**. Index `external_id`.
3. Delete the broken `Subscription.plan` property (line 74) — `plan` is now a field.
4. @payments/models.py:143: `RefundStatusUpdate.status` uses `PaymentStatuses`; correct to `RefundStatuses`. Its `__str__` at line 150 references `self.payment`, which does not exist — fix to `self.refund`.
5. Fix `from venv import logger` at @payments/services/payment_service.py:5 and @payments/services/payment_adapters/base.py:3 to the stdlib `logging`.
6. @payments/serializers.py:56: `BillingProfileSerializer` and its `create`/`update` resolve the org from `request.organization`.
7. @payments/views.py:74: `BillingProfileViewSet` mixes in `TenantScopedViewMixin`.
8. Migration: destructive rebuild of `payments_billingprofile` and `payments_subscription`. Both are empty in every environment. Reverse path recreates the prior schema.
9. Register `payments` models in a new `payments/admin.py`.

Spec use-case: shared foundation — the spec's Risks-assumed entry requiring the org-to-subscription link be repaired and verified independently before anything is built on it.

Tests:
- **Unit**: `payments/tests/test_models.py` — `BillingProfile` is reachable as `organization.billing_profile`; `Subscription.plan` resolves; `RefundStatusUpdate.__str__` does not raise.
- **Integration**: `payments/tests/views/test_billing_profile_view_set.py` — existing tests updated for org scoping; a member of org A cannot read org B's billing profile.
- **Migration**: forward and reverse both apply cleanly on an empty database.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Destructive migration on a primary key plus a serializer/viewset re-scope across several files.

**Review models**: reviewer Tier 4 — this phase is the spec's named one-way door. A missed reference to the old `user` PK, or a reverse migration that does not actually reverse, is expensive to discover later.

**Reusable skills**: `add-migration`, `add-model`.

Acceptance: `Organization.objects.create(...).billing_profile` round-trips, `Subscription.plan` returns a `BillingPlan` without raising, and the full `payments` test suite is green.

---

### Phase 2 — Authenticate provider webhooks and make them idempotent

**Goal**: a forged POST can no longer move billing state, and the same provider event delivered twice produces the same final state.

**Feature flag**: none — hardening an existing endpoint, no new gated path.

Changes:
1. New `ProviderWebhookEvent` model + migration.
2. @payments/services/payment_adapters/base.py:13 and @payments/services/subscription_adapters/base.py:18 gain `verify_signature(raw_body, headers) -> bool` and `get_event_id(payload) -> str`.
3. @payments/services/payment_adapters/mercadopago_payment_adapter.py: implement MercadoPago's `x-signature` HMAC check. New `MERCADOPAGO_WEBHOOK_SECRET` setting.
4. @payments/views.py:21: `PaymentsViewSet` gains explicit empty `authentication_classes`, a signature check that 403s on failure, and a `get_or_create` on `ProviderWebhookEvent` that short-circuits with 200 when `processed_at` is already set.
5. The `provider` URL kwarg starts selecting the adapter through a new `payment_provider_registry` DI provider in @di_core/containers.py:53, replacing the hardcoded MercadoPago factories at lines 59-66.
6. @payments/services/payment_adapters/mercadopago_payment_adapter.py:19-22 and @payments/services/subscription_adapters/mercadopago_subscription_adapter.py:22: fill the four empty mapping dicts and actually reference `PAYMENT_STATUS_MAPPING` in `check_status` (line 91) and `SUBSCRIPTION_STATUS_MAPPING` in `create_status_update_from_payment_payload` (line 223), which today write raw provider strings into `choices`-constrained columns.

Spec use-case: shared foundation, supporting the spec's **Idempotency** rule that a provider event delivered more than once produces the same final state.

Tests:
- **Unit**: `payments/tests/services/payment_adapters/test_mercadopago_payment_adapter.py` — signature verification accepts a correctly-signed body and rejects a tampered one; every provider status maps to a valid `PaymentStatuses` member.
- **Integration**: `payments/tests/views/test_payment_webhooks.py` — unsigned POST is rejected; the same signed event posted twice creates one `ProviderWebhookEvent` and runs the handler once.

**Suggested AI model**: Tier 3. HMAC verification plus DI-container restructuring for provider selection.

**Review models**: reviewer Tier 4 — a signature check that verifies the wrong bytes looks correct and passes tests while remaining forgeable.

**Reusable skills**: `add-model`, `add-migration`, `add-env-var` (for `MERCADOPAGO_WEBHOOK_SECRET` and, in Phase 2b, the Stripe keys).

Acceptance: a POST to `payment-update` without a valid signature returns 403 and creates nothing; a valid event delivered twice leaves exactly one `ProviderWebhookEvent` with one status update.

---

### Phase 2b — Stripe adapter behind the provider abstraction *(parallel track)*

**Goal**: a second provider exists, so the abstraction is validated against reality rather than imagination. Runs alongside Phases 3-8; it does not block them.

Ordered as a parallel track because it carries the slowest external dependency in this plan — a Stripe account, sandbox credentials, and product/price objects configured on the Stripe side. Starting it early means that setup is not discovered on the critical path.

**Feature flag**: none — a new adapter that no organization is routed to until Phase 9.

Changes:
1. New `payments/services/payment_adapters/stripe_payment_adapter.py` and `payments/services/subscription_adapters/stripe_subscription_adapter.py`, implementing the full base interfaces.
2. Where the Stripe shape does not fit the existing MercadoPago-derived interface, **change the interface**, then update the MercadoPago adapter to match. Specifically expected: `create_subscription_plan` needs an interval, and refunds are provider-side objects rather than status polls.
3. Stripe webhook signature verification via `stripe.Webhook.construct_event`.
4. `stripe` dependency added; `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` settings.
5. Register in the `payment_provider_registry` from Phase 2.

Spec use-case: shared foundation, closing the spec's Risks-assumed entry on provider-agnosticism.

Tests:
- **Unit**: `payments/tests/services/subscription_adapters/test_stripe_subscription_adapter.py` — plan creation, subscription creation, cancellation, and webhook parsing against recorded Stripe fixtures.
- **Integration**: `payments/tests/services/test_provider_registry.py` — both providers satisfy the same interface; a parametrized suite runs the same assertions against each.

**Suggested AI model**: Tier 3. New adapter against a documented SDK, with interface reshaping that ripples into the MercadoPago implementation.

**Reusable skills**: `add-env-var`.

Acceptance: the parametrized adapter conformance suite passes for both `mercadopago` and `stripe`, and no interface method exists that only one provider can implement.

---

### Phase 3 — Plan catalog, limits, and entitlements

**Goal**: plans, their limits, and their entitlements exist as queryable data, with the `unlimited` and `free` plans seeded. Ship value: none user-visible; this is the catalog everything else reads.

**Feature flag**: none — new tables nothing yet reads.

Changes:
1. New `payments/billing_constants.py`: `LimitedResource`, `LimitKind`, `Entitlement`, `BillingState`, `BillingInterval` as `TextChoices`.
2. New models `BillingPlan`, `PlanLimit`, `PlanEntitlement` + migration.
3. Delete `OrganizationTier` (@organizations/models.py:84), `SubscriptionPlan` (@organizations/models.py:172), and `Organization.tier` (@organizations/models.py:101). Remove the tier read at @organizations/graphql.py:15. Delete @organizations/organization_subscription_plan_factory.py, whose `SubscriptionPlan.objects.filter(subscription=...)` at line 11 and `subscription_plan.plan_external_id` at line 25 both reference fields that have never existed.
4. Replace the `subscription_plan_factory` DI registration at @di_core/containers.py:68 with a `BillingPlanFactory` that reads `BillingPlan` and satisfies the `make_plan_from_subscription` protocol at @payments/services/subscription_plan_factory/base.py:12 — the name the `PaymentService` actually calls at @payments/services/payment_service.py:252, which the deleted class never implemented.
5. Data migration seeding two plans: `unlimited` (every `PlanLimit` with `limit_value=NULL`, every entitlement enabled, price 0, `is_default_for_new_organizations=True`) and `free` (real ceilings, `event_occurrences` postpaid with an allowance, restricted entitlements).
6. Admin registration for all three models, with `PlanLimit` and `PlanEntitlement` as inlines.
7. Clean up the now-dangling test references at @organizations/tests/test_views.py:22 and :56.

Spec use-case: shared scaffolding — the catalog behind every use-case.

Tests:
- **Unit**: `payments/tests/test_billing_plan.py` — the partial unique constraint permits exactly one default plan; `PlanLimit` rejects a duplicate `resource_key` per plan.
- **Integration**: `payments/tests/test_plan_seed_migration.py` — after migrating, `unlimited` exists with a NULL-valued `PlanLimit` for every `LimitedResource` member. This test is what guarantees a new limited resource added later is not silently missing from the rollback plan.

**Suggested AI model**: Tier 3. Three new models plus deletion of two models with cross-app references and a seeding data migration.

**Reusable skills**: `add-model`, `add-migration`.

Acceptance: `BillingPlan.objects.get(slug="unlimited").limits.count()` equals `len(LimitedResource.choices)` and every one has `limit_value` NULL; `grep -r "OrganizationTier\|organizations.SubscriptionPlan"` returns nothing outside migrations.

---

### Phase 4 — Place every organization on a plan

**Goal**: every organization, existing and new, has exactly one `Subscription` with its own copy of the plan's limits. There is no plan-less state.

**Feature flag**: none. Every org lands on `unlimited`, so no behavior changes.

Changes:
1. New models `SubscriptionPlanLimit`, `SubscriptionEntitlement` + migration.
2. New `payments/services/subscription_service.py` with `create_subscription_for_organization(organization, plan)` — creates the `Subscription` and copies `PlanLimit`/`PlanEntitlement` rows into the subscription-scoped tables — and `change_plan(subscription, plan)`, which re-copies non-overridden rows. Registered in @di_core/containers.py.
3. Wire the free-plan hook into **both** organization creation paths: `OrganizationService.create_organization` at @organizations/services.py:89, and the raw insert in the reseller GraphQL mutation at @public_api/mutations.py:887, which bypasses the service entirely and creates no membership, audit record, or webhook.
4. Reseller children get **no** subscription — `resolve_billing_root(organization)` walks `parent` (@organizations/models.py:120) to the nearest `can_invite_organizations=True` ancestor, mirroring the cycle-guarded walk in `get_branding_root` at @organizations/models.py:155.
5. Data migration placing every existing organization on `unlimited`.
6. Admin: `SubscriptionPlanLimit` inline on `Subscription`, editable, setting `is_overridden=True` on save. This is the support lever that replaces an enforcement bypass.
7. `is_billing_owner` on @organizations/models.py:226 + migration, following the `is_active` precedent at line 272.

Spec use-case: **Use-case 1 — New organization lands on the free plan.**

Tests:
- **Unit**: `payments/tests/services/test_subscription_service.py` — plan-change re-copies non-overridden limits and leaves `is_overridden=True` rows untouched; catalog edits do not propagate to an existing subscription.
- **Integration**: `organizations/tests/test_organization_creation_billing.py` — an org created via REST, via `provision_tenant_for_user`, and via the reseller GraphQL mutation each end up with a subscription; a reseller child ends up with none and resolves to its root.
- **Migration**: every pre-existing organization has a subscription on `unlimited` afterward.

**Suggested AI model**: Tier 3. Multi-file: two models, a new service, two creation-path call sites, a backfill migration, and a membership field.

**Review models**: reviewer Tier 4 — the reseller-root walk is a cycle-prone tree traversal on user-mutable data, and the two divergent org-creation paths are exactly the kind of thing that leaves half the orgs plan-less.

**Reusable skills**: `add-model`, `add-migration`.

Acceptance: `Organization.objects.filter(billing_root_filter(), subscription__isnull=True).count() == 0` (`billing_root_filter` from `payments.services.subscription_service` — a nested reseller is its own billing root too, not just a parent-less organization), and an org created through any of the three paths is on the default plan.

---

### Phase 5 — Effective limits and usage counting

**Goal**: the system can answer, for any organization and resource, what the ceiling is and what the current usage is — pooled at the reseller root. Ship value: none on its own; this is the engine every enforcement phase calls.

**Feature flag**: none — a read-only service nothing yet calls.

Changes:
1. New `payments/services/entitlement_service.py`:
   - `resolve_billing_root(organization)` — **already lands in Phase 4**, in `payments/services/subscription_service.py`, alongside `is_billing_root(organization)` and `billing_root_filter()` (the single "is a billing root" predicate, used by the Phase 4 backfill migration and the creation-path service). Phase 5 imports it from there rather than re-implementing it here — this bullet is stale as an implementation instruction (kept for traceability to the original phase split) but not as a dependency: `entitlement_service.py` still needs `resolve_billing_root` in scope, just via `from payments.services.subscription_service import resolve_billing_root`.
   - `get_effective_limit(organization, resource_key)` — the subscription's `SubscriptionPlanLimit` plus active `SubscriptionAddOn` quantity. NULL stays NULL (unlimited).
   - `get_current_usage(organization, resource_key)` — point-in-time count summed across the **whole reseller subtree**, one counter function per `LimitedResource` member, registered in a dict keyed by resource.
   - `check_limit(organization, resource_key, delta=1, lock=False)` → `LimitCheckResult`. With `lock=True`, takes `SELECT ... FOR UPDATE` on the root `Subscription` row.
   - `has_entitlement(organization, entitlement_key)`.
2. New `SubscriptionAddOn` model + migration.
3. New `OverLimitError` in `payments/exceptions.py` plus the DRF handler entry rendering the **Over-limit error** contract.
4. Registered in @di_core/containers.py.

Spec use-case: shared scaffolding — the **Effective limit** and **Counting** rules under the spec's *State transitions and edge cases*.

Tests:
- **Unit**: `payments/tests/services/test_entitlement_service.py` — effective limit is plan + add-ons; NULL is unlimited; a resource with no `SubscriptionPlanLimit` row is treated as unlimited, not zero (fail-open, so a missing seed row cannot lock an org out).
- **Integration**: `payments/tests/services/test_pooled_limits.py` — a three-level reseller tree sums usage across all descendants against the root's ceiling; a cyclic `parent` chain terminates rather than recursing forever.
- **Concurrency**: `payments/tests/services/test_limit_concurrency.py` — two threads calling `check_limit(lock=True)` for the last unit against a real database serialize, and exactly one sees capacity.

**Suggested AI model**: Tier 4. Pooled resolution over a user-mutable tree plus row-level locking semantics that have to be correct under real concurrency.

**Review models**: reviewer Tier 4, fixer Tier 3 — every enforcement phase in this plan is only as correct as this service, and a lock taken on the wrong row silently permits overshoot.

**Reusable skills**: `add-model`, `add-migration`.

Acceptance: for a reseller root with two children each holding 3 members against a pooled limit of 5, `check_limit(child_a, "organization_members")` returns `allowed=False` with `current_usage=6, ceiling=5`.

---

### Phase 6a — Enforce pre-paid limits: seats and invitations

**Goal**: an organization at its seat limit cannot add another member, through any surface, and is told exactly why.

**Feature flag**: none — the `unlimited` plan is the switch. A test asserts an `unlimited` org sees no change.

Changes:
1. @organizations/services.py:239 `invite_user_to_organization` and :368 `accept_invitation` and :487 (the invite branch of `provision_tenant_for_user`) call `check_limit(org, "organization_members", lock=True)` inside their existing `transaction.atomic` blocks and raise `OverLimitError`.
2. @organizations/views.py:742 `reactivate` is also guarded — it raises the seat count without being a create, and would otherwise be an unmetered path.
3. All guarded methods accept `bypass_limits: bool = False`.
4. Strawberry error extension rendering `OverLimitError` identically for @public_api/mutations.py:901 `create_invitation`.

Spec use-case: **Use-case 2 — Organization hits a pre-paid limit and is blocked**, and **Use-case 7** for the GraphQL path.

Tests:
- **Unit**: `organizations/tests/services/test_invitation_limits.py` — at the limit, the invite raises and no `OrganizationInvitation` row is created; pending invitations count toward the ceiling.
- **Integration**: `organizations/tests/test_seat_enforcement.py` — blocked identically through REST, through `accept_invitation`, and through the GraphQL mutation, with byte-identical error bodies. Reactivation is blocked at the limit. **Unlimited-plan test**: an org on `unlimited` invites past every threshold with no change in behavior or query count.
- **Concurrency**: two simultaneous invites for the last seat — exactly one succeeds, and the org never exceeds its limit.

**Suggested AI model**: Tier 3. Several call sites across a service, a viewset action, and a GraphQL mutation, with transactional semantics.

**Reusable skills**: `create-rest-endpoint` (error contract), `create-graphql-public-query` (error extension).

Acceptance: acceptance scenarios 6 and 7 in the spec pass as automated tests; an org on `unlimited` is never blocked.

---

### Phase 6b — Enforce pre-paid limits: calendars, groups, bundles, availability

**Goal**: rooms and resource calendars, calendar groups, bundle calendars, and availability windows all block at their effective limit, including through bulk sync and import.

**Feature flag**: none — `unlimited` plan is the switch.

Changes:
1. @calendar_integration/services/calendar_service.py: guard `create_resource_calendar` (:759), `create_virtual_calendar` (:715), `create_application_calendar` (:604), `create_calendar` (:818).
2. @calendar_integration/services/calendar_group_service.py:221 `create_group`.
3. @calendar_integration/services/calendar_bundle_service.py — bundle calendar creation.
4. Availability-window creation service.
5. **Bulk sync paths**, which are the ones a request-scoped guard would miss: @calendar_integration/services/calendar_sync_service.py:267 (`Calendar.objects.update_or_create` for imported rooms) and :354 (imported account calendars). These check remaining headroom *before* the bulk write and import up to the ceiling, recording a partial-import warning rather than failing the whole sync — the spec accepts partial failure over unmetered creation.
6. GraphQL: @public_api/mutations.py:1401 `create_resource_calendar`, :1432 `create_calendar`, and especially :1583 `import_resource_calendars`, the bulk path.

Spec use-case: **Use-case 2**, plus the sync and import enforcement point named in the spec's *Enforcement points*.

Tests:
- **Unit**: `calendar_integration/tests/services/test_calendar_limits.py` — each guarded service method raises at its ceiling and creates nothing.
- **Integration**: `calendar_integration/tests/test_sync_limit_enforcement.py` — an org with headroom for 2 rooms importing 10 gets exactly 2 and a recorded warning, and re-running the import is not double-counted. **Unlimited-plan test**: an `unlimited` org's full sync imports everything, unchanged.

**Suggested AI model**: Tier 3. Many call sites, with the bulk-path partial-import semantics being the non-mechanical part.

**Review models**: reviewer Tier 4 — the bulk sync writers at @calendar_integration/services/calendar_sync_service.py:1164-1182 are the single most likely place for an unmetered path to survive, and objective 1 depends on them.

**Reusable skills**: none — service-layer edits against existing patterns.

Acceptance: every creation path listed above rejects at the ceiling; a sync into an org with no headroom creates zero calendars and does not raise to the caller.

---

### Phase 6c — Enforce pre-paid limits: webhook subscriptions and API system users

**Goal**: the last two pre-paid resources are guarded, closing the "no unmetered path" objective for pre-paid resources.

**Feature flag**: none — `unlimited` plan is the switch.

Changes:
1. Webhook subscription creation in `webhooks/` guarded on `webhook_subscriptions`.
2. Public API system user creation in `public_api/` guarded on `public_api_system_users`.
3. Entitlement gates, which are boolean rather than numeric: `partner_api` checked in @public_api/middlewares.py:18 `PublicApiSystemUserMiddleware` so an org without the entitlement cannot use the GraphQL API at all; `external_calendar_google` / `external_calendar_microsoft` checked at @calendar_integration/services/calendar_service.py:393 `authenticate`, the common chokepoint both connection paths flow through; `white_label_branding` checked in `OrganizationBranding` resolution at @organizations/models.py:532.

Spec use-case: **Use-case 2** and **Use-case 7**, plus the **Feature entitlements** rule.

Tests:
- **Unit**: per-resource limit tests for both resources.
- **Integration**: `public_api/tests/test_entitlement_gates.py` — an org without `partner_api` gets a structured 402 from every GraphQL operation; an org without `external_calendar_google` cannot connect a Google account. **Unlimited-plan test**: all entitlements enabled, every path open.

**Suggested AI model**: Tier 2. Applies the pattern Phases 6a and 6b establish; the entitlement gates are thin checks at three named chokepoints.

**Reusable skills**: `create-graphql-public-query` (permission wiring conventions).

Acceptance: every member of `LimitedResource` with `kind=prepaid` has a guarded creation path with a test proving it blocks — the closing condition for spec objective 1 on pre-paid resources.

---

### Phase 7 — Meter event occurrences

**Goal**: every event occurrence that happens is recorded exactly once, ever, in the cycle it falls in. Ship value: none user-visible; this is the counter behind post-paid billing.

**Feature flag**: none — a write-only meter nothing yet enforces or charges against.

Changes:
1. New `MeteredOccurrence` model + migration, with the `(organization, event_id, occurrence_start)` unique constraint.
2. New `payments/services/metering_service.py`: `meter_occurrences_for_period(subscription, window_start, window_end)` expands occurrences via the existing `get_occurrences_in_range` path at @calendar_integration/models.py:1172 — which annotates through the `calculate_recurring_events` SQL functions — and `bulk_create(..., ignore_conflicts=True)` into `MeteredOccurrence`. The unique constraint is what makes re-running a window, or running overlapping windows, harmless.
3. New Celery beat task `meter_event_occurrences` in @vinta_schedule_api/celerybeat_schedule.py, sweeping a window that deliberately **overlaps** the previous run so a missed run self-heals on the next pass.
4. `is_within_allowance` and `unit_price` are stamped at meter time against the effective limit, so a later limit change does not retroactively reprice already-metered occurrences.
5. New `reconcile_period(subscription, period)` recomputing occurrences for a closed period and reporting any drift against what was metered — the spec's named mitigation for silent revenue drift.

Spec use-case: **Use-case 4** (metering half), and the spec's *Counting* rule that an open-ended weekly series contributes roughly four occurrences per cycle rather than an unbounded charge at creation.

Tests:
- **Unit**: `payments/tests/services/test_metering_service.py` — an open-ended weekly series meters ~4 occurrences in a month, not infinitely; running the same window twice produces the same row count; overlapping windows do not double-count.
- **Integration**: `payments/tests/test_metering_reconciliation.py` — after metering a full period, `reconcile_period` reports zero drift. A recurring exception (@calendar_integration/services/calendar_event_service.py:1119) and a bulk-modification continuation (:1958) are each counted once, not twice.

**Suggested AI model**: Tier 4. This is the spec's highest-severity risk — silent revenue drift or overcharging — and it sits on computed occurrences, exception rows, and bulk-modification continuations interacting.

**Review models**: reviewer Tier 4 — a double-count here is invisible until a customer disputes a bill.

**Reusable skills**: `add-model`, `add-migration`.

Acceptance: acceptance scenario 5 in the spec passes; metering a period twice yields identical `MeteredOccurrence` counts; `reconcile_period` reports zero drift for a closed period.

---

### Phase 8 — Enforce the post-paid allowance

**Goal**: an organization with a payment method accrues past its included allowance and is never interrupted; one without a payment method is blocked at the allowance.

**Feature flag**: none — `unlimited` has a NULL `event_occurrences` limit, so nobody is blocked until migrated.

Changes:
1. `has_payment_method(organization)` on the entitlement service — resolved at the billing root.
2. Event creation paths call `check_postpaid_allowance`: block with `remedy="add_payment_method"` when at the allowance with no payment method; allow and let it accrue otherwise. Call sites: @calendar_integration/services/calendar_event_service.py:406 `create_event`, :1071 `create_recurring_event`, :1119 `create_recurring_event_exception`, @calendar_integration/services/calendar_group_service.py:539 `create_grouped_event`, @calendar_integration/services/calendar_bundle_service.py:386 `create_bundle_event` (which fans out one row per member calendar and so must check the fan-out count, not 1).
3. Guard the surfaces that reach those services: @calendar_integration/views.py:888, @calendar_integration/token_views.py:154, @public_api/mutations.py:2200 `schedule_event`, @calendar_integration/mutations.py:1146 and :1288 (the booking-code paths), and the bulk sync writer at @calendar_integration/services/calendar_sync_service.py:1168.

Spec use-case: **Use-case 4** (enforcement half).

Tests:
- **Unit**: `calendar_integration/tests/services/test_postpaid_enforcement.py` — with a payment method, creation past the allowance succeeds and accrues; without one, it raises with `remedy="add_payment_method"`.
- **Integration**: `calendar_integration/tests/test_event_creation_surfaces.py` — all six entry points enforce identically. A bundle event fanning out to 5 calendars checks 5 units of headroom, not 1. **Unlimited-plan test**: unchanged behavior on every path.

**Suggested AI model**: Tier 3. Six entry points and one fan-out case, against the pattern Phase 6 established.

**Reusable skills**: none.

Acceptance: spec acceptance scenarios 3 and 4 pass as automated tests.

---

### Phase 9 — Upgrade, add-on purchase, and proration

**Goal**: an organization on the free plan can choose a paid plan, pay, and see its limits lift — with no support or engineering intervention. Spec objective 2.

**Feature flag**: none — new endpoints on a new path, purely additive surface.

Changes:
1. `payments/billing_views.py` implementing the endpoints in **API Design**, registered in @payments/routes.py.
2. `IsBillingOwnerOrAdmin` permission in @organizations/permissions.py, alongside `IsOrganizationAdmin` at line 91, additionally allowing an acting reseller root via the subtree check at @public_api/capabilities.py:29.
3. `SubscriptionService.change_plan` drives the provider through the adapter, re-copies limits on confirmed payment, and moves `billing_state` to `ACTIVE`.
4. `purchase_add_on` — idempotent on `purchase_idempotency_key`; capacity is granted only on confirmed payment, arriving via the Phase 2 webhook path.
5. Proration on upgrade computed provider-side. Downgrade takes no cash refund: the plan change is scheduled for the next period boundary while the lower limits apply immediately with a grace window.
6. Serializers, filtersets, and `make update_schema` to regenerate `schema.yml`.

Spec use-case: **Use-case 3 — Organization buys more of a pre-paid resource**, and the upgrade half of **Use-case 1**.

Tests:
- **Unit**: `payments/tests/services/test_plan_change.py` — upgrade re-copies limits; downgrade schedules for the boundary and enforces immediately.
- **Integration**: `payments/tests/views/test_billing_views.py` — the same `idempotency_key` posted twice yields one add-on and one charge; a plain member gets 403 while a billing owner succeeds; an upgrade lifts the effective limit and the previously blocked invite from Phase 6a then succeeds.

**Suggested AI model**: Tier 3. Endpoint surface, permission class, provider round-trips, and idempotency semantics.

**Review models**: reviewer Tier 4 — idempotency failures here charge customers twice, which the spec calls out explicitly.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: spec acceptance scenario 1 passes end to end against the Stripe sandbox — an org at its seat limit upgrades, pays, and the previously rejected invitation succeeds with no manual step.

---

### Phase 10 — Grace, dunning, and the restricted transition

**Goal**: a failed payment moves an organization through a warned grace period with retries, and into Restricted if unresolved.

**Feature flag**: none — the state machine only leaves `FREE`/`ACTIVE` on a real payment failure.

Changes:
1. `payments/services/dunning_service.py` implementing the spec's lifecycle diagram as explicit transitions, rejecting any transition not on it.
2. Webhook handlers for payment failure move `ACTIVE → GRACE` and stamp `grace_period_ends_at` from `BillingPlan.grace_period_days`, falling back to a `BILLING_DEFAULT_GRACE_PERIOD_DAYS` setting.
3. Celery beat `process_dunning` — retries the charge on a schedule across the window with escalating email, and moves `GRACE → RESTRICTED` on expiry.
4. `RESTRICTED → ACTIVE` on payment success; `GRACE → FREE` when an org falls back under free limits.
5. Cancellation: `ACTIVE → CANCELLED`, running to period end, then `CANCELLED → FREE` by a period-close sweep.
6. Email templates for the dunning ladder; in-app notification on entering grace.

Spec use-case: **Use-case 5 — Payment fails and the organization degrades.**

Tests:
- **Unit**: `payments/tests/services/test_dunning_service.py` — every transition on the spec's diagram is permitted and every one not on it raises. Grace expiry with no resolution lands in `RESTRICTED`; payment during grace returns to `ACTIVE`.
- **Integration**: `payments/tests/test_dunning_schedule.py` — the retry ladder fires on schedule with escalating notifications and does not retry after resolution.

**Suggested AI model**: Tier 3. State machine plus scheduled task, both well-specified by the diagram.

**Reusable skills**: none.

Acceptance: an org whose payment fails moves to `GRACE`, receives the ladder, and lands in `RESTRICTED` at expiry; paying at any point returns it to `ACTIVE`.

---

### Phase 11 — Restricted enforcement and sync pause

**Goal**: a restricted organization cannot write and stops costing us third-party spend, while its data stays fully readable.

**Feature flag**: none — reachable only from `RESTRICTED`, which nothing enters until Phase 10 ships.

Changes:
1. A restricted-state check in the enforcement service raising `OverLimitError` with `remedy="resolve_billing"` on create/update/delete of `OrganizationModel` subclasses. Applied at the same guarded service methods from Phases 6a-6c and 8, plus update and delete paths.
2. Reads, exports, authentication, and every `/billing/` endpoint stay open. The `/billing/` surface is explicitly exempt — an org must be able to pay its way out.
3. Sync pause at both levels: the three task bodies in @calendar_integration/tasks/calendar_sync_tasks.py (:19, :57, :99), which already early-return on a missing org, and the `request_*` methods at @calendar_integration/services/calendar_sync_service.py:177, :288, :474 so work is not queued at all. Also the webhook-triggered path at @calendar_integration/services/calendar_webhook_service.py:175.
4. Reseller cascade: `resolve_billing_root` already routes children to the root, so a restricted root restricts the subtree by construction. An explicit test proves it rather than leaving it implicit.
5. Grace-period warning copy states plainly that calendar sync will stop, and the resync-on-recovery path is exercised.

Spec use-case: **Use-case 5** (restricted half) and **Use-case 6** (reseller cascade).

Tests:
- **Unit**: `payments/tests/services/test_restricted_enforcement.py` — writes blocked, reads allowed, billing endpoints allowed.
- **Integration**: `calendar_integration/tests/test_restricted_sync_pause.py` — no sync task is enqueued for a restricted org and a directly-invoked task early-returns; on returning to `ACTIVE`, a resync runs and the calendar reconciles. `payments/tests/test_reseller_restriction.py` — restricting a root blocks writes in every descendant.

**Suggested AI model**: Tier 3. Broad but mechanical surface; the resync-after-recovery path is the part with real substance.

**Review models**: reviewer Tier 4 — the failure mode is locking a paying customer out of the very endpoints they need to pay, and the spec's Risks-assumed entry flags the sync-drift consequence as customer-visible beyond billing.

**Reusable skills**: none.

Acceptance: spec acceptance scenarios 8 and 9 pass; a restricted org can still reach every `/billing/` endpoint and read all of its data.

---

### Phase 12 — Usage API and approaching-limit warnings

**Goal**: an organization can see where it stands, and is warned before it is blocked rather than by being blocked.

**Feature flag**: none — a new read endpoint, purely additive.

Changes:
1. `GET /billing/usage/` returning per-resource usage against effective limits plus `billing_state`, resolved at the billing root.
2. Celery beat `check_approaching_limits` emitting an in-app notification when an org crosses a threshold (default 80%), debounced so it fires once per resource per cycle rather than on every check.
3. Notification templates for approaching-limit and limit-reached.
4. `make update_schema`.

Spec use-case: **Use-case 8 — Organization inspects its usage.**

Tests:
- **Unit**: `payments/tests/test_usage_serialization.py` — an unlimited resource serializes as `null`, not `0`; a reseller child reports the pooled root figures.
- **Integration**: `payments/tests/views/test_usage_view.py` — usage matches what enforcement counts, for every `LimitedResource`. This is the test that keeps the read API from drifting away from the enforcement counters. `payments/tests/test_limit_warnings.py` — the warning fires once at the threshold, not repeatedly.

**Suggested AI model**: Tier 2. Read endpoint plus a scheduled notification, both against established patterns.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `GET /billing/usage/` reports, for every `LimitedResource` member, the same usage number enforcement would use, and a warning fires before an org is blocked.

---

### Phase 13 — Cycle close, overage charge, and reconciliation

**Goal**: a billing cycle closes with accrued overage charged alongside the recurring fee, reconciled against the provider with no unexplained drift. Spec objective 3.

**Feature flag**: none — reachable only for orgs on a real plan with postpaid usage.

Changes:
1. Celery beat `close_billing_periods` — for each subscription whose `current_period_end` has passed: sum `MeteredOccurrence` rows outside the allowance, charge the total through the adapter, roll the period forward, and reset the postpaid counters.
2. Overage settles **monthly** even for annually-billed plans, per the spec's *Time-bounded behavior*.
3. The charge carries an idempotency key derived from `(subscription, period_start)` so a re-run cannot double-charge.
4. `reconcile_period` from Phase 7 is run as part of close and reports drift between what was metered and what was charged.
5. A management command re-running reconciliation for a named closed period, for finance.

Spec use-case: the settlement half of **Use-case 4**, and spec objective 3.

Tests:
- **Unit**: `payments/tests/services/test_cycle_close.py` — only occurrences outside the allowance are charged; the total matches unit price times count; an annually-billed plan still settles overage monthly.
- **Integration**: `payments/tests/test_cycle_close_idempotency.py` — running close twice for the same period produces one charge. `payments/tests/test_reconciliation.py` — a full simulated cycle reconciles to zero drift against a recorded provider fixture.

**Suggested AI model**: Tier 4. Money leaves the building here, and a double-charge or a drift is exactly the failure the spec rates high-severity.

**Review models**: reviewer Tier 4, fixer Tier 3.

**Reusable skills**: none.

Acceptance: a simulated full cycle with usage above the allowance produces exactly one overage charge matching the metered total, and reconciliation reports zero unexplained discrepancies — spec objective 3.

---

### Phase 14 — Roll organizations onto real plans

**Goal**: existing organizations move off `unlimited` onto real plans without any of them entering a blocked or restricted state. Spec objective 4.

This phase is **gated on real-world signal, not on phase number**: it cannot start until Phases 1-13 are merged and the Phase 13 reconciliation has run clean against the Stripe sandbox for at least one simulated cycle.

**Feature flag**: none — this phase *is* the rollout switch being flipped, per-org.

Changes:
1. A one-off script (per @ai-tools/skills/add-one-off-script/SKILL.md: dry-run by default, idempotent, batched, resumable, CSV backup before writes) that, for a target set of organizations, reports current usage against the target plan's limits and **only** migrates organizations already under every ceiling.
2. Organizations over a target-plan ceiling are reported and left on `unlimited`, or given an `is_overridden` `SubscriptionPlanLimit` at their current usage, so no one is blocked by the rollout itself.
3. Seed the real paid plans as catalog rows.
4. A runbook: dry-run report → migrate one internal organization → soak → migrate a cohort → migrate the remainder.
5. Rollback: move an organization back to `unlimited` — a single `change_plan` call, no deploy.

Spec use-case: rollout, closing spec objective 4.

Tests:
- **Unit**: `scripts/one_off/.../tests.py` — dry-run writes nothing; an org over a ceiling is never migrated silently; re-running is idempotent.
- **Integration**: a fixture org set spanning under-limit, at-limit, and over-limit cases produces the correct partition.

**Suggested AI model**: Tier 3. The script contract is well-specified by the skill; the judgement is in the over-limit partitioning.

**Review models**: reviewer Tier 4 — this is the phase that can block real customers, and objective 4 sets the threshold at zero.

**Reusable skills**: `add-one-off-script`, `run-one-off-script-django`.

Acceptance: after the full rollout, zero organizations are in `RESTRICTED` or blocked as a direct result, and every migrated organization is at or under every ceiling on its assigned plan.

---

## 6. Risk & Rollout Notes

**No feature flag; the plan catalog is the switch.** Every organization is seeded onto `unlimited` in Phase 3, and enforcement code from Phase 6 onward runs but cannot block anyone whose `SubscriptionPlanLimit.limit_value` is NULL. Rollout is Phase 14 migrating organizations onto real plans, one cohort at a time. Rollback for any single organization is a `change_plan` back to `unlimited` — no deploy. Because there is no flag, **every enforcement phase carries a test asserting an `unlimited` organization sees unchanged behavior**; that test set is the equivalent of the flag-off suite, and it is what makes the absence of a flag safe. There is deliberately no flag-removal phase.

**Migration safety.** Phase 1's rebuild of `payments_billingprofile` and `payments_subscription` is destructive. It is safe only because both tables, along with `organizations_organizationtier` and `organizations_subscriptionplan`, are empty in every environment — no code assigns `Organization.tier`, and the only writes are in @organizations/tests/test_views.py. **Verify emptiness in each environment before applying.** Phase 3 drops `Organization.tier` from `organizations_organization`, a column with a null value on every row; the drop takes a brief `ACCESS EXCLUSIVE` lock but rewrites nothing. Phase 4's backfill inserts one `Subscription` plus a copy of the limit rows per organization — batch it. `MeteredOccurrence` is the one table with unbounded growth; it is indexed on `(subscription, billing_period_start)` and old periods are archivable once reconciled.

**Concurrency and contention.** Pre-paid checks serialize on `SELECT ... FOR UPDATE` against the root `Subscription` row. For a reseller tree, that is one row for the entire subtree — the point of maximum contention in this design, and it is deliberate, because pooled limits cannot be checked correctly otherwise. The lock is held only for the duration of the caller's existing transaction. If contention shows up on a large reseller, the mitigation is a per-resource advisory lock rather than the subscription row; do not reach for it before it is measured.

**Metering correctness is the highest-severity risk in this plan.** Occurrences are computed, not stored, so metering depends on a scheduled task running correctly. Mitigations, all in Phase 7: the `(organization, event_id, occurrence_start)` unique constraint makes re-runs and overlapping windows harmless; the sweep window deliberately overlaps the previous one so a missed run self-heals; `reconcile_period` recomputes a closed period and reports drift; and `unit_price` is stamped at meter time so later limit changes cannot retroactively reprice. `event_id` is a soft reference so deleting an event cannot erase the record that it was billed.

**Sync enforcement can break working automated flows.** Adding limit checks to @calendar_integration/services/calendar_sync_service.py means a sync that always succeeded can now import partially. This is accepted — the spec prefers partial failure to unmetered creation — and Phase 14's over-limit partitioning is what keeps it from happening at rollout. Partial imports record a warning rather than raising to the caller.

**Restricted-state sync pausing has consequences beyond billing.** A paused organization's calendars drift from their external providers, and recovery needs a resync that is not instantaneous. Phase 10's grace-period copy says so explicitly, and Phase 11 tests the resync-on-recovery path rather than assuming it.

**Reseller blast radius is deliberate.** One failed reseller payment restricts the entire subtree. This was chosen, not defaulted into. If it proves too blunt in practice, the change is localized to the restricted check in Phase 11.

**Webhook security is a prerequisite, not a nicety.** Until Phase 2 lands, @payments/views.py:36 and :61 accept unauthenticated, unsigned POSTs. Once organization state depends on those webhooks, a forged event could lift limits or clear a restriction. Phase 2 precedes every phase that reads webhook state.

**One-way doors.** Two: the move of the billing relationship from user to organization (Phase 1), and any charge actually taken from a customer (Phase 9 onward). Phase 1 lands first, deliberately, before real money can flow.

## 7. Open Questions

1. **Trials.** Deferred from v1. When they land, `BillingState` gains a `TRIALING` member, plus an expiry sweep and a decision about over-limit resources at expiry. *Recommended default when taken up:* time-boxed trial of one designated tier, no payment method to start, falling back to `FREE` rather than `RESTRICTED`. *Owner:* product and sales.
2. **Coupons.** Deferred from v1. *Recommended default when taken up:* plans and recurring add-ons only, never overage, modeled on our side so reconciliation still matches. *Owner:* sales.
3. **The approaching-limit warning threshold** is 80% as a settings default. Whether it should be per-plan, or multiple thresholds (80% then 95%), is unresolved. *Recommended default:* one global threshold in v1; make it per-plan only if support reports it firing wrongly. *Owner:* product.
4. **Free-plan limit values and the paid tiers' numbers** are seeded data this plan does not fix, per the spec. Phase 3 seeds `unlimited` and a placeholder `free`; Phase 14 needs the real numbers before it can run. *Owner:* product and sales. **This blocks Phase 14, not the phases before it.**
5. **Archival policy for `MeteredOccurrence`.** It grows without bound. *Recommended default:* retain reconciled periods for one year, then archive. *Owner:* engineering, with finance input on the retention floor.
6. **Whether `is_billing_owner` should be exposed in the membership API** or stay admin-only in v1. *Recommended default:* admin-settable through the existing `OrganizationMembershipViewSet` update path; not exposed on the public GraphQL surface. *Owner:* the requester.

## 8. Touch List

**Phase 1** — edit [payments/models.py](../payments/models.py), [payments/serializers.py](../payments/serializers.py), [payments/views.py](../payments/views.py), [payments/services/payment_service.py](../payments/services/payment_service.py), [payments/services/payment_adapters/base.py](../payments/services/payment_adapters/base.py); new @payments/admin.py, @payments/migrations/0003_billing_profile_to_organization.py; new @payments/tests/test_models.py.

**Phase 2** — edit [payments/views.py](../payments/views.py), [payments/models.py](../payments/models.py), [payments/services/payment_adapters/base.py](../payments/services/payment_adapters/base.py), [payments/services/payment_adapters/mercadopago_payment_adapter.py](../payments/services/payment_adapters/mercadopago_payment_adapter.py), [payments/services/subscription_adapters/base.py](../payments/services/subscription_adapters/base.py), [payments/services/subscription_adapters/mercadopago_subscription_adapter.py](../payments/services/subscription_adapters/mercadopago_subscription_adapter.py), [di_core/containers.py](../di_core/containers.py), [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py), `.env.example`, `.env.docker.example`; new @payments/tests/views/test_payment_webhooks.py.

**Phase 2b** — new @payments/services/payment_adapters/stripe_payment_adapter.py, @payments/services/subscription_adapters/stripe_subscription_adapter.py, @payments/tests/services/subscription_adapters/test_stripe_subscription_adapter.py, @payments/tests/services/test_provider_registry.py; edit [payments/services/dataclasses.py](../payments/services/dataclasses.py), [di_core/containers.py](../di_core/containers.py), [pyproject.toml](../pyproject.toml), settings and env templates.

**Phase 3** — new @payments/billing_constants.py, @payments/migrations/0005_billing_plan_catalog.py, @payments/migrations/0006_seed_plans.py, @payments/services/billing_plan_factory.py, @payments/tests/test_billing_plan.py, @payments/tests/test_plan_seed_migration.py; edit [payments/models.py](../payments/models.py), [payments/admin.py](../payments/admin.py), [organizations/models.py](../organizations/models.py), [organizations/graphql.py](../organizations/graphql.py), [di_core/containers.py](../di_core/containers.py), [organizations/tests/test_views.py](../organizations/tests/test_views.py); delete @organizations/organization_subscription_plan_factory.py.

**Phase 4** — new @payments/services/subscription_service.py, @payments/services/billing_dataclasses.py, @payments/tests/services/test_subscription_service.py, @organizations/tests/test_organization_creation_billing.py; edit [payments/models.py](../payments/models.py), [organizations/services.py](../organizations/services.py), [organizations/models.py](../organizations/models.py), [public_api/mutations.py](../public_api/mutations.py), [di_core/containers.py](../di_core/containers.py), [payments/admin.py](../payments/admin.py).

**Phase 5** — new @payments/services/entitlement_service.py, @payments/exceptions.py, @payments/tests/services/test_entitlement_service.py, @payments/tests/services/test_pooled_limits.py, @payments/tests/services/test_limit_concurrency.py; edit [payments/models.py](../payments/models.py), [di_core/containers.py](../di_core/containers.py), [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py) (DRF exception handler).

**Phase 6a** — edit [organizations/services.py](../organizations/services.py), [organizations/views.py](../organizations/views.py), [public_api/mutations.py](../public_api/mutations.py); new @organizations/tests/services/test_invitation_limits.py, @organizations/tests/test_seat_enforcement.py.

**Phase 6b** — edit [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py), [calendar_integration/services/calendar_group_service.py](../calendar_integration/services/calendar_group_service.py), [calendar_integration/services/calendar_bundle_service.py](../calendar_integration/services/calendar_bundle_service.py), [calendar_integration/services/calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py), [public_api/mutations.py](../public_api/mutations.py); new @calendar_integration/tests/services/test_calendar_limits.py, @calendar_integration/tests/test_sync_limit_enforcement.py.

**Phase 6c** — edit the webhooks and public_api creation services, [public_api/middlewares.py](../public_api/middlewares.py), [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py), [organizations/models.py](../organizations/models.py); new @public_api/tests/test_entitlement_gates.py.

**Phase 7** — new @payments/services/metering_service.py, @payments/tests/services/test_metering_service.py, @payments/tests/test_metering_reconciliation.py; edit [payments/models.py](../payments/models.py), [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py), [di_core/containers.py](../di_core/containers.py).

**Phase 8** — edit [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py), [calendar_integration/services/calendar_group_service.py](../calendar_integration/services/calendar_group_service.py), [calendar_integration/services/calendar_bundle_service.py](../calendar_integration/services/calendar_bundle_service.py), [calendar_integration/services/calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py), [calendar_integration/views.py](../calendar_integration/views.py), [calendar_integration/token_views.py](../calendar_integration/token_views.py), [calendar_integration/mutations.py](../calendar_integration/mutations.py), [public_api/mutations.py](../public_api/mutations.py); new @calendar_integration/tests/services/test_postpaid_enforcement.py, @calendar_integration/tests/test_event_creation_surfaces.py.

**Phase 9** — new @payments/billing_views.py, @payments/billing_serializers.py, @payments/tests/services/test_plan_change.py, @payments/tests/views/test_billing_views.py; edit [payments/routes.py](../payments/routes.py), [payments/services/subscription_service.py](../payments/services/subscription_service.py), [organizations/permissions.py](../organizations/permissions.py), `schema.yml`.

**Phase 10** — new @payments/services/dunning_service.py, @payments/tasks.py, @payments/tests/services/test_dunning_service.py, @payments/tests/test_dunning_schedule.py, dunning email templates; edit [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py), [payments/views.py](../payments/views.py), [di_core/containers.py](../di_core/containers.py).

**Phase 11** — edit [calendar_integration/tasks/calendar_sync_tasks.py](../calendar_integration/tasks/calendar_sync_tasks.py), [calendar_integration/services/calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py), [calendar_integration/services/calendar_webhook_service.py](../calendar_integration/services/calendar_webhook_service.py), [payments/services/entitlement_service.py](../payments/services/entitlement_service.py), plus every service guarded in Phases 6a-6c and 8 for update/delete; new @payments/tests/services/test_restricted_enforcement.py, @calendar_integration/tests/test_restricted_sync_pause.py, @payments/tests/test_reseller_restriction.py.

**Phase 12** — edit [payments/billing_views.py](../payments/billing_views.py), [payments/tasks.py](../payments/tasks.py), [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py), `schema.yml`; new notification templates, @payments/tests/test_usage_serialization.py, @payments/tests/views/test_usage_view.py, @payments/tests/test_limit_warnings.py.

**Phase 13** — new @payments/services/billing_cycle_service.py, @payments/management/commands/reconcile_billing_period.py, @payments/tests/services/test_cycle_close.py, @payments/tests/test_cycle_close_idempotency.py, @payments/tests/test_reconciliation.py; edit [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py), [payments/services/metering_service.py](../payments/services/metering_service.py).

**Phase 14** — new `scripts/one_off/2026-XX-XX-migrate-organizations-to-plans/` (script, tests, Django management-command runner), a rollout runbook, and a data migration seeding the real paid plans.
