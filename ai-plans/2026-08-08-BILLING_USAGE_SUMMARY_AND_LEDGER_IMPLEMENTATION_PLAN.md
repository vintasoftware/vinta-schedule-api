# Billing Usage Summary & Occurrence Ledger — Implementation Plan

Customers can answer two questions for themselves: *"where do I stand this cycle?"* (summary) and *"what exactly am I being charged for?"* (line-item ledger). Today only the first is partly answerable, and only for the current moment.

This plan picks up a non-goal the original billing work deferred — [BILLING_PLANS_AND_LIMITS_SPEC.md](2026-07-18-BILLING_PLANS_AND_LIMITS_SPEC.md) explicitly excluded *"Usage analytics and historical reporting… Trend charts, historical usage exports, and mid-cycle overage estimates"* — and implements the reporting half of its **Use-case 8, Organization inspects its usage**.

There is no sibling `..._SPEC.md`. The decisions below were settled by interrogation before drafting; every one of them is recorded in **Guiding Decisions** with its rationale, and the handful that were left to a stated default appear in **Open Questions**.

## 1. Goals

1. **Enriched current-cycle summary.** `GET /billing/usage/` gains billing-period bounds, the plan snapshot, the split between plan-included and add-on-purchased capacity, per-organization attribution across a pooled reseller subtree, and a mid-cycle overage estimate in money — all as **additive** fields, leaving every existing key intact.
2. **Durable per-period statements.** A new `BillingPeriodSummary` (plus a `BillingPeriodResourceUsage` child) is written by `CycleCloseService` when it closes a period, so a customer can read back what was counted and charged for a cycle after that cycle has rolled. This is the only mechanism by which historical **prepaid** counts will ever exist.
3. **Auditable line-item ledger.** `GET /billing/usage/occurrences/` exposes the `MeteredOccurrence` rows that drive post-paid charges — paginated, filterable by period, allowance side, organization, and occurrence-start range — so a customer disputing an invoice can tie every unit of money back to a specific occurrence.
4. **One source of truth for every number.** The summary, the statements, the ledger, the enforcement checks (`check_limit` / `check_postpaid_allowance`), and the approaching-limit warnings must be incapable of disagreeing. Every figure this plan exposes is read through the existing `EntitlementService` / `MeteredOccurrenceQuerySet` methods, never re-derived.

**Non-goals:**

- **Per-item history for prepaid resources.** Seven of the eight `LimitedResource` members are live `COUNT()`s over tables in other apps ([entitlement_service.py:87-215](../payments/services/entitlement_service.py#L87-L215)). No per-item ledger is introduced for them. They appear in summaries as counts, never as enumerable rows.
- **Backfill.** No `BillingPeriodSummary` rows are reconstructed for periods that closed before this ships. History starts empty and fills one cycle at a time.
- **Public GraphQL surface.** Internal REST only. No `public_api` types, no `PublicAPIResources` member, no `FIELD_TO_RESOURCE_MAPPING` entry.
- **CSV / Excel export.** The paginated JSON ledger is the audit surface. An async export is a separate feature with its own task, storage, retention, and permission story.
- **Trend charts, projections, forecasts.** The summary reports overage accrued *to date*. It does not project an end-of-cycle total.
- **Exposing reconciliation drift to customers.** Drift is persisted on the statement row for internal investigation and shown in Django admin. It is not serialized into any customer-facing response.
- **Changing what gets metered, or how.** No change to `MeteringService`, to the sweep, or to any counter's definition of usage. Phase 1 changes a counter's *return shape* while holding its totals byte-for-byte identical.
- **Invoices or receipts.** `BillingPeriodSummary` is a usage statement, not an invoice document. It links to the `Payment` that settled the overage; it does not render one.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Detail = post-paid ledger only** | `MeteredOccurrence` is the only per-item record that exists, and it is the only one tied to money. The seven prepaid resources have nothing to enumerate — their "usage" is a `COUNT()` evaluated at read time, and the rows behind it live in four other apps and get hard-deleted. Enumerating them would produce a list that silently disagrees with the count beside it the moment a row is deleted. Counts for prepaid, line items for post-paid, and the response says which is which. |
| **Extend `GET /billing/usage/`, don't replace it** | The endpoint already exists and already resolves the billing root the same way every other billing read does. A second endpoint answering the same question is a second number that can drift from the first. All new keys are additive; no existing key changes name, type, or meaning. |
| **New `BillingPeriodSummary` + `BillingPeriodResourceUsage`** | `ClosedPeriod` is a transient dataclass returned by `CycleCloseService.close_subscription` and then discarded. Nothing persists a closed period. Past periods are today reconstructable *only* for post-paid, via `distinct(billing_period_start)` on the ledger — which loses zero-usage periods entirely and can never recover prepaid counts. Writing the statement at close is the only point where all of that information is simultaneously in hand. |
| **Child table for resources, JSON for org attribution** | One `BillingPeriodResourceUsage` row per resource per period (8 rows) keeps the queryable unit explicit and serializes naturally. The per-organization breakdown hangs off it as a `by_organization` JSON blob because it is only ever read wholesale with its parent row — a third table would add a join for data nothing filters on. |
| **Forward-only, no backfill** | Reconstructed history is approximate history, and approximate numbers presented on a billing surface are worse than absent ones. An empty list that fills each cycle is honest; a backfilled figure that disagrees with what was actually charged is a support ticket. Explicitly re-stated in **Risk & Rollout Notes** because "the history is empty on day one" is a product expectation, not a bug. |
| **Prepaid history: snapshot at close, `null` before** | `BillingPeriodResourceUsage.total` is nullable and `null` means **"not recorded"**, never zero. That distinction also covers a case forward-only rollout does not: a `LimitedResource` member added *after* a period closed has no row for that period, and must not read as "you used none". |
| **Counters return a per-organization breakdown; totals are the sum** | Per-org attribution is required by the summary. The alternative — a parallel set of breakdown counters beside the enforcement counters — is two implementations of "what counts as usage" that will eventually disagree, and the disagreement surfaces as a wrong number on an invoice. Instead `UsageCounter` is widened to return `dict[organization_id, int]` and `_count_usage` returns `sum(...)`. The count *is* the sum of the breakdown, structurally. This mirrors the reasoning already written into `MeteredOccurrenceQuerySet`'s docstring about there being one definition of a billing period. |
| **Pooled reads resolve to the billing root, attributed per child** | Enforcement pools usage across the subtree (`get_pooled_organization_ids`), so a child's ceiling *is* the root's. Reporting anything narrower would fail to explain why a child got blocked. Attribution per organization is what makes a reseller root able to see which child consumed the capacity. |
| **Ledger requires `IsBillingOwnerOrAdmin`; summary stays `IsAuthenticated`** | A ledger row carries an `event_id` and an exact `occurrence_start` — that is calendar content, and it spans every calendar in the pool including ones the caller has no membership scope on. A count is not. The summary keeps today's permission (a read must never block, including for a `RESTRICTED` organization); the line-item view requires billing authority. |
| **Event enrichment is best-effort and may be `null`** | `MeteredOccurrence.event_id` is a soft `BigIntegerField` precisely so the billing record outlives the event. A deleted event is an expected state, not an error: the row serializes with `event: null` and the charge still stands. Resolution is a single batched query per page, never a per-row lookup. |
| **The resolved title is the *series root*'s title** | `event_id` stores the series root, followed back through `bulk_modification_parent` (see the `MeteredOccurrence` docstring). For a modified occurrence, the title shown is therefore the master's, not that occurrence's own. Documented in the API response and in the serializer, because a customer comparing the ledger against their calendar will otherwise think it is a bug. |
| **Period detail addressed by pk** | `billing_period_start` is a `DateTimeField`; putting a datetime in a URL path invites timezone- and format-normalisation bugs on both sides. The statement is a real row now, so it gets a real pk, and `billing_period_start` is a filter on the list endpoint. |
| **No feature flag** | This repo has no flag framework and is pre-production — the standing convention recorded in [WHITELABEL_API_PROVISIONING](2026-06-16-WHITELABEL_API_PROVISIONING_IMPLEMENTATION_PLAN.md#L66) and [IN_APP_NOTIFICATIONS](2026-06-13-IN_APP_NOTIFICATIONS_IMPLEMENTATION_PLAN.md#L258). Every phase here is either net-new surface (a table, three routes) or strictly additive fields on one response. The single exception is Phase 1, which touches enforcement-critical code; it is gated not by a flag but by a test asserting the totals are unchanged for every resource, and by a reviewer model step-up. No flag-removal phase follows. |
| **Bundled phase granularity** | Chosen at planning time. The two statement-read endpoints share a queryset, a permission, and a serializer tree, so they ship together rather than as two PRs whose second one is fifty lines. Every phase still stays MR-sized, single-concern, independently mergeable, and independently reversible. |

## 3. Data Model Changes

### 3.1 New `BillingPeriodSummary`

New model in @payments/models.py, exported from @payments/\_\_init\_\_.py's model surface if one exists (the app currently imports directly from `payments.models`; follow whatever the neighbouring models do).

```python
class BillingPeriodSummary(BaseModel):
    """One closed billing period, as a durable statement.

    ``CycleCloseService`` returns a ``ClosedPeriod`` dataclass and discards it;
    this is that value made durable. It exists because a closed period is the
    only moment at which the plan in force, the prepaid counts, the accrued
    overage, and the payment that settled it are all simultaneously knowable —
    afterwards the subscription has rolled and the prepaid counts have moved on.

    **The unique constraint is the correctness mechanism**, the same pattern
    ``MeteredOccurrence`` and ``ProviderWebhookEvent`` use: cycle close is
    idempotent on ``(subscription, period_start)`` and catch-up runs re-enter
    it, so the write must be a no-op on re-run rather than something the caller
    has to remember to check.

    Not an ``OrganizationModel``, for the same reason ``MeteredOccurrence`` is
    not: billing legitimately reads across a pooled subtree, and tenant-scoped
    managers would force an ``original_manager`` escape at nearly every call
    site. ``organization`` is always the resolved **billing root**.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="period_summaries"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing_period_summaries",
    )
    billing_period_start = models.DateTimeField(db_index=True)
    billing_period_end = models.DateTimeField()

    # Plan snapshot: the plan in force for THIS period, not whatever the
    # subscription points at now. A plan change after close must not rewrite
    # history, exactly as MeteredOccurrence.unit_price is stamped at meter time.
    plan_slug = models.CharField(max_length=100)
    plan_name = models.CharField(max_length=255)
    billing_interval = models.CharField(max_length=20, choices=BillingInterval)
    currency = models.CharField(max_length=3)

    overage_total = models.DecimalField(max_digits=12, decimal_places=4)
    charged = models.BooleanField()
    payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="period_summaries",
    )

    # Internal only — never serialized to a customer. Persisted so a disputed
    # invoice can be investigated against what reconciliation saw at the time.
    reconciliation_unmetered = models.PositiveIntegerField()
    reconciliation_orphaned = models.PositiveIntegerField()

    closed_at = models.DateTimeField()

    objects: ClassVar[BillingPeriodSummaryManager] = BillingPeriodSummaryManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "billing_period_start"],
                name="uniq_billing_period_summary",
            )
        ]
        indexes: ClassVar = [
            models.Index(
                fields=["organization", "-billing_period_start"],
                name="billing_period_org_idx",
            )
        ]
        ordering = ("-billing_period_start",)
```

### 3.2 New `BillingPeriodResourceUsage`

```python
class BillingPeriodResourceUsage(BaseModel):
    """Per-resource usage as of one closed period.

    ``total`` is nullable and ``null`` means **not recorded**, never zero — the
    state of every prepaid resource for a period that closed before this feature
    shipped, and of any ``LimitedResource`` member added after a period closed.
    Rendering "not recorded" as 0 would tell a customer they used none of
    something we simply never counted.

    ``by_organization`` maps ``organization_id -> count`` across the pooled
    subtree. A JSON blob rather than a third table because it is only ever read
    wholesale alongside its parent row; nothing filters or aggregates on it in
    SQL.
    """

    summary = models.ForeignKey(
        BillingPeriodSummary, on_delete=models.CASCADE, related_name="resources"
    )
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    kind = models.CharField(max_length=20, choices=LimitKind, null=True, blank=True)
    total = models.PositiveIntegerField(null=True, blank=True)
    limit_value = models.PositiveIntegerField(null=True, blank=True)  # null == unlimited
    overage_unit_price = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    by_organization = models.JSONField(default=dict)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["summary", "resource_key"],
                name="uniq_billing_period_resource_usage",
            )
        ]
```

Note the two distinct nulls on this model, which reviewers should not collapse: `total=None` means *not recorded*; `limit_value=None` means *unlimited*, the same fail-open semantics `EffectiveLimit` already uses.

### 3.3 Manager, queryset, admin, factory

- @payments/querysets.py — `BillingPeriodSummaryQuerySet` with `for_organizations(organization_ids)` (mirroring `MeteredOccurrenceQuerySet.for_organizations`, and named for the same reason: every read is organization-scoped and going through a named method keeps that visible at the call site).
- @payments/managers.py — `BillingPeriodSummaryManager`, following `MeteredOccurrenceManager`'s hand-written delegation (the file's comment explains why `from_queryset` is unusable here under django-stubs).
- @payments/admin.py — register both models read-only, with `reconciliation_unmetered` / `reconciliation_orphaned` visible. Admin is where drift is surfaced.
- Test object construction uses `model_bakery` (`baker.make`), the convention throughout this app — `payments` has no `factories.py` and should not grow one for these models.

### 3.4 Type plumbing

@payments/services/entitlement_service.py — `UsageCounter` widens:

```python
# was: Callable[[UsageContext], int]
UsageCounter = Callable[["UsageContext"], dict[int, int]]
```

Every one of the eight counters returns `{organization_id: count}` instead of an `int`; `_count_usage` returns `sum(breakdown.values())`. `get_current_usage` keeps its `int` return type and its exact current behavior. A new `get_usage_breakdown(organization, resource_key) -> dict[int, int]` is the public entry point for the read surface, resolving root and subscription the same way.

@payments/services/billing_dataclasses.py — no change to `ClosedPeriod`. Phase 2 persists from it rather than reshaping it.

## 4. API Design

All routes are internal REST, registered in @payments/routes.py under the existing `billing/usage` namespace.

### 4.1 `GET /billing/usage/` — current cycle (enriched)

Permission `IsAuthenticated` (unchanged). Every existing key is preserved with its current name, type, and meaning; everything below marked **new** is additive.

```json
{
  "billing_state": "active",
  "billing_root_organization_id": 12,                          // new
  "plan": {"slug": "pro", "name": "Pro", "currency": "USD"},   // new, null when no subscription
  "billing_period": {"start": "2026-08-01T00:00:00Z",          // new, null when no subscription
                     "end": "2026-09-01T00:00:00Z"},
  "estimated_overage_total": "12.5000",                        // new
  "limits": [
    {
      "resource_key": "event_occurrences",
      "kind": "postpaid",
      "limit_value": 1000,
      "current_usage": 1250,
      "overage_unit_price": "0.0100",
      "included_in_plan": 500,                                 // new
      "add_on_quantity": 500,                                  // new
      "by_organization": [                                     // new
        {"organization_id": 12, "name": "Acme", "usage": 900},
        {"organization_id": 31, "name": "Acme West", "usage": 350}
      ]
    }
  ]
}
```

- `limit_value` continues to mean *plan limit plus active add-ons* — unchanged. `included_in_plan` and `add_on_quantity` decompose it, and `included_in_plan + add_on_quantity == limit_value` holds whenever `limit_value` is non-null.
- `estimated_overage_total` is `MeteredOccurrenceQuerySet.for_billing_period(...).for_organizations(...).overage_total()` — the same derivation `CycleCloseService` charges, over the *current, open* period. It is accrued-to-date, not a projection.
- `by_organization` lists every organization in the pool that contributed, resolved from the counter breakdown.

**Errors.** `403` when there is no active organization (existing `_require_organization` behavior). An organization with no subscription returns `billing_state: "free"`, `plan: null`, `billing_period: null`, `estimated_overage_total: "0.0000"`, and unlimited (`limit_value: null`) rows — matching the fail-open rule `_effective_limit_for_subscription` already implements.

### 4.2 `GET /billing/usage/periods/` — closed period statements

Permission `IsAuthenticated`. Paginated (`LimitOffsetPagination`, project default page size). Ordered `-billing_period_start`.

```json
{"count": 3, "next": null, "previous": null, "results": [
  {"id": 9, "billing_period_start": "2026-07-01T00:00:00Z",
   "billing_period_end": "2026-08-01T00:00:00Z",
   "plan_slug": "pro", "plan_name": "Pro", "billing_interval": "monthly",
   "currency": "USD", "overage_total": "31.2000", "charged": true,
   "payment_id": 417, "closed_at": "2026-08-01T00:04:12Z"}
]}
```

Filters: `billing_period_start_after`, `billing_period_start_before`, `charged`.

### 4.3 `GET /billing/usage/periods/{id}/` — one statement, with resources

Permission `IsAuthenticated`. Same body as a list row, plus:

```json
{"resources": [
  {"resource_key": "organization_members", "kind": "prepaid",
   "total": 14, "limit_value": 25, "overage_unit_price": null,
   "by_organization": [{"organization_id": 12, "name": "Acme", "usage": 14}]},
  {"resource_key": "event_occurrences", "kind": "postpaid",
   "total": 1250, "limit_value": 1000, "overage_unit_price": "0.0100",
   "by_organization": [...]}
]}
```

`total: null` renders as `null`, meaning *not recorded* — the client must not display it as `0`. Called out explicitly in the drf-spectacular field description so it reaches the generated client docs.

**Errors.** `404` for a pk outside the caller's pooled subtree — not `403`, so the endpoint does not confirm the existence of another tenant's statement.

### 4.4 `GET /billing/usage/occurrences/` — the ledger

Permission `IsAuthenticated` **and** `IsBillingOwnerOrAdmin`, with the object-level check run in the view body against the resolved billing root — the same two-step dance [`SubscriptionViewSet.get_subscription`](../payments/billing_views.py#L195-L207) and [`AddOnViewSet.create`](../payments/billing_views.py#L321-L330) already perform, and for the documented reason: `request.organization` is not yet resolved when `has_permission` runs.

```json
{"count": 1250, "next": "...", "previous": null, "results": [
  {"id": 88301,
   "organization": {"id": 31, "name": "Acme West"},
   "event": {"id": 42, "title": "Weekly standup",
             "calendar": {"id": 7, "name": "Team"},
             "owners": [{"user_id": 9, "name": "Dana Reyes"}]},
   "occurrence_start": "2026-08-03T14:00:00Z",
   "billing_period_start": "2026-08-01T00:00:00Z",
   "is_within_allowance": false,
   "unit_price": "0.0100"}
]}
```

- `event` is `null` when the referenced event no longer exists — an expected state, documented in the schema.
- `event.title` is the **series root's** title (see **Guiding Decisions**); the field description says so.
- `owners` come from `CalendarOwnership` on the event's calendar. `CalendarEvent` has no organizer field; ownership is calendar-level and can be several memberships.

Filters (django-filter): `billing_period_start` (defaults to the current period when omitted), `is_within_allowance`, `organization` (validated to be inside the caller's pool), `occurrence_start_after`, `occurrence_start_before`. Ordering: `occurrence_start` / `-occurrence_start`, default `-occurrence_start`.

Pagination: a `LimitOffsetPagination` subclass with `max_limit = 1000`, so an audit pull does not need a thousand round trips while a single request stays bounded.

## 5. Phased Rollout

### Phase 0 — Add the billing period statement models

**Goal**: the tables exist and are inspectable in admin. Ship value: none on its own — this is the scaffolding every later phase writes to or reads from, split out so the migration lands and is reviewed independently of the logic that populates it.

**Feature flag**: none — purely additive new tables that no existing code reads or writes. See **Guiding Decisions**.

Changes:
1. @payments/models.py: add `BillingPeriodSummary` and `BillingPeriodResourceUsage` exactly as in **Data Model Changes**, docstrings included.
2. @payments/querysets.py: `BillingPeriodSummaryQuerySet.for_organizations`.
3. @payments/managers.py: `BillingPeriodSummaryManager`, following `MeteredOccurrenceManager`'s hand-written delegation.
4. @payments/migrations/: one migration creating both tables, the two unique constraints, and the org/period index. No data migration.
5. @payments/admin.py: register both read-only; surface `reconciliation_unmetered` / `reconciliation_orphaned` and the `payment` link.
6. No factory module — this app builds test objects with `model_bakery`. Add fixtures in the phase's own test file if the `baker.make` calls get repetitive.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @payments/tests/test_billing_period_summary_model.py — the `(subscription, billing_period_start)` unique constraint rejects a duplicate; the `(summary, resource_key)` constraint rejects a duplicate; `total=None` and `limit_value=None` both round-trip and remain distinguishable from `0`.
- **Integration**: @payments/tests/test_billing_period_summary_model.py — `for_organizations` restricts to the given pool; the migration applies and reverses cleanly.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Two models with exact precedent in `MeteredOccurrence` + manager/queryset/admin/factory wiring — pattern application across ~6 files, above pure boilerplate.

**Reusable skills**: `add-model` (both models, multi-tenancy contract and manager/factory conventions); `add-migration` (the create-table migration and its reverse path).

Acceptance: `make migrate` applies and reverses the new migration cleanly, both models are visible read-only in Django admin, and no existing behavior reads or writes them.

~250 LoC.

---

### Phase 1 — Per-organization breakdown in the usage counters

**Goal**: `EntitlementService` can answer *"who in this pool consumed it"* for every resource, with the totals enforcement uses provably unchanged. Ship value: none customer-visible on its own — but it is the only way the summary gets attribution without a second, drift-prone definition of usage.

**Feature flag**: none — this repo has no flag framework. The equivalent safety net is the totals-unchanged test named below, which is the acceptance criterion for this phase.

Changes:
1. @payments/services/entitlement_service.py: widen `UsageCounter` to `Callable[[UsageContext], dict[int, int]]`. Rewrite all eight counters to group by `organization_id` rather than aggregating to a scalar:
   - `_count_organization_members` — group memberships and pending invitations separately, then merge per organization.
   - `_count_availability_windows` — group `AvailableTime` and `BlockedTime` separately (both still through `unscoped().only_user_authored()`, preserving the group-scoped-rows rule that docstring documents) and merge.
   - `_count_resource_calendars`, `_count_bundle_calendars`, `_count_calendar_groups`, `_count_webhook_subscriptions`, `_count_public_api_system_users` — `values("organization_id").annotate(Count("pk"))`.
   - `_count_event_occurrences` — same grouping over the existing `for_billing_period(...).for_organizations(...)` queryset. Do **not** re-derive the period or re-expand the calendar; that constraint is the whole point of the counter's docstring.
2. `_count_usage` returns `sum(breakdown.values())` — so the total is structurally the sum of the parts, not a second query.
3. Add `EntitlementService.get_usage_breakdown(organization, resource_key) -> dict[int, int]`, resolving root and subscription exactly as `get_current_usage` does, and honoring the same `exclude_invitation_id` rejection rule.
4. Organizations in the pool that contributed nothing are absent from the dict rather than present with `0`; the read layer decides whether to render them.

Spec use-case: shared scaffolding for **Use-case 8, Organization inspects its usage** — no customer-visible change in this phase.

Tests:
- **Unit**: @payments/tests/services/test_usage_counters.py — for every member of `LimitedResource`, `sum(breakdown.values()) == ` the value the counter returned before this change, exercised over a pooled subtree with contributions from several children. This is the regression gate; it is what stands in for a flag-off test.
- **Unit**: same file — an organization contributing nothing is absent from the breakdown, not present with `0`; the `exclude_invitation_id` seat exclusion still applies and still raises for any other `resource_key`.
- **Integration**: @payments/tests/services/test_pooled_limits.py and @payments/tests/test_over_limit_error.py — unchanged assertions must still pass, proving `check_limit`, `check_postpaid_allowance`, and `check_seat_limit_for_invitation_accept` see identical numbers.
- **Integration**: @payments/tests/services/test_usage_warning_service.py — the approaching-limit beat task still fires at the same thresholds.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Eight counters across four apps, each with its own subtlety (soft-delete filters, `unscoped()`, `only_user_authored()`, the invitation exclusion), all on the enforcement-critical path.

**Review models**: reviewer Tier 4 — every enforcement decision in the product reads through these counters. A grouping bug that shifts a total silently mis-bills or wrongly blocks a customer, and it would not surface as an exception. The independent review runs on the most capable model. Fixer stays on the project default.

**Reusable skills**: none — no clean match.

Acceptance: `uv run pytest payments/ organizations/ -n auto` is green with no assertion changed in any pre-existing limit-enforcement test, and `get_usage_breakdown` returns a per-organization dict summing to `get_current_usage` for every `LimitedResource` member.

~350 LoC.

---

### Phase 2 — Persist the statement at cycle close

**Goal**: closing a billing period leaves a durable, auditable record of what was counted and charged. Ship value: nothing is readable yet, but from this deploy forward every closed cycle is recoverable — and until it ships, history is being lost irrecoverably every cycle.

**Feature flag**: none — additive write inside an existing service. The write is wrapped so a failure cannot roll back or duplicate a charge; see Changes item 3.

Changes:
1. @payments/services/cycle_close_service.py: in `_close_one_period`, after `reconcile_period` and `_charge_overage` and **before** `_roll_period`, build and persist a `BillingPeriodSummary` plus its eight `BillingPeriodResourceUsage` children. The ordering matters: prepaid counts must be snapshotted while `current_period_start` still names the period being closed.
2. Prepaid counts come from `EntitlementService.get_usage_breakdown` (Phase 1), one call per resource, with root and pool resolved once for the whole set. `total` is the sum; `by_organization` is the dict. `limit_value` / `kind` / `overage_unit_price` come from `get_effective_limit`.
3. **Idempotency**: `get_or_create` on `(subscription, billing_period_start)`, inside the existing period transaction. Cycle close is already idempotent on that key and catch-up runs re-enter it, so a re-run must be a no-op at the database level rather than a second statement. Persisting must never be able to fail a close that already charged — the write is ordered after the charge and any unexpected error is logged and swallowed, with the missing statement recoverable by re-running close (which the unique constraint makes safe).
4. `overage_total`, `charged`, `payment`, and the two reconciliation counters are copied from the `ClosedPeriod` / `ReconciliationReport` already in hand. `closed_at` is `timezone.now()`.
5. Plan snapshot (`plan_slug`, `plan_name`, `billing_interval`, `currency`) is read from the subscription's plan **before** `_apply_pending_plan_change_if_due` runs, so a pending change taking effect at the boundary does not stamp the incoming plan onto the outgoing period.

Spec use-case: **Use-case 8, Organization inspects its usage** — the persistence half.

Tests:
- **Unit**: @payments/tests/services/test_cycle_close.py — closing a period writes one summary with eight resource rows; `overage_total` equals the `ClosedPeriod`'s; `payment` links the charge when one was made and is `null` when `charged=False`.
- **Integration**: same file — a catch-up run over three elapsed periods writes exactly three statements.
- **Integration**: @payments/tests/test_cycle_close_idempotency.py — re-running close over an already-closed period writes no second statement and raises nothing, alongside the existing charge-idempotency assertions.
- **Integration**: same file — a subscription with a pending plan change effective at the boundary stamps the **outgoing** plan on the statement, not the incoming one.
- **Integration**: same file — prepaid `by_organization` matches `get_usage_breakdown` for a pooled subtree with contributions from two children.
- **Integration**: same file — an exception raised while persisting the statement does not roll back the overage charge and does not prevent the period from rolling.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Ordering-sensitive work inside a transactional close path with idempotency and catch-up semantics — multi-file, non-trivial branching.

**Review models**: reviewer Tier 4 — this phase writes inside the transaction that charges customers. Getting the ordering wrong relative to `_roll_period` or `_apply_pending_plan_change_if_due` produces a statement that disagrees with the invoice, and a failure mode here can in principle affect whether a charge commits. Fixer stays on the project default.

**Reusable skills**: none — no clean match.

Acceptance: closing one billing period for a subscription produces exactly one `BillingPeriodSummary` with eight `BillingPeriodResourceUsage` children whose figures match what `CycleCloseService` charged and what `EntitlementService` counted, and re-running close over the same period changes nothing.

~300 LoC.

---

### Phase 3 — Enrich the current-usage summary

**Goal**: a customer opening the billing screen sees the cycle they are in, the plan they are on, how their capacity splits between plan and add-ons, who in their organization tree consumed it, and what the overage has cost so far.

**Feature flag**: none — additive response fields on an existing endpoint. Backwards compatibility is asserted by the test named below.

Changes:
1. @payments/billing_views.py, `BillingUsageViewSet.retrieve_usage`: resolve the billing root, the pooled organization ids, and the subscription **once** for the whole response. Today the action loops all eight resources calling `get_effective_limit` and `get_current_usage`, each of which independently re-walks the `parent` chain and re-runs the subtree BFS — sixteen root resolutions and eight subtree walks per request. Adding attribution on top of that without fixing it would multiply an existing N+1 rather than introduce one.
2. Populate `billing_root_organization_id`, `plan`, `billing_period` (from `current_billing_period_start` and the subscription's period end — the same anchor the meter and the counters use, never the stored column alone), and `estimated_overage_total` via `MeteredOccurrenceQuerySet…overage_total()`.
3. Per limit row, add `included_in_plan` (the `SubscriptionPlanLimit.limit_value`), `add_on_quantity` (the active add-on `Sum`), and `by_organization` from `get_usage_breakdown`. Organization names come from one batched fetch over the pool, not per row.
4. @payments/serializers.py: extend `EffectiveLimitUsageSerializer` and `UsageResponseSerializer` with the new fields; add `UsageByOrganizationSerializer` and a plan-snapshot serializer. Every new field carries a drf-spectacular description; `by_organization` documents that a non-contributing organization is omitted.
5. Regenerate `schema.yml` (`make update_schema`) — the pre-commit `spectacular-schema-export` hook enforces this.
6. Preserve the null-subscription path exactly: `plan: null`, `billing_period: null`, unlimited rows, `estimated_overage_total: "0.0000"`.

Spec use-case: **Use-case 8, Organization inspects its usage** — sub-step 1, *"they read current usage against effective limits, per resource"*, extended with period, cost, and attribution.

Tests:
- **Unit**: @payments/tests/test_usage_serialization.py — the new serializers render attribution, the plan snapshot, and the add-on split; `included_in_plan + add_on_quantity == limit_value` whenever `limit_value` is non-null.
- **Integration**: @payments/tests/views/test_usage_view.py — **backwards compatibility**: every key the endpoint returned before this phase is still present with the same type and the same value for a fixture that predates the change. This is the flag-off equivalent for an existing caller.
- **Integration**: same file — a pooled reseller subtree attributes usage to the right children and omits non-contributors; `estimated_overage_total` equals `overage_total()` over the current period.
- **Integration**: same file — an organization with no subscription still gets `200` with `billing_state: "free"` and null plan/period.
- **Integration**: same file — a `RESTRICTED` organization can still read the endpoint (the read-never-blocks rule in the viewset docstring).
- **Integration**: same file — query-count assertion proving root resolution and the subtree walk each happen once per request, not once per resource.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Cross-field serializer work plus a query-shape refactor of the view, with a strict backwards-compatibility contract.

**Reusable skills**: `create-rest-endpoint` (serializer conventions, drf-spectacular annotation, schema regeneration — applied to an existing route rather than a new one).

Acceptance: `GET /billing/usage/` returns every field it returned before, unchanged, plus period bounds, plan snapshot, add-on split, per-organization attribution, and an `estimated_overage_total` equal to `MeteredOccurrenceQuerySet.overage_total()` for the current period — and resolves the billing root once per request rather than sixteen times.

~400 LoC.

---

### Phase 4 — Closed-period statement endpoints

**Goal**: a customer can list their closed billing periods and open any one of them to see what was counted and charged. Bundled per the chosen granularity: list and detail share a queryset, a permission, and a serializer tree.

**Feature flag**: none — two net-new routes no existing code reaches.

Changes:
1. @payments/billing_views.py: `BillingPeriodViewSet` (`ListModelMixin` + `RetrieveModelMixin` + `GenericViewSet`), `IsAuthenticated`, queryset filtered through `BillingPeriodSummaryQuerySet.for_organizations` over the caller's resolved pool. A pk outside the pool yields `404`, not `403`.
2. @payments/serializers.py: `BillingPeriodSummarySerializer` (list) and `BillingPeriodSummaryDetailSerializer` (adds `resources`), plus `BillingPeriodResourceUsageSerializer`. The `total` field's description states that `null` means *not recorded*, never zero. Reconciliation fields are **not** serialized.
3. @payments/filtersets.py: `BillingPeriodSummaryFilterSet` — `billing_period_start_after`, `billing_period_start_before`, `charged`.
4. @payments/routes.py: register `billing/usage/periods` with basename `BillingUsagePeriod`.
5. Detail prefetches `resources` so a statement is one query plus one, not one per resource row.
6. Regenerate `schema.yml`.

Spec use-case: **Use-case 8, Organization inspects its usage** — the historical read, which the original spec's **Negative scope** deferred.

Tests:
- **Integration**: @payments/tests/views/test_billing_period_views.py — list returns only the caller's pooled statements, newest first, paginated; the date filters and `charged` narrow correctly.
- **Integration**: same file — detail returns all eight resource rows; a `total` of `null` serializes as `null` and not `0`.
- **Integration**: same file — a statement belonging to another billing root returns `404`.
- **Integration**: same file — reconciliation fields appear nowhere in either response body.
- **Integration**: same file — an organization with no closed periods gets `200` with an empty list, not a `404`.
- **Integration**: same file — query-count assertion on detail proving `resources` is prefetched.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Standard DRF viewset + serializer + filterset wiring with close precedent in `BillingPlanViewSet` and `SubscriptionAddOnFilterSet`.

**Reusable skills**: `create-rest-endpoint` (viewset base, permissions, filterset, route registration, schema export).

Acceptance: `GET /billing/usage/periods/` lists the caller's closed statements newest-first with working date and `charged` filters, `GET /billing/usage/periods/{id}/` returns that statement with its eight resource rows and no reconciliation data, and a statement outside the caller's pool returns `404`.

~350 LoC.

---

### Phase 5 — The occurrence ledger endpoint

**Goal**: a billing owner or admin can page through every metered occurrence behind their post-paid charges, filtered to a period, and tie each unit of money to a specific occurrence.

**Feature flag**: none — one net-new route no existing code reaches.

Changes:
1. @payments/billing_views.py: `MeteredOccurrenceViewSet` (`ListModelMixin` + `GenericViewSet`). `IsAuthenticated` + `IsBillingOwnerOrAdmin`, with `check_object_permissions` called in the view body against the resolved billing root — the two-step pattern `SubscriptionViewSet` and `AddOnViewSet` already use, for the ordering reason their comments document.
2. Queryset: `MeteredOccurrence.objects.for_organizations(pool)`, defaulting to `for_billing_period(subscription.pk, current_billing_period_start(subscription))` when no period filter is supplied.
3. @payments/filtersets.py: `MeteredOccurrenceFilterSet` — `billing_period_start`, `is_within_allowance`, `organization` (**validated to be inside the caller's pool**; an id outside it is a validation error, not a silent empty result), `occurrence_start_after`, `occurrence_start_before`, plus `OrderingFilter` on `occurrence_start` defaulting to `-occurrence_start`.
4. @payments/pagination.py (new): `LargeLimitOffsetPagination` with `max_limit = 1000`.
5. @payments/serializers.py: `MeteredOccurrenceSerializer`. Event enrichment is resolved **per page, in one batch**: collect the page's `event_id`s, fetch matching `CalendarEvent`s with `select_related("calendar")` and prefetched `CalendarOwnership` (with its `membership` join), and hand the map to the serializer through context. A missing event serializes `event: null`. `event_id` is a soft `BigIntegerField`, so this cannot be a `select_related` and must never become a per-row lookup.
6. Field descriptions state that `event.title` is the **series root's** title and that `event` may be `null` for a deleted event.
7. @payments/routes.py: register `billing/usage/occurrences` with basename `BillingUsageOccurrence`.
8. Regenerate `schema.yml`.

Spec use-case: **Use-case 8, Organization inspects its usage** — sub-step 1 taken to line-item granularity, so a customer can audit rather than only observe.

Tests:
- **Integration**: @payments/tests/views/test_occurrence_ledger_view.py — a plain member gets `403`; a billing owner and an admin get `200`; an acting reseller root gets `200` for a descendant's ledger.
- **Integration**: same file — with no period filter the response covers exactly the current billing period; with an explicit `billing_period_start` it covers that closed period.
- **Integration**: same file — `is_within_allowance=false` returns exactly the rows whose `unit_price` sums to the period's `overage_total`. This is the assertion that ties the ledger to the money.
- **Integration**: same file — an `organization` filter outside the caller's pool is a validation error, not an empty `200`.
- **Integration**: same file — a row whose event was deleted serializes with `event: null` and its `unit_price` intact.
- **Integration**: same file — query-count assertion over a 50-row page proving event, calendar, and owner resolution is batched (constant queries, not 50).
- **Integration**: same file — `limit` above `max_limit` is clamped to 1000.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Cross-app batched enrichment over a soft reference, pool-validated filtering, and the object-permission ordering dance — more than standard viewset wiring.

**Review models**: reviewer Tier 3 — the permission boundary here is the one place in this plan where a mistake leaks calendar content across membership scopes. Worth an explicit review step-up above whatever the project default is for a read endpoint, though below the enforcement-path phases. Fixer stays on the project default.

**Reusable skills**: `create-rest-endpoint` (viewset, permissions, filterset, pagination, route registration, schema export).

Acceptance: a billing owner can page the current period's metered occurrences filtered to overage-only, the summed `unit_price` of those rows equals the period's `overage_total`, a non-billing member gets `403`, a deleted event's row still serializes with `event: null`, and a 50-row page resolves event data in a constant number of queries.

~450 LoC.

---

No flag-removal phase follows: no flag is introduced. See **Guiding Decisions**.

## 6. Risk & Rollout Notes

**Migration safety.** Phase 0 creates two new tables with no FK from any hot table into them; the FKs point outward to `Subscription`, `Organization`, and `Payment`. No `ALTER` on an existing table, no rewrite, no lock on anything in the request path. Reverse is a clean `DROP`.

**The riskiest change is Phase 1, and it is not a migration.** Widening `UsageCounter` touches the code path behind every limit check in the product. A grouping error does not raise — it returns a wrong number, and a wrong number here either blocks a customer who is under their limit or bills one who is over it. Mitigations: the totals-unchanged test over every `LimitedResource` member is the phase's acceptance criterion; every pre-existing enforcement test must pass with no assertion edited; the phase carries a reviewer model step-up. Rollback is a plain revert — no data has changed shape.

**Phase 2 writes inside the transaction that charges customers.** The statement write is ordered *after* `_charge_overage` and *before* `_roll_period`, and is failure-isolated: an error persisting a statement is logged and swallowed rather than allowed to roll back a committed charge or block the period from rolling. A missing statement is recoverable by re-running close, which the `(subscription, billing_period_start)` unique constraint makes safe. This is the same reasoning `MeteredOccurrence` and `ProviderWebhookEvent` already encode — the constraint is the correctness mechanism, not the code path.

Note the related standing hazard recorded in [reference: ATOMIC_REQUESTS trap] terms: a green test under test settings does not by itself prove charge/commit ordering under production settings. Phase 2's ordering assertions should be read as behavioral, and the interaction with `ATOMIC_REQUESTS` verified against production settings before the phase is considered done.

**Query-plan regressions.** The occurrence ledger reads through the existing `metered_occ_sub_period_idx` on `(subscription, billing_period_start)`. Filtering by `organization` and ordering by `occurrence_start` within a period is bounded by that index's selectivity; for a large organization this is the one query in the plan worth checking a plan for before Phase 5 merges. If it regresses, the fix is a composite index on `(subscription, billing_period_start, occurrence_start)` — deliberately not added preemptively, since it duplicates an existing index's prefix.

**Backfill.** None, by decision. The consequence is a product expectation, not a defect: on the day this ships, `GET /billing/usage/periods/` returns an empty list for every customer, and it fills one entry per closed cycle thereafter. Prepaid history for periods closed before Phase 2's deploy is permanently unavailable and is represented as `total: null` (*not recorded*), never `0`. Whoever writes the release note should say this plainly.

**Deploy ordering.** Single repo, no cross-repo producer. Phases must merge in order: Phase 2 reads the models from Phase 0 and the breakdown from Phase 1; Phase 3 reads the breakdown from Phase 1; Phase 4 reads the models from Phase 0 and is empty until Phase 2 has run at least one close. Each is independently reversible in reverse order.

**Rollback.** No feature flag exists to flip. Phases 3–5 are revert-and-deploy — no data is written by them. Phase 2 is revert-and-deploy plus, optionally, leaving already-written statements in place (they are read-only records and harmless). Phase 0's migration reverses cleanly. Phase 1 is a plain code revert.

**Client handoff.** Phases 3, 4, and 5 each change `schema.yml`. Run the `handoff-to-client` skill before merging the last of them so the web SPA team gets one coherent document covering the additive fields on `/billing/usage/` and the three new routes, rather than three partial ones.

**Observability.** Phase 2 should log at `INFO` on each statement written (subscription, period, overage total, drift) and at `ERROR` on a swallowed persistence failure — the latter is the signal that history is being lost, and it is otherwise invisible. Adoption of the new endpoints is measurable from request logs by route; no new metric infrastructure is introduced.

**Audit trail.** These are all reads plus one system-initiated write during cycle close. Per the project's audit scope (business writes only, no sync), no `AuditService` wiring is needed. Worth confirming with whoever owns that scope if statement creation should be audited.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should `BillingPeriodSummary` creation emit an audit record? | **No** — the audit trail's scope is business writes by an actor, and this is a system-initiated snapshot during cycle close. The statement row is itself the durable record. | Audit-trail owner |
| Is `max_limit = 1000` the right ceiling for the ledger? | **Yes for v1.** It bounds a single response while making an audit pull practical. Revisit if a customer's cycle routinely exceeds a few thousand occurrences. | Backend |
| Should the ledger expose the occurrence's own title for a modified occurrence, rather than the series root's? | **No for v1.** `event_id` stores the series root by design, and resolving a modified occurrence's own title means re-entering the recurrence expansion — a second opinion on data the meter already decided. Documented in the field description instead. | Product |
| Should a reseller root be able to filter the summary *down* to one child, rather than only seeing attribution? | **Not in v1.** Attribution in `by_organization` answers the question; a scoping parameter changes what "your usage" means and deserves its own decision. | Product |
| Should `estimated_overage_total` appear for a `RESTRICTED` organization? | **Yes.** The endpoint's existing rule is that a read never blocks, precisely so an organization can see what it must resolve. Hiding the number it needs would invert that. | Product |
| Does the web SPA need the closed-period endpoints at launch, or is the enriched current summary enough for the first release? | **Ship all of it** — the phases are independently mergeable, so this is a sequencing question, not a scope one. If the SPA is not ready, Phases 4 and 5 can merge and sit unconsumed. | Frontend |

## 8. Touch List

**Phase 0 — statement models**
- @payments/models.py — add `BillingPeriodSummary`, `BillingPeriodResourceUsage`
- @payments/querysets.py — add `BillingPeriodSummaryQuerySet`
- @payments/managers.py — add `BillingPeriodSummaryManager`
- @payments/migrations/00XX_billing_period_summary.py — new
- [admin.py](../payments/admin.py) — register both, read-only
- [factories.py](../payments/factories.py) — two factories
- @payments/tests/test_billing_period_summary_model.py — new

**Phase 1 — counter breakdown**
- [entitlement_service.py:56-225](../payments/services/entitlement_service.py#L56-L225) — `UsageContext`, `UsageCounter`, all eight counters
- [entitlement_service.py:347-400](../payments/services/entitlement_service.py#L347-L400) — `get_current_usage`, `_count_usage`, new `get_usage_breakdown`
- [test_usage_counters.py](../payments/tests/services/test_usage_counters.py) — totals-unchanged regression gate
- [test_pooled_limits.py](../payments/tests/services/test_pooled_limits.py) — unchanged assertions must pass
- [test_over_limit_error.py](../payments/tests/test_over_limit_error.py) — unchanged assertions must pass
- [test_usage_warning_service.py](../payments/tests/services/test_usage_warning_service.py) — unchanged assertions must pass

**Phase 2 — cycle close persistence**
- [cycle_close_service.py](../payments/services/cycle_close_service.py) — `_close_one_period` writes the statement
- [test_cycle_close.py](../payments/tests/services/test_cycle_close.py) — ordering, catch-up, plan snapshot, failure isolation
- [test_cycle_close_idempotency.py](../payments/tests/test_cycle_close_idempotency.py) — re-running close writes no second statement

**Phase 3 — enriched summary**
- [billing_views.py:83-147](../payments/billing_views.py#L83-L147) — `BillingUsageViewSet.retrieve_usage`
- [serializers.py:146-161](../payments/serializers.py#L146-L161) — `EffectiveLimitUsageSerializer`, `UsageResponseSerializer`, new nested serializers
- [test_usage_serialization.py](../payments/tests/test_usage_serialization.py)
- [test_usage_view.py](../payments/tests/views/test_usage_view.py) — backwards-compatibility contract
- [schema.yml](../schema.yml) — regenerated

**Phase 4 — statement endpoints**
- [billing_views.py](../payments/billing_views.py) — `BillingPeriodViewSet`
- [serializers.py](../payments/serializers.py) — list + detail + resource serializers
- [filtersets.py](../payments/filtersets.py) — `BillingPeriodSummaryFilterSet`
- [routes.py](../payments/routes.py) — register `billing/usage/periods`
- @payments/tests/views/test_billing_period_views.py — new
- [schema.yml](../schema.yml) — regenerated

**Phase 5 — occurrence ledger**
- [billing_views.py](../payments/billing_views.py) — `MeteredOccurrenceViewSet`
- [serializers.py](../payments/serializers.py) — `MeteredOccurrenceSerializer` + batched event enrichment
- [filtersets.py](../payments/filtersets.py) — `MeteredOccurrenceFilterSet`
- @payments/pagination.py — new, `LargeLimitOffsetPagination`
- [routes.py](../payments/routes.py) — register `billing/usage/occurrences`
- @payments/tests/views/test_occurrence_ledger_view.py — new
- [schema.yml](../schema.yml) — regenerated
