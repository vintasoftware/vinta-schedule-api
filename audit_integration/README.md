# audit_integration

The glue between `vinta_audit_logs` — a generic, installable audit log — and this
project. `vinta_audit_logs` knows how to write and read an append-only trail; it
does not know that a tenant here is an `Organization`, that an actor can be an
API token, or that memberships carry groups and permissions. Everything in this
app is that knowledge.

Nothing here should be needed to *use* the audit log. Call sites talk to
`AuditService` and hold portable DTOs; this app is what makes those DTOs land in
the right columns.

## The four settings

`vinta_audit_logs` is configured entirely through these (see
`vinta_schedule_api/settings/base.py`):

| Setting | Points at |
| --- | --- |
| `AUDIT_SCOPE_MODEL` | `audit_integration.OrganizationAuditScope` |
| `AUDIT_IDENTITY_MODEL` | `audit_integration.OrganizationAuditIdentity` |
| `AUDIT_CELERY_APP` | this project's Celery app, by dotted path |
| `AUDIT_SERVICE_FACTORY` / `AUDIT_REPOSITORY_FACTORY` | callables that pull the service and repository out of the DI container |

The first two are the `AUTH_USER_MODEL` pattern. The last three are dotted paths
rather than DI providers on purpose: a package should not force its DI library on
the projects installing it.

## The two seams

They run in different places, which is why they are separate objects.

**`OrganizationAuditService`** — runs *synchronously*, in the request. Turns this
project's principals into portable snapshots: `actor_from_membership`,
`actor_from_system_user`, `actor_from_single_use_code`, `system_actor`,
`scope_from_organization_id`. Synchronous because every input is mutable state —
a membership's groups, a token's scopes — and an audit trail that re-reads them
in the worker records what was true at write time rather than at action time.

**`OrganizationAuditRepository`** — runs in the worker. Turns those snapshots
into rows: `build_scope_defaults`, `build_identity_defaults`,
`attach_identity_relations`, and the inverse `identity_to_snapshot` /
`scope_to_ref`.

## What an identity records

One row per audit record — a snapshot, never shared between records.

Authorization here is groups and permissions; there is no `role` column on
`OrganizationMembership`. So what gets recorded is the groups and permissions
themselves, twice:

- `membership_groups` / `membership_permissions` — live relations, for queries
  that join ("everything done by anyone holding this permission").
- `group_names` / `permission_keys` — JSON lists of strings, inherited from
  `AbstractAuditIdentity`. The durable half: groups get renamed and deleted, and
  when they do the relations lose their rows while these do not. Also what a
  replica using the stock identity model receives.

Records migrated from the old `audit` app carry neither. The old schema stored a
derived `"admin"` / `"member"` label and nothing else, so those rows have
`metadata["legacy_membership_role"]` and empty group/permission lists — see
`migrations/0002_backfill_from_legacy_audit.py`. Anything reading across that
boundary needs to know it: before the move there is a role, after it there are
groups.

## Filtering by columns only this project has

`AuditQuery` is portable by design — every field on it is a string, a timestamp,
or a pair of them, because the same object has to mean the same thing pointed at
Postgres, at an in-memory dict, or at a warehouse. It cannot name
`users.User.email`.

`OrganizationAuditQuery` (in `types.py`) extends it, and
`OrganizationAuditRepository._filtered_queryset` teaches the ORM repository the
new fields:

```python
from audit_integration.types import OrganizationAuditQuery

# Everything one person did in one organization.
page = audit_service.query(
    OrganizationAuditQuery(
        scope_keys=[str(organization.id)],
        actor_user_emails=["hugo@vinta.com.br"],
    )
)
```

Available today: `actor_user_emails`, `actor_group_ids`,
`actor_permission_codenames`, `organization_ids`.

**Adding another** takes two edits, and they must land together:

1. A field on `OrganizationAuditQuery`, optional and defaulting to `None`.
2. A clause in `OrganizationAuditRepository._filtered_queryset`, after the
   `super()` call.

Three rules keep that safe:

- **Extend, never replace.** Subclassing means every portable filter still works,
  and any repository can still be handed one of these.
- **`None` means inactive, `[]` means active-and-unsatisfiable.** Same as the
  portable filters.
- **A backend that cannot apply a field must refuse, not ignore it.**
  `AuditQuery.active_extension_fields()` reports which extension fields are set,
  and `vinta_audit_logs.filtering.record_matches` raises `NotImplementedError`
  rather than returning results that look filtered and are not. That is why
  handing an `OrganizationAuditQuery` with `actor_user_emails` to
  `InMemoryAuditRepository` is an error rather than a silent full result.

### The cost

These filters **join**; that is what they are for, and it is the trade. The
portable filters read denormalized columns on the audit row itself and use the
browse indexes (`scope_key`, then the narrowing column, then `created_at DESC`).
The extension filters reach through to the identity table and past it, so they
do not, and they get slower as the log grows.

Use them for investigation, not for a paginated view someone loads on every page.
And pair them with a portable filter whenever you can: `scope_keys` plus an actor
email lets Postgres cut the log to one tenant on the index first and join only
what survives. The email on its own is a scan.

`organization_ids` is the exception — the scope key *is* the organization pk, so
it lands on the same index every browse uses.

## Testing

- `tests/test_hooks.py` — the two seams, tested where each runs.
- `tests/test_project_query.py` — the extension filters, including the refusal.
- `tests/test_legacy_backfill.py` — the one-shot migration off the old `audit`
  app: idempotency, batching, and the cases that would otherwise abort it
  (a deleted user account, an absent legacy table).
