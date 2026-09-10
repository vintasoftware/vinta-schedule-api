# API changes: billing is keyed by scope, not organization

- **Date:** 2026-09-09
- **Scope:** `vinta-django-billing` 0.7.0 → 0.8.0 upgrade, vs `main` (`ee8d2615`)
- **Audience:** Web SPA (React), Partner integrations
- **Breaking changes:** yes — one query parameter and three response fields are renamed across four billing endpoints

## Summary

The billing engine stopped keying its data on the **organization** and started keying it on a **billing scope** — a row naming whoever pays. The engine can now sell to an organization, a bare user, or anything else, and `scope_type` says which.

**For this API nothing about who pays has changed.** Every payer here is still an organization, and there is exactly one scope per organization, created and maintained automatically. What changed is the *identifier* the billing endpoints speak in: where they used to accept and return an organization id, they now accept and return a **scope id**.

Scope ids are **not** organization ids. They are a different sequence over a different table. An organization id passed where a scope id is expected will either 400 or silently match the wrong payer, so this is not a change you can absorb by renaming a field and passing the same value.

Four endpoints are affected, all under `/billing/usage/`. No URL changed, no status code changed, and no authentication or permission behaviour changed. There is **no deprecation window** — the old names are gone in the same release.

If your integration does not read `/billing/usage/`, you are unaffected.

---

## Breaking changes

### 1. `?organization=` is now `?scope=`

| Endpoint | Before | After |
| --- | --- | --- |
| `GET /billing/usage/occurrences/` | `?organization=<organization id>` | `?scope=<scope id>` |
| `GET /billing/usage/occurrences{format}` | `?organization=<organization id>` | `?scope=<scope id>` |

`integer`, optional. Semantics are otherwise unchanged: it narrows the ledger to rows attributed to one payer, and the id must be inside the caller's pooled billing subtree — an id outside it is a validation error, not an empty result.

Sending `?organization=` now does nothing. It is not rejected — unknown query parameters are ignored — so an un-migrated client silently receives the **unfiltered** ledger for its whole pooled subtree. This is the one change here that fails quietly, and on a widening rather than a narrowing, so check for it first.

### 2. `by_organization` is now `by_scope`, and its entries are re-keyed

Affects:

| Endpoint | Schema |
| --- | --- |
| `GET /billing/usage/retrieve_usage/` | `UsageResponse.limits[].by_scope` (via `EffectiveLimitUsage`) |
| `GET /billing/usage/retrieve_usage{format}` | same |
| `GET /billing/usage/periods/{id}/` | `BillingPeriodSummaryDetail.resources[].by_scope` (via `BillingPeriodResourceUsage`) |
| `GET /billing/usage/periods/{id}{format}` | same |

The array itself is renamed, and so is the identifying field inside each entry:

| Before | After | Type |
| --- | --- | --- |
| `by_organization` | `by_scope` | `array<UsageByScope>`, **required** |
| `by_organization[].organization_id` | `by_scope[].scope_id` | `integer`, **required** |
| `by_organization[].name` | `by_scope[].name` | `string`, **required** — unchanged |
| `by_organization[].usage` | `by_scope[].usage` | `integer`, **required** — unchanged |

`name` still renders the organization's name, so anything you display is unchanged. Only the id you key on moved.

The absent-not-zero contract is unchanged: a payer in the pool that contributed nothing to a resource is **omitted from the array entirely**, never present with `usage: 0`. Ordering is now by `scope_id` ascending (was `organization_id` ascending) — if you relied on the order matching an organization-id sort you no longer can, so sort client-side if it matters.

A scope that no longer exists still renders, with `name: ""`, and still counts toward the row's `total`. That behaviour is unchanged; only the field name is.

Before:

```json
{
  "resource_key": "organization_members",
  "total": 14,
  "by_organization": [
    { "organization_id": 12, "name": "Acme", "usage": 900 },
    { "organization_id": 31, "name": "Acme West", "usage": 350 }
  ]
}
```

After:

```json
{
  "resource_key": "organization_members",
  "total": 14,
  "by_scope": [
    { "scope_id": 4,  "name": "Acme", "usage": 900 },
    { "scope_id": 9,  "name": "Acme West", "usage": 350 }
  ]
}
```

Note the ids differ between the two payloads on purpose: scope `4` is the scope that bills organization `12`. There is no arithmetic relationship between the two numbers.

### 3. `billing_root_organization_id` is now `billing_root_scope_id`

| Endpoint | Before | After | Type |
| --- | --- | --- | --- |
| `GET /billing/usage/retrieve_usage/` | `billing_root_organization_id` | `billing_root_scope_id` | `integer`, **required** |
| `GET /billing/usage/retrieve_usage{format}` | same | same | same |

The id of the payer holding the subscription this usage is charged against — the reseller root for a child organization, the organization itself otherwise. Same meaning, now expressed as a scope id.

If you compared this value against an organization id you hold (to decide "am I the billing root?"), that comparison is now always false. Compare it against a `by_scope[].scope_id` instead, or drive the decision off something else — the API does not currently expose an organization-id-to-scope-id mapping.

---

## Not changed

Called out because they are the things most likely to be assumed broken:

- **No URL, method, or status code changed.** Every path is exactly as it was.
- **Authentication and permissions are unchanged.** Who may read billing, and for which organization, is the same question answered the same way. `X-Organization-Id` still selects the tenant on every billing endpoint — that header is this API's own tenancy mechanism and has nothing to do with the scope rename.
- **Every other billing endpoint is untouched** — billing profiles, payment providers, plans, subscriptions, add-ons, payment methods, the two provider webhooks. Their request and response shapes are byte-identical.
- **No GraphQL surface changed.** The public GraphQL API exposes no billing usage fields, so partner integrations built on it are unaffected unless they also call the REST usage endpoints.
- **Notification and email content is unchanged.** Dunning and usage-warning messages still name the organization.

## Migration checklist

1. Regenerate your client from the updated `schema.yml`.
2. Search for `by_organization`, `organization_id` **inside billing usage payloads**, and `billing_root_organization_id`. Rename to `by_scope`, `scope_id`, `billing_root_scope_id`.
3. Search for `?organization=` on `/billing/usage/occurrences/`. This is the silent one — an un-migrated call returns a wider result set rather than an error.
4. Delete any code that compares a billing id against an organization id. Those comparisons are now always false.
5. If you cached or persisted `organization_id` values read out of a billing payload, they are stale — they were organization ids and their replacements are scope ids over a different table.
