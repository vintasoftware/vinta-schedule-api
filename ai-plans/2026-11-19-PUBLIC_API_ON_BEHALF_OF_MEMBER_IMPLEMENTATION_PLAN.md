# Public API On-Behalf-Of Member Attribution — Implementation Plan

Spec: [2026-11-19-PUBLIC_API_ON_BEHALF_OF_MEMBER_SPEC.md](2026-11-19-PUBLIC_API_ON_BEHALF_OF_MEMBER_SPEC.md).

## 1. Goals

1. Organization-wide and org-less public API tokens can name the organization member they act for with an `X-Public-Api-On-Behalf-Of` header. Every audit record the mutation writes with the token as the actor then carries that member as a claim, and the token stays the actor.
2. A mutation request whose header names someone who is not a current member of the bound organization is rejected before any resolver runs. Nothing changes and nothing is audited.
3. A per-token `requires_on_behalf_of` switch, off by default, makes the header mandatory on that token's mutation requests. Org admins (REST, GraphQL token creation) and staff (Django admin) can set it, and every change to it is audited.
4. Staff searching the audit trail for a person also find the token actions claimed for them, and each such record is visibly labeled as a claim.
5. The partner-facing concept docs describe the header, which tokens may send it, the switch, and the error codes.

Non-goals:

- Auditing reads. The audit trail records writes only, and this plan does not change that.
- Verified attribution: no signed assertions, OAuth token exchange or consent flow. That is v2 in the spec.
- Any change to authorization. Naming a member never narrows or widens what the token can reach.
- Notifications, event organizer and "created by" fields. They keep their current values.
- Echoing the accepted member in API responses.
- An organization-admin audit view. Audit records stay readable only in the Django admin.
- Partner-specific user ids, and identifying the member by email.
- The REST API, webhooks and signed-in users. Only public GraphQL token requests are affected.
- Backfilling attribution onto existing audit records.
- A feature flag (see **Guiding Decisions**).

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Header contract** | `X-Public-Api-On-Behalf-Of: <membership user id>`, a positive integer. It sits next to the existing `X-Public-Api-Organization-Id`. It is a contract partners code against, so the name is a one-way door. |
| **Where it is checked** | A new Strawberry `SchemaExtension`, `OnBehalfOfMemberExtension`, in @public_api/extensions.py, registered in @public_api/schema.py. It runs in `on_execute`, after Strawberry has parsed and validated the document, so `execution_context.operation_type` says whether this is a mutation without the middleware parsing the body a second time. It runs inside the middleware's `organization_context`, so the membership lookup is already scoped to the request's organization. |
| **What counts as a write** | The executed operation is a `MUTATION`. A document can hold several operations, but GraphQL executes only the one `operationName` selects, so "mixed query and mutation" means "the selected operation is a mutation". Subscriptions are not served by this endpoint. |
| **Rejection shape** | When a request is rejected, the extension raises a `GraphQLError` in `on_execute` before it yields, following the `OrganizationRateLimiter` precedent. Strawberry turns that into an error result and skips execution, so no resolver runs and nothing is written. That gives HTTP 200 with `errors[0].extensions.code`, the same shape as every other GraphQL error here. The codes are `ON_BEHALF_OF_MEMBER_INVALID` (malformed, empty, or not an **active** member: memberships are deactivated with `is_active=False`, not deleted) and `ON_BEHALF_OF_MEMBER_REQUIRED` (the switch is on and the header is absent). They are declared as constants in @public_api/constants.py. |
| **Which tokens it applies to** | Organization-wide tokens (`organization` set, `scoped_to_membership_user_id` null) and org-less tokens (`organization` null; the member is checked against the organization chosen by `X-Public-Api-Organization-Id`). Member-scoped tokens: the header is ignored entirely, never validated, never recorded. Read-only operations: the header is ignored and not validated. |
| **How the claim reaches the audit record** | The audit app owns attribution. `OrganizationAuditService.on_behalf_of_member(system_user, membership)` is a context manager that builds the `CLAIMED_MEMBERSHIP` snapshot once and binds it, with the token's actor key, in a request-scoped `contextvars.ContextVar` in @audit_integration/services.py. It resets the variable on every exit path, the same discipline as `organization_context`. `OrganizationAuditService.record` applies the claim **only when the record's actor is that token** (`actor_type == system_user` and `actor_key` matches), so a claim can never end up on a different actor. The header check (parse, active-membership lookup, the switch) lives in `PublicAPIAuthService.on_behalf_of_claim`, which raises typed errors and returns the context manager. The extension only decides whether the header applies, calls the service, and maps the errors to `GraphQLError`. A caller that passes its own `on_behalf_of` keeps it. A context variable rather than an attribute on the `SystemUser` instance, because calendar services re-fetch or receive the token through several paths (`user_or_token`), and an instance attribute would silently drop the claim on any path that holds a different instance. |
| **Audit storage** | The audit package's own record-level `on_behalf_of` identity ("the identity behind the actor, when one principal acted as another"). It already has a column, round-trips through every repository, and appears in the admin CSV export. The claimed member is an identity of a new type, `AuditActorType.CLAIMED_MEMBERSHIP`: its key and `user_id` are the membership user id, its label is `label_for_user` at action time, and its `metadata` names the token that made the claim. The type is what marks it as a claim. No schema change. (The first draft added three custom columns to the token's actor row. They were replaced during implementation once the native field turned up.) |
| **Labeling the claim** | When a claim applies, the token actor's `identity_label` becomes `"<token label> on behalf of <member label> (claimed)"`. That puts the claim in front of anyone reading the admin list, and makes the package admin's existing free-text search (which matches `actor__identity_label`) find these records by the member's username with no change to the package. |
| **Searching for a person** | `OrganizationAuditQuery.actor_user_emails` matches `actor__user__email` **or** `on_behalf_of__user__email`, per the spec's "Both, labeled" answer. No separate filter is added. |
| **The switch** | `SystemUser.requires_on_behalf_of = BooleanField(default=False)`. It is meaningless on a member-scoped token: the header is ignored there, so the switch would reject every one of its writes forever. Setting it to `true` on a member-scoped token is therefore refused with a validation error on every surface. Enforcement skips member-scoped tokens too, as a second line of defense. |
| **Switch surfaces** | REST `SystemUserTokenCreateSerializer` (optional, default false) and `SystemUserTokenUpdateSerializer` (optional; omitted means unchanged, so existing PUT/PATCH callers that send only `available_resources` see no change); the read serializers expose it. GraphQL `CreateSystemUserTokenInput` gains an optional field. The Django admin `SystemUserAdmin` exposes it, and only staff reach the admin, which covers org-less tokens. The GraphQL `create_scoped_system_user` mutation is not touched, because it only mints member-scoped tokens. |
| **Auditing switch changes** | Every change to `requires_on_behalf_of` writes an `AuditAction.UPDATE` record on the token, with a diff of the field. The actor is the request's membership (REST), the acting token (GraphQL), or `actor_from_user(staff_user, organization_id)` (admin, which falls back to the system actor when the staff user is not a member). Token creation with the switch on is covered by the existing create audit record, which gains the field in its payload. |
| **Feature flag** | **No flag — purely additive surface.** A request without the header from a token with the switch off runs exactly the pre-feature code path, the switch defaults to off, and the new audit columns are null on every existing row. Each gated phase still ships an explicit test proving that path is unchanged. |
| **Phase granularity** | One spec use-case per phase, except that "partner books for a member" and "partner sends a wrong member" ship together in Phase 3. Recording a claim without checking it would break the spec's second objective. Shared scaffolding (the audit columns plus the claim context, and the token field) lands in two wave-1 foundation phases. |
| **Parallelism** | Phases run concurrently wherever the graph allows. Phases 4 and 5 are serialized because both edit the extension's decision function (see the **Execution graph**). |

## 3. Data Model Changes

### 3.1 Audit records — no schema change

The claim uses the audit package's existing `Audit.on_behalf_of` foreign key to the identity table. The only addition is a value in [audit_integration/constants.py](../audit_integration/constants.py): `AuditActorType.CLAIMED_MEMBERSHIP = "claimed_membership"`. Identity types are not database choices, so it needs no migration.

### 3.2 `SystemUser.requires_on_behalf_of`

In [public_api/models.py](../public_api/models.py):

```python
requires_on_behalf_of = models.BooleanField(
    default=False,
    help_text=(
        "When set, mutation requests from this token must name the member they act for "
        "in the X-Public-Api-On-Behalf-Of header. Organization-wide and org-less tokens only."
    ),
)
```

Migration `public_api/migrations/0011_systemuser_requires_on_behalf_of.py`: one `AddField` with a constant default. Postgres 11+ adds it without a table rewrite.

### 3.3 Type plumbing

- @audit_integration/services.py: a private `_OnBehalfOfClaim(actor_key, member)` dataclass and its `ContextVar`, bound only through `OrganizationAuditService.on_behalf_of_member`.
- @public_api/constants.py: `ON_BEHALF_OF_HEADER = "X-Public-Api-On-Behalf-Of"`, and an `OnBehalfOfErrorCode` `StrEnum` with `INVALID` and `REQUIRED`.
- @public_api/exceptions.py: `OnBehalfOfMemberError` with `OnBehalfOfMemberInvalidError` and `OnBehalfOfMemberRequiredError`, each carrying its error code and message; `ORG_WIDE_ONLY_SWITCH_MESSAGE`, shared by the service error and the admin form.

## 4. API Design

### 4.1 GraphQL request header

| Token | Operation | Header | Switch | Result |
|---|---|---|---|---|
| Member-scoped | any | any | n/a (cannot be on) | Served as today; header ignored. |
| Org-wide / org-less | query | any | any | Served as today; header ignored. |
| Org-wide / org-less | mutation | absent | off | Served as today, no claim. |
| Org-wide / org-less | mutation | absent | on | `ON_BEHALF_OF_MEMBER_REQUIRED`, nothing executed. |
| Org-wide / org-less | mutation | not a positive int, or not a current member of the bound organization | any | `ON_BEHALF_OF_MEMBER_INVALID`, nothing executed. |
| Org-wide / org-less | mutation | current member | any | Executed; every audit record with the token as the actor carries the claim. |

Error body (HTTP 200):

```json
{"data": null, "errors": [{"message": "X-Public-Api-On-Behalf-Of does not name a current member of this organization.", "extensions": {"code": "ON_BEHALF_OF_MEMBER_INVALID"}}]}
```

The message never echoes the submitted value back, so it gives no help to anyone probing which ids are members.

### 4.2 REST token management (`SystemUserTokenViewSet`)

- `POST` create: optional `requires_on_behalf_of: bool` (default `false`). `true` together with a non-null `scoped_to_user` → 400 on `requires_on_behalf_of`.
- `PUT` / `PATCH` update: optional `requires_on_behalf_of: bool`; when omitted, the value is unchanged. `true` on a member-scoped token → 400. A change writes the audit record described in **Guiding Decisions**.
- Read serializers (`SystemUserTokenResponseSerializer`, `SystemUserTokenSerializer`) gain a read-only `requires_on_behalf_of`.
- Regenerate `schema.yml`.

### 4.3 GraphQL `createSystemUserToken`

`CreateSystemUserTokenInput.requires_on_behalf_of: bool = False`, passed through to the created token.

## 5. Phased Rollout

### Crew

| Agent | Tier | Takes | Why this tier |
|---|---|---|---|
| `tier1` | 1 | Phase 2 | One `BooleanField` and its migration. Exact precedent in `0004_add_systemuser_deleted_at`. |
| `tier2-1` | 2 | Phase 1, Phase 7, Phase 4, Phase 5 | Audit columns that mirror `system_user_scoped_to_membership`, a context variable shaped like `organization_context`, one query filter, and two narrow branches in an extension Phase 3 builds. |
| `tier2-2` | 2 | Phase 6, Phase 8 | Serializer fields with cross-field validation, an admin field, audit calls that follow the existing `AuditAction.UPDATE` precedent, then a concept doc. |
| `tier3` | 3 | Phase 3 | A new schema extension that short-circuits execution, ties together the token kind, the bound organization, the membership check and request-scoped state, and is security-sensitive. No precedent for a pre-execution rejection in this schema. |

### Execution graph

Wave = how deep a phase sits in the dependency graph. Phases in the same wave have no dependency on each other and are implemented concurrently.

| Wave | Phases | Agent | Depends on |
|---|---|---|---|
| 1 | Phase 1, Phase 2 | `tier2-1`, `tier1` | — |
| 2 | Phase 3, Phase 6, Phase 7 | `tier3`, `tier2-2`, `tier2-1` | Phase 1 (Phase 3, Phase 7), Phase 2 (Phase 6) |
| 3 | Phase 4 | `tier2-1` | Phase 3 |
| 4 | Phase 5 | `tier2-1` | Phase 2, Phase 4 |
| 5 | Phase 8 | `tier2-2` | Phase 3, Phase 4, Phase 5 |

**File overlap:**
- The three wave-2 phases touch disjoint files. Phase 3 edits the extension, the schema and the public API constants. Phase 6 edits the token serializers, views, service, GraphQL input and admin. Phase 7 edits the audit repository and query type.
- Phase 3, Phase 4 and Phase 5 all edit @public_api/extensions.py and @public_api/tests/test_on_behalf_of.py. Phases 4 and 5 are serialized for that reason. The Phase 5 → Phase 4 edge is a real file overlap, not only an artifact dependency.

**Idle:**
- `tier3` is idle in waves 1, 3, 4 and 5. Only Phase 3 needs Tier 3, and handing it Phases 4 and 5 would run Tier 2 work on a dearer model.
- `tier1` is idle from wave 2 on.
- `tier2-2` is idle in wave 1 and in waves 3 and 4.
- Waves 3 to 5 are a single-phase chain: org-less tokens, then the switch, then the docs that describe both. Each step edits or documents the previous one's decision function.

---

### Phase 1 — Audit columns and claim context

**Goal**: A request-scoped claim, once bound, reaches every audit record whose actor is the claiming token, through the package's native `on_behalf_of`. Ship value: none on its own. Nothing binds a claim until Phase 3. This is the scaffolding Phases 3 and 7 both build on.

**Depends on**: nothing — starts from the base branch.

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [audit_integration/services.py](../audit_integration/services.py): the claim context variable and `OrganizationAuditService.on_behalf_of_member(system_user, membership)`, as described under **Type plumbing**.
2. [audit_integration/constants.py](../audit_integration/constants.py): `AuditActorType.CLAIMED_MEMBERSHIP`.
3. [audit_integration/services.py](../audit_integration/services.py): override `record`, with the same signature as the base. When no `on_behalf_of` was passed and the bound claim belongs to the actor's token, fill `on_behalf_of` with a `CLAIMED_MEMBERSHIP` identity and relabel the actor `"<token label> on behalf of <member label> (claimed)"`. Otherwise call the base unchanged.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @audit_integration/tests/test_on_behalf_of_claim.py:
  - With no claim bound, the dispatched record is unchanged and has no `on_behalf_of`.
  - With a claim for the acting token, `on_behalf_of` is the claimed member and the actor label says so.
  - A claim for a **different** token, a non-token actor with a matching key, and an explicit `on_behalf_of` are each left alone.
  - The context manager resets the variable after a normal exit and after an exception.
- **Integration**: same file: a claimed record round-trips through the repository.

**Assigned to**: `tier2-1` (Tier 2) — one service override on a package extension point, plus a context variable shaped like `organization_context`.

**Reusable skills**: `write-unit-test`.

Acceptance: an audit record written inside `on_behalf_of_member(...)` for the acting token has the member as its `on_behalf_of` (type `claimed_membership`), and its actor label reads "… on behalf of … (claimed)". With no claim bound, every existing audit test passes unchanged.

### Phase 2 — Token `requires_on_behalf_of` field

**Goal**: Tokens carry a `requires_on_behalf_of` setting, defaulting to off. Ship value: none on its own. Phase 5 enforces it and Phase 6 exposes it.

**Depends on**: nothing — starts from the base branch.

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [public_api/models.py](../public_api/models.py): the `requires_on_behalf_of` field from **Data Model Changes**.
2. `public_api/migrations/0011_systemuser_requires_on_behalf_of.py`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: [public_api/tests/test_models.py](../public_api/tests/test_models.py): a new token defaults to `requires_on_behalf_of=False`, and an org-less token (`organization=None`) still saves with the field set.

**Assigned to**: `tier1` (Tier 1) — a single column and its migration, with exact precedent.

**Reusable skills**: `add-migration`.

Acceptance: `uv run python manage.py makemigrations --check` is clean, and every existing token reads `requires_on_behalf_of=False`.

### Phase 3 — Record and check claims for org-wide tokens

**Goal**: An organization-wide token's mutation that names a current member is audited with that member as a claim, and one naming anyone else is rejected before anything runs.

**Depends on**: Phase 1 (the `on_behalf_of_claim` context manager and `actor_from_system_user` reading it).

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [public_api/constants.py](../public_api/constants.py): `ON_BEHALF_OF_HEADER` and `OnBehalfOfErrorCode`.
2. [public_api/extensions.py](../public_api/extensions.py): `OnBehalfOfMemberExtension(SchemaExtension)`. In `on_execute`:
   1. Read `public_api_system_user` and `public_api_organization` from the request in the execution context.
   2. Skip, by just yielding, unless the operation is a `MUTATION` and the token is organization-wide (`organization_id` set and `scoped_to_membership_user_id` null). Org-less tokens are skipped in this phase; Phase 4 widens the check.
   3. Call `PublicAPIAuthService.on_behalf_of_claim(system_user, organization, header)`. It returns a no-op context manager when the header is absent. Otherwise it parses the header as a positive int, checks `OrganizationMembership.objects.active()` in the bound organization, and returns `audit_service.on_behalf_of_member(...)`. A bad value raises `OnBehalfOfMemberInvalidError`.
   4. Map an `OnBehalfOfMemberError` to `GraphQLError(error.message, extensions={"code": error.code})`, raised before yielding, so execution is skipped. Otherwise yield inside the returned context manager.
3. [public_api/schema.py](../public_api/schema.py): register the extension after `OrganizationRateLimiter`, so a rate-limited request is rejected before a membership query is spent on it.

Spec use-case: UC1 — a partner books for a staff member; UC4 — a partner sends a wrong member (bundled; see **Guiding Decisions**).

Tests:
- **Integration**: @public_api/tests/test_on_behalf_of.py, through the real `/graphql/` endpoint with the middleware:
  - **Happy path:** an org-wide token creates an event naming a current member. The response succeeds, and the audit record for the event has the token as the actor, with the claim columns and the "(claimed)" label.
  - **Several changes, one request:** a mutation that writes more than one audit record carries the claim on every one.
  - **Rejections:** a member of another organization, an id that is not a member, `abc`, `0`, `-1`, and an empty header each return `ON_BEHALF_OF_MEMBER_INVALID`. The target row is unchanged and no audit record is written.
  - **Reads:** a query-only request with an invalid header is answered normally.
  - **Member-scoped tokens:** a member-scoped token sending a header writes exactly today's audit record. The header is ignored.
  - **Org-less tokens:** an org-less token sending a header in this phase is also ignored. Phase 4 changes this.
  - **No header (regression test):** an org-wide token's mutation without the header produces a response and an audit record byte-identical to before.
  - **No bleed:** the claim context is unbound after the request, including after a resolver raised.

**Assigned to**: `tier3` (Tier 3) — a new pre-execution short-circuit in the schema, crossing token kind, bound organization, membership lookup and request-scoped state, on a security boundary.

**Reusable skills**: `create-graphql-public-query` (for how public API requests, permissions and organization binding fit together); `write-unit-test`.

Acceptance: an org-wide token mutation with `X-Public-Api-On-Behalf-Of: <current member>` produces audit records carrying that claim. One with any other value returns `ON_BEHALF_OF_MEMBER_INVALID` and changes nothing.

### Phase 4 — Accept claims from org-less tokens

**Goal**: An org-less token acting inside the organization chosen by `X-Public-Api-Organization-Id` can name a member of that organization, and only that organization.

**Depends on**: Phase 3 (the `OnBehalfOfMemberExtension` decision function this widens).

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [public_api/extensions.py](../public_api/extensions.py): the token-kind check admits org-less tokens (`organization_id` null and `scoped_to_membership_user_id` null). The membership check uses the bound `public_api_organization`. An org-less token with no organization chosen has no bound organization, so the header is ignored, as described under **Edge cases** in the spec.

Spec use-case: UC2 — an org-less token acts inside one organization.

Tests:
- **Integration**: @public_api/tests/test_on_behalf_of.py, extended:
  - An org-less token choosing organization A and naming a member of A succeeds and carries the claim.
  - The same member named while choosing organization B returns `ON_BEHALF_OF_MEMBER_INVALID`.
  - An org-less token without the organization header behaves exactly as today.

**Assigned to**: `tier2-1` (Tier 2) — one branch added to an existing decision function, with tests that follow the Phase 3 fixtures.

**Reusable skills**: `write-unit-test`.

Acceptance: org-less token mutations naming a member of the chosen organization carry the claim. Naming a member of any other organization is rejected.

### Phase 5 — Enforce the `requires_on_behalf_of` switch

**Goal**: A token with `requires_on_behalf_of` on cannot change data without naming a member, while its read-only requests keep working.

**Depends on**: Phase 2 (the `SystemUser.requires_on_behalf_of` field), Phase 4 (the decision function in @public_api/extensions.py after it admits org-less tokens; both phases edit it).

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [public_api/extensions.py](../public_api/extensions.py): in the decision function, a mutation from an org-wide or org-less token with no header and `requires_on_behalf_of=True` returns "reject: `ON_BEHALF_OF_MEMBER_REQUIRED`". Member-scoped tokens are never subject to it, even if the field were somehow true.

Spec use-case: UC3 — an org admin makes attribution mandatory (enforcement half).

Tests:
- **Integration**: @public_api/tests/test_on_behalf_of.py, extended:
  - A switch-on token sending a mutation without the header gets `ON_BEHALF_OF_MEMBER_REQUIRED`, and nothing changes.
  - A switch-on token sending a query without the header is answered.
  - A switch-on token naming a valid member succeeds with the claim.
  - A switch-on token naming an invalid member gets `ON_BEHALF_OF_MEMBER_INVALID`: the invalid code wins over the required code.
  - A switch-off token without the header behaves exactly as today.
  - A member-scoped token with the field forced on in the database is still served.

**Assigned to**: `tier2-1` (Tier 2) — one branch and its tests, in code this member already extended in Phase 4.

**Reusable skills**: `write-unit-test`.

Acceptance: with the switch on, a mutation without the header returns `ON_BEHALF_OF_MEMBER_REQUIRED` and changes nothing, and a query without it is answered.

### Phase 6 — Manage the switch (REST, GraphQL creation, admin)

**Goal**: Org admins can set `requires_on_behalf_of` when creating a token and later, staff can set it in the Django admin, and every change is audited.

**Depends on**: Phase 2 (the `SystemUser.requires_on_behalf_of` field).

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [public_api/serializers.py](../public_api/serializers.py):
   - `SystemUserTokenCreateSerializer`: optional `requires_on_behalf_of` (default false). Cross-field validation rejects `true` together with a non-null `scoped_to_user`. Pass it to `create_system_user`.
   - `SystemUserTokenUpdateSerializer`: optional `requires_on_behalf_of`.
   - Both read serializers expose it read-only.
2. [public_api/services.py](../public_api/services.py) `create_system_user`: new keyword `requires_on_behalf_of: bool = False`, written on create and included in the existing create audit payload. It raises a project exception when combined with `scoped_to_membership`, so the admin and both GraphQL paths cannot bypass the rule.
3. [public_api/views.py](../public_api/views.py) `update`: when the field is in the validated data and differs from the stored value, refuse it on a member-scoped token (400), otherwise save it and write `AuditAction.UPDATE` on the token with a diff of the field, with the request membership as the actor. Omitted means unchanged. A PUT carrying only `available_resources` behaves exactly as today.
4. [public_api/types.py](../public_api/types.py) `CreateSystemUserTokenInput`: `requires_on_behalf_of: bool = False`. [public_api/mutations.py](../public_api/mutations.py) `create_system_user_token` passes it through.
5. [public_api/admin/system_user.py](../public_api/admin/system_user.py): add the field to the edit fieldset. In `save_model`, when it changed, write the same audit record with `actor_from_user(request.user, obj.organization_id)` (system actor for org-less tokens), and refuse it on member-scoped tokens through the admin form's `clean`.
6. Regenerate `schema.yml` (`uv run python manage.py spectacular --file schema.yml`).

Spec use-case: UC3 — an org admin makes attribution mandatory (management half).

Tests:
- **Integration**: [public_api/tests/test_views.py](../public_api/tests/test_views.py):
  - Create with the switch on.
  - Create with the switch on plus `scoped_to_user` → 400.
  - Update toggling it on and off writes one audit record each, with the right diff and actor.
  - Update without the field leaves it unchanged and writes no switch audit record (regression test).
  - Update setting it on a member-scoped token → 400.
  - Non-admin → 403, unchanged.
- **Integration**: [public_api/tests/test_provisioning_flow.py](../public_api/tests/test_provisioning_flow.py), next to the existing `createSystemUserToken` tests: `createSystemUserToken` with `requiresOnBehalfOf: true` creates a switch-on token. Omitted gives off.
- **Integration**: [public_api/tests/test_admin.py](../public_api/tests/test_admin.py): toggling in the admin saves it and audits it, and toggling on a member-scoped token is refused.
- **Unit**: `create_system_user` refuses the switch together with a scoped membership.

**Assigned to**: `tier2-2` (Tier 2) — serializer fields with cross-field validation, a view branch, and audit calls on an existing precedent.

**Reusable skills**: `create-rest-endpoint` (serializer, view and schema conventions for `SystemUserTokenViewSet`); `write-unit-test`.

Acceptance: an org admin can create or update a token with `requires_on_behalf_of`, each change shows up in the audit trail, and a member-scoped token can never have it on.

### Phase 7 — Staff search finds claimed actions

**Goal**: A staff search for a person's actions returns both what they did directly and what tokens claimed to do for them, and the claimed ones are labeled.

**Depends on**: Phase 1 (the claimed member stored as `Audit.on_behalf_of`, which this filters on).

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. [audit_integration/repositories.py](../audit_integration/repositories.py) `_filtered_queryset`: the `actor_user_emails` filter becomes, per email, `Q(actor__user__email__iexact=email) | Q(on_behalf_of__user__email__iexact=email)`.
2. [audit_integration/types.py](../audit_integration/types.py): update the `actor_user_emails` comment to say it includes claimed actions, and that these are told apart by `actor_type == system_user` plus the on-behalf columns.

Spec use-case: UC5 — a staff member investigates an incident.

Tests:
- **Integration**: [audit_integration/tests/test_project_query.py](../audit_integration/tests/test_project_query.py):
  - For a member with one direct action and one claimed action (written with a claim bound through the Phase 1 API, with no extension needed), `actor_user_emails=[member email]` returns both, and the claimed one has the system-user actor type and the "(claimed)" label.
  - A different member's email returns neither.
  - Case-insensitive match.
  - Existing filter tests unchanged (regression).
- **Integration**: a package-admin free-text search for the member's username returns the claimed record. This covers the label from Phase 1 at the admin surface.

**Assigned to**: `tier2-1` (Tier 2) — one filter widened on an established repository, with tests against existing fixtures.

**Reusable skills**: `write-unit-test`.

Acceptance: `OrganizationAuditQuery(actor_user_emails=[x])` returns the actions x took and the actions tokens claimed for x, each claimed one labeled as a claim.

### Phase 8 — Partner docs for on-behalf-of

**Goal**: Partners can find out how to use the header, which tokens may send it, the switch, and both error codes, without asking us.

**Depends on**: Phase 3 (the `ON_BEHALF_OF_MEMBER_INVALID` error code and org-wide behavior it documents), Phase 4 (the org-less behavior it documents), Phase 5 (the `ON_BEHALF_OF_MEMBER_REQUIRED` code and switch behavior it documents).

**Feature flag**: none — see **Guiding Decisions**.

Changes:
1. @docs/concepts/on-behalf-of.md (new): served automatically by the concept-doc allow-list in [public_api/docs_content.py](../public_api/docs_content.py). Cover:
   - what the header is for: audit attribution, recorded as the token's claim, not as an access change;
   - the request table from **API Design**, in prose;
   - the two error codes and an example error body;
   - the switch, and that it applies only to mutations;
   - that member-scoped tokens never need it.
   Plain English, no internal names.
2. [public_api/tests/test_docs_content.py](../public_api/tests/test_docs_content.py): add `on-behalf-of` to `EXPECTED_SLUGS`.

Spec use-case: shared — the spec's fifth objective (partners can adopt it without asking us).

Tests:
- **Integration**: [public_api/tests/test_docs_content.py](../public_api/tests/test_docs_content.py): the `on-behalf-of` doc is listed and retrievable, its title comes from its first heading, and both error-code strings appear in its markdown (so a renamed constant fails here).

**Assigned to**: `tier2-2` (Tier 2) — the doc has to describe the behavior accurately, so it is written against the merged code, not from the plan.

**Reusable skills**: `handoff-to-client` (optional follow-up once merged, to hand partner and frontend teams the header contract and the REST field).

Acceptance: `GET /public-api-docs/concepts/on-behalf-of/` returns the doc, and it names the header, both error codes and the switch.

## 6. Risk & Rollout Notes

- **No feature flag.** Every phase is safe to deploy on its own. Without the header and with the switch off, a request takes the pre-feature path, and each phase carries a test that proves it.
- **Migrations:** one, `public_api` `0011`, an `AddField` with a constant default. On Postgres 11+ it does not rewrite the table and takes only a brief lock. It reverses cleanly. The audit side needs no migration.
- **Query cost:** Phase 3 adds one indexed membership lookup per mutation request that carries the header, and none otherwise. Phase 7's `OR` across two joins applies only when `actor_user_emails` is set, which is staff-only and never on a hot path.
- **Transaction semantics:** a rejection happens before execution, so under `ATOMIC_REQUESTS` there is nothing to roll back. No `set_rollback()` is needed. Phase 3's tests assert that the target rows are unchanged.
- **Claim isolation:** the context variable is bound around execution only, reset in `finally`, and matched against the token id when read. Celery tasks queued from the request run outside that context. If any task writes audit records of its own (rather than receiving a snapshot built in the request), it records no claim. That is consistent with the spec's "audit record only" scope and is called out in **Open Questions**.
- **Partner rollout:** turning the switch on for a token whose integration does not send the header breaks that integration's writes immediately. The error message says so plainly, and turning it off restores the old behavior. Both changes are audited.
- **Rollback:** revert the phase's PR. For Phase 2, also reverse the migration. Claims already written stay in the audit trail, which is the correct outcome for an audit record.
- **Deploy order:** none required. Any subset of merged phases is consistent.

## 7. Open Questions

1. **Is a claimed (unverified) attribution enough for each HIPAA client?** Carried over from the spec. Recommended default: yes for v1. Owner: the project lead, with each engagement's compliance contact. This plan does not block on it.
2. **Does storing the member's label (username) on the audit row match our PHI and PII posture for audit data?** Carried over from the spec. Recommended default: yes. It matches what member actors already store in `identity_label`. Owner: the project lead. If the answer is no, Phase 1 drops `on_behalf_of_label`, and the actor label uses the member id instead.
3. **Do any audit records get written from Celery tasks for token-initiated work?** If so, they carry no claim, because the claim is request-scoped. Recommended default: accept for v1, and list any such task during Phase 3's review. Owner: the Phase 3 implementer, who confirms by grepping for audit writes reached from `.delay(` paths.

## 8. Touch List

**Phase 1**
- [audit_integration/constants.py](../audit_integration/constants.py)
- [audit_integration/services.py](../audit_integration/services.py)
- @audit_integration/tests/test_on_behalf_of_claim.py (new)

**Phase 2**
- [public_api/models.py](../public_api/models.py)
- @public_api/migrations/0011_systemuser_requires_on_behalf_of.py (new)
- [public_api/tests/test_models.py](../public_api/tests/test_models.py)

**Phase 3**
- [public_api/constants.py](../public_api/constants.py)
- [public_api/extensions.py](../public_api/extensions.py)
- [public_api/schema.py](../public_api/schema.py)
- @public_api/tests/test_on_behalf_of.py (new)

**Phase 4**
- [public_api/extensions.py](../public_api/extensions.py)
- @public_api/tests/test_on_behalf_of.py

**Phase 5**
- [public_api/extensions.py](../public_api/extensions.py)
- @public_api/tests/test_on_behalf_of.py

**Phase 6**
- [public_api/serializers.py](../public_api/serializers.py)
- [public_api/services.py](../public_api/services.py)
- [public_api/views.py](../public_api/views.py)
- [public_api/types.py](../public_api/types.py)
- [public_api/mutations.py](../public_api/mutations.py)
- [public_api/admin/system_user.py](../public_api/admin/system_user.py)
- `schema.yml` (regenerated)
- [public_api/tests/test_views.py](../public_api/tests/test_views.py)
- [public_api/exceptions.py](../public_api/exceptions.py)
- [public_api/tests/test_provisioning_flow.py](../public_api/tests/test_provisioning_flow.py)
- [public_api/tests/test_on_behalf_of.py](../public_api/tests/test_on_behalf_of.py)
- [public_api/tests/test_admin.py](../public_api/tests/test_admin.py)

**Phase 7**
- [audit_integration/repositories.py](../audit_integration/repositories.py)
- [audit_integration/types.py](../audit_integration/types.py)
- [audit_integration/tests/test_project_query.py](../audit_integration/tests/test_project_query.py)

**Phase 8**
- @docs/concepts/on-behalf-of.md (new)
- [public_api/tests/test_docs_content.py](../public_api/tests/test_docs_content.py)
