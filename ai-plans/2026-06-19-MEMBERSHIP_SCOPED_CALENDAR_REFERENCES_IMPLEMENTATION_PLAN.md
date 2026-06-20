# Membership-Scoped Calendar References — Implementation Plan

> No `..._SPEC.md` sibling exists for this feature. This plan was authored
> directly from a Step-0 interrogation with the requester; the **Guiding
> Decisions** table captures every confirmed decision so the plan is
> self-contained.

## 1. Goals

1. Replace all direct `User` foreign-key references in
   [calendar_integration/models.py](../calendar_integration/models.py) with
   references to `organizations.models.OrganizationMembership`, so that calendar
   ownership / attendance / management-token actors are always scoped to a
   *membership* (a user **within a specific organization**) rather than a bare
   user that could belong to many orgs.
2. Make resolving the final `user_id` **joinless**: every referencing row
   physically carries the `user_id` (alongside its existing `organization_id`),
   and the relation to `OrganizationMembership` is expressed through the
   project's `ForeignObject` join pattern on `(organization_id, user_id)`.
3. Give `OrganizationMembership` a **composite primary key
   `(user_id, organization_id)`**, dropping its implicit `AutoField` `id`, so a
   membership's identity *is* the (user, org) pair.
4. Enforce **PROTECT** delete semantics: a membership that still owns calendars,
   has event attendances, or has live management tokens cannot be deleted.
5. Land the whole change behaviour-preservingly per relation via an
   **expand → migrate → contract** sequence, with each phase independently
   mergeable and reversible.

**Non-goals:**

- Changing `ExternalAttendee` / `EventExternalAttendance` — external attendees
  are out of scope; only the internal `User`-backed relations move.
- Reworking the tenant-resolution seam
  (`get_active_organization_membership`) or the `X-Organization-Id` header
  contract — those stay as-is.
- Introducing a runtime feature flag (see **Guiding Decisions** — a PK/schema
  refactor can't be meaningfully flag-gated).
- Changing the *meaning* of who may own a calendar or attend an event — only the
  storage/identity shape changes.
- Migrating any non-calendar `User` FK (e.g. `OrganizationInvitation.invited_by`,
  audit `created_by`-style fields) to memberships.
- Backfilling or auto-creating memberships for orphaned `(user, org)` pairs
  (those are reported, not repaired — see Backfill decision).

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Reference mechanism** | New `OrganizationMembershipForeignKey` field (modelled on [`TenantSafeForeignKey`](../common/fields.py#L29)): stores a denormalized `<name>_user_id` column and uses the row's existing `organization_id`; a `ForeignObject` joins `(organization_id, <name>_user_id)` → `OrganizationMembership(organization_id, user_id)`. **Why:** Django 6 forbids a real `ForeignKey` to a composite-PK model, so the join *must* be expressed as a `ForeignObject` regardless. Denormalizing `user_id` onto the row is what makes resolution joinless — this is true in every design, so the field type is the foundation everything else builds on. |
| **Composite PK on `OrganizationMembership`** | `pk = models.CompositePrimaryKey("user", "organization")`, dropping the implicit `id`. **Why:** explicit requester decision — a membership's identity should be the (user, org) pair. Surfaced trade-off (accepted): it provides *no additional* joinless benefit over the denormalization above, and it forces converting `OrganizationInvitation.membership` and `public_api.SystemUser.scoped_to_membership` off real FKs and rewriting ~34 `membership.id` / `.pk` references. Sequenced **last and isolated** so the high-value calendar refactor never depends on it. |
| **PROTECT enforcement** | A raw-SQL composite `FOREIGN KEY (organization_id, <name>_user_id) REFERENCES organization_membership(user_id, organization_id) ON DELETE RESTRICT`, added through the project's raw-SQL migration framework. **Why:** a `ForeignObject` carries no `on_delete` and creates no DB constraint, so PROTECT can only be guaranteed at the database level. Postgres can target the composite PK (or, pre-Phase 7, the existing `uniq_membership_user_organization` unique constraint). |
| **Backfill of orphans** | For existing rows whose `(user_id, organization_id)` has no `OrganizationMembership`, leave the membership reference null, keep `user_id`, and emit a CSV report of affected rows. **Why:** users removed from an org may still own historical calendar rows; silently re-creating memberships would re-grant access, and failing the migration would block deploys on legacy data. Non-destructive + resumable. |
| **API contract** | REST + public GraphQL switch to exposing **membership identity** instead of bare `user_id`. **Why:** explicit requester decision. Because a composite-PK membership has no scalar `id`, the external representation is an open question (see **Open Questions**) — default recommendation: expose `{ user_id, organization_id, role }` as the membership object. Consumer-visible break; handled by coordinated deploy, **not** a flag. |
| **Reverse accessors** | Drop `user.calendar_ownerships`, `user.event_attendances`, `user.calendar_events`, `user.calendar_event_management_tokens`; rewrite every `request.user`-keyed filter site to resolve the active membership first. **Why:** explicit requester decision — the clean end-state routes all calendar access through membership. The denormalized `user_id` column still allows direct `..._user_id=` filters where a raw user filter is genuinely needed. |
| **Rollout safety** | No runtime feature flag. **Why:** a primary-key change and column add/drop cannot be gated behind a boolean at runtime; the real safety mechanism is **expand → migrate → contract** phasing (new columns added & backfilled before any read switches; old columns dropped only after all reads move) plus the orphan report. Each phase is independently mergeable and reversible. |
| **Migration locking** | All column adds are nullable + `db_default`-free initially; backfills run in batched data migrations; the composite-PK swap and constraint adds use lock-aware patterns via the `migration-author` agent. **Why:** `EventAttendance` and `CalendarManagementToken` can be high-volume; a naive `ALTER` that rewrites or long-locks the table is unacceptable on hot paths. |

## 3. Data Model Changes

### 3.1 New field type — `OrganizationMembershipForeignKey`

Add to [common/fields.py](../common/fields.py), mirroring
[`TenantSafeForeignKey`](../common/fields.py#L29):

```python
class OrganizationMembershipForeignKey(models.Field):
    """
    References an OrganizationMembership by (organization_id, user_id) without a
    real DB FK (Django 6 forbids FK-to-composite-PK). Stores a denormalized
    `<name>_user_id` column and reuses the row's existing `organization_id`,
    then exposes a ForeignObject join so `.membership` / select_related works and
    `user_id` is available with no join.
    """
    tenant_field = "organization_id"

    def contribute_to_class(self, cls, name):
        # 1. Concrete column: `<name>_user_id` (plain integer/UUID matching User PK)
        # 2. ForeignObject `<name>`:
        #      from_fields=[f"{name}_user_id", "organization_id"]
        #      to_fields=["user_id", "organization_id"]  # membership's PK columns
        #      editable=False, on_delete carried for ORM only (DB enforces RESTRICT)
        ...
```

The actual PROTECT constraint is added per-table via raw SQL (see each cutover
phase); the `ForeignObject`'s `on_delete` is informational at the ORM layer.

### 3.2 `OrganizationMembership` composite primary key

In [organizations/models.py](../organizations/models.py#L196), after Phase 7:

```python
class OrganizationMembership(BaseModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="organization_memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE,
                                      related_name="memberships")
    role = ...
    is_active = ...

    pk = models.CompositePrimaryKey("user", "organization")

    class Meta:
        # `uniq_membership_user_organization` becomes redundant with the
        # composite PK and is removed in the same migration.
        ...
```

`BaseModel` ([common/models.py](../common/models.py#L22)) supplies only
`created`/`modified`/`meta` — no explicit `id` — so dropping the implicit
`AutoField` is clean, but every `.id`/`.pk` consumer must be migrated first
(Phase 7a).

### 3.3 Calendar-side model changes (per relation)

| Model | Today | After |
|---|---|---|
| [`CalendarOwnership`](../calendar_integration/models.py#L221) | `user = FK(User)` + `Calendar.users` M2M | `membership = OrganizationMembershipForeignKey(...)` storing `membership_user_id`; `Calendar.memberships` M2M through `CalendarOwnership` |
| [`EventAttendance`](../calendar_integration/models.py#L400) | `user = FK(User)` (nullable in sync path) + `CalendarEvent.attendees` M2M | `membership = OrganizationMembershipForeignKey(null=True)`; `CalendarEvent.attendee_memberships` M2M |
| [`CalendarManagementToken`](../calendar_integration/models.py#L1634) | `user = FK(User, null=True)` (alt to `external_attendee`) | `membership = OrganizationMembershipForeignKey(null=True)` |

### 3.4 Type plumbing

- `serialize_event_internal_attendee` in
  [calendar_service_utils.py](../calendar_integration/services/calendar_service_utils.py#L88)
  currently reads `attendance.user.id/.email/.get_full_name()`. After cutover it
  resolves through `attendance.membership.user` (or the denormalized
  `attendance.membership_user_id` + a single user fetch).
- `EventAttendanceInputData` (used in
  [mutations.py](../calendar_integration/mutations.py#L708),
  [serializers.py](../calendar_integration/serializers.py#L1170),
  [public_api/mutations.py](../public_api/mutations.py#L1896)) keeps a
  `user_id` field on the *input* side (callers still pass user ids) but the
  service resolves it to a membership before persisting.

## 4. API Design

The external contract change ("expose membership") is applied **inside each
relation's cutover phase**, not as one big-bang API phase, so each consumer-visible
change ships with the model change it reflects and stays reviewable.

### 4.1 GraphQL

- [calendar_integration/graphql.py](../calendar_integration/graphql.py#L258)
  `EventAttendanceGraphQLType`: replace the `user` field with a `membership`
  field resolving to a membership type exposing `{ user_id, organization_id,
  role }` (final shape pending **Open Questions**).
- Calendar owner fields likewise expose membership instead of user.

### 4.2 REST

- [calendar_integration/serializers.py](../calendar_integration/serializers.py)
  ownership + attendance serializers swap `user` representation for membership.
- Regenerate `schema.yml` (drf-spectacular) in each cutover phase.

## 5. Phased Rollout

> Ordering rationale: the joinless-`user_id` value is delivered entirely by the
> calendar-side denormalization (Phases 0–6). The composite-PK identity change
> (Phase 7) is risky, ripples across `organizations` + `public_api`, and is
> **not** a prerequisite for the calendar refactor (the `ForeignObject` and the
> raw-SQL PROTECT constraint can both target the existing
> `uniq_membership_user_organization` constraint until Phase 7). It is therefore
> sequenced last and isolated.

---

### Phase 0 — Add `OrganizationMembershipForeignKey` field type

**Goal**: ship value: none on its own — pure scaffolding the relation phases consume.

Changes:
1. [common/fields.py](../common/fields.py): add `OrganizationMembershipForeignKey`
   per **Data Model Changes → New field type**. No model uses it yet.
2. Add module docstring explaining the composite-PK / `ForeignObject` rationale.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `common/tests/test_fields.py` — define a throwaway model in the test
  using the field; assert the generated SQL JOINs on both
  `(organization_id, *_user_id)`, that `*_user_id` is a concrete column, and that
  `select_related("<name>")` resolves a membership without an extra query.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.
Subclassing Django's field machinery + `ForeignObject` wiring is subtle; one
exact precedent ([`TenantSafeForeignKey`](../common/fields.py#L29)) but novel
target shape.

**Reusable skills**: none.

Acceptance: a model declaring `OrganizationMembershipForeignKey` migrates,
queries, and `select_related`s the membership with zero extra queries; existing
suite unaffected.

---

### Phase 1 — `CalendarOwnership`: expand + backfill

**Goal**: every `CalendarOwnership` row gains a membership reference populated
from its existing `(user_id, organization_id)`; reads still use `user`. No
behaviour change.

Changes:
1. [calendar_integration/models.py](../calendar_integration/models.py#L221): add
   `membership = OrganizationMembershipForeignKey(...)` (nullable for now)
   **alongside** the existing `user` FK. Keep `Calendar.users` M2M untouched.
2. Schema migration: add the `membership_user_id` column (nullable, no rewrite).
3. Data migration (batched, resumable): set `membership_user_id = user_id` where
   an `OrganizationMembership(user_id, organization_id)` exists; for orphans leave
   null and append to a CSV report (see **Risk & Rollout Notes** → backfill).

Spec use-case: Ownership — expand step.

Tests:
- **Integration**: `calendar_integration/tests/.../test_ownership_expand.py` —
  rows with a matching membership get `membership_user_id` set; orphan rows stay
  null and appear in the report; **behaviour-unchanged** assertion: all existing
  ownership reads (via `user`) return identical results.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.
Batched data migration + orphan reporting.

**Reusable skills**: `add-migration`; `add-one-off-script` (if the backfill is
large enough to warrant the operational-script contract rather than a data
migration).

Acceptance: post-migrate, `membership_user_id` is populated for every
non-orphan ownership; orphan count matches the emitted report; no read path
changed.

---

### Phase 2 — `CalendarOwnership`: cutover (drop `user`, expose membership)

**Goal**: ownership reads/writes, the `Calendar` M2M, and the ownership API all
go through membership; the `user` FK and its reverse accessor are removed; PROTECT
is enforced.

Changes:
1. Rewrite all `CalendarOwnership.user` read/write sites to use `membership` /
   `membership_user_id`:
   - [calendar_service.py](../calendar_integration/services/calendar_service.py#L558)
     (creates at 558/649/697; reads at 837, 1274–1275)
   - [calendar_bundle_service.py](../calendar_integration/services/calendar_bundle_service.py#L202)
   - [calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py#L385)
     (`update_or_create`)
   - [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py#L120)
     (120/133/397/424/455)
   - [views.py](../calendar_integration/views.py#L245) (245/295/520/620/634/1151 —
     resolve `request.user` → active membership first)
   - [permissions.py](../calendar_integration/permissions.py#L25) (25/78/82)
   - [serializers.py](../calendar_integration/serializers.py#L1048)
2. Replace `Calendar.users` M2M with `Calendar.memberships` (through
   `CalendarOwnership`, `through_fields=("calendar_fk", "membership")`). If Django
   can't drive an M2M through a `ForeignObject`-to-composite target, drop the M2M
   sugar and expose an explicit `Calendar.ownerships`-based queryset helper
   (validate early — see Risk).
3. API: swap ownership `user` representation for membership in REST serializers +
   GraphQL; regenerate `schema.yml`.
4. Drop the `user` FK column + reverse accessor `user.calendar_ownerships`.
5. Add raw-SQL composite FK constraint
   `(organization_id, membership_user_id) → organization_membership` with
   `ON DELETE RESTRICT` (PROTECT).

Spec use-case: Ownership — cutover step.

Tests:
- **Unit**: permission/owner-resolution helpers resolve via membership.
- **Integration**: ownership create/read/update/delete through services + views;
  default-calendar resolution; group-slot access; deleting a membership with a
  live ownership raises (PROTECT); deleting a User with a live ownership is
  blocked through the membership cascade (documented behaviour change).
- **E2E**: not required — no new browser flow (API/service only).

**Suggested AI model**: Tier 4 — `claude-opus-4-7[1m]`. Multi-file behavioural
rewrite across services/views/permissions/serializers + M2M reshape + raw-SQL
constraint; the largest cutover. Consider splitting into **2a** (service/permission
layer + model) and **2b** (views + API + M2M + constraint) if the diff exceeds
~300 LoC.

**Reusable skills**: `add-migration`; `create-postgres-function` /
`add-migration` for the raw-SQL FK constraint; `create-rest-endpoint` +
`create-graphql-public-query` patterns for the API reshape.

Acceptance: no reference to `CalendarOwnership.user` or
`user.calendar_ownerships` remains (`grep` clean); ownership APIs return
membership; PROTECT is enforced; full suite green.

---

### Phase 3 — `EventAttendance`: expand + backfill

**Goal**: every `EventAttendance` row gains a nullable membership reference
backfilled from `(user_id, organization_id)`; reads still use `user`. No
behaviour change.

Changes:
1. [models.py](../calendar_integration/models.py#L400): add nullable
   `membership = OrganizationMembershipForeignKey(null=True)` alongside `user`.
   `user` is already nullable in the sync path
   ([calendar_sync_service.py:814](../calendar_integration/services/calendar_sync_service.py#L814))
   — null users stay null memberships.
2. Schema migration: add `membership_user_id` (nullable, no rewrite).
3. Batched data migration: backfill from `user_id`; orphans → null + report.

Spec use-case: Attendance — expand step.

Tests:
- **Integration**: backfill populates non-orphans; null-user attendances stay
  null; behaviour-unchanged assertion on all attendance reads.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.

**Reusable skills**: `add-migration`; `add-one-off-script` if large.

Acceptance: `membership_user_id` populated for non-orphan attendances; report
matches orphan count; no read path changed.

---

### Phase 4 — `EventAttendance`: cutover (+ `attendees` M2M, API)

**Goal**: attendance reads/writes, the `CalendarEvent` attendee M2M, and the
attendance API go through membership; `user` FK + reverse accessor removed;
PROTECT enforced.

Changes:
1. Rewrite `EventAttendance.user` sites:
   - [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py#L437)
     (437/439/545/632/724/729/766 — bulk create, `{a.user_id: a}` maps,
     `User.objects.get(id=attendance.user_id)`)
   - [calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py#L809)
     (809/812/842/814/890)
   - [calendar_service_utils.py](../calendar_integration/services/calendar_service_utils.py#L88)
     (88–98 serialize; 203 `user__id__in` filter)
   - [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py#L171)
     (171–172, 286/288 attendee-diff for permissions)
2. Replace `CalendarEvent.attendees` M2M with `CalendarEvent.attendee_memberships`
   (through `EventAttendance`). Same Django-M2M-over-composite caveat as Phase 2.
3. API: `EventAttendanceGraphQLType.user` → membership
   ([graphql.py:258](../calendar_integration/graphql.py#L258)); REST serializers
   ([serializers.py:1237](../calendar_integration/serializers.py#L1237)) and
   `EventAttendanceInputData` resolution
   ([mutations.py:708](../calendar_integration/mutations.py#L708),
   [public_api/mutations.py:1896](../public_api/mutations.py#L1896)). Input keeps
   `user_id`; service resolves to membership. Regenerate `schema.yml`.
4. Drop `user` FK + `user.event_attendances` / `user.calendar_events`.
5. Raw-SQL composite FK constraint (nullable, `ON DELETE RESTRICT`).

Spec use-case: Attendance — cutover step.

Tests:
- **Unit**: attendee serialization resolves name/email via membership.user.
- **Integration**: event create/update with attendees; sync path with null-user
  attendances; attendee-diff permission checks; PROTECT on membership delete;
  adapter ingestion ([google](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py#L301),
  [outlook](../calendar_integration/services/calendar_adapters/ms_outlook_calendar_adapter.py#L218))
  still maps external attendee data unchanged.
- **E2E**: not required — API/service only.

**Suggested AI model**: Tier 4 — `claude-opus-4-7[1m]`. Touches the sync engine,
event service, permission diffing, adapters, and public API. Split **4a**
(service/sync/permission) / **4b** (M2M + API + constraint) if >300 LoC.

**Reusable skills**: `add-migration`; `create-graphql-public-query`.

Acceptance: no `EventAttendance.user` / `user.event_attendances` /
`user.calendar_events` references remain; attendance APIs return membership;
sync + adapters unchanged; PROTECT enforced; suite green.

---

### Phase 5 — `CalendarManagementToken`: expand + backfill

**Goal**: tokens gain a nullable membership reference backfilled from
`(user_id, organization_id)`; reads still use `user`. No behaviour change.

Changes:
1. [models.py](../calendar_integration/models.py#L1691): add nullable
   `membership = OrganizationMembershipForeignKey(null=True)` alongside the
   nullable `user`. (`external_attendee`-backed tokens keep null user/membership.)
2. Schema migration: add `membership_user_id` (nullable).
3. Batched data migration: backfill from `user_id`; orphans → null + report.

Spec use-case: Management-token actor — expand step.

Tests:
- **Integration**: backfill populates non-orphan tokens; external-attendee tokens
  stay null; token consume/permission reads unchanged.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6`
if the data migration mirrors Phases 1/3 closely it's near-mechanical).

**Reusable skills**: `add-migration`.

Acceptance: `membership_user_id` populated for non-orphan, user-backed tokens;
report matches orphan count; no read path changed.

---

### Phase 6 — `CalendarManagementToken`: cutover (+ API)

**Goal**: token minting, consumption, permission resolution, and the token API
go through membership; `user` FK + reverse accessor removed; PROTECT enforced.

Changes:
1. Rewrite `CalendarManagementToken.user` sites:
   - [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py#L82)
     (82/87/115/127/137 lookups; 192 `user_id ==` check; 421/452/485/512
     `get_or_create(user=...)`; 581/585 mint with `minted_by_system_user`;
     647/657/705/723/744/747 consume)
   - [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py#L468)
     (468–469/799–802/1229–1232 audit actor `permission_token.user`)
   - [calendar_service_utils.py](../calendar_integration/services/calendar_service_utils.py#L296)
     (296–307/322–332 token create with `user=owner`)
   - [token_views.py](../calendar_integration/token_views.py),
     [managers.py](../calendar_integration/managers.py) (`consume`,
     `get_token_error_code`)
2. API: expose membership where token actor is surfaced; regenerate `schema.yml`.
3. Drop `user` FK + `user.calendar_event_management_tokens`. (`minted_by_system_user`
   stays — it points at `public_api.SystemUser`, untouched here.)
4. Raw-SQL composite FK constraint (nullable, `ON DELETE RESTRICT`).

Spec use-case: Management-token actor — cutover step.

Tests:
- **Integration**:
  [test_management_token_manager.py](../calendar_integration/tests/test_management_token_manager.py),
  [test_calendar_permission_service_codes.py](../calendar_integration/tests/test_calendar_permission_service_codes.py)
  updated — mint/consume/permission resolution via membership; external-attendee
  token path unchanged; audit-actor resolution; PROTECT on membership delete.
- **E2E**: not required.

**Suggested AI model**: Tier 4 — `claude-opus-4-7[1m]`. The permission/token
system is correctness-critical (consume() atomicity, audit actor).

**Reusable skills**: `add-migration`.

Acceptance: no `CalendarManagementToken.user` /
`user.calendar_event_management_tokens` references remain; token mint/consume/
permission flows pass; PROTECT enforced; suite green.

---

### Phase 7 — `OrganizationMembership` composite primary key

> Isolated, highest-risk identity change. Prerequisite: Phases 1–6 merged (so
> the calendar tables already join via `(organization_id, user_id)` and don't
> rely on `membership.id`).

#### Phase 7a — Convert remaining real FKs + rewrite `.id` references

**Goal**: remove every dependency on `OrganizationMembership.id` / `.pk` so the
PK can be swapped. Ship value: none on its own (enabling step).

Changes:
1. `OrganizationInvitation.membership`
   ([organizations/models.py:312](../organizations/models.py#L312)): convert the
   `OneToOneField` off a real FK to a `ForeignObject`-based one-to-one on
   `(organization_id, membership_user_id)` (or resolve membership by
   `(organization, accepted-user)` lookup and drop the stored link). Decide which
   in implementation; the OneToOne is nullable so either is viable.
2. `public_api.SystemUser.scoped_to_membership`
   ([public_api/models.py:23](../public_api/models.py#L23)): same conversion —
   this FK underpins per-owner-scoped tokens; reroute to the membership's
   `(organization_id, user_id)` columns.
3. Rewrite the ~34 non-test `membership.id` / `.pk` / `membership_id` references
   across `organizations/{services,querysets,filtersets,serializers,views,permissions}.py`,
   `public_api/{services,mutations,types,serializers,queries}.py`,
   [common/utils/view_utils.py](../common/utils/view_utils.py),
   [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py),
   [webhook_membership_side_effects.py](../webhooks/services/webhook_membership_side_effects.py)
   to use `(user_id, organization_id)` instead of a scalar id. Update admin and any
   GraphQL/REST type that exposes a membership `id`.

Spec use-case: shared enabling step — no use-case.

Tests:
- **Integration**: invitation accept still links/ resolves the created membership;
  scoped SystemUser tokens still scope correctly; membership list/detail APIs and
  webhook side-effects pass with no `.id` usage.

**Suggested AI model**: Tier 4 — `claude-opus-4-7[1m]`. Cross-app, correctness-
critical (token scoping + invitation accept).

**Reusable skills**: `add-migration`.

Acceptance: `grep -rn "membership\.\(id\|pk\)\|membership_id" --include=*.py`
(excluding migrations/tests) returns zero; invitation + scoped-token suites green.

#### Phase 7b — Swap to `CompositePrimaryKey`

**Goal**: `OrganizationMembership` PK becomes `(user, organization)`; implicit
`id` dropped.

Changes:
1. [organizations/models.py:196](../organizations/models.py#L196): add
   `pk = models.CompositePrimaryKey("user", "organization")`; remove the now-
   redundant `uniq_membership_user_organization` constraint.
2. Lock-aware migration (raw-SQL via `migration-author`): drop the `id` column +
   its sequence, add the composite PK, repoint the calendar PROTECT FK constraints
   from the old unique constraint to the composite PK.
3. Verify `BaseOrganizationModelManager`/`OrganizationMembershipManager` queries
   and `filter_by_organization` still behave (no `id`-based assumptions).

Spec use-case: composite-PK identity (requester decision).

Tests:
- **Integration**: membership create/get/update/delete by `(user, organization)`;
  manager methods; admin; the PROTECT constraints from Phases 2/4/6 now reference
  the composite PK and still block deletes.
- **Migration test**: forward + reverse migration on a seeded DB.

**Suggested AI model**: Tier 4 — `claude-opus-4-7[1m]`. A live PK swap on a
referenced, multi-tenant table.

**Reusable skills**: `add-migration` (+ `migration-author` sub-agent).

Acceptance: `OrganizationMembership` has no `id` column; PK is
`(user_id, organization_id)`; all suites green; forward/reverse migration clean
on seeded data.

---

> **No feature-flag-removal phase**: per **Guiding Decisions**, this refactor
> ships no runtime feature flag, so the mandatory flag-removal phase does not
> apply.

## 6. Risk & Rollout Notes

- **No runtime feature flag.** Safety is structural: every relation is expanded
  (new nullable column + backfill) and proven behaviour-neutral *before* its
  cutover phase flips reads; old columns are dropped only in the cutover, which
  is independently reversible. Composite-PK (Phase 7) is isolated last.
- **Backfill / orphan report.** Each expand phase emits a CSV of
  `(table, row_id, user_id, organization_id)` for `(user, org)` pairs with no
  membership. Backfills are **batched, idempotent, resumable** (re-running skips
  already-populated rows). Prefer the `add-one-off-script` contract for any table
  large enough that a single data migration would lock too long.
- **Locks on hot tables.** `EventAttendance` and `CalendarManagementToken` may be
  high-volume. Column adds are nullable (no table rewrite). The composite FK
  constraints are added `NOT VALID` then `VALIDATE CONSTRAINT` in a separate step
  to avoid a long `ACCESS EXCLUSIVE` hold. The Phase 7b PK swap is the riskiest
  lock — schedule in a low-traffic window; `migration-author` produces the
  lock-aware SQL + reverse path.
- **Documented behaviour change — User deletion.** With PROTECT, deleting a
  `User` (which cascades to their memberships) is now **blocked** if any
  membership still owns a calendar / has attendance / has a live token. Previously
  those rows cascaded away with the user. Operationally, user deletion now
  requires clearing or reassigning calendar rows first. Call this out in release
  notes.
- **Django M2M over a `ForeignObject`/composite target.** `Calendar.memberships`
  and `CalendarEvent.attendee_memberships` (Phases 2/4) rely on a `ManyToManyField`
  whose `through_fields` point at a `ForeignObject` to a composite-PK model — not a
  proven Django path. **Validate in a spike at the start of Phase 2**; fallback is
  to drop the M2M convenience field and expose explicit through-model querysets.
- **API contract break.** Switching REST + GraphQL from `user_id` to membership
  is consumer-visible. Coordinate the deploy with API consumers; if any external
  integration depends on the current shape, version the affected
  endpoints/types rather than breaking in place.
- **Rollback.** Each cutover is reversible by re-adding the dropped `user` column
  from the retained `membership_user_id` (data is identical) and reverting the
  code. Phase 7b reverse migration re-adds the `id` column + sequence.

## 7. Open Questions

1. **External membership identifier.** A composite-PK membership has no scalar
   `id`. What does the API expose? *Recommended default:* a `membership` object
   `{ user_id, organization_id, role }` (org is usually implicit from request
   context, so this is effectively `user_id` + `role`). Owner: API consumers /
   eng lead. Must be resolved before Phase 2's API reshape.
2. **`OrganizationInvitation.membership` link strategy** (Phase 7a): keep a stored
   `ForeignObject` one-to-one on `(organization_id, membership_user_id)`, or drop
   the stored link and resolve membership by `(organization, accepted-user)` on
   demand? *Recommended default:* keep the stored `ForeignObject` link for
   parity. Owner: eng.
3. **Are all internal `EventAttendance.user` rows guaranteed org members?** If a
   non-trivial number are orphans (Phase 3 report), the attendee API may surface
   null memberships for historical events — confirm that's acceptable vs. a
   one-off cleanup. Owner: product.

## 8. Touch List

**Phase 0**
- edit: [common/fields.py](../common/fields.py)
- new: `common/tests/test_fields.py` (or extend existing)

**Phase 1** (ownership expand)
- edit: [calendar_integration/models.py](../calendar_integration/models.py#L221)
- new: `calendar_integration/migrations/00XX_calendarownership_membership.py`
- new: ownership backfill data migration / one-off script
- new: `calendar_integration/tests/.../test_ownership_expand.py`

**Phase 2** (ownership cutover)
- edit: [calendar_service.py](../calendar_integration/services/calendar_service.py),
  [calendar_bundle_service.py](../calendar_integration/services/calendar_bundle_service.py),
  [calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py),
  [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py),
  [views.py](../calendar_integration/views.py),
  [permissions.py](../calendar_integration/permissions.py),
  [serializers.py](../calendar_integration/serializers.py),
  [graphql.py](../calendar_integration/graphql.py),
  [calendar_integration/models.py](../calendar_integration/models.py#L143) (M2M),
  `schema.yml`
- new: cutover migration + raw-SQL PROTECT constraint migration

**Phase 3** (attendance expand)
- edit: [calendar_integration/models.py](../calendar_integration/models.py#L400)
- new: migration + backfill + `test_attendance_expand.py`

**Phase 4** (attendance cutover)
- edit: [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py),
  [calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py),
  [calendar_service_utils.py](../calendar_integration/services/calendar_service_utils.py),
  [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py),
  [mutations.py](../calendar_integration/mutations.py),
  [serializers.py](../calendar_integration/serializers.py),
  [graphql.py](../calendar_integration/graphql.py#L258),
  [public_api/mutations.py](../public_api/mutations.py#L1896),
  [calendar_integration/models.py](../calendar_integration/models.py#L1072) (M2M),
  `schema.yml`
- new: cutover migration + raw-SQL PROTECT constraint migration

**Phase 5** (token expand)
- edit: [calendar_integration/models.py](../calendar_integration/models.py#L1691)
- new: migration + backfill + token-expand test

**Phase 6** (token cutover)
- edit: [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py),
  [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py),
  [calendar_service_utils.py](../calendar_integration/services/calendar_service_utils.py),
  [token_views.py](../calendar_integration/token_views.py),
  [managers.py](../calendar_integration/managers.py), `schema.yml`
- edit tests: [test_management_token_manager.py](../calendar_integration/tests/test_management_token_manager.py),
  [test_calendar_permission_service_codes.py](../calendar_integration/tests/test_calendar_permission_service_codes.py)
- new: cutover migration + raw-SQL PROTECT constraint migration

**Phase 7a** (FK conversions + `.id` rewrites)
- edit: [organizations/models.py](../organizations/models.py#L312) (invitation),
  [public_api/models.py](../public_api/models.py#L23) (`scoped_to_membership`),
  `organizations/{services,querysets,filtersets,serializers,views,permissions}.py`,
  `public_api/{services,mutations,types,serializers,queries}.py`,
  [common/utils/view_utils.py](../common/utils/view_utils.py),
  [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py),
  [webhook_membership_side_effects.py](../webhooks/services/webhook_membership_side_effects.py)
- new: migrations for the two FK conversions

**Phase 7b** (composite PK)
- edit: [organizations/models.py](../organizations/models.py#L196)
- new: lock-aware composite-PK migration (forward + reverse) + repoint of the
  three calendar PROTECT constraints
