# Update Resource Calendar — Implementation Plan

Lets integrations edit a manual (`provider=INTERNAL`) resource calendar's `capacity` and other resource attributes through a dedicated Public GraphQL mutation, while rejecting Google-synced calendars. No `..._SPEC.md` sibling exists; requirements were captured via the Step-0 interrogation and the source ask in [building-blocks-integration-v3.md](../docs/building-blocks-integration-v3.md#L676-L678) ("Prompt A-2 — Resource-calendar capacity edit").

## 1. Goals

1. Add a `CalendarService.update_resource_calendar(...)` service method that partially updates a resource calendar's `capacity`, `name`, `description`, `manage_available_windows`, `accepts_public_scheduling`, and `visibility`, guarded so only `provider=INTERNAL` **and** `calendar_type=RESOURCE` calendars are editable.
2. Expose a dedicated `updateResourceCalendar` Public GraphQL mutation over that service method, org-scoped via the system-user token, gated behind a new `update_resource_calendar` resource grant.
3. Distinguish "omit `capacity`" (leave unchanged) from "explicit `null`" (clear to unlimited) at both the GraphQL and service layers.
4. Cover the INTERNAL-vs-synced guard, the wrong-type guard, and org scoping with tests at both layers.

**Non-goals:**
- Do **not** change the existing `updateCalendar` mutation or its `update_calendar` service method (they stay PERSONAL-only). This is a parallel, additive surface.
- Do **not** allow provider-scoped tokens to call this mutation — org-wide tokens only (mirrors `updateCalendar` / `createResourceCalendar` / `disableResourceCalendar`).
- Do **not** add a write path that *re-syncs* edits back to Google or any external provider. INTERNAL calendars only.
- Do **not** replace `disableResourceCalendar`; it remains the dedicated disable path even though this mutation can also set `visibility`.
- No REST surface, no bulk-upsert, no new model/table/migration.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Dedicated mutation vs. extending `updateCalendar`** | New `updateResourceCalendar` + `update_resource_calendar` service method. `updateCalendar`/`update_calendar` are hard-scoped to `CalendarType.PERSONAL` (it raises `ValueError` on any other type — see [calendar_service.py:882-886](../calendar_integration/services/calendar_service.py#L882-L886)). Overloading it would blur PERSONAL vs RESOURCE semantics and force a confusing type-branch into a method whose contract is "personal calendars only". A sibling method mirrors the existing `create_resource_calendar` / `disable_resource_calendar` pair. |
| **Editable field set** | `capacity`, `name`, `description`, `manage_available_windows`, `accepts_public_scheduling`, `visibility` — the full resource-calendar attribute set. Resource calendars can't currently edit *any* of these post-create (no path exists), so the editor is comprehensive rather than capacity-only. |
| **Guard** | Reject unless `provider == CalendarProvider.INTERNAL` **and** `calendar_type == CalendarType.RESOURCE`. Synced (`GOOGLE`/`MICROSOFT`/`APPLE`/`ICS`) calendars and non-resource calendars raise `ValueError` → surfaced as `success=false` + explicit `error_message`. |
| **Org scoping** | Lookup is `Calendar.objects.filter_by_organization(self.organization.id).get(id=calendar_id)`; the org comes from the token (`info.context.request.public_api_organization`), never from `input.organization_id`. A calendar in another org surfaces as `Calendar.DoesNotExist` → generic `"Calendar not found."` (no existence leak across orgs). |
| **`capacity` omit-vs-null** | GraphQL input field defaults to `strawberry.UNSET`; the service method uses a module-level `_UNCHANGED` sentinel. Omitted (`UNSET`) → `_UNCHANGED` → field untouched. Explicit `null` → `None` → `capacity` cleared (unlimited). Explicit int → set. `None` cannot double as "no change" here because clearing is a valid operation. |
| **Other-field semantics** | `name`, `description`, `manage_available_windows`, `accepts_public_scheduling`, `visibility` keep the existing `None = no change` partial-update convention (matching `update_calendar`). Only `capacity` needs the extra sentinel because only `capacity` supports an explicit clear. |
| **Privacy field shape** | Input exposes `is_private: bool | None` and translates to `accepts_public_scheduling = not is_private` at the resolver, identical to `updateCalendar` / `createResourceCalendar`, so the external contract stays consistent. |
| **Token scope** | New `PublicAPIResources.UPDATE_RESOURCE_CALENDAR` grant; **not** added to `PROVIDER_SCOPED_RESOURCES`. Org-wide admin tokens only. No `assert_calendar_in_owner_scope` needed. |
| **Audit** | Reuse `_audit_calendar_write(AuditAction.UPDATE, calendar, diff=compute_diff(before, after))` — same pattern as `update_calendar`. |
| **Feature flag** | **None.** Purely additive surface: brand-new mutation, brand-new resource grant, brand-new service method. No existing caller's behavior changes (the guard lives only on the new method; `update_calendar` is untouched). Per the plan-feature additive-surface exemption, no flag and therefore no flag-removal phase. |

## 3. Data Model Changes

**None.** All target fields already exist on the `Calendar` model: `capacity` ([models.py:95-102](../calendar_integration/models.py#L95-L102)), `name`, `description`, `manage_available_windows`, `accepts_public_scheduling`, `visibility`, `provider`, `calendar_type`. No migration.

### 3.1 Service method signature (new — Phase 1)

In [calendar_service.py](../calendar_integration/services/calendar_service.py), add a module-level sentinel and method alongside `update_calendar` / `disable_resource_calendar`:

```python
_UNCHANGED = object()  # module-level sentinel: "field omitted, leave unchanged"

@transaction.atomic()
def update_resource_calendar(
    self,
    calendar_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    capacity: int | None | object = _UNCHANGED,
    manage_available_windows: bool | None = None,
    accepts_public_scheduling: bool | None = None,
    visibility: str | None = None,
) -> Calendar:
    ...
```

Guard body mirrors `disable_resource_calendar` but checks both `provider` and `calendar_type`:

```python
calendar = Calendar.objects.filter_by_organization(self.organization.id).get(id=calendar_id)
if calendar.provider != CalendarProvider.INTERNAL:
    raise ValueError(
        f"Calendar {calendar_id} is synced from an external provider "
        f"(provider={calendar.provider}) and cannot be edited."
    )
if calendar.calendar_type != CalendarType.RESOURCE:
    raise ValueError(
        f"Calendar {calendar_id} is not a resource calendar (type={calendar.calendar_type})."
    )
```

Then a `before`-snapshot / per-field `update_fields` / `compute_diff` write block matching [calendar_service.py:888-912](../calendar_integration/services/calendar_service.py#L888-L912), with `capacity` written when `capacity is not _UNCHANGED`.

## 4. API Design

### 4.1 `updateResourceCalendar` mutation

Defined on the `Mutation` class in [public_api/mutations.py](../public_api/mutations.py), next to `update_calendar` ([mutations.py:1285-1321](../public_api/mutations.py#L1285-L1321)) and `disable_resource_calendar` ([mutations.py:1323+](../public_api/mutations.py#L1323)).

- **Field**: `updateResourceCalendar(input: UpdateResourceCalendarInput!): UpdateResourceCalendarResult!`
- **Permissions**: `permission_classes=[IsAuthenticated, OrganizationResourceAccess]`
- **Input** (`UpdateResourceCalendarInput`):
  - `organization_id: int` (present for contract symmetry; org is taken from token, not this field)
  - `calendar_id: int`
  - `name: str | None = None`
  - `description: str | None = None`
  - `capacity: int | None = strawberry.UNSET` — omit = unchanged; explicit `null` = clear
  - `manage_available_windows: bool | None = None`
  - `is_private: bool | None = None` — translated to `accepts_public_scheduling = not is_private`
  - `visibility: str | None = None` — accepts `CalendarVisibility` values (`ACTIVE` / `UNLISTED` / `INACTIVE`)
- **Result** (`UpdateResourceCalendarResult`): `success: bool`, `error_message: str | None`, `calendar: CalendarGraphQLType | None`
- **Resolver**: init service via `_get_org_and_init_calendar_service(info)`; translate `is_private`; translate `capacity` (`strawberry.UNSET → _UNCHANGED`, else pass through including `None`); call `calendar_service.update_resource_calendar(...)`; map `Calendar.DoesNotExist → "Calendar not found."` and `(ValueError, DjangoValidationError, IntegrityError) → str(e)`, returning `success=False`. On success return `success=True, calendar=...`.
- **Errors**:
  - synced provider → `success=false`, `error_message="Calendar … is synced from an external provider … and cannot be edited."`
  - wrong type → `success=false`, `error_message="Calendar … is not a resource calendar …"`
  - cross-org / missing id → `success=false`, `error_message="Calendar not found."`
  - missing resource grant → `OrganizationResourceAccess` denies (standard permission error, no resolver entry)

### 4.2 Permission wiring

- Add `UPDATE_RESOURCE_CALENDAR = "update_resource_calendar", "Update Resource Calendar"` to `PublicAPIResources` in [public_api/constants.py](../public_api/constants.py#L38-L39) (next to the other resource-calendar resources).
- Add `"updateResourceCalendar": PublicAPIResources.UPDATE_RESOURCE_CALENDAR` to `FIELD_TO_RESOURCE_MAPPING` in [public_api/permissions.py](../public_api/permissions.py#L62-L64).
- Do **not** add it to `PROVIDER_SCOPED_RESOURCES`.

## 5. Phased Rollout

### Phase 1 — `update_resource_calendar` service method + guard

**Goal**: `CalendarService` can partially update an INTERNAL resource calendar's attributes and rejects synced / non-resource calendars. Ship value: none externally on its own (no GraphQL surface yet) — foundation the mutation phase consumes; fully unit-testable.

**Feature flag**: none — additive new service method, no existing caller reaches it.

Changes:
1. [calendar_service.py](../calendar_integration/services/calendar_service.py): add module-level `_UNCHANGED` sentinel and `update_resource_calendar(...)` method (signature in **Data Model Changes**), placed adjacent to `update_calendar` / `disable_resource_calendar`. Reuse `is_initialized_or_authenticated_calendar_service`, the org-scoped lookup, the INTERNAL+RESOURCE guard, the `before`/`update_fields`/`compute_diff` write block, and `_audit_calendar_write(AuditAction.UPDATE, ...)`.
2. Confirm `CalendarProvider`, `CalendarType`, `CalendarVisibility` are already imported in the module (they are — used by `create_resource_calendar` / `disable_resource_calendar`).

Spec use-case: shared scaffolding for the resource-calendar edit use-case (service layer half).

Tests:
- **Unit**: `calendar_integration/tests/` (alongside existing `CalendarService` tests for `create_resource_calendar` / `update_calendar` / `disable_resource_calendar`) — covers: capacity set to int; capacity explicit `None` clears; capacity omitted (`_UNCHANGED`) leaves value untouched; name/description/`manage_available_windows`/`accepts_public_scheduling`/`visibility` partial update; `provider != INTERNAL` (Google) raises `ValueError`; `calendar_type != RESOURCE` raises `ValueError`; cross-org lookup raises `Calendar.DoesNotExist`; audit diff recorded on a real change and no audit write when nothing changes.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (with iteration) / `gpt-5-mini` / `gemini-2.5-flash`. Single-method addition with an exact in-file precedent (`disable_resource_calendar` + `update_calendar`); the only novel bit is the `_UNCHANGED` sentinel.

**Reusable skills**: none (no model/migration/view/function change); follow the existing service-test pattern in `calendar_integration/tests/`.

Acceptance: `update_resource_calendar` updates an INTERNAL resource calendar's fields with correct omit/null/value semantics for `capacity`, raises `ValueError` for synced and non-resource calendars, raises `Calendar.DoesNotExist` cross-org, writes an audit diff only when a field changed, and its unit tests pass.

### Phase 2 — `updateResourceCalendar` Public GraphQL mutation

**Goal**: An integration with the `update_resource_calendar` grant can edit a manual resource calendar's capacity (and other attributes) via GraphQL; synced and cross-org calendars are rejected with the right response shape.

**Feature flag**: none — brand-new mutation field + brand-new resource grant; no existing field changes shape.

Changes:
1. [public_api/constants.py](../public_api/constants.py#L38-L39): add `UPDATE_RESOURCE_CALENDAR` to `PublicAPIResources`.
2. [public_api/permissions.py](../public_api/permissions.py#L62-L64): add `"updateResourceCalendar"` → `UPDATE_RESOURCE_CALENDAR` to `FIELD_TO_RESOURCE_MAPPING`. Leave `PROVIDER_SCOPED_RESOURCES` unchanged.
3. [public_api/mutations.py](../public_api/mutations.py): add `UpdateResourceCalendarInput` (with `capacity: int | None = strawberry.UNSET`) and `UpdateResourceCalendarResult` near the existing resource-calendar input/result types ([mutations.py:240-274](../public_api/mutations.py#L240-L274)); add the `update_resource_calendar` resolver near [mutations.py:1285](../public_api/mutations.py#L1285), translating `is_private` and the `UNSET`→`_UNCHANGED` capacity sentinel, delegating to the Phase 1 service method, and mapping exceptions to the result shape (mirror `update_calendar` at [mutations.py:1303-1321](../public_api/mutations.py#L1303-L1321)).

Spec use-case: resource-calendar capacity/attribute edit from the integration (GraphQL surface half).

Tests:
- **Integration**: `public_api/tests/test_calendar_mutations.py` (or `test_mutations.py`, matching where `updateCalendar` / `createResourceCalendar` tests live) — covers: happy path editing `capacity` on an INTERNAL resource calendar (grant present); `capacity` explicit `null` clears it; `capacity` omitted leaves it; editing `name`/`description`/`is_private`/`manage_available_windows`/`visibility`; **synced calendar rejected** (`provider=GOOGLE` → `success=false` + provider error_message, DB row unchanged); wrong-type rejected (`PERSONAL` → `success=false`); **org scoping** (calendar in another org → `success=false` + `"Calendar not found."`; `input.organization_id` pointing elsewhere is ignored, token org wins); permission denied without the `update_resource_calendar` grant; unauthenticated denied.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches three files (constants, permissions, mutations) plus a broad integration-test matrix including the guard and org-scope edges; the `UNSET` sentinel plumbing across the GraphQL→service boundary benefits from the stronger tier.

**Reusable skills**: `create-graphql-public-query` (Public GraphQL mutation wiring: strawberry type + `mutations.py` registration + permission classes + `FIELD_TO_RESOURCE_MAPPING` entry).

Acceptance: a token with the `update_resource_calendar` grant can edit an INTERNAL resource calendar's `capacity` (set / clear / leave) and other attributes via `updateResourceCalendar`; Google-synced calendars, wrong-type calendars, and cross-org calendars are rejected with the documented `success=false` shapes; a token without the grant is denied; integration tests pass; `schema.yml`/GraphQL schema reflects the new mutation.

## 6. Risk & Rollout Notes

- **Feature flag**: none — purely additive surface (see **Guiding Decisions**). No flag-removal phase.
- **Migrations / locks**: none. No schema change; all fields pre-exist on `Calendar`.
- **Backfill**: none.
- **Existence-leak risk**: cross-org and missing-id both collapse to `"Calendar not found."`; the provider/type guards only fire *after* the org-scoped lookup succeeds, so they can never reveal another org's calendar. Phase 2 tests assert this.
- **Sentinel correctness**: the omit-vs-null distinction for `capacity` is the one subtle bug surface — `None` means "clear", `_UNCHANGED`/`strawberry.UNSET` means "leave". Both phases have explicit tests for all three states (omit / null / value).
- **Overlap with `disableResourceCalendar`**: this mutation can set `visibility=INACTIVE` too. Acceptable — they coexist; `disableResourceCalendar` stays the canonical disable verb. No deprecation in this plan.
- **Rollback**: revert the two phase PRs. Because nothing reads the new grant except the new mutation and no existing behavior changed, reverting is clean (a token holding the now-removed grant simply has a dangling resource row, harmless).
- **Phase independence**: Phase 1 merges and is useful/tested on its own (service method). Phase 2 depends on Phase 1's method but Phase 1 standing alone breaks nothing.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should `visibility` be a typed GraphQL enum rather than `str`? | Ship as `str` validated by the service (matches how visibility flows elsewhere in the public API); promote to a strawberry enum later if integrations mis-type it. | API eng |
| Should clearing `capacity` to `null` be disallowed for resources that currently have bookings exceeding no-limit assumptions? | No extra guard in v1 — `null` = unlimited is always safe (it only relaxes a constraint). Revisit only if a capacity-enforcement feature lands. | Product |
| Do integrations need a matching REST endpoint? | No — resource-calendar management is GraphQL-only per the building-blocks integration surface. | Product |

## 8. Touch List

**Phase 1 — service method**
- Edit: [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py) — add `_UNCHANGED` sentinel + `update_resource_calendar(...)`.
- Edit/Create: `calendar_integration/tests/` service tests for `update_resource_calendar` (follow existing `create_resource_calendar` / `disable_resource_calendar` test module).

**Phase 2 — GraphQL mutation**
- Edit: [public_api/constants.py](../public_api/constants.py#L38) — add `UPDATE_RESOURCE_CALENDAR`.
- Edit: [public_api/permissions.py](../public_api/permissions.py#L62) — add `FIELD_TO_RESOURCE_MAPPING` entry.
- Edit: [public_api/mutations.py](../public_api/mutations.py#L240) — add `UpdateResourceCalendarInput` / `UpdateResourceCalendarResult` + `update_resource_calendar` resolver.
- Edit: `public_api/tests/test_calendar_mutations.py` (or `test_mutations.py`) — integration tests (guard, org scoping, capacity omit/null/value, permission, auth).
- Regenerate: GraphQL schema artifact (`schema.yml` or equivalent) if the project commits a generated schema.
