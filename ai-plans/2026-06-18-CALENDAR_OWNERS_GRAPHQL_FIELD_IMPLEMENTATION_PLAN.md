# Calendar `owners` GraphQL Field — Implementation Plan

No dedicated `_SPEC.md` exists for this slice. The source of record is the "owners" gap documented in
[docs/building-blocks-integration-v2.md](../docs/building-blocks-integration-v2.md) under
**Appointment Types & Calendar Groups & Bundles (Admin)** (the `owners { ... }` ❌ markers in the
`calendarGroups` query and the "Gaps" note). This plan translates that gap into phased delivery.

## 1. Goals

1. `CalendarGraphQLType` in the Public GraphQL API exposes an `owners` field resolving to a list of
   ownership records shaped `{ id, isDefault, user { id, email, profile { firstName, lastName, profilePicture } } }`,
   so the Provider/Admin app can display who owns a calendar, group slot calendar, or bundle child.
2. `CalendarBundleGraphQLType` (the bundle parent type) exposes the same `owners` field, so a bundle
   calendar surfaces its own owners in one pass.
3. Selecting `owners` (including the nested `user.profile`) across every entry point that returns a
   calendar — `calendars`, `calendarGroups` → slots → calendars, `calendarBundles` → children — does
   **not** trigger N+1 queries.
4. Owner data is strictly organization-scoped: a token for organization A can never read owner rows,
   user emails, or profiles belonging to organization B, proven by an adversarial cross-org test.

**Non-goals:**
- No new `PublicAPIResources` value and no new entry in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`.
  The existing `CALENDAR` resource already gates every calendar-returning query; `owners` is a nested
  field under those queries and inherits that gate.
- No filtering of `owners` by a per-owner-scoped token — a scoped token already only reaches its own
  calendars, and co-owners are same-organization data, so all co-owners are returned (see
  **Guiding Decisions**). The per-owner token scoping itself is the separate
  [2026-06-17 tokens plan](2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_IMPLEMENTATION_PLAN.md).
- No `isPrivate` field on `CalendarGroup` (the other gap in the same doc section) — separate work.
- No mutations. `owners` is read-only.
- No REST surface change — this is Public GraphQL only.
- No write path on `CalendarOwnership` (create/update/delete of ownerships) — read exposure only.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Owner type** | New `CalendarOwnershipGraphQLType` wrapping the `CalendarOwnership` through-model — `id` = the ownership row pk, `is_default` = the ownership flag, `user` = the owning `User`. We expose the ownership row (not a flattened user) because the app needs the row identity and `isDefault` per the requested shape `{ id, user { ... } }`. The outer `id` is the **ownership** id, not the user id. |
| **User/Profile reuse** | Reuse the existing `UserGraphQLType` and `ProfileGraphQLType` from [users/graphql.py](../users/graphql.py) — they already expose `id`, `email`, and `profile { first_name, last_name, profile_picture }`. No new user/profile type. |
| **Field placement** | Add `owners` to `CalendarGraphQLType` (covers `calendars`, group-slot `calendars`, and bundle `children` — all resolve to `CalendarGraphQLType`) **and** to `CalendarBundleGraphQLType` (the bundle parent). These are the only two calendar-shaped public types that need it. |
| **Org-scoping correctness** | `CalendarOwnership` is an `OrganizationModel` whose `calendar` FK is an `OrganizationForeignKey`. Every `owners` resolution starts from a calendar already filtered by `filter_by_organization(org.id)`, so traversing `ownerships` can only reach same-org ownership rows. The owning `User` is a global record, but it is only reachable because it owns a calendar **in this org** — no cross-org leak path exists. Tests assert this explicitly rather than relying on the argument. |
| **Scoped-token owner visibility** | A per-owner-scoped token sees **all** co-owners of the calendars it can reach. The token's scope already constrains *which calendars* it sees (sibling plan); co-owners of a visible calendar are same-org data, so no additional filtering on `owners` is applied. |
| **N+1 strategy** | Two layers. (a) Field-level `strawberry_django.field(prefetch_related=[...])` so the strawberry-django optimizer prefetches `ownerships__user__profile` for optimizer-driven paths. (b) Explicit `.prefetch_related(...)` on the resolvers that build their own querysets / bypass the optimizer (`calendars`, `calendar_groups`, `calendar_bundles`, and the bundle `children` resolver). Query-count assertions guard both. |
| **Feature flag** | **None.** Adding GraphQL fields is purely additive: a field is only resolved when a client explicitly selects it, and no field exists yet, so no existing query path changes shape or cost. The repo also has no feature-flag infra. Justified skip under the "purely additive new surface" rule. |
| **`is_default` exposure** | Exposed as `isDefault` on the owner type so the app can mark a calendar's default owner. Cheap — already on the ownership row we load. |

## 3. Data Model Changes

No database changes. `CalendarOwnership` already exists with the `ownerships` related name and the
`is_default` column; `Profile` already exists with `first_name` / `last_name` / `profile_picture`.

### 3.1 New GraphQL type — `CalendarOwnershipGraphQLType`

New `@strawberry_django.type(CalendarOwnership)` in [calendar_integration/graphql.py](../calendar_integration/graphql.py)
(near `CalendarGraphQLType`, around line 30):

```python
from users.graphql import UserGraphQLType  # reuse existing user/profile types

@strawberry_django.type(CalendarOwnership)
class CalendarOwnershipGraphQLType:
    id: strawberry.auto  # noqa: A003  -> CalendarOwnership.pk
    is_default: strawberry.auto
    user: UserGraphQLType = strawberry_django.field()
```

`CalendarOwnership` is modeled in [calendar_integration/models.py:221-248](../calendar_integration/models.py#L221-L248).
`UserGraphQLType` / `ProfileGraphQLType` are in [users/graphql.py:9-27](../users/graphql.py#L9-L27).

### 3.2 `CalendarGraphQLType.owners`

Add to `CalendarGraphQLType` at [calendar_integration/graphql.py:30-43](../calendar_integration/graphql.py#L30-L43):

```python
    owners: list[CalendarOwnershipGraphQLType] = strawberry_django.field(
        prefetch_related=["ownerships__user__profile"],
    )
```

The default resolver reads the `ownerships` related manager; the `prefetch_related` hint feeds the
strawberry-django optimizer.

### 3.3 `CalendarBundleGraphQLType.owners`

Add the identical field to `CalendarBundleGraphQLType` at
[calendar_integration/graphql.py:353-367](../calendar_integration/graphql.py#L353-L367). Because that
type also maps `@strawberry_django.type(Calendar)`, the same `ownerships__user__profile` prefetch and
`CalendarOwnershipGraphQLType` apply.

### 3.4 Type plumbing

- Import `CalendarOwnership` model + `UserGraphQLType` into `calendar_integration/graphql.py`.
- Confirm no import cycle between `calendar_integration/graphql.py` and `users/graphql.py` (users is a
  leaf app; the import direction is calendar → users, which is safe).

## 4. API Design

No new query or mutation. `owners` is a nested field reachable through the **existing** queries in
[public_api/queries.py](../public_api/queries.py):

| Query | Returns | Owners reachable via |
|---|---|---|
| `calendars` | `list[CalendarGraphQLType]` | `calendar.owners` |
| `calendarGroups` → `slots` → `calendars` | `list[CalendarGraphQLType]` | each slot calendar's `owners` |
| `calendarBundles` | `list[CalendarBundleGraphQLType]` + `children` | bundle `owners` and each child's `owners` |

GraphQL selection shape exposed to clients:

```graphql
calendars {
  id name
  owners {
    id
    isDefault
    user { id email profile { firstName lastName profilePicture } }
  }
}
```

Permission: unchanged. `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` already maps `calendars`,
`calendarGroups`, and `calendarBundles` to `PublicAPIResources.CALENDAR`
([public_api/permissions.py:25-92](../public_api/permissions.py#L25-L92)). Nested fields are not
separately gated by this class, so no mapping entry is added.

## 5. Phased Rollout

### Phase 1 — Expose `owners` on `CalendarGraphQLType`

**Goal**: the `calendars` query (and any path resolving `CalendarGraphQLType`) can return
`owners { id isDefault user { id email profile { firstName lastName profilePicture } } }`, org-scoped,
without N+1 on the `calendars` query.

**Feature flag**: none — purely additive GraphQL field (see **Guiding Decisions**).

Changes:
1. [calendar_integration/graphql.py](../calendar_integration/graphql.py): import `CalendarOwnership`
   and `UserGraphQLType`; add `CalendarOwnershipGraphQLType` (`id`, `is_default`, `user`); add
   `owners: list[CalendarOwnershipGraphQLType]` with `prefetch_related=["ownerships__user__profile"]`
   to `CalendarGraphQLType`.
2. [public_api/queries.py](../public_api/queries.py): in the `calendars` resolver
   ([lines 270-315](../public_api/queries.py#L270-L315)), add
   `.prefetch_related("ownerships__user__profile")` to the queryset so the explicit `list(queryset)`
   materialization is N+1-free regardless of the optimizer.

Spec use-case: expose calendar owners (the `owners` gap in the building-blocks doc) — primary entry
point `calendars`.

Tests:
- **Integration**: [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — extend the
  calendar query tests: (a) a calendar with two `CalendarOwnership` rows returns both, with correct
  `id` (ownership pk), `isDefault`, and nested `user.email` + `user.profile.firstName/lastName/profilePicture`;
  (b) **org-scoping**: a calendar in org B owned by a user in org B is *not* returned to an org-A token,
  and querying org A never surfaces org-B owner emails/profiles (cross-org leak assertion using two
  organizations + two system users); (c) **N+1**: assert the query count for `calendars { owners { user { profile } } }`
  over N calendars is constant (use `django_assert_max_num_queries` / `CaptureQueriesContext`), not
  O(N). Reuse fixtures at [test_queries.py:63-126](../public_api/tests/test_queries.py#L63-L126) and the
  ownership-creation pattern at [test_queries.py:1097-1152](../public_api/tests/test_queries.py#L1097-L1152).

**Suggested AI model**: Tier 2 — `claude-sonnet-4-6` / `gpt-5-mini` / `gemini-2.5-flash`. New strawberry
type + field with exact precedent, but the N+1 and cross-org tests need care.

**Reusable skills**: `create-graphql-public-query` (type/field + permission-mapping conventions, even
though no new top-level field is added — it documents the prefetch + scoping patterns); `write-tests`.

Acceptance: `query { calendars { owners { id isDefault user { id email profile { firstName lastName profilePicture } } } } }`
returns each calendar's ownerships for the caller's org, an org-A token sees zero org-B owner data, and
the resolved query count is constant in the number of calendars.

### Phase 2 — N+1 hardening for group and bundle entry points

**Goal**: selecting `owners` through `calendarGroups` (slot calendars), `calendarBundles`, and bundle
`children` is also N+1-free — the resolvers that build their own querysets or bypass the optimizer
prefetch ownerships too.

**Feature flag**: none — additive.

Changes:
1. [public_api/queries.py](../public_api/queries.py): `calendar_bundles` resolver
   ([lines ~679-690](../public_api/queries.py#L679-L690)) — add `ownerships__user__profile` to the
   existing `prefetch_related("bundle_children")`, and prefetch `bundle_children__ownerships__user__profile`
   so child owners are covered.
2. [public_api/queries.py](../public_api/queries.py): `calendar_groups` resolver — prefetch the slot
   calendars' ownerships (`...slots' calendars__ownerships__user__profile`, matching the actual related
   path from group → slots → calendars; confirm the path against the slot/group models before wiring).
3. [calendar_integration/graphql.py](../calendar_integration/graphql.py): the `CalendarBundleGraphQLType.children`
   resolver ([line 365](../calendar_integration/graphql.py#L365)) returns `list(self.bundle_children.all())`,
   bypassing the optimizer — ensure the parent query's prefetch (step 1) populates `bundle_children` so
   `children` reads from the prefetch cache rather than re-querying.

Spec use-case: expose calendar owners — group-slot and bundle entry points.

Tests:
- **Integration**: [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) —
  (a) `calendarGroups { slots { calendars { owners { user { profile } } } } }` returns owners and is
  query-count-bounded over N slot calendars; (b) `calendarBundles { owners { ... } children { owners { ... } } }`
  returns owners for both the bundle and its children and is query-count-bounded; (c) org-scoping
  reasserted for at least the bundle path (org-A token sees no org-B child owners).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Multi-resolver
prefetch wiring across group/bundle/children paths with query-count assertions; the related-path
through slots needs verification against the models.

**Reusable skills**: `write-tests`.

Acceptance: resolving `owners` through `calendarGroups`, `calendarBundles`, and bundle `children`
issues a constant number of queries regardless of how many calendars/children are returned.

### Phase 3 — Expose `owners` on `CalendarBundleGraphQLType`

**Goal**: the bundle parent calendar itself exposes `owners` (not only its children), so the Admin app
can show who owns a bundle.

**Feature flag**: none — additive.

Changes:
1. [calendar_integration/graphql.py](../calendar_integration/graphql.py): add
   `owners: list[CalendarOwnershipGraphQLType] = strawberry_django.field(prefetch_related=["ownerships__user__profile"])`
   to `CalendarBundleGraphQLType` ([lines 353-367](../calendar_integration/graphql.py#L353-L367)). The
   `calendar_bundles` resolver already prefetches `ownerships__user__profile` after Phase 2, so the
   bundle parent path is N+1-free.

Spec use-case: expose calendar owners — bundle parent type.

Tests:
- **Integration**: [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) —
  `calendarBundles { owners { id isDefault user { id email profile { firstName lastName profilePicture } } } }`
  returns the bundle's own ownerships, org-scoped (cross-org leak assertion), and query-count-bounded.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Single
field add on an existing type with Phase 1 as exact precedent.

**Reusable skills**: `write-tests`.

Acceptance: `calendarBundles { owners { ... } }` returns the bundle calendar's owners for the caller's
org, with no cross-org leak and constant query count.

## 6. Risk & Rollout Notes

- **Feature flag**: none. Additive GraphQL fields are resolved only when explicitly selected; no
  existing client query changes shape or cost. No flag-removal phase is needed.
- **No migrations**: zero schema change, so no locks, no rewrites, no backfill, no partition concern.
- **N+1 / query-plan risk**: the only performance risk is a client selecting `owners` deeply across
  many calendars. Mitigated by field-level + resolver-level `prefetch_related` and guarded by
  query-count assertions in every phase. If a path is missed, the symptom is extra queries (perf), not
  incorrect data or a leak.
- **Cross-org leak risk**: the owning `User` is a global record. The only safe-guard is that `owners`
  is reachable exclusively through org-filtered calendars. Phases 1–3 each ship an explicit two-org
  test asserting org-A tokens never receive org-B owner rows, emails, or profiles. This is the highest
  priority test, not an afterthought.
- **Profile picture exposure**: `profile_picture` (`S3DirectImageField`) serializes to a URL string via
  the existing `ProfileGraphQLType.profile_picture: str | None`. No signed-URL/permission change is in
  scope — it behaves exactly as the existing `users` query already exposes it.
- **Rollback**: revert the PR(s). No data or schema state to unwind. Each phase is independently
  revertible — reverting Phase 3 leaves Phases 1–2 working; reverting Phase 2 only restores the N+1 on
  group/bundle paths (correctness unaffected); reverting Phase 1 removes the field entirely.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should `owners` be paginated/limited? A calendar with many owners returns an unbounded list. | No — calendar ownership cardinality is small (providers per calendar). Revisit only if a calendar with >50 owners appears. | Eng |
| Should `profile_picture` return a time-limited signed URL in the public API rather than the stored URL? | Match existing behavior (the `users` query already returns it as-is). Treat any change as a separate cross-cutting decision. | Product/Eng |
| Does the Provider/Admin app need owner ordering (e.g. default owner first)? | Return DB order for now; add `order_by("-is_default", "id")` only if the app requests it. | Frontend |

## 8. Touch List

**Phase 1 — owners on `CalendarGraphQLType`**
- Edit [calendar_integration/graphql.py](../calendar_integration/graphql.py) — add
  `CalendarOwnershipGraphQLType`, `owners` field on `CalendarGraphQLType`, imports.
- Edit [public_api/queries.py](../public_api/queries.py#L270-L315) — prefetch on `calendars` resolver.
- Edit [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — shape, org-scoping,
  N+1 tests.

**Phase 2 — N+1 hardening (groups / bundles / children)**
- Edit [public_api/queries.py](../public_api/queries.py#L679-L690) — prefetch on `calendar_bundles` and
  `calendar_groups` resolvers.
- Edit [calendar_integration/graphql.py](../calendar_integration/graphql.py#L365) — ensure `children`
  reads from prefetch cache.
- Edit [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — group/bundle/children
  query-count + org-scoping tests.

**Phase 3 — owners on `CalendarBundleGraphQLType`**
- Edit [calendar_integration/graphql.py](../calendar_integration/graphql.py#L353-L367) — add `owners`
  field on `CalendarBundleGraphQLType`.
- Edit [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — bundle-owners shape +
  org-scoping + query-count tests.
</content>
</invoke>
