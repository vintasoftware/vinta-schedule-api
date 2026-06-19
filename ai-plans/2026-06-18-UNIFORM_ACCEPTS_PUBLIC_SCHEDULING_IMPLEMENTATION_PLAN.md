# Uniform `accepts_public_scheduling` / `is_private` Across Calendar, CalendarGroup & Bundles — Implementation Plan

> No sibling `..._SPEC.md` exists — this feature was planned directly from the prompt. The
> use-cases below are derived from the prompt and from the downstream
> [Single-Use Scheduling Codes spec](2026-06-17-SINGLE_USE_SCHEDULING_CODES_SPEC.md), which is the
> primary consumer of the `is_private` semantics defined here.

## 1. Goals

1. Make `accepts_public_scheduling` a **uniform** concept across all three bookable entities —
   `Calendar`, `CalendarGroup`, and bundle calendars (`Calendar` with `calendar_type=BUNDLE`) — by
   adding the missing field to `CalendarGroup` (Calendar/bundle already have it).
2. Expose the restricted concept on the Public GraphQL API as a derived, read-only **`is_private`**
   field on `CalendarGraphQLType`, `CalendarGroupGraphQLType`, and `CalendarBundleGraphQLType`,
   where `is_private == not accepts_public_scheduling`.
3. Accept **`is_private`** on the write surface — `CalendarGroupInput`/`UpdateCalendarGroupInput`,
   `CreateCalendarBundleInput`/`UpdateCalendarBundleInput`, `CreateResourceCalendarInput`, and a
   net-new plain-`Calendar` create/update mutation — translating it to `accepts_public_scheduling`.
4. Gate the **codeless public booking** path for `CalendarGroup` on the new group-level
   `accepts_public_scheduling`, mirroring how `Calendar`/bundle already gate via
   `CalendarPermissionService.can_perform_scheduling`, so that a private group is bookable only
   with a token or (downstream) a scheduling code.
5. Ship semantics that let the **separately-planned single-use scheduling-codes feature** treat
   `is_private == True` as "codeless public booking disallowed; a code unlocks it" — for calendars,
   groups, and bundles uniformly.

**Non-goals:**

- **Scheduling-code mint / with-code mutations** — built in
  [Single-Use Scheduling Codes plan](2026-06-17-SINGLE_USE_SCHEDULING_CODES_IMPLEMENTATION_PLAN.md).
  This plan only ships the `is_private` flag those codes unlock.
- **Replacing or restructuring `visibility`** (ACTIVE / UNLISTED / INACTIVE) — it stays orthogonal
  (listing/discovery), untouched.
- **A second boolean column** — `is_private` is a derived view over `accepts_public_scheduling`,
  not stored.
- **A feature-flag system** — none exists in the project; backward-compat is handled by field
  defaults and secure-by-default behavior (see **Guiding Decisions** and **Risk & Rollout Notes**).
- **Backfilling existing `CalendarGroup` rows** — all groups default to private; see the
  deliberate behavior change in **Risk & Rollout Notes**.
- **UI / patient-portal screens** — API only; no browser flow, so no E2E phase.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Storage shape** | Keep `accepts_public_scheduling` (`BooleanField`, `default=False`) as the single canonical knob on `Calendar` (covers bundles, since a bundle *is* a `Calendar`). Add an identical field to `CalendarGroup`. *Why:* avoids two overlapping booleans to keep in sync; `Calendar` already uses this exact field and the permission gate already reads it. |
| **`is_private` is derived, not stored** | GraphQL exposes `is_private` as `not accepts_public_scheduling` on read, and write inputs accept `is_private` and translate to `accepts_public_scheduling = not is_private`. *Why:* one source of truth; `is_private` is the external-facing, intuitive name ("restricted"), the model keeps the existing name to avoid churning the gate logic and existing migrations. |
| **Orthogonal to `visibility`** | `is_private`/`accepts_public_scheduling` controls *bookability* (whether the codeless public path is allowed); `visibility` continues to control *listing/discovery*. A calendar can be `ACTIVE` (listed) yet private (not codeless-bookable), or `UNLISTED` yet publicly bookable. *Why:* matches current `Calendar` semantics and the codes spec ("the flag only decides whether the codeless public path is allowed"). |
| **Group gate is additive & authoritative for the group path** | Group codeless public booking requires `CalendarGroup.accepts_public_scheduling == True` (or a valid group-scoped token/code). When the group authorizes the booking, the underlying per-member-calendar `create_event` is performed under that group authorization rather than re-gated by each child calendar's own flag. *Why:* the group becomes the unit of public-booking policy; member calendars stay independently gated for direct calendar booking. |
| **Default private, no backfill (CalendarGroup)** | New field defaults to `False` (private) for all rows including existing ones; no data migration flips them. *Why:* secure-by-default and consistent with `Calendar`. This is a **deliberate breaking change** — see **Risk & Rollout Notes**. |
| **No feature flag** | The project has no feature-flag module. Read/write additions are purely additive; the one behavioral change (group gating) is shipped after the group write path exists (so owners can mark groups public via API first) and is documented as a coordinated breaking change. *Why:* introducing a flag framework is out of proportion to this change; secure-by-default + phase ordering covers rollout. |
| **`is_private` on write is optional, omit = no change** | On update inputs, `is_private` is nullable; omitting it leaves `accepts_public_scheduling` untouched. On create inputs it defaults to `True` (private) to match the model default. *Why:* omit-vs-explicit distinction avoids accidentally flipping privacy on unrelated updates. |

## 3. Data Model Changes

### 3.1 `CalendarGroup.accepts_public_scheduling`

Add to `CalendarGroup` in @calendar_integration/models.py (currently lines 250–277, no privacy field):

```python
accepts_public_scheduling = models.BooleanField(
    default=False,
    help_text=(
        "If true, this group can be booked by external users through public scheduling "
        "links without a scheduling code. If false (default), the group is restricted: "
        "booking requires a token or a single-use scheduling code."
    ),
)
```

- `CalendarGroup` is an `OrganizationModel` subclass — the migration is a plain multi-tenant column
  add (no partitioning concern; `CalendarGroup` is low-cardinality per org). No `db_default`
  backfill of existing rows beyond the column default (all existing groups become private).
- Mirrors `Calendar.accepts_public_scheduling` at @calendar_integration/models.py#L108-L113 verbatim
  in shape.

### 3.2 `Calendar` / bundle

No model change — `Calendar.accepts_public_scheduling` already exists
(@calendar_integration/models.py#L108-L113) and bundles inherit it (a bundle is a `Calendar`).

### 3.3 Type plumbing

- `CalendarSettingsData` (@calendar_integration/services/dataclasses.py#L200-L203) already carries
  `accepts_public_scheduling` for calendars. For the group gate (Phase 7) add a small group
  settings carrier (or reuse a bool) so `can_perform_scheduling`'s group path can read the group's
  flag. Keep it minimal — a single bool passed into the group authorization check.

## 4. API Design

### 4.1 Read — `is_private` derived field

On each GraphQL type, expose:

```python
@strawberry_django.field
def is_private(self) -> bool:
    return not self.accepts_public_scheduling
```

- `CalendarGraphQLType` (@calendar_integration/graphql.py#L29-L42) — `accepts_public_scheduling`
  is a real column on `Calendar`; field resolves directly.
- `CalendarBundleGraphQLType` (@calendar_integration/graphql.py#L352-L367) — same backing column
  (bundle is a `Calendar`). Note: the type's docstring currently says *"No isPrivate … (non-goals —
  separate plans)"* — this plan is that separate plan; update the docstring.
- `CalendarGroupGraphQLType` (@calendar_integration/graphql.py#L338-L346) — resolves the new
  column from Phase 3.1, available after Phase 1.

### 4.2 Write — accept `is_private`

| Input | File | Field added |
|---|---|---|
| `CalendarGroupInput` | @calendar_integration/mutations.py#L306-L311 | `is_private: bool = True` |
| `UpdateCalendarGroupInput` | @calendar_integration/mutations.py#L314-L320 | `is_private: bool \| None = None` (omit = unchanged) |
| `CreateCalendarBundleInput` | @public_api/mutations.py#L384-L399 | `is_private: bool = True` |
| `UpdateCalendarBundleInput` | @public_api/mutations.py#L411-L427 | `is_private: bool \| None = None` |
| `CreateResourceCalendarInput` | @public_api/mutations.py#L157-L165 | `is_private: bool = True` |
| New `CreateCalendarInput` / `UpdateCalendarInput` | @public_api/mutations.py (new) | full plain-calendar create/update mutation carrying `is_private` |

Each resolver translates `is_private` → `accepts_public_scheduling = not is_private` before calling
the service layer.

### 4.3 Group booking gate

Group codeless public booking is authorized when `CalendarGroup.accepts_public_scheduling == True`,
or by a valid group-scoped token/code via the existing
`CalendarPermissionService.can_perform_scheduling` group path
(@calendar_integration/services/calendar_permission_service.py#L364-L376). When the group
authorizes, member-calendar `create_event` calls run under that authorization.

## 5. Phased Rollout

Phases are ordered so the model foundation lands first, the group **write** path lands before the
group **gate** (so owners can opt groups back to public via API before the gate becomes
authoritative), and the net-new plain-calendar surface is last (largest, most cuttable).

### Phase 1 — Add `accepts_public_scheduling` to `CalendarGroup`

**Goal**: ship value: none on its own — foundation column so the group can carry a privacy flag.
The field is written nowhere and read nowhere yet, so behavior is unchanged.

**Feature flag**: none — pure additive column, no reachable behavior; default `False` is inert
until Phase 7 reads it.

Changes:
1. @calendar_integration/models.py: add `accepts_public_scheduling` to `CalendarGroup` (see
   **Data Model Changes → CalendarGroup.accepts_public_scheduling**).
2. New migration in @calendar_integration/migrations/ via `makemigrations` — single `AddField`,
   `default=False`. Multi-tenant column add on an `OrganizationModel`; no backfill, no `db_default`
   data flip.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @calendar_integration/tests/test_models.py — assert a freshly created `CalendarGroup`
  has `accepts_public_scheduling is False`; assert the column accepts `True`.
- **Migration**: assert `migrate` then `migrate --reverse` is clean (forward/back).

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`.
Single-field migration with exact precedent at @calendar_integration/models.py#L108-L113.

**Reusable skills**: `add-migration`; `add-model` (for the field + factory convention).

Acceptance: `CalendarGroup` has an `accepts_public_scheduling` boolean defaulting to `False`;
migrations apply and reverse cleanly; no existing behavior changes.

### Phase 2 — Expose `is_private` (read) on the three GraphQL types

**Goal**: a Public API consumer can read whether a calendar, group, or bundle is private.

**Feature flag**: none — additive read field; existing queries are unaffected.

Changes:
1. @calendar_integration/graphql.py: add the `is_private` resolver (see **API Design → Read**) to
   `CalendarGraphQLType`, `CalendarBundleGraphQLType`, and `CalendarGroupGraphQLType`.
2. Update the `CalendarBundleGraphQLType` docstring (currently disclaims `isPrivate` as a non-goal).
3. Regenerate the GraphQL schema snapshot if the project tracks one (check for a committed schema
   under `public_api/` / `schema` artifacts).

Spec use-case: Use-case A — *consumer reads the restricted state of any bookable entity.*

Tests:
- **Integration**: @public_api/tests/ — query each type and assert `is_private == not
  accepts_public_scheduling` for both `True`/`False` backing values; assert no other field changed.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` if schema
regeneration touches >3 files) / `gpt-5-mini` / `gemini-2.5-flash`. Three near-identical resolvers
plus a schema snapshot.

**Reusable skills**: `create-graphql-public-query` (type-exposure conventions); `write-tests`.

Acceptance: querying a calendar, bundle, and group returns `is_private` reflecting the inverse of
`accepts_public_scheduling`; no write path or gating changed.

### Phase 3 — Accept `is_private` on CalendarGroup create/update inputs

**Goal**: an org-token caller can set/clear a group's privacy when creating or updating it.

**Feature flag**: none — additive input field; omitted `is_private` on update = unchanged.

Changes:
1. @calendar_integration/mutations.py: add `is_private: bool = True` to `CalendarGroupInput` and
   `is_private: bool | None = None` to `UpdateCalendarGroupInput`.
2. In the group create/update resolvers + the group service they call, translate `is_private` →
   `accepts_public_scheduling = not is_private`; on update, apply only when `is_private is not None`.

Spec use-case: Use-case B — *owner sets group privacy via the API.*

Tests:
- **Integration**: @calendar_integration/tests/ — create group with `is_private` omitted → private;
  create with `is_private=False` → public; update toggling both directions; update omitting
  `is_private` leaves `accepts_public_scheduling` untouched.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: group create/update accepts `is_private` and persists the inverse to
`accepts_public_scheduling`; omit-on-update is a no-op.

### Phase 4 — Accept `is_private` on bundle create/update inputs

**Goal**: an org-token caller can set/clear a bundle's privacy on create or update.

**Feature flag**: none — additive input field.

Changes:
1. @public_api/mutations.py: add `is_private: bool = True` to `CreateCalendarBundleInput` and
   `is_private: bool | None = None` to `UpdateCalendarBundleInput`; remove the "No isPrivate"
   docstring disclaimers.
2. Plumb to `calendar_bundle_service.create_bundle_calendar` /
   update (@calendar_integration/services/calendar_bundle_service.py) so the bundle `Calendar`'s
   `accepts_public_scheduling` is set to `not is_private`.

Spec use-case: Use-case C — *owner sets bundle privacy via the API.*

Tests:
- **Integration**: @public_api/tests/ — create bundle private by default; create public with
  `is_private=False`; update toggling; update omitting leaves it unchanged; assert the backing
  bundle `Calendar.accepts_public_scheduling`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: bundle create/update accepts `is_private` and writes the inverse to the bundle
calendar's `accepts_public_scheduling`.

### Phase 5 — Accept `is_private` on resource-calendar input

**Goal**: an org-token caller can set a resource calendar's privacy at creation.

**Feature flag**: none — additive input field.

Changes:
1. @public_api/mutations.py: add `is_private: bool = True` to `CreateResourceCalendarInput`
   (@public_api/mutations.py#L157-L165).
2. Plumb to the resource-calendar creation service so `accepts_public_scheduling = not is_private`.

Spec use-case: Use-case D — *owner sets resource-calendar privacy at creation.*

Tests:
- **Integration**: @public_api/tests/ — create resource calendar private by default; create public
  with `is_private=False`; assert persisted `accepts_public_scheduling`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: resource-calendar create accepts `is_private` and persists the inverse.

### Phase 6 — New plain-Calendar create/update mutation with `is_private`

**Goal**: an org-token caller can create/update a plain (personal) `Calendar` through the Public API
including its privacy — net-new surface (none exists today).

**Feature flag**: none — brand-new endpoint at a new path; no existing code reads/writes it.

Changes:
1. @public_api/mutations.py: add `CreateCalendarInput` / `UpdateCalendarInput` (core fields: `name`,
   `description`, plus `is_private: bool = True` on create / `is_private: bool | None = None` on
   update) and `createCalendar` / `updateCalendar` mutations returning `CalendarGraphQLType`.
2. Register the new fields and map them in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`
   (@public_api/permissions.py#L29-L74) to **granular write resources** `PublicAPIResources.CREATE_CALENDAR`
   / `PublicAPIResources.UPDATE_CALENDAR` (added to `public_api/constants.py`). **Correction during
   implementation:** the original plan said map to the generic `CALENDAR` resource, but that is the
   *read* resource used by the `calendars` query and is in `PROVIDER_SCOPED_RESOURCES` — reusing it
   would let a read-only token write, and would let a per-owner-scoped token write any owner's
   calendar. The codebase convention is granular per-action write resources
   (`CREATE_RESOURCE_CALENDAR`, `CREATE_CALENDAR_BUNDLE`, `UPDATE_CALENDAR_BUNDLE`, …) that are NOT
   provider-scoped, so calendar create/update stays org-wide-token-only. No migration is required:
   `ResourceAccess.resource_name` uses class-form `choices=PublicAPIResources`, so new enum members
   don't change migration state.
3. Resolve through a DI-injected calendar service; translate `is_private` →
   `accepts_public_scheduling`.

Spec use-case: Use-case E — *owner creates/updates a plain calendar (with privacy) via the API.*

Tests:
- **Integration**: @public_api/tests/ — create plain calendar private/public; update toggling
  privacy and other fields; authz test that the field requires the `CALENDAR` resource; cross-org
  negative test.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New mutation pair
+ permission mapping + service wiring across multiple files.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: `createCalendar` / `updateCalendar` exist on the Public GraphQL schema, are org-token
gated via the `CALENDAR` resource, and persist `accepts_public_scheduling` from `is_private`.

### Phase 7 — Gate codeless public group booking on `accepts_public_scheduling`

**Goal**: a private group (`accepts_public_scheduling=False`) can no longer be booked through the
codeless public path; booking requires a token (or, downstream, a scheduling code). Public groups
(`accepts_public_scheduling=True`) book exactly as before. **This is the behavioral phase and the
prerequisite the scheduling-codes feature builds on.**

**Feature flag**: none available. Mitigation: ships *after* Phase 3 so owners can already mark
groups public via the API; behavior change is documented in **Risk & Rollout Notes** and requires
stakeholder coordination before merge.

Changes:
1. @calendar_integration/services/calendar_permission_service.py: extend the scheduling
   authorization so the group path consults the group's `accepts_public_scheduling` (codeless grant
   when `True`), in addition to the existing group-scoped-token grant
   (@calendar_integration/services/calendar_permission_service.py#L364-L376).
2. @calendar_integration/services/calendar_group_service.py: in `create_grouped_event`, authorize
   at the group level first; when authorized, perform member-calendar `create_event` calls under the
   group authorization so a private member calendar does not independently block a group booking the
   group itself permits.
3. Thread a minimal group settings carrier (see **Type plumbing**) so the gate can read the flag.

Spec use-case: Use-case F — *codeless public booking is allowed only for non-private groups.*

Tests:
- **Integration**: @calendar_integration/tests/ —
  - public group (`accepts_public_scheduling=True`): codeless group booking succeeds.
  - private group (default): codeless group booking is rejected (`PermissionDenied`).
  - private group + valid group-scoped token: booking succeeds (existing token path intact).
  - **Backward-compat assertion**: single-`Calendar` and bundle booking behavior is unchanged by
    this phase (their gate already keys on `Calendar.accepts_public_scheduling`).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Cross-service
business-logic change with authorization branching and concurrency-adjacent booking path.

**Reusable skills**: `write-tests`; `systematic-debugging` (if the group→member authorization
threading surfaces regressions).

Acceptance: codeless public booking succeeds for public groups and is rejected for private groups;
group-scoped-token booking still works; calendar and bundle booking behavior is byte-for-byte
unchanged.

## 6. Risk & Rollout Notes

- **No feature-flag system** — the project has none, so phases rely on additivity and ordering.
  Phases 1–6 are purely additive (new column unread until Phase 7, new read field, new input
  fields, new mutation). Phase 7 is the only behavioral change.
- **Deliberate breaking change in Phase 7 (group gating).** Today a `CalendarGroup` is publicly
  bookable when its **primary member calendar** accepts public scheduling
  (gate at @calendar_integration/services/calendar_event_service.py#L249-L256). After Phase 7,
  codeless public group booking requires the **group's own** `accepts_public_scheduling`, which
  defaults to `False` for **all** existing groups (no backfill, per decision). **Any group
  currently bookable publicly via its primary calendar will stop being codeless-bookable until an
  owner sets it public.** Mitigation: (a) Phase 3 ships the group write path *before* Phase 7 so
  owners can opt groups back to public via API; (b) coordinate the Phase 7 merge with the Building
  Blocks / Medplum integration team and clinic owners (the stakeholders named in the codes spec)
  and announce which groups must be re-marked public; (c) if a softer landing is needed, a one-off
  data script (see Open Questions) can pre-set selected groups to public before Phase 7 merges.
- **Migration safety (Phase 1).** Single nullable-free `AddField` with a literal default on a
  low-volume `OrganizationModel` table; no rewrite of a hot/partitioned table, no lock concern.
  Reverse path is a clean `RemoveField`.
- **Rollback story.** Phases 2–6 roll back by removing the GraphQL field / input field / mutation
  (no data change). Phase 1 rolls back via reverse migration once Phase 7 (the only reader) is also
  reverted. Phase 7 rolls back by reverting the gate; the column can stay.
- **Semantics contract for downstream codes.** Once `is_private` is public, the scheduling-codes
  feature depends on "`is_private == True` ⇒ codeless path blocked, code unlocks". Keep the gate's
  meaning stable; treat the input/read shape as a committed external contract from launch.
- **Schema artifact.** If a GraphQL schema snapshot is committed, regenerate it in Phases 2 and 6;
  a stale snapshot will fail CI.

## 7. Open Questions

1. **Should existing publicly-bookable groups be pre-marked public before Phase 7?** Recommended
   default: no automatic backfill (per decision) — instead notify owners and let them flip via the
   Phase 3 API; offer an optional one-off script (using `add-one-off-script`) that sets
   `accepts_public_scheduling=True` for groups whose primary calendar is currently public, run only
   if stakeholders request a zero-disruption cutover. Owner: integration team + clinic owners.
2. **Plain-Calendar mutation field set (Phase 6).** Beyond `name` / `description` / `is_private`,
   should it also accept `visibility`, `manage_available_windows`, `capacity`? Recommended default:
   minimal (`name`, `description`, `is_private`) for this change; extend later if integrators ask.
   Owner: API owner.
3. **Resource-calendar update parity (Phase 5).** Only `CreateResourceCalendarInput` exists; there
   is no resource-calendar update input. Recommended default: leave update out of scope (resource
   calendars are typically recreated/disabled). Owner: API owner.

## 8. Touch List

**Phase 1 — CalendarGroup field**
- edit [models.py](../calendar_integration/models.py#L250) — add `accepts_public_scheduling`.
- new `@calendar_integration/migrations/000X_calendargroup_accepts_public_scheduling.py`.
- edit [test_models.py](../calendar_integration/tests/test_models.py) + the `CalendarGroup` factory.

**Phase 2 — `is_private` read**
- edit [graphql.py](../calendar_integration/graphql.py#L29) — `CalendarGraphQLType`,
  `CalendarGroupGraphQLType`, `CalendarBundleGraphQLType` resolvers + bundle docstring.
- regenerate committed GraphQL schema snapshot (if any).
- new/edit integration tests under @public_api/tests/.

**Phase 3 — group write**
- edit [mutations.py](../calendar_integration/mutations.py#L306) — `CalendarGroupInput`,
  `UpdateCalendarGroupInput` + resolvers; group service translation.
- edit integration tests under @calendar_integration/tests/.

**Phase 4 — bundle write**
- edit [mutations.py](../public_api/mutations.py#L384) — bundle inputs + resolvers.
- edit [calendar_bundle_service.py](../calendar_integration/services/calendar_bundle_service.py).
- edit integration tests under @public_api/tests/.

**Phase 5 — resource write**
- edit [mutations.py](../public_api/mutations.py#L157) — `CreateResourceCalendarInput` + resolver.
- edit integration tests under @public_api/tests/.

**Phase 6 — plain-Calendar mutation**
- edit [mutations.py](../public_api/mutations.py) — new `CreateCalendarInput`/`UpdateCalendarInput`
  + `createCalendar`/`updateCalendar`.
- edit [permissions.py](../public_api/permissions.py#L29) — `FIELD_TO_RESOURCE_MAPPING` entries.
- regenerate committed GraphQL schema snapshot (if any).
- new integration tests under @public_api/tests/.

**Phase 7 — group gate**
- edit [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py#L336).
- edit [calendar_group_service.py](../calendar_integration/services/calendar_group_service.py).
- edit [dataclasses.py](../calendar_integration/services/dataclasses.py#L200) — group settings carrier.
- new/edit integration tests under @calendar_integration/tests/.
