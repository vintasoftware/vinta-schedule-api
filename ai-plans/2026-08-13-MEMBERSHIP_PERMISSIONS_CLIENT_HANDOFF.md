# API changes: membership permissions replace `role`

- **Date:** 2026-08-13
- **Scope:** `plan/vinta-django-orgs-migration/phase-5` vs `main` (Phase 5 of the organization-membership migration; the phase branch stack `phase-0` … `phase-5` reaches `main` together)
- **Audience:** Web SPA (React), Partner integrations
- **Breaking changes:** 7

## Summary

The API no longer tells you what a member **is called**; it tells you what they **may do**.

Every response that carried a membership `role` (`"admin"` / `"member"`) now carries a
`permissions` array of capability strings such as `"organizations.manage_members"`. The
write side changed too: `POST /organization-members/{user_id}/update-role/` is gone and is
replaced by `POST /organization-members/{user_id}/groups/`, which takes a list of group
names. The public GraphQL `createInvitation` mutation takes `groups: [String!]` instead of
`role: OrgRole`, and the `OrgRole` enum has been deleted from the schema.

Nothing about *who is allowed to do what* changed. An admin is still an admin; they now
report as holding four capabilities instead of reporting `role: "admin"`. Every change
below is a change of representation, and every one of them breaks an unmodified client
that reads or writes `role`.

There is **no deprecation window**. The old field and the old endpoint are gone in the
same deploy.

## Breaking changes

| # | What breaks | Where | What the client must do |
|---|---|---|---|
| 1 | `role` removed from the response | `GET /organizations/current/` | Read `permissions` (array of strings) instead. |
| 2 | `role` removed from each row | `GET /organizations/mine/` | Read `permissions` per row. |
| 3 | `role` removed from each row | `GET /organization-members/`, `GET /organization-members/{user_id}/` | Read `permissions` per row. |
| 4 | Endpoint removed (404) | `POST /organization-members/{user_id}/update-role/` | Call `POST /organization-members/{user_id}/groups/` with a group list. |
| 5 | `role` removed from the membership identity object | REST `CalendarOwnership.membership`, `EventAttendance.membership`; GraphQL `owners { membership }`, `attendeeMemberships`, `externalEventChangeRequests { resolvedBy }` | Drop the field from your selection set (GraphQL will **error** on an unknown field, not ignore it). If you used it to decide what to render, read the member's `permissions` from `GET /organization-members/` instead. |
| 6 | Input field `role: OrgRole` replaced by `groups: [String!]` | GraphQL `createInvitation` | Send `groups: ["organization_admin"]` instead of `role: ADMIN`; omit the field for the previous `MEMBER` default. |
| 7 | Enum `OrgRole` deleted from the GraphQL schema | GraphQL | Remove any generated `OrgRole` type / variable declaration. A query declaring `$role: OrgRole` now fails validation. |

Deploy ordering: these all ship in one release. There is no interim release in which both
representations are available.

## Mapping an old `role` check onto `permissions`

`permissions` is a flat array of `"<app_label>.<codename>"` strings. Today four exist:

| Capability string | Means |
|---|---|
| `organizations.manage_members` | May invite, deactivate, reactivate and re-group members. **This is the old `role === "admin"`.** |
| `organizations.manage_organization` | May change the organization's own settings. |
| `organizations.manage_branding` | The permission half of the white-label branding gate (see `can_manage_branding` below for the whole gate). |
| `payments.manage_billing` | May change the plan, buy add-ons, manage the payment method, cancel the subscription. |

Direct translations:

```diff
- const isAdmin = membership.role === "admin";
+ const isAdmin = membership.permissions.includes("organizations.manage_members");

- if (membership.role === "admin") { showBillingSettings(); }
+ if (membership.permissions.includes("payments.manage_billing")) { showBillingSettings(); }

- const isPlainMember = membership.role === "member";
+ const isPlainMember = membership.permissions.length === 0;
```

Notes that matter when you write that code:

- **Do not reconstruct a role from the list.** `["organizations.manage_members", …]` is not
  a synonym for "admin" forever — gate each piece of UI on the specific capability it needs.
  That is the whole point of the change: a member can hold `payments.manage_billing` alone,
  which no `role` value could express.
- **The list grows.** Treat an unrecognised string as an unknown capability, not an error.
- **An empty array is a valid, normal value** — it is a member with no elevated capabilities.
- **The array is unordered by contract** (it is currently sorted, but do not depend on that).
  Compare with `includes`, not by index or equality.
- **`permissions` is organization-scoped.** In `GET /organizations/mine/` each row reports
  the capabilities for *that row's* organization; a user who administers one organization
  and is a plain member of another gets two different arrays in the same response.
- **`can_manage_branding` remains a separate boolean.** It is the
  *composite* of `organizations.manage_branding`, the organization's white-label
  entitlement, and the "has no parent organization" rule. Do not replace it with a
  `permissions.includes("organizations.manage_branding")` check — that would show the
  branding UI to a permitted member of an organization whose plan does not include it.

## Changed operations

### `GET /organizations/current/`

- **Status:** changed — **breaking**
- **Auth:** session/JWT. Any authenticated user with at least one active membership. `404`
  when the caller has no membership (unchanged).
- **Request:** unchanged. Optional `X-Organization-Id` header (required when the caller has
  two or more active memberships).
- **Response 200:** `role` removed, `permissions` added. `can_manage_branding`
  remains the composite of the branding capability, entitlement, and parentless rule.

Before:

```json
{
  "role": "admin",
  "organization": { "id": 42, "name": "Acme Inc", "slug": "acme-inc", "...": "..." },
  "can_manage_branding": true
}
```

After:

```json
{
  "permissions": [
    "organizations.manage_branding",
    "organizations.manage_members",
    "organizations.manage_organization",
    "payments.manage_billing"
  ],
  "organization": { "id": 42, "name": "Acme Inc", "slug": "acme-inc", "...": "..." },
  "can_manage_branding": true
}
```

A plain member gets `"permissions": []`.

- **Client migration notes — Web SPA:** this is the response the session/bootstrap store is
  built from. Replace the stored `role` with the `permissions` array and change every
  downstream `role === "admin"` selector to a capability check.

### `GET /organizations/mine/`

- **Status:** changed — **breaking**
- **Auth:** any authenticated user. No `X-Organization-Id` header required (this is the
  endpoint that tells the client which organization ids exist).
- **Response 200:** a bare JSON array (no pagination envelope). `role` removed per row,
  `permissions` added per row.

Before:

```json
[
  { "organization": { "id": 42, "name": "Acme Inc", "slug": "acme-inc" }, "role": "admin",  "can_manage_branding": true },
  { "organization": { "id": 77, "name": "Beta LLC", "slug": "beta-llc" }, "role": "member", "can_manage_branding": false }
]
```

After:

```json
[
  { "organization": { "id": 42, "name": "Acme Inc", "slug": "acme-inc" },
    "permissions": ["organizations.manage_branding", "organizations.manage_members",
                    "organizations.manage_organization", "payments.manage_billing"],
    "can_manage_branding": true },
  { "organization": { "id": 77, "name": "Beta LLC", "slug": "beta-llc" },
    "permissions": [],
    "can_manage_branding": false }
]
```

- **Client migration notes — Web SPA:** the organization switcher can now grey out or badge
  entries per capability rather than per role.

### `GET /organization-members/` and `GET /organization-members/{user_id}/`

- **Status:** changed — **breaking**
- **Auth:** session/JWT, and the caller must hold `organizations.manage_members` in the
  active organization (`403` otherwise). `X-Organization-Id` as usual.
- **Response 200:** `role` removed, `permissions` added. `user_id`, `organization_id`,
  `is_active`, `user_email`, `user_first_name`, `user_last_name` unchanged. The list
  response keeps its `{count, next, previous, results}` envelope.

Before (one row):

```json
{
  "user_id": 9001,
  "organization_id": 42,
  "role": "admin",
  "is_active": true,
  "user_email": "dana@acme.example",
  "user_first_name": "Dana",
  "user_last_name": "Okafor"
}
```

After:

```json
{
  "user_id": 9001,
  "organization_id": 42,
  "permissions": [
    "organizations.manage_branding",
    "organizations.manage_members",
    "organizations.manage_organization",
    "payments.manage_billing"
  ],
  "is_active": true,
  "user_email": "dana@acme.example",
  "user_first_name": "Dana",
  "user_last_name": "Okafor"
}
```

- **Client migration notes — Web SPA:** the member datatable's "Role" column becomes a
  capability column. `permissions.includes("organizations.manage_members")` is the exact
  predicate the old `role === "admin"` cell used.

### `POST /organization-members/{user_id}/groups/` (replaces `update-role`)

- **Status:** added, replacing a removed endpoint — **breaking**
- **Auth:** session/JWT; caller must hold `organizations.manage_members` in the active
  organization. `X-Organization-Id` resolves the organization as on every other member
  endpoint; a `user_id` outside it returns `404`.
- **Request:**

  | Field | Type | Required | Constraints |
  |---|---|---|---|
  | `groups` | array of string | yes | Non-empty. Each item must be one of `organization_admin`, `organization_billing_owner`, `organization_member`. |

  The list **replaces** the member's current groups; it is not additive.

  - `organization_admin` — every capability (`manage_members`, `manage_organization`,
    `manage_branding`, `manage_billing`).
  - `organization_billing_owner` — `payments.manage_billing` only.
  - `organization_member` — no capabilities. This is how you demote somebody.
  - Naming a capability group **and** `organization_member` stores the capability group
    alone (`["organization_admin", "organization_member"]` is accepted and behaves as
    `["organization_admin"]`).
  - `["organization_admin", "organization_billing_owner"]` is accepted and keeps both.

- **Response 200:** the updated membership, in the same shape as
  `GET /organization-members/{user_id}/` — so the response tells you the capabilities the
  write produced, without a second request.

- **Errors:**

  | Status | When | Body |
  |---|---|---|
  | `400` | Unknown group name, or an empty / missing `groups` list | DRF field-error object: `{"groups": {"0": ["\"owner\" is not a valid choice."]}}` |
  | `400` | The assignment would leave the organization with nobody who can manage members | `{"detail": "Cannot remove the last active member who can manage members from the organization."}` |
  | `402` | The organization's billing state forbids writes | standard billing-restriction body |
  | `403` | Caller does not hold `organizations.manage_members` | `{"detail": "..."}` |
  | `404` | `user_id` is not a member of the active organization | `{"detail": "Not found."}` |

- **Idempotency:** assigning the groups a member already holds is a `200`, not an error —
  including the sole administrator re-assigning `organization_admin` to themselves. The
  last-administrator guard fires only on *losing* `organizations.manage_members`.

- **Example:**

  ```http
  POST /organization-members/9001/groups/
  X-Organization-Id: 42
  Content-Type: application/json

  {"groups": ["organization_admin"]}
  ```

  ```json
  {
    "user_id": 9001,
    "organization_id": 42,
    "permissions": [
      "organizations.manage_branding",
      "organizations.manage_members",
      "organizations.manage_organization",
      "payments.manage_billing"
    ],
    "is_active": true,
    "user_email": "dana@acme.example",
    "user_first_name": "Dana",
    "user_last_name": "Okafor"
  }
  ```

- **Client migration notes — Web SPA:** the promote/demote controls change endpoint, body
  and success shape:

  ```diff
  - POST /organization-members/9001/update-role/  {"role": "admin"}
  + POST /organization-members/9001/groups/       {"groups": ["organization_admin"]}

  - POST /organization-members/9001/update-role/  {"role": "member"}
  + POST /organization-members/9001/groups/       {"groups": ["organization_member"]}
  ```

  New capability available to the UI: `["organization_billing_owner"]` grants billing
  management without full administration. That state existed in the database before but
  had no API to set it.

  The "cannot demote the last admin" `400` still exists; its `detail` string changed, so
  match on the status code rather than the message.

### GraphQL `createInvitation`

- **Status:** changed — **breaking**
- **Auth:** partner Public API token with the `invitation` resource scope; the acting
  organization must be permitted to invite, and `organizationId` must be the acting
  organization or a descendant (all unchanged).
- **Request:** `role: OrgRole = MEMBER` is replaced by `groups: [String!]! = ["organization_member"]`.

Before:

```graphql
mutation ($input: CreateInvitationInput!) { createInvitation(input: $input) { invitation { id email expiresAt } token inviteUrl } }
```
```json
{"input": {"userEmail": "new@partner.example", "organizationId": "77", "role": "ADMIN"}}
```

After:

```json
{"input": {"userEmail": "new@partner.example", "organizationId": "77", "groups": ["organization_admin"]}}
```

  - Omitting `groups` invites a plain member — identical to the old `role: MEMBER` default.
  - `groups: []` is also accepted and means the same as omitting it.
  - `role: ADMIN` → `groups: ["organization_admin"]`.
  - `organization_billing_owner` is **refused at invitation time** with
    `"Cannot assign organization_billing_owner to an invitation. Allowed groups:
    organization_member, organization_admin."` — an invitation has no way to carry it.
    Assign it after the invitation is accepted, via
    `POST /organization-members/{user_id}/groups/`.
  - Any other value is refused with `"Cannot assign <name> to an invitation. …"`. Nothing
    is created when the mutation errors.

- **Response:** unchanged (`invitation { id email expiresAt }`, plus `token` / `inviteUrl`
  when `sendEmail: false`).
- **Client migration notes — Partner integrations:** if your client is code-generated from
  the GraphQL schema, regenerate: `OrgRole` no longer exists and a persisted query
  declaring `$role: OrgRole` fails validation before it reaches a resolver.

### GraphQL membership identity objects

- **Status:** changed — **breaking**
- Affected fields: `calendars { owners { membership } }`,
  `calendarBundles { … owners { membership } }`,
  `calendarEvents { attendeeMemberships }`,
  `externalEventChangeRequests { resolvedBy }`.
- Each of these objects loses its `role: String!` field and keeps `userId` and
  `organizationId`.
- GraphQL rejects a query selecting a field that does not exist, so **a client that still
  selects `role` here gets a validation error for the whole document**, not a partial
  response. Remove the selection.

Before:

```graphql
owners { id isDefault membership { userId organizationId role } }
```

After:

```graphql
owners { id isDefault membership { userId organizationId } }
```

If you used that `role` to render an owner badge, fetch the capability from
`GET /organization-members/` (REST) and join on `userId`.

### REST membership identity objects

- **Status:** changed — **breaking**
- Affected: the nested `membership` object on `CalendarOwnership` and `EventAttendance`
  representations (calendar and event endpoints).
- `{"user_id": 9001, "organization_id": 42, "role": "admin"}` becomes
  `{"user_id": 9001, "organization_id": 42}`.
- Unlike GraphQL, a REST client that reads the missing key gets `undefined` rather than an
  error — which is exactly the silent-misbehaviour case to check for (a `role === "admin"`
  test simply becomes permanently false).

## Other contract changes

- **Auth, tokens, headers, rate limits, webhooks: unchanged.** `X-Organization-Id` keeps
  its meaning and its 400/403 rules.
- **No authorization outcome changed.** Whoever could call an endpoint before this release
  can call it after. Only the reported representation moved.
- **Server-side vocabulary.** The strings in `permissions` are the same strings the server
  checks internally, so a client gating on `"payments.manage_billing"` and the server
  enforcing it cannot disagree.

## Rollout

- Not behind a flag. The change is live the moment the release deploys; both the removed
  field and the removed endpoint disappear in that deploy.
- Staging first, as usual. Regenerate any typed client from the updated `schema.yml`
  (REST) and from the GraphQL schema (public API) before adopting.
- Client work needed before the deploy: clients that keep reading `role` will render as
  though every member were a plain member, and any promote/demote button pointed at
  `update-role/` will start returning `404`.
