# Migrate `organizations` onto vinta-django-orgs — Implementation Plan

Migrates this repo's bespoke multi-tenancy layer onto [vinta-django-orgs](https://github.com/vintasoftware/vinta-django-orgs) `0.2.0`, and replaces the flat `role` / `is_billing_owner` membership attributes with the package's `auth.Group` / `auth.Permission` model.

> **Amended 2026-08-12 for package `0.2.0`.** The plan was originally written against `0.1.1`, whose Django app was labelled `organizations` — the same label as ours. Everything about the collision that forced (Phase 1a) renaming our app to `tenancy`, and everything that rename in turn forced (Phase 1b's content-type relabel, the `audit.subject_type` namespace backfill, and the seeded-database migration-history command), existed *only* to service that clash. `0.2.0` renames the package's own Python packages and app labels to `vinta_orgs` / `vinta_orgs_custom_data`, so the clash is gone and our app keeps the name `organizations`. Phases 1a, 1b, and 1c are collapsed into a single **Phase 1**. See the **Amendments** section at the end for the full record.

No SPEC sibling exists — this is a migration of existing behavior rather than a new feature, so the contract is "what the system does today, on a different foundation". Every behavioral change this plan *does* make is named in **Guiding Decisions** and nowhere else.

## 1. Goals

1. **Retire the bespoke tenancy layer.** `@common/fields.py`'s `TenantSafeForeignKey` / `OrganizationForeignKey` / `OrganizationOneToOneField` and `organizations.OrganizationModel` are replaced by the package's `OrganizationSafeForeignKey` / `OrganizationSafeOneToOneField` / `SingleOrganizationModelMixin` across all 34 scoped models and 65 declared relations.
2. **Adopt implicit, context-bound organization scoping.** The default manager on every scoped model scopes to the organization bound to the current `contextvars` context, with `STRICT_ORGANIZATION_FILTER = True` so an unbound query raises rather than silently returning nothing.
3. **Replace `role` / `is_billing_owner` with groups and permissions.** Three global `auth.Group` rows plus custom `Meta.permissions` back every authorization decision that today reads `membership.is_admin` or `membership.is_billing_owner`. Every check reads `has_perm`, never a group name.
4. **Keep the `X-Organization-Id` request contract intact.** A custom retriever plugged into `ORGANIZATION_RETRIEVERS` resolves the organization from the existing header; `TenantScopedViewMixin` keeps its membership-aware 400/403 resolution rules and binds the context.
5. **Expose resolved permissions on the API, drop `role`.** REST and public GraphQL surfaces report what the caller *may do*, not what they *are called*.

**Non-goals:**

- **`vinta_orgs_custom_data`.** The package's second app (per-organization dynamic tables, org-specific field definitions, per-table permission rows) is not installed, not migrated, and not referenced.
- **`OrganizationSite` and domain-based tenancy.** We do not do subdomain tenancy. `retrieve_by_domain` stays out of `ORGANIZATION_RETRIEVERS` and `CACHE_ORGANIZATION_RETRIEVAL` is irrelevant. The `OrganizationSite` model is *not* swappable, so installing the package app creates its table regardless — it stays empty and unread.
- **Per-organization custom roles.** Global groups only. Checks read permissions rather than group names specifically so a per-org layer can be added later without touching call sites, but no per-org group rows, name mangling, or tenant-facing role-management surface ships here.
- **Session-based and `Organization-Slug` header resolution.** `retrieve_by_session` and `retrieve_by_http_header` stay out of the retriever list.
- **Re-litigating which models are tenant-scoped.** The 34 models that are scoped today are the 34 that are scoped after. `payments` is the one genuine ambiguity — see **Open Questions**.
- **Reversibility guarantees per phase.** Pre-launch, no production tenants (see **Guiding Decisions**). Migrations are forward-only where a reverse would cost real work.

## 2. Guiding Decisions

**Amended 2026-08-12** (package `0.1.1` → `0.2.0`): the **App-label collision** row below is replaced. Its old resolution — rename our app to `tenancy` — is withdrawn, along with every consequence it had. Affects the phases formerly numbered 1a, 1b, and 1c, now collapsed into **Phase 1**; the **No feature flag** row is narrowed to match. Nothing else in this table changes: the composite-PK unwind, the scoping-semantics flip, the retriever decision, both slug rows, and the whole group/permission design are independent of what the package's app is called.

**Amended 2026-08-13** (package `0.2.0` → `0.3.0`): a new **Package owns the authorization substrate** row is added below, and the **Organization resolution**, **Scoping semantics flip**, and **Four rules stay hand-written** rows are revised. `0.3.0` upstreams the four substrate pieces this plan had been building by hand — the membership `is_active` gate, organization-named permission resolution, the DRF resolution seam, and the transactional-flush test fixture. Affects Phases 3, 3.5, 4, 5, and 6, all of which are rewritten in place. The group/permission *design* is unchanged: the catalog, the three seeded groups, and every phase's acceptance criterion stand exactly as written. What changes is who implements the substrate underneath them.

**Amended 2026-08-15** (package `0.3.0` → `0.4.0`): `0.4.0` **deletes the function-based API**. `vinta_orgs.helpers` no longer exists, and the module-level organization-context functions (`get_current_organization`, `set_current_organization`, `clear_current_organization`, `reset_current_organization`, `organization_context`) are removed in favour of class-specialized generics — a project declares one `OrganizationState[Organization]`, one `OrganizationService[Organization]` and one `MembershipService[Organization, Membership]` subclass, binding its swapped models once so django-stubs can infer concrete return types. Two further breaking changes matter here: **organization ownership is now immutable on existing rows** (`save()` / `update()` / `bulk_update()` / `update_or_create()` and conflict-updating `bulk_create()` raise `OrganizationCannotBeUpdatedError` unless passed `unsafe_organization_update=True`), and the zero-argument model getters are typed against the abstract bases. Affects Phases 3, 3.5, and 6, plus rebases of the branches between them. **Phases 0, 1, 2a and 2b are untouched**: they pin `>=0.2,<0.3`, where the function API still exists, so Phase 2a's re-export module keeps working on its own branch and is rewritten in Phase 3, which owns the bump. Nothing in the group/permission design changes.

**Amended 2026-08-14**: the **Package owns the authorization substrate**, **Organization resolution**, and **Scoping semantics flip** decisions now apply consistently to the final implementation. The application no longer overrides package `0.3.0` behavior for `create()`, `bulk_create()`, membership resolution, or membership-shaped permission checks. Affects Phases 3, 3.5, and 6; the branches between them are rebased without body changes.

| Decision | Resolution |
|---|---|
| **Package owns the authorization substrate** (amended, package `0.3.0`) | **Anything `vinta-django-orgs` `0.3.0` ships, we consume rather than reimplement.** The membership `is_active` gate, organization-named permission resolution, membership-shaped permission checks, the DRF resolution seam and table, the active-membership query, scoped-manager `create()` / `bulk_create()`, and the transactional-flush fixture all come from the package. **What stays ours** is domain or an application-specific adapter: the capability catalog and its codenames, the `X-Organization-Id` header name and integer-pk-to-slug translation, the existing client-facing 400/403 bodies, the reseller-root subtree walk, the branding entitlement gates, and `membership_role_label` with the two published role strings. Request-aware code reads the package-populated `request.organization_membership`; code outside a request calls `resolve_membership_for_user` with an explicit slug when it has one and accepts the upstream ambiguity refusal when it does not. The application does not keep a second resolver, user-level membership stash, or permission helper. The test that keeps the line honest is the one already established by the prune chore: if a test would still pass against a stock package install, it belongs upstream, not here. |
| **App-label collision — none** (amended, package `0.2.0`) | **There is no collision, so our app keeps the name `organizations` and nothing is renamed.** `0.1.1` labelled the package's own app `organizations`, identical to ours, and the original plan resolved that by renaming ours to `tenancy` with `db_table` pinned on every model. `0.2.0` renames the *package's* Python packages and app labels to `vinta_orgs` and `vinta_orgs_custom_data` for exactly this reason, so ours is unambiguous as it stands. Consequences of withdrawing the rename: no `git mv`, no `label = "tenancy"`, **no `db_table` pins at all** (the default `{app_label}_{model}` already resolves to each model's existing table name — verify with `makemigrations --check` rather than assuming, and pin only a model whose default would not match), no migration-graph rewrite across 79 migration files, no `django_content_type` / `auth_permission` relabel, no `audit.subject_type` namespace backfill, and no seeded-database migration-history command. Our imports read `from organizations...` exactly as they do today; the *package's* read `from vinta_orgs...`. The reason table renames were avoided in the first place — the **five** raw-SQL PROTECT FKs name tables as string literals — still holds and is now satisfied for free. (Corrected while verifying this amendment: the original text said the FKs lived in `calendar_integration` *and* `audit/migrations/0001_initial.py`. All five are in `calendar_integration` — `0026`, `0032`, `0036`, `0038`, `0040`; `audit` has no raw-SQL table literal at all.) |
| **Composite primary key** | `OrganizationMembership.pk = SafeCompositePrimaryKey("user", "organization")` is unwound back to a surrogate `id`, because Django cannot hang a `ManyToManyField` off a composite-PK model and the package's `groups` / `permissions` fields are exactly that. **The `uniq_membership_user_organization` unique constraint is kept**, which is what makes this cheap: the five raw-SQL composite FKs target that *constraint*, not the PK, so they need no rebind and no data migration. |
| **Scoping semantics flip** | Our `BaseOrganizationModelManager` requires an explicit `filter_by_organization(org_id)`; the package's `objects` scopes implicitly and returns `.none()` when nothing is bound. This is the single most dangerous delta in the migration — an unbound query in a Celery task reads as "no data" rather than as a bug. Mitigated two ways: a behavior-neutral audit phase binds every call site *before* any model flips, and `STRICT_ORGANIZATION_FILTER = True` from the moment the first one does, so the failure is an exception rather than an empty list. (Amended, package `0.3.0`: strict mode is now the package **default**, and `0.3.0` additionally stops `create()` / `bulk_create()` scoping the queryset they are built from — both insert without reading, so the scoping could only ever refuse a valid call, including `create(organization=organization)` and every `instance.related_set.create(...)`. Phase 3 deletes the application's now-obsolete overrides so this behavior has one owner. We keep the setting spelled out explicitly in `SHARED_SCHEMA_ORGANIZATIONS` rather than inheriting it: this plan's central safety argument rests on it, and a default is a weaker guarantee than a declaration. `get_or_create()` / `update_or_create()` still scope, which is correct — they look a row up first, and that lookup is exactly the one that must not span tenants. That is the Phase 2a blocker, now fixed upstream.) |
| **Organization resolution** (amended, package `0.3.0`) | A custom adapter reads `X-Organization-Id` (integer PK), translates it to the slug expected by the package, and preserves the application's existing refusal bodies. `TenantScopedViewMixin` is retained rather than replaced by `OrganizationMiddleware` — the middleware runs before DRF authentication has populated `request.user`, so a user-dependent organization cannot be resolved there. The mixin composes `vinta_orgs.drf.OrganizationScopedAPIViewMixin`, which owns the `perform_authentication` seam, writes `request.organization_membership`, binds the organization, and releases it in a `finally` around `dispatch`; `resolve_membership_for_user` owns the resolution table. Application code reads `request.organization_membership` instead of copying it onto the user. Outside a request, callers use `resolve_membership_for_user` directly, so a user with multiple active memberships and no selected organization is refused rather than silently assigned their oldest membership. The package raises `AmbiguousOrganizationError` where the application raised a DRF `ValidationError`; the 400 status and the `{"detail": ...}` body are contract and must survive the swap — pin them, do not re-derive them. |
| **Slug becomes NOT NULL** | `AbstractOrganization` declares `slug = CharField(max_length=255, unique=True)`, NOT NULL. We inherit it rather than overriding, and backfill `slugify(name)` with a numeric disambiguator on collision, falling back to `org-<pk>` when the derived value fails `@organizations/slug_validation.py`'s reserved-word or confusable-character rules. Note the consequence: **slug is public** (it appears in branded login URLs), so a derived slug discloses the organization name. Accepted here **only for the backfill** — the pre-existing rows the migration touches, on the grounds that there is no production data to disclose. |
| **Slug precondition for branding writes is retired** (review of the phase formerly numbered 1c) | `organizations.permissions.evaluate_branding_write_gate`'s third condition (`if not organization.slug: return NO_SLUG`) — "an eligible organization must also have picked a public slug before it may write branding" — is retired as a product rule. The NOT NULL slug above made it unsatisfiable for any organization reachable through a supported write path the moment this phase shipped; the Phase 1c review closed the one remaining loophole (an out-of-band `queryset.update(slug="")` past `save()`, which nothing prevented at the database level) with `models.CheckConstraint(condition=~models.Q(slug=""), name="organization_slug_not_blank")` on `Organization.Meta.constraints`, making the retirement permanent rather than merely untested. The `BrandingWriteGateReason.NO_SLUG` enum member, its `BRANDING_GATE_EXCEPTIONS` entry, and the `if not organization.slug` check are kept dead-with-reason rather than deleted immediately — they are still part of the gate's public contract — and are scheduled for removal in **Phase 4** (see that phase's Changes list). |
| **Runtime slug default is opaque, not name-derived** (review of the phase formerly numbered 1c) | The row above's disclosure trade-off was accepted **only for the Phase 1 slug backfill** of pre-existing rows (no production data to disclose at that moment) — it was never sanctioned as `Organization.save()`'s permanent runtime default, which would make name disclosure permanent for every organization saved without an explicit slug from this deploy on. `Organization.save()`'s fallback (used when a caller left `slug` blank) now derives the opaque `org-<token>` form (`organizations.slug_generation.derive_organization_slug(..., disclose_name=False)`), not `slugify(name)`. Name-derivation remains sanctioned for exactly one runtime write path — `OrganizationService.create_organization`, the self-serve "create my own organization" flow, where the human caller explicitly chose `name` for their own, about-to-be-public organization — which now computes and passes an explicit, name-derived `slug` itself rather than relying on the model's fallback. |
| **No feature flag** | This repo has no feature-flag module, and the change is not flag-shaped: a default-manager swap and a change of base class cannot be gated per-request or per-tenant, because they are resolved at class-definition and migration time. Combined with pre-launch status and no production tenants, the flag would cost a PR and gate nothing. **The safety mechanism is the audit phase plus `STRICT_ORGANIZATION_FILTER`, not a flag.** There is consequently no flag-removal phase. |
| **Group scope** | Three global `auth.Group` rows — `organization_admin`, `organization_billing_owner`, `organization_member` — shared by every organization, seeded by a data migration. Every authorization check reads `user.has_perm("app.codename")`, **never** `membership.groups.filter(name=...)`, so introducing per-org groups later changes the seeding and nothing above the auth backend. This deliberately diverges from the package's own `IsOrganizationOwner`, which filters on `groups__name='organization_owner'` — we do not use that class. |
| **Permission catalog shape** | Custom `Meta.permissions` named for *capabilities* (`manage_billing`, `manage_members`, `manage_branding`), not for the model-CRUD triples `auth.Permission` defaults to. Our authorization questions are behavioral ("may this member change the plan"), and mapping them onto `change_subscription` would misrepresent them. |
| **Four rules stay hand-written** (amended, package `0.3.0`) | `IsBillingOwnerOrAdmin`'s acting-reseller-root walk grants over organization B from a membership in A. `OrganizationManagementPermission` gates on membership *absence*. Both branding gates are entitlement-driven, not role-driven. These four keep bespoke logic; what changes is the sub-check they compose with (`membership.is_admin` becomes an organization-named permission check). **The reseller-root rationale is restated, because `0.3.0` invalidates the original one.** The row used to argue the walk was unavoidable *because the backend keys on the current organization and cannot see a grant held in an ancestor* — `has_organization_permission(user, permission, organization)` now takes the organization as an argument, so that specific obstacle is gone. The walk stays anyway, on the surviving reason: **which** ancestor to ask about is `resolve_billing_root` plus `is_target_in_subtree`, and that is our billing topology, not something a tenancy package can answer. Note the standing finding from Phase 4's review, unchanged by this amendment: the branch is **unreachable as a grant** — every REST caller passes an ancestor-or-self while the subtree test admits only self-or-descendant, so the intersection is `target == acting`, where the direct check has already granted. It is a correct statement of the subtree rule that no request can make decisive. Left in place; the decision to collapse it is still deliberately deferred. |
| **Pre-launch posture** | No production tenants. Data migrations are written to be idempotent because that costs nothing, but phases are not required to carry a tested reverse path, and the API break ships without a deprecation window. |
| **`meta` and timestamp indexes** | `BaseModel` gives `Organization` / `OrganizationMembership` a `meta` JSONField and `db_index=True` on `created` / `modified`; `AbstractOrganization` extends `TimeStampedModel`, which gives neither. `meta` is verifiably unused on both models (only `payments` reads it), so it is dropped. The two timestamp indexes are dropped with it — neither model is queried by timestamp range. |

## 3. Data Model Changes

### 3.1 `organizations.Organization`

```python
# organizations/models.py
from vinta_orgs.models import AbstractOrganization


class Organization(AbstractOrganization):
    # Inherited from AbstractOrganization: name, slug (NOT NULL unique),
    # created, modified. `name` is redeclared only where our max_length differs.
    should_sync_rooms = models.BooleanField(default=False, ...)
    external_event_update_policy = models.CharField(...)
    week_start = models.CharField(...)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="child_organizations", ...)
    can_invite_organizations = models.BooleanField(default=False, ...)

    class Meta(AbstractOrganization.Meta):
        # No `db_table` — the app label is still `organizations`, so Django's
        # default already resolves to `organizations_organization`.
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="uniq_org_name_per_parent"),
        ]
```

Dropped: `BaseModel`'s `meta` JSONField, the `created` / `modified` indexes. Retained unchanged: `is_reseller()`, `get_branding_root()`, `resolve_branding()`, `resolve_branding_for_display()`.

`slug` is no longer nullable. `@organizations/slug_validation.py` stays exactly where it is, unchanged — the format, reserved-word, and confusable-character rules still apply at every write surface.

### 3.2 `organizations.OrganizationMembership`

```python
# organizations/models.py
from vinta_orgs.models import AbstractOrganizationMembership


class OrganizationMembership(AbstractOrganizationMembership):
    # Inherited: organization, user, groups (M2M auth.Group),
    # permissions (M2M auth.Permission), created, modified.
    is_active = models.BooleanField(default=True, db_default=True, db_index=True, ...)

    objects = OrganizationMembershipManager()   # see 3.3

    class Meta(AbstractOrganizationMembership.Meta):
        # No `db_table` — see 3.1.
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"],
                                    name="uniq_membership_user_organization"),
        ]
```

Three structural changes, in dependency order:

1. **`pk` reverts to a surrogate `id`.** `SafeCompositePrimaryKey` is removed from the model; `@common/fields.py`'s `SafeCompositePrimaryKey` and `_SafeCompositeAttribute` are deleted in the final phase once nothing imports them.
2. **`uniq_membership_user_organization` is kept.** This is load-bearing. Five migrations in `calendar_integration` — `0026_calendarownership_membership_protect_fk.py`, `0032_eventattendance_...`, `0036_calendarmanagementtoken_...`, `0038_externaleventchangerequest_resolved_by_...`, `0040_bookingpolicy_...` — declare `FOREIGN KEY (<name>_user_id, organization_id) REFERENCES organizations_organizationmembership (user_id, organization_id)`. A composite FK may target any unique constraint, so all five survive the PK change untouched. (There is no `audit` equivalent; an earlier revision of this plan said there was.)
3. **`role` and `is_billing_owner` are dropped**, but not until Phase 6, after the group backfill and the API surface change have both landed.

`AbstractOrganizationMembership` also renames the reverse accessor: it declares `related_name="memberships"` on both FKs, where ours uses `organization_memberships` (user side) and `memberships` (organization side). The user-side rename touches every `user.organization_memberships` call site.

### 3.3 Manager plumbing

`AbstractOrganizationMembership` sets `objects = SingleOrganizationUnscopedManager()` deliberately — a membership is *how* an organization gets selected, so scoping it to the selected organization is circular. Our `OrganizationMembershipManager` must therefore inherit from `SingleOrganizationUnscopedManager` rather than `models.Manager`, or the reverse accessors (`user.memberships`, `organization.memberships`) become scoped and every membership lookup that runs before an organization is bound returns nothing.

Its three domain methods survive, one with a changed body:

- `occupying_a_seat(organization_ids)` — unchanged.
- `active_for_user(user)` — unchanged.
- `billing_recipients(organization_id)` — the `Q(role=ADMIN) | Q(is_billing_owner=True)` filter becomes a permission-shaped query, `filter(groups__permissions__codename="manage_billing")`, keeping the "who may write billing" and "who receives dunning" definitions derived from one source rather than two.

`BaseOrganizationModelManager` and `BaseOrganizationModelQuerySet` in `@organizations/managers.py` / `@organizations/querysets.py` are deleted once all 34 models have moved to the package's managers.

### 3.4 Permission catalog

Declared as custom `Meta.permissions` on the models that own the capability:

| Permission | Declared on | Replaces |
|---|---|---|
| `organizations.manage_members` | `OrganizationMembership` | `membership.is_admin` in `IsOrganizationAdmin`, `CalendarGroupPermission`, `User.is_organization_admin` |
| `organizations.manage_organization` | `Organization` | `membership.is_admin` on organization-update paths |
| `organizations.manage_branding` | `Organization` | the role half of the branding gates (the entitlement half is unchanged) |
| `payments.manage_billing` | `Subscription` | `membership.is_admin or membership.is_billing_owner` in `IsBillingOwnerOrAdmin` |

Seeded group → permission mapping, written by a data migration:

- `organization_admin` → all four.
- `organization_billing_owner` → `payments.manage_billing`.
- `organization_member` → none (it exists so "has a membership" and "has no capabilities" are distinguishable, and so a future per-org layer has a base to extend).

### 3.5 Type plumbing

- `OrganizationRole` (`TextChoices`) is deleted. `public_api.types.OrgRole`, the strawberry enum mirroring it, is deleted with the invitation input change (see **API Design**).
- Delete `get_active_organization_membership()` and its `_UNSET` sentinel from `@organizations/models.py`. Request-aware callers read `request.organization_membership`; off-request callers use `vinta_orgs.helpers.resolve_membership_for_user` directly. No caller may retain the old silent oldest-membership fallback.
- `OrganizationMembershipForeignKey` in `@common/fields.py` is retained — see **Open Questions**.

## 4. API Design

### 4.1 Membership representation

`role` is removed from every response and replaced by a resolved permission list. Group names are never exposed, so a later per-org group layer is not a second client break.

```
GET /organizations/current/
- { "organization": {...}, "role": "admin", "can_manage_branding": true }
+ { "organization": {...},
+   "permissions": ["organizations.manage_members", "organizations.manage_organization",
+                   "organizations.manage_branding", "payments.manage_billing"],
+   "can_manage_branding": true }
```

`can_manage_branding` stays a distinct field rather than folding into `permissions`: it is the *composite* of `organizations.manage_branding` and the `white_label_branding` entitlement plus the parentless check, and collapsing it into the permission list would misreport an entitled-but-unpermitted caller.

`GET /organizations/mine/` gains the same `permissions` key per row and drops `role`. `GET /organization-members/` drops `role` and `is_billing_owner`.

### 4.2 Group assignment replaces role update

```
- POST /organization-members/{user_id}/update-role/   { "role": "admin" }
+ POST /organization-members/{user_id}/groups/        { "groups": ["organization_admin"] }
```

Write-side only, and the one place a group name is accepted — assigning a group is the act of choosing one, so there is nothing to abstract. Errors preserved from the old endpoint: setting the current value is an idempotent success, and demoting the last active member holding `organizations.manage_members` in the organization is rejected (the "protect the last active admin" rule, restated in permission terms).

### 4.3 Public GraphQL invitation input

`OrganizationInvitation.role` becomes `groups`, and the `OrgRole` enum is deleted:

```
- inviteToOrganization(email: String!, role: OrgRole = MEMBER)
+ inviteToOrganization(email: String!, groups: [String!] = ["organization_member"])
```

This is a breaking change for partner integrations. `@ai-tools/skills/handoff-to-client` produces the migration document in Phase 5.

## 5. Phased Rollout

Bundled by architectural layer, per the granularity decision. The two layers that would otherwise blow past the reviewability target are split into sub-phases; a sub-phase is still one concern and still independently mergeable.

---

### Phase 0 — Bind the organization at every unscoped call site

**Goal**: no user-visible outcome. Ship value: none on its own — this is the safety net that makes Phase 2 survivable. Every query that will *become* implicitly scoped gets an explicit organization binding while the managers still ignore it, so the phase is provably behavior-neutral and the risky flip lands on already-bound code.

**Feature flag**: none — see the **Guiding Decisions** "No feature flag" row.

Changes:

1. Add `common/organization_context.py`: a thin re-export of the package's `organization_context` / `set_current_organization` so call sites import from one place and Phase 2 does not re-touch them.
2. Wrap every org-scoped Celery task body in `organization_context(...)`: `@payments/tasks.py`, `@audit/tasks.py`, `@calendar_integration/tasks/calendar_sync_tasks.py`, `@webhooks/tasks.py`. Tasks that fan out across organizations bind per-iteration, not once.
3. Same for the five management commands: `@payments/management/commands/reconcile_billing_period.py`, `@calendar_integration/management/commands/webhook_health_check.py`, `refresh_webhook_subscriptions.py`, `repair_untruncated_recurring_parents.py`, `cleanup_webhook_events.py`.
4. `@organizations/admin.py`: the admin is intentionally cross-organization. Point its querysets at `original_manager` explicitly rather than binding a context, so the intent is written down rather than inferred.
5. Add a pytest fixture that asserts no scoped query runs unbound, so Phase 2's flip has a tripwire already in CI.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `common/tests/test_organization_context.py` — binding, nesting, and restoration of the previous binding.
- **Integration**: `calendar_integration/tests/tasks/test_sync_task_scoping.py` — a sync task run under the new binding produces byte-identical results to the same task before the change. This is the phase's whole point: prove neutrality.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Not mechanical — deciding *where* a task's organization boundary sits (per-run vs per-iteration) is a judgement call per task, and getting it wrong is exactly the bug Phase 2 would then bake in.

**Review models**: reviewer Tier 4 — this phase's correctness is the premise every later phase rests on, and a missed call site here surfaces as a silent empty result much later, in a phase whose diff does not contain the bug.

**Reusable skills**: none.

Acceptance: every Celery task and management command that touches an org-scoped model runs inside an `organization_context`, the full suite is green, and the sync-task neutrality test passes against pre-change output.

---

### Phase 1 — Adopt the package's abstract bases, unwind the composite PK, backfill slugs

**Goal**: `Organization` and `OrganizationMembership` are the package's models, carrying our extra fields, with `groups` and `permissions` M2Ms available and unused. Our app is still called `organizations` and every table is still where it was.

**Collapsed from the phases formerly numbered 1a, 1b, and 1c** (amended 2026-08-12 for package `0.2.0` — see the **Amendments** section). Phase 1a existed to rename our app to `tenancy`; Phase 1b existed to repair what that rename broke. `0.2.0` labels the package's own apps `vinta_orgs` / `vinta_orgs_custom_data`, so neither is needed. What survives is the substance of 1c plus one module from 1b (the retriever). Concretely, the following are **not** in this plan any more and must not be reintroduced: the `organizations/` → `tenancy/` move, `label = "tenancy"`, any `db_table` pin, the 79-file migration-graph rewrite, `0023_move_content_types_to_tenancy.py`, `audit/migrations/0002_backfill_subject_type_namespace.py`, and `rename_organizations_migration_history`.

**Feature flag**: none.

Changes:

1. `@pyproject.toml`: add `vinta-django-orgs>=0.2,<0.3` and sync `uv.lock`. Pin the minor — the package is Development Status `Alpha`, and `0.2.0` is itself an app-label rename, which is precisely the kind of move a minor bump makes.
2. `@vinta_schedule_api/settings/base.py`: add `vinta_orgs.apps.OrganizationsConfig` to `INSTALLED_APPS`, kept separate from `INTERNAL_INSTALLED_APPS` (which drives di_core's DI wiring and names only this project's apps). Set `ORGANIZATION_MODEL = "organizations.Organization"` and `ORGANIZATION_MEMBERSHIP_MODEL = "organizations.OrganizationMembership"` so the package's own models are `_meta.swapped` rather than leaving a phantom CASCADE relation on `User.delete()`. Add the `SHARED_SCHEMA_ORGANIZATIONS` dict with `ORGANIZATION_RETRIEVERS` pointing at our retriever (change 3) and every non-goal retriever omitted. `INTERNAL_INSTALLED_APPS` still names `organizations` — it is not renamed.
3. Write `@common/org_retrievers.py::retrieve_by_x_organization_id`, reading `X-Organization-Id` by integer PK. Not yet consulted by anything — `TenantScopedViewMixin` starts using it in Phase 2b. (Carried over from the phase formerly numbered 1b, which is otherwise withdrawn; this module was never rename-dependent.)
4. Resolve the admin double-registration with a supported call rather than a `sys.modules` patch: after `django.contrib.admin.autodiscover()` has run (or via the relevant `AppConfig.ready()`), `admin.site.unregister(...)` for whichever of the package's own admin registrations collide with `@organizations/admin.py`'s existing `ModelAdmin` registrations, then leave ours in place. **This is a security-relevant step, not cosmetic**: the package's `OrganizationMembershipAdmin` exposes `role`, `is_billing_owner`, and `groups` as unguarded staff-editable fields, which ours deliberately does not.
5. Remove `pk = SafeCompositePrimaryKey("user", "organization")` from `OrganizationMembership`; add back a surrogate `id`. Keep the `uniq_membership_user_organization` constraint — the raw-SQL PROTECT FKs target it and must not be touched.
6. Reparent both models onto `AbstractOrganization` / `AbstractOrganizationMembership` as shown in **Data Model Changes**. Drop `meta` and the two timestamp indexes. **Declare no `db_table`** — the app label is unchanged, so every default already matches the existing table; prove it with `makemigrations --check` rather than assuming, and pin only a model whose default would not match.
7. `OrganizationMembershipManager` inherits `SingleOrganizationUnscopedManager`; set `default_manager_name = "objects"`. Getting this wrong scopes `user.memberships` and breaks every pre-selection lookup.
8. Rename the user-side reverse accessor: `user.organization_memberships` becomes `user.memberships` at every call site.
9. Slug backfill data migration, then `ALTER COLUMN slug SET NOT NULL`, then the `organization_slug_not_blank` CHECK constraint. Backfill derives `slugify(name)` with a numeric disambiguator on collision and an `org-<pk>` fallback when the derived value fails `@organizations/slug_validation.py`. `Organization.save()`'s runtime fallback is the **opaque** `org-<token>` form, not `slugify(name)`; the one sanctioned name-derived runtime path is `OrganizationService.create_organization`, which computes and passes an explicit slug itself. Both Guiding Decisions rows on slugs apply in full.
10. Retire `evaluate_branding_write_gate`'s `NO_SLUG` condition explicitly, per the **Slug precondition for branding writes is retired** Guiding Decision — the CHECK constraint in change 9 makes it permanently unreachable. Keep the enum member dead-with-reason until Phase 4 deletes it, and **delete any test helper that manufactured the old state via `queryset.update(slug="")`** rather than leaving a test that fabricates an unreachable condition.
11. `role` and `is_billing_owner` stay on the model, untouched and still read by every permission class. Nothing about authorization changes in this phase.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `organizations/tests/test_app_identity.py` — `Organization._meta.app_label == "organizations"`, `Organization._meta.db_table == "organizations_organization"` (and the same for every other model in the app), the package's app is installed under label `vinta_orgs`, and `makemigrations --check --dry-run` reports nothing pending. This is the regression gate for the whole amendment: it fails loudly if a future change reintroduces a rename or a table move.
- **Unit**: `organizations/tests/test_membership_pk.py` — a membership round-trips through save / refresh / delete on the surrogate PK; `uniq_membership_user_organization` still rejects a duplicate `(user, organization)`.
- **Integration**: `calendar_integration/tests/test_membership_protect_fk.py` — deleting a membership with a `CalendarOwnership` still raises the raw-SQL `RESTRICT`, proving the FK survived the PK change. Note there are **five** such raw-SQL composite FKs across the repo, not two; enumerate them and cover each.
- **Integration**: `organizations/tests/test_slug_backfill.py` — collision disambiguation, reserved-word fallback, idempotent re-run, NOT NULL after, and the CHECK constraint rejecting a blank slug written past `save()`.
- **Unit**: `organizations/tests/test_membership_manager.py` — `user.memberships` returns rows with no organization bound (the unscoped-manager contract).
- **Integration**: `common/tests/test_org_retrievers.py` — the retriever resolves a valid header, returns `None` on a missing, empty, or non-integer header, and returns `None` (never raises) on an unknown PK.
- **Integration**: `organizations/tests/test_admin_registrations.py` — the membership admin registered in `admin.site` is ours, and exposes neither `role` nor `is_billing_owner` nor `groups` as an editable field.

**Suggested AI model**: Tier 4 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Unwinding a composite PK while keeping a constraint that raw SQL depends on, against a base class whose manager semantics are subtle, is the hardest single phase here. The rename plumbing that used to surround this is gone, but none of the difficulty was in the rename.

**Review models**: reviewer Tier 4, fixer Tier 3 — the PROTECT-FK interaction is the kind of thing that passes tests and fails in production, and change 4 is an authorization surface.

**Reusable skills**: `add-migration`; `add-model` for the reparenting conventions.

Acceptance: our app is still labelled `organizations` with every table at its original name and no content-type or migration-history surgery anywhere in the diff; the package is installed under `vinta_orgs`; memberships have a surrogate PK; `groups` / `permissions` M2M tables exist and are empty; every organization has a valid non-null slug and a blank one is rejected by the database; all five raw-SQL PROTECT FKs still fire; the membership admin is ours; and the suite is green with authorization behavior unchanged.

---

### Phase 2a — Flip `calendar_integration` onto the package's mixin and relations

**Goal**: the 28 scoped models and 59 relations in `calendar_integration` use `SingleOrganizationModelMixin` and `OrganizationSafeForeignKey`, with implicit scoping live and strict.

**Feature flag**: none.

Changes:

1. `@calendar_integration/models.py`: `OrganizationModel` → `SingleOrganizationModelMixin` on all 28 models; `OrganizationForeignKey` / `OrganizationOneToOneField` → `OrganizationSafeForeignKey` / `OrganizationSafeOneToOneField` on all 59 relations. The `<name>` / `<name>_fk` column layout is identical between the two implementations, so this is a model-level change with no column churn — verify per model rather than assuming.
2. `STRICT_ORGANIZATION_FILTER = True` in `SHARED_SCHEMA_ORGANIZATIONS`. Phase 0 bound the call sites; this is where an unbound one starts raising.
3. Remove now-redundant explicit `filter_by_organization(...)` calls **only** where the implicit scope provably covers them. Where a query deliberately crosses organizations, switch to `original_manager` and say why in a comment.
4. The package's `class_prepared` receiver adds an `(organization, pk)` index to every scoped model and drops the FK's single-column index. Expect a migration per model; review the index diff rather than accepting it blind.
5. `AUTO_DEFER_SAFE_JOINS` defaults to `True`, which splits `select_related` on a safe relation into a second query. This changes query counts across the app — update the assertions, don't silence them.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `calendar_integration/tests/test_implicit_scoping.py` — a bound query returns only that organization's rows; an unbound one raises `OrganizationNotFoundError`; `original_manager` still crosses organizations.
- **Integration**: `calendar_integration/tests/test_safe_relation_joins.py` — a cross-organization row does not join through a safe relation (reads as missing, not as another tenant's data).
- The existing `calendar_integration` suite passes, with query-count assertions updated for `AUTO_DEFER_SAFE_JOINS` and each change justified in review.

**Suggested AI model**: Tier 4. 28 models and 59 relations with a semantics change underneath each, plus per-model index review and query-plan consequences.

**Review models**: reviewer Tier 4 — a mis-flipped relation reads another organization's data, which is the most serious defect available in this codebase.

**Reusable skills**: `add-migration` for the index migrations; `add-model` for the mixin conventions.

Acceptance: all 28 models scope implicitly, an unbound query raises, the cross-organization join test proves isolation, and the suite is green.

---

### Phase 2b — Flip the remaining scoped models

**Goal**: `audit`, `webhooks`, `public_api`, and `organizations` finish the model layer; `TenantScopedViewMixin` binds the request's organization.

**Feature flag**: none.

Changes:

1. Same flip as Phase 2a for the remaining 6 models and 6 relations across `@audit/models.py`, `@webhooks/models.py`, `@public_api/models.py`, `@organizations/models.py`.
2. `@common/utils/view_utils.py`: after `TenantScopedViewMixin.initial()` resolves the membership, bind the organization to the context and unbind on response. The resolution table (400 on ambiguity, 403 on non-member, per-action opt-outs via `active_org_resolution_optional` / `active_org_optional_actions`) is unchanged — only the binding is new.
3. `@public_api/middlewares.py::PublicApiSystemUserMiddleware` runs before DRF and resolves a system user; confirm it binds an organization before touching scoped models, or moves its scoped work behind the binding.
4. Delete `OrganizationModel`, `BaseOrganizationModelManager`, `BaseOrganizationModelQuerySet` from `organizations/`. `@common/fields.py`'s `TenantSafeForeignKey` / `TenantSafeOneToOneField` stay until Phase 6, but **not** for the reason first written here: `OrganizationMembershipForeignKey` extends `models.Field` directly, not either of them, and deleting `organizations.OrganizationForeignKey` in this phase removed their last users. They now have **zero** users and are dead code with no dependency holding them — Phase 6 deletes them because Phase 6 owns that deletion, not because anything still builds on them.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `common/tests/test_tenant_scoped_binding.py` — the full resolution table still holds (0 / 1 / 2+ memberships × header present / absent / non-member), and the organization is bound during the view body and unbound after.
- **Integration**: `public_api/tests/test_system_user_scoping.py` — a public-API request resolves and binds correctly, and an unbound path raises rather than leaking.
- **Integration**: `audit/tests/test_audit_scoping.py` — audit writes land in the bound organization.

**Suggested AI model**: Tier 3. Fewer models than 2a, but the request-lifecycle binding and the public-API middleware ordering are the judgement-heavy parts.

**Review models**: reviewer Tier 4 — the binding/unbinding lifecycle under DRF exception paths is easy to get subtly wrong, and a leaked binding crosses tenants on the *next* request.

**Reusable skills**: `add-migration`.

Acceptance: all 34 models scope implicitly under strict mode, the resolution table test is green, no binding survives a response (including on the exception path), and the suite is green.

---

### Phase 3 — Groups, permissions, and the organization auth backend

**Goal**: every membership carries groups whose permissions mirror its current `role` / `is_billing_owner`, and `has_perm` answers correctly — with nothing reading it yet.

**Amended 2026-08-13** (package `0.2.0` → `0.3.0`): this phase now carries the version bump, and the auth backend is registered **unsubclassed**. See the **Package owns the authorization substrate** Guiding Decision.

**Feature flag**: none.

Changes:

0. **Rewrite `common/organization_context.py` as a class-specialized state object.** `0.4.0` deletes the module-level `get_current_organization` / `set_current_organization` / `clear_current_organization` / `reset_current_organization` / `organization_context` functions that Phase 2a made this module re-export. Declare one `ProjectOrganizationState(OrganizationState[Organization])` with `model_class = Organization`, and keep the module's existing seven public names as thin shims over `organization_state.get()` / `.set()` / `.clear()` / `.reset()` / `.context()`. **The shims are the point**: roughly 260 call sites across 33 files import these names from this module, and only two test modules import them from the package directly — repoint those two. Rewriting one module keeps the other ~260 untouched, which is why this is a Phase 3 change and not a stack-wide sweep. `OrganizationOrSlug` is no longer public upstream; restate it here in terms of *our* `Organization`, as this module already does for the other signatures.

1. **Bump `vinta-django-orgs` to `>=0.4,<0.5`.** This is the phase that first consumes post-`0.3.0` API, which is why the bump lands here rather than in Phase 1 — the same reasoning as the `0.3.0` bump it replaces, re-verified against `0.4.0`. Phases 0, 1, 2a and 2b pin `>=0.2,<0.3` and keep passing on their own branches, because the function API `0.4.0` deletes still exists there; the first place their code runs under `0.4.0` is this phase's full-suite run. **Three `0.4.0` breaking changes need checking rather than assuming.** *Organization ownership is now immutable on existing rows* — `save()`, `update()`, `bulk_update()`, `update_or_create()` and conflict-updating `bulk_create()` raise `OrganizationCannotBeUpdatedError` unless passed `unsafe_organization_update=True`. Verified before choosing this point: no migration in this repo writes `organization` on an existing row, so nothing needs the opt-in — if a data migration ever does, that call is where the flag belongs, never a blanket setting. *`vinta_orgs.helpers` is deleted*, so `resolve_membership_for_user` and friends move onto a `MembershipService` subclass (see change 0's sibling below). *The zero-argument model getters are typed against the abstract bases*, so pass the concrete class where a model class itself is needed. `ORGANIZATION_GROUP_SEEDERS` already points at our own callable rather than the deleted package helper, so the upstream note about repointing it does not apply to us.

   The `0.3.0`-era reasoning this bump inherits, still true and still worth keeping: `is_active` predates this plan on our concrete model, and a concrete model may legally override a field inherited from an *abstract* base, so our declaration (which carries `db_index` / `db_default` the package's does not) wins and the model state is unchanged — `makemigrations --check` must stay clean, and it failing is the signal that this reasoning was wrong. We use neither `IsOrganizationOwner` nor `DjangoOrganizationModelPermissions`, so their "no organization selected → `False`" behaviour reaches nothing.

1b. **Declare the service subclasses.** `0.4.0` replaces `vinta_orgs.helpers` with generics bound once per project: `Organizations(OrganizationService[Organization])` and `Memberships(MembershipService[Organization, OrganizationMembership])`, each with `model_class`. `MembershipService` derives the organization model from its membership model's foreign key, so it needs no organization-service parameter. The call sites to move are `resolve_membership_for_user` → `memberships.resolve_for_user` and `resolve_organization_for_user` → `memberships.resolve_organization_for_user`; three files under `accounts/` use the first. Our own `OrganizationService` in `organizations/services.py` is a *different, domain* class and is not what this replaces — do not conflate them; if the names collide confusingly, alias the package one at import.

   The `0.3.0` bump's remaining instruction stands unchanged: `is_active` predates this plan on our concrete model, and a concrete model may legally override a field inherited from an *abstract* base, so our declaration (which carries `db_index` / `db_default` the package's does not) wins and the model state is unchanged — `makemigrations --check` must stay clean, and it failing is the signal that this reasoning was wrong. We use neither `IsOrganizationOwner` nor `DjangoOrganizationModelPermissions`, so their new "no organization selected → `False`" behaviour reaches nothing. And the `create()` / `bulk_create()` change only *removes* raises, so nothing written against `0.2.0` breaks. Phases 0, 1, 2a and 2b are therefore untouched by the bump. **Delete the application's `OrganizationScopedManager.create()` and `bulk_create()` overrides in this phase:** package `0.3.0` now owns their exact unscoped-insert behavior, and retaining the overrides creates a second implementation with stale comments. Keep the application-specific `get_queryset()`, `get_or_create()`, `update_or_create()`, and `bulk_update()` behavior.
2. Declare the four custom permissions from the **Permission catalog** as `Meta.permissions` on `Organization`, `OrganizationMembership`, and `Subscription`.
3. Add `vinta_orgs.auth_backends.OrganizationModelBackend` to `AUTHENTICATION_BACKENDS`, **as the package ships it — no subclass**. (Corrected 2026-08-12: the original text read `organizations.auth_backends`, which was the *package's* path under `0.1.1`. The amendment sweep converted our own app's paths but missed this one package reference — the class is the package's, not a repo-owned module.) It unions global and per-organization permissions and keys the org half on `get_current_organization()` — which Phase 2b now guarantees is bound during a request. Under `0.3.0` it also filters `is_active` inside the membership lookup, so the repo-owned subclass Phase 3.5 used to add is not written in the first place. **Append it; keep `ModelBackend` first and do not remove it.** `django.contrib.auth.get_user` drops any session whose recorded `_auth_user_backend` path is *absent* from `AUTHENTICATION_BACKENDS`, so appending is always safe and it is **removing** a recorded path that signs every live session out. (Corrected 2026-08-13: the first draft of this amendment stated the opposite. The code has always appended, and `test_lists_the_stock_backend_first_and_the_package_backend_second` pins it.)
4. **Seed the groups through `ORGANIZATION_GROUP_SEEDERS`** so `vinta_orgs.testing` can replay the seed after a transactional flush. The data migration below stays the production path and keeps its own frozen literals; the seeder is the same catalog reachable as a callable. Register `vinta_orgs.testing` via `pytest_plugins` in the root `conftest.py`.
5. Data migration: seed the three global groups and their permission mappings.
6. Data migration: assign groups from existing state — `role == ADMIN` → `organization_admin`, `is_billing_owner` → `organization_billing_owner`, everything else → `organization_member`. Idempotent; `role` and `is_billing_owner` are read, not written. **The forward refuses when the seeded groups are absent; the reverse tolerates it.** Forward is about to assign memberships to groups, so a missing group means the seed did not run and silently assigning nothing would leave every membership ungrouped; the reverse only detaches, so nothing to detach is a completed no-op. Getting this asymmetry wrong is what took two unrelated migration tests down on CI and was misread as a load flake across four phases — see the correction section in `ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md`.
7. `@organizations/services.py`: membership and invitation creation assigns groups *in addition to* setting `role`, so both representations stay consistent until Phase 6 drops one.
8. `billing_recipients()` switches to the permission-shaped query from **Manager plumbing**. Use `OrganizationMembershipQuerySet.holding_permission(...)`, which `0.3.0` ships as the membership model's default queryset, rather than hand-writing the union of a membership's own grant with its groups'.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `organizations/tests/test_permission_backend.py` — an admin's membership resolves the four permissions under a bound organization and **none** under a different bound organization; the union with global permissions works; an unbound context yields no organization permissions. Scope this to **our** wiring — that our `AUTHENTICATION_BACKENDS` entry is the package's class, in the right order, against our concrete membership model. A test that would pass against a stock package install belongs upstream (the standard the prune chore sets).
- **Integration**: `organizations/tests/test_group_backfill_migration.py` — every combination of `role` × `is_billing_owner` maps to the right groups, and re-running changes nothing.
- **Integration**: `payments/tests/test_dunning_recipients.py` — `billing_recipients` returns the same set before and after the query change. Pin literal expected recipients rather than deriving the expectation from the same filter under test.
- **Unit**: the application manager does not override `create()` or `bulk_create()`, while named-organization creates, related-manager creates, and unbound bulk creates retain the package `0.3.0` behavior already covered through the application's concrete models.

**Suggested AI model**: Tier 3. Established Django patterns, but the backend's per-organization cache semantics and the backfill's edge cases need care.

**Reusable skills**: `add-migration`.

Acceptance: `user.has_perm("payments.manage_billing")` is `True` under a bound organization where the membership is a billing owner and `False` under any other, every membership has exactly one group matching its old role, and no permission class reads groups yet.

---

### Phase 3.5 — Make the authorization substrate correct before migrating onto it

**Goal**: `has_perm` becomes a *safe* thing for Phase 4 to read. A defect would otherwise be silently inherited by a mechanical swap: permission checks that run before the organization is resolved. No permission class changes what it *reads* in this phase — only when the check runs.

**Inserted 2026-08-13** (decimal id, so no existing phase is renumbered). The defect was found during Phase 2b and is recorded in `ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md`. The `check_permissions` ordering is **pre-existing** — not caused by this migration — but Phase 4 rewrites exactly the classes it affects, so shipping Phase 4 on top of it would bake it in.

**Amended 2026-08-13** (package `0.2.0` → `0.3.0`): this phase carried two defects. **The second is withdrawn** — `0.3.0` puts `is_active` on `AbstractOrganizationMembership` and filters it inside `OrganizationModelBackend._get_membership`, so the repo-owned subclass is not written and Phase 3 registers the package's backend directly. The first survives, but as *adoption* rather than implementation: `vinta_orgs.drf.OrganizationScopedAPIViewMixin` owns the seam and `resolve_membership_for_user` owns the table. The phase keeps its id and its acceptance criteria; it gets substantially smaller. Its findings are what drove the upstream fixes, so the branch is rewritten rather than abandoned — the record of *why* the package changed lives in its PR.

**Feature flag**: none.

Changes:

1. **Compose `vinta_orgs.drf.OrganizationScopedAPIViewMixin` into `TenantScopedViewMixin`.** Today `initial()` calls `super().initial()` (which runs authentication *and* `check_permissions`) and only then `_resolve_active_organization()`. So every permission class calling `get_active_organization_membership(user)` at `has_permission` time finds `_active_membership` unset and falls through to `user.memberships.filter(is_active=True).order_by("created").first()` — the user's **oldest** membership — while `get_queryset` answers from `X-Organization-Id`. A user who is an admin of an older organization A and a plain member of B can therefore pass a collection-level `IsOrganizationAdmin` gate while the queryset serves B. ~15 call sites across `@organizations/permissions.py`, `@calendar_integration/permissions.py`, `@public_api/permissions.py`, `@users/permissions.py` are affected. Phase 2b already closed this for `/public-api-tokens/` specifically (adding the mixin there would otherwise have *widened* access) — that local fix collapses into the general one. **Do not reimplement `initial()`**: that would push content negotiation and versioning behind authentication and turn a 406 into a 401 for a bad-`Accept` anonymous request. The package's mixin overrides `perform_authentication` for exactly this reason, and binds/releases in a `finally` around `dispatch`. Verify it is first in the MRO of every base viewset and every hand-rolled user.
2. **Delegate the resolution table to `memberships.resolve_for_user`**, keeping only what is ours: the `X-Organization-Id` header name, the integer-pk-to-slug lookup, and the existing refusal bodies. **Use `0.4.0`'s `UNRESOLVED_ORGANIZATION` instead of the unmatchable-slug sentinel this phase invented.** Our header carries an integer pk while the resolver matches by slug, so when the pk names no organization we had to return *something that could not match* — a 279-character string, longer than the `slug` column and rejected by `validate_organization_slug` — because returning `None` would have meant "no header supplied" and silently downgraded a 403 into a resolve-or-400. `0.4.0` adds `UNRESOLVED_ORGANIZATION` for exactly this: a resolver for a non-slug identifier can now say "identifier supplied but not found" without inventing a sentinel. Replace the constant, keep every row of the table and every mutation test that pins it — including the one proving that reverting the sentinel to `None` flips a 403 to a 200. **Delete `get_active_organization_membership`, its `_UNSET` sentinel, and the `user._active_membership` stash.** The package already writes the resolved row to `request.organization_membership`; request-aware callers must read that attribute. Off-request callers must invoke `resolve_membership_for_user` directly, passing a slug when they have an organization and accepting `AmbiguousOrganizationError` when they do not. Do not preserve the old fallback that silently selected the oldest active membership. The package raises `AmbiguousOrganizationError` where we raised a DRF `ValidationError`, and `OrganizationAccessDeniedError` where we raised `PermissionDenied`; both must still render 400 and 403 with the existing `{"detail": ...}` bodies. Map them explicitly rather than assuming DRF's default handler agrees.
3. **Preserve the 401 boundary.** Authentication must still run first, and an unauthenticated request must still get 401 rather than a 400 from organization resolution. Resolution failures (400 on ambiguity, 403 on non-member) must keep their current codes for authenticated callers. The whole resolution table from `@common/utils/view_utils.py` is the contract.
4. **Do not change what any permission class reads.** `membership.is_admin` / `is_billing_owner` stay. This phase moves *when* the check runs; Phase 4 moves *what is checked*.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `common/tests/test_permission_check_ordering.py` — for a user who is an admin of organization A and a plain member of B, a request naming B is refused by `IsOrganizationAdmin`; the same request naming A is admitted. Mutation-test it: restoring the old ordering must turn it red, or it proves nothing.
- **Integration**: `common/tests/test_resolution_table_after_reorder.py` — the full existing table still holds (0 / 1 / 2+ memberships × header present / absent / non-member × per-action opt-outs), and 401 still precedes 400/403 for an unauthenticated caller. **This test is ours and stays ours** even though the package now implements the table: it pins the *status codes and bodies our clients depend on*, across the exception translation, which is exactly the seam a package upgrade can silently move.
- **Static regression**: no production or test module imports or calls `get_active_organization_membership`, and no code writes `_active_membership`; request-path tests prove callers use the package-populated `request.organization_membership`, while an off-request caller with two active memberships no longer silently selects the oldest.
- **Integration**: a deactivated admin is refused. Now that the gate is the package's, test it through **our** stack — a real request against a real endpoint — rather than by unit-testing the backend. One test, not the six the repo-owned subclass needed.
- The existing suite passes. A test that changes status code expectations must say why in its diff.

**Suggested AI model**: Tier 4 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Reordering authentication, resolution, and authorization moves 400/403/401 boundaries across every tenant-facing endpoint; the failure mode is a widened grant that no test names. Kept at Tier 4 after the `0.3.0` amendment shrank the phase: adopting someone else's seam has the same blast radius as writing one, and the exception-translation step is new risk the original phase did not carry.

**Review models**: reviewer Tier 4 — every finding here is an authorization defect, and the reorder's blast radius is the whole API.

**Reusable skills**: none.

Acceptance: `get_active_organization_membership` and the user-level `_active_membership` stash are absent; every request-path caller sees the package-populated membership selected by `X-Organization-Id`; an ambiguous off-request caller is refused rather than assigned the oldest membership; a deactivated membership resolves no permissions; the full resolution table — every status code and body — is unchanged for every case that was already correct; and the suite is green.

---

### Phase 4 — Migrate the permission classes to `has_perm`

**Goal**: authorization decisions read permissions instead of `role` / `is_billing_owner`, with identical outcomes.

**Amended 2026-08-13** (package `0.2.0` → `0.3.0`): every check routes through `vinta_orgs.authorization.has_organization_permission` instead of a repo-owned helper. **Read the escalation analysis below before implementing** — it is the reason the package's defaults are what they are, and it is the part of this phase that a mechanical swap gets wrong.

**Feature flag**: none.

Changes:

0. **Use `vinta_orgs.authorization.has_organization_permission(user, permission, organization)` — never a bare `user.has_perm(...)`.** Two independent reasons, each sufficient. *The organization*: `has_perm` answers for the **bound** organization, and two families ask about a different one — the acting-reseller-root branch (an ancestor, which is the whole branch) and the ~dozen DRF views not on `TenantScopedViewMixin`, which bind nothing at all. `ServiceAccountViewSet` is one of those and carries `IsOrganizationAdmin`; under a bare `has_perm` it would refuse **every** caller. *The source of the grant*: `has_perm` unions the organization half with a global half (`user.user_permissions` plus the user's own `auth.Group` rows) and short-circuits for superusers — none of which is a statement about the organization named, and none of which could grant anything under the two flat columns being replaced. `0.3.0` defaults `include_global` and `allow_superuser` **off** for exactly this reason; leave them off. Concretely, leaving them on grants all four capabilities in **every** organization to (a) anyone given a permission directly in the Django user admin, and (b) anyone added to the seeded `organization_admin` group, which the user form's `filter_horizontal` picker lists. Both were inert under `role` and must stay inert. `has_perm` itself keeps stock `ModelBackend` semantics — the Django admin and every other consumer are unaffected.
1. `@organizations/permissions.py`: `IsOrganizationAdmin` reads `organizations.manage_members`. `IsBillingOwnerOrAdmin`'s direct check reads `payments.manage_billing`; **its acting-reseller-root branch keeps its bespoke subtree walk** — `resolve_billing_root` and `is_target_in_subtree` are our billing topology, not something the package can answer (see the revised **Four rules stay hand-written** Guiding Decision, which restates this rationale now that `has_organization_permission` takes the organization as an argument).
2. `OrganizationManagementPermission` keeps its membership-*absence* gate verbatim; nothing about it is permission-shaped.
3. Both branding gates keep their entitlement logic; only the role half becomes `organizations.manage_branding`. `user_administers_branding_eligible_organization` (the S3Direct `auth` callable) iterates permitted memberships instead of `role=ADMIN` ones.
4. `@users/models.py::is_organization_admin(organization)` wraps `has_organization_permission`, keeping its signature so `@calendar_integration/permissions.py` needs no change beyond what it inherits. **It must stay membership-bounded.** The body it replaces was structurally so; `has_perm` is not, and its global half and superuser short-circuit consult no membership at all, so it would answer `True` for organizations the caller does not belong to. Every current caller happens to be guarded, which means the failure would not surface as a test failure — the next unguarded caller would be a cross-tenant read.
5. Sweep the remaining classes across `@calendar_integration/permissions.py` (7 classes), `@public_api/permissions.py` (2), `@users/permissions.py` (1).
7. **Test fixtures**: `baker.make(OrganizationMembership, role=ADMIN)` produces no groups, so every test that builds an admin membership and then exercises a permission class needs groups assigned. **Decision (2026-08-13): a shared test helper, updated per module** — not a `post_save` signal. The signal would have covered every write path including baker, but it is a production behaviour change made to serve tests, and Phase 6 would have had to unpick it. The cost is that every test module creating an admin membership must be found and updated, and the failure mode is a test that silently asserts the wrong thing — so the sweep must be exhaustive and the helper must be the only sanctioned way to build a privileged membership in tests.
6. Delete the dead-with-reason `BrandingWriteGateReason.NO_SLUG` (and its `BRANDING_GATE_EXCEPTIONS` entry and the `if not organization.slug` check in `evaluate_branding_write_gate`) from `@organizations/permissions.py` — retired in Phase 1 (see the plan's Guiding Decisions "Slug precondition for branding writes is retired" row) once `organization_slug_not_blank` made it permanently unreachable through any supported write path. `OrganizationSlugRequiredForBrandingError` in `@organizations/exceptions.py` goes with it once nothing references it.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `organizations/tests/test_permissions_parity.py` — for each of the 15 permission classes, a matrix of (membership state × target object) yields the same allow/deny as before the change. This is the phase's contract. **It must include the escalation rows**, which are the ones a mechanical swap passes without: a plain member holding the permission via `user_permissions` is refused; a plain member added to the global `organization_admin` group is refused; a superuser with no admin membership is refused. That last one is *parity, not policy* — `role == ADMIN` refused a superuser holding no admin membership, so admitting one would be a widening, and `IsBillingOwnerOrAdmin` gates operations that charge a card at Stripe / MercadoPago rather than merely reading rows the Django admin already exposes. Keep at least two rows that pass under both the old and new implementations, as controls, so the matrix cannot be satisfied by a helper that simply refuses everyone.
- **Integration**: `payments/tests/test_reseller_root_billing.py` — an admin of a reseller parent may still manage a descendant's billing while the bound organization is the descendant. This is the case `has_perm` alone gets wrong, so it gets its own test.
- **Integration**: `organizations/tests/test_branding_gate_parity.py` — entitled-but-unpermitted and permitted-but-unentitled both still deny.

**Suggested AI model**: Tier 4. Fifteen classes, four of which do not fit the model being migrated to; the risk is silently widening a grant.

**Review models**: reviewer Tier 4 — every finding here is an authorization defect.

**Reusable skills**: none.

Acceptance: no permission class reads `role` or `is_billing_owner`, the parity matrix is green across all 15 classes, and the reseller-root case still passes.

---

### Phase 5 — Expose permissions on REST and GraphQL, drop `role` from the API

**Goal**: clients receive resolved permissions and assign groups; `role` leaves the contract.

**Amended 2026-08-13** (package `0.2.0` → `0.3.0`): the batch projection behind the `permissions` field is `vinta_orgs.authorization.resolve_membership_permissions`, not a repo-owned reimplementation.

**Feature flag**: none.

Changes:

0. **Resolve the published `permissions` list with `vinta_orgs.authorization.resolve_membership_permissions(memberships)`.** A membership list spans N users and the backend caches per `(user, organization)`, so asking it per row is N lookups; the package's batch form walks prefetched relations and is constant in N. **What it publishes must equal what the permission classes enforce** — same three exclusions as Phase 4 (no global half, no superuser short-circuit, nothing for an inactive membership or an inactive user). Publishing more than that tells a client it may do things every gate will refuse, which is a support ticket rather than a breach, but still wrong. Pin the two against each other across every membership shape; that test, not a comment, is what keeps them in step.
1. `@organizations/serializers.py`: `MyMembershipSerializer` and the `mine` serializer swap `role` for `permissions`; the member-list serializer drops `role` and `is_billing_owner`. `can_manage_branding` stays a distinct field for the reason given in **API Design**.
2. `@organizations/views.py`: `update-role` becomes `POST /organization-members/{user_id}/groups/`, preserving idempotency and the last-admin protection restated as "the last active member holding `organizations.manage_members`".
3. `@public_api/types.py`: delete `OrgRole`; the invitation input takes `groups`.
4. `@organizations/graphql.py` and `@public_api/queries.py` / `mutations.py`: update the affected fields.
5. Regenerate `@schema.yml`.
6. Produce the client handoff via `handoff-to-client`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `organizations/tests/test_membership_api_surface.py` — responses carry `permissions` and no `role`; the permission list matches what the backend resolves.
- **Integration**: `organizations/tests/test_group_assignment_endpoint.py` — assignment, idempotent re-assignment, rejection of the last-admin demotion, rejection of an unknown group name.
- **Integration**: `public_api/tests/test_invitation_groups.py` — the GraphQL invitation accepts `groups` and defaults to `organization_member`.

**Suggested AI model**: Tier 3. Multi-surface change with real validation rules, against established serializer and strawberry precedent.

**Reusable skills**: `create-rest-endpoint` for the groups endpoint and the schema regeneration; `create-graphql-public-query` for the invitation input; `handoff-to-client` for the migration document.

Acceptance: `grep -rn "role" schema.yml` returns no membership-role field, the group-assignment endpoint enforces last-admin protection, and the client handoff document exists.

---

### Phase 6 — Drop `role` / `is_billing_owner` and delete the old tenancy layer

**Goal**: one representation of authorization, and none of the bespoke tenancy code.

**Amended 2026-08-13** (package `0.2.0` → `0.3.0`): the seeded-group test repair is `vinta_orgs.testing`'s fixture, not a repo-owned one in `conftest.py`.

**Feature flag**: none.

Changes:

0. **Take the seeded-group repair from `vinta_orgs.testing`** rather than writing one. A `transaction=True` teardown runs `flush`, which re-emits `post_migrate` — rebuilding content types and permissions — but does **not** re-run data migrations, so Phase 3's three `auth_group` rows vanish for the rest of that worker's session and every membership built afterwards silently holds nothing. It must be **setup, not teardown**: the flush runs inside pytest-django's own finalizer, which is later than any conftest fixture's, so there is no teardown hook late enough to repair it. Two properties are worth carrying over from the repo-owned version: reseed from the live catalog via `ORGANIZATION_GROUP_SEEDERS` rather than from the migration's frozen literals, and keep the regression test in **one** test rather than splitting hazard from repair, since `--dist load` could otherwise put the halves on different workers and make the gate vacuous. **This is the defect that was misread as a parallel-load flake across four phases** — the correction section in `ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md` records why three separate diagnostics each confirmed the wrong answer. Blast radius with the repair disabled is 364 failures, not the 4 CI happened to surface.
1. Drop the `role` and `is_billing_owner` columns from `OrganizationMembership`; delete `OrganizationRole`.
2. Delete from `@common/fields.py`: `TenantSafeForeignKey`, `TenantSafeOneToOneField`, `SafeCompositePrimaryKey`, `_SafeCompositeAttribute`. `TenantSafeForeignKey` / `TenantSafeOneToOneField` have had **no users at all** since Phase 2b (see that phase's change 4) — this is a straight deletion, not a migration off them. Keep `OrganizationMembershipForeignKey` (see **Open Questions**); it extends `models.Field` directly and needs no reparenting onto them, only whatever the package's field classes offer.
3. Delete the compatibility shims left in `@organizations/services.py` that wrote both representations.
4. `grep -rn "OrganizationRole\|is_billing_owner\|OrganizationModel\b" --include="*.py"` across the repo returns nothing outside migrations.
5. Remove tests that exercised the dual-write period.
6. Delete the repo-owned `membership_holds_permission` implementation and import `vinta_orgs.authorization.membership_holds_permission` at every call site. The package implementation filters `is_active=True`; no application wrapper or re-export may weaken that contract.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- The full suite passes with no reference to the dropped fields.
- An inactive membership that still carries an administrator group is refused by both `ExternalEventChangeRequestQuerySet.resolvable_by` and `ExternalEventChangeRequestService.can_resolve`.

**Suggested AI model**: Tier 1. Mechanical deletion once the greps are clean.

**Reusable skills**: `add-migration` for the column drops.

Acceptance: the grep in change 4 returns nothing outside migrations, `membership_holds_permission` has one implementation in the installed package and refuses inactive memberships through both application call paths, the suite is green, and `makemigrations --check` reports nothing pending.

---

## 6. Risk & Rollout Notes

**No feature flag, by decision.** Justified in **Guiding Decisions**. The substitutes are Phase 0's behavior-neutral binding pass, `STRICT_ORGANIZATION_FILTER = True`, and the parity test matrices in Phases 3–5. There is no flag-removal phase because no flag is declared.

**The scoping flip is the top risk.** Phase 2a changes the default manager on 28 models at once. Its failure mode is a query that returns nothing where it used to return rows, which strict mode converts into an exception — loud, but only on a code path that actually executes. Phase 0's binding pass and its tripwire fixture exist because CI coverage of Celery tasks and management commands is thinner than of views.

**Query plans change.** `AUTO_DEFER_SAFE_JOINS` defaults to `True`, splitting `select_related` on safe relations into a second query — the package's own [benchmarks](https://github.com/vintasoftware/vinta-django-orgs/blob/main/benchmarks/RESULTS.md) explain why (PostgreSQL costs the key and organization conditions as independent when they are not). The `class_prepared` receiver also replaces each FK's single-column index with `(organization, pk)`. Both are improvements in the general case; neither is free on a specific hot query. Review the index migration per model rather than accepting the autodetector's output.

**Locks.** No table is renamed and no `db_table` pin is needed — the app label never changes, so every table stays at the name Django already derives for it. The remaining DDL is index add/drop per scoped model, the membership PK change, `ALTER COLUMN slug SET NOT NULL`, the `organization_slug_not_blank` CHECK, and two column drops. Pre-launch, so lock duration is not a production concern — but the migrations are written as if it were, because that posture is cheap now and expensive to retrofit.

**Alpha dependency.** `vinta-django-orgs` is `0.4.0`, Development Status `Alpha`, first published to PyPI on 2026-08-11. Pinned `>=0.4,<0.5`. **Four minor releases have each carried a breaking change** — `0.1.0` to `OrganizationMembership.objects` scoping, `0.2.0` renaming both app labels, `0.3.0` defaulting `STRICT_ORGANIZATION_FILTER` to `True`, and `0.4.0` deleting the entire function-based API in favour of class-specialized generics — so the minor pin is doing real work. Being the package's author is what makes this acceptable: a breaking upstream change is a decision, not a surprise.

**This plan has now been amended three times for exactly such a bump**, which is no longer just an argument for a tight pin — it is the dominant maintenance cost of the migration. Each bump has been cheaper than the last **only because the coupling is concentrated**: `common/organization_context.py` re-exports the package's binding API for ~260 call sites, so `0.4.0`'s deletion of that API is one module's rewrite rather than a 33-file sweep. Keep it that way. The rule that follows from three bumps: **wrap the package's surface in one project-owned module per concern, and let call sites import ours.** Where that discipline held, `0.4.0` cost a shim; where it did not — the three `accounts/` files importing `vinta_orgs.helpers` directly — it costs a per-file edit.

**Being the package's author cuts both ways.** `0.3.0` exists because this migration found the gaps, and adopting it is the right call — one implementation, tested upstream, reviewed once. The risk it introduces is that *our* review of this repo no longer covers the code that makes these decisions. The mitigations are concrete rather than aspirational: `include_global` / `allow_superuser` stay off and are **asserted** in the parity matrix rather than assumed from a default; the resolution table's status codes and bodies are pinned on our side of the exception translation; and the deactivated-admin case is tested through a real request rather than by unit-testing the package's backend. Each of those is a place where a future upstream change could widen a grant silently, and each now fails loudly here instead.

**Rollback.** Pre-launch posture: no per-phase reverse path is guaranteed. The practical unit of rollback is the phase branch. Phase 0 is cleanly revertible (no DDL at all); Phase 1 onward is not, because the PK change, the slug NOT NULL constraint, and the CHECK constraint discard information.

**Backfills.** Two, both idempotent and both small enough to run in one transaction pre-launch: slugs (Phase 1) and group assignment (Phase 3). The content-type backfill the original plan carried is withdrawn along with the app rename. Written batched and resumable anyway — see `add-one-off-script`'s contract for the shape — so they remain usable if the pre-launch assumption changes.

**Deploy ordering.** Single repo, no cross-repo producer. The one external ordering constraint is Phase 5: the web SPA and partner integrations break when `role` leaves the API, so the client handoff must land before that phase deploys.

## 7. Open Questions

| Question | Recommended default |
|---|---|
| **Do `payments` models become organization-scoped?** They carry a plain `models.OneToOneField("organizations.Organization")` today, not `OrganizationForeignKey`, and do not inherit `OrganizationModel` — so they are outside the 34. Under implicit scoping they would arguably belong inside. | **Leave them as-is for this migration.** Billing is read at the *billing root*, which is frequently an ancestor of the bound organization (`resolve_billing_root`), so implicit scoping to the bound organization would return nothing for exactly the pooled-reseller case billing exists to serve. Revisit as its own plan. |
| **Does `OrganizationMembershipForeignKey` survive the PK change?** With a surrogate PK restored, a real FK to `membership.id` becomes legal again, which would let the `ForeignObject` and its denormalized `<name>_user_id` column go away. | **Keep it.** The raw-SQL PROTECT FKs already bind to `uniq_membership_user_organization` and work unchanged; repointing `audit` and `calendar_integration` at a surrogate id means a new column, a backfill, and a constraint rebind on both, to buy nothing this plan needs. |
| **Does the package's `class_prepared` index receiver conflict with hand-authored indexes?** It skips a model that already declares an exact `(organization, pk)` index, but several models declare organization-leading indexes with a *different* second column. | **Review per model in Phase 2a.** Where the composite is genuinely redundant, drop the hand-authored one in the same migration; where it serves a different query shape, keep both and note why. |
| **Should `organization_member` carry any permission at all?** It is currently empty, existing only to distinguish "member with no capabilities" from "no membership". | **Leave it empty.** A permission granted to every member is indistinguishable from no check, and adding one later is additive. |

## 8. Touch List

**Phase 0**
- `@common/organization_context.py` (new)
- [payments/tasks.py](payments/tasks.py), [audit/tasks.py](audit/tasks.py), [calendar_integration/tasks/calendar_sync_tasks.py](calendar_integration/tasks/calendar_sync_tasks.py), [webhooks/tasks.py](webhooks/tasks.py)
- [payments/management/commands/reconcile_billing_period.py](payments/management/commands/reconcile_billing_period.py), [calendar_integration/management/commands/](calendar_integration/management/commands/) (4 commands)
- [organizations/admin.py](organizations/admin.py)
- `@common/tests/test_organization_context.py`, `@calendar_integration/tests/tasks/test_sync_task_scoping.py` (new)

**Phase 1**
- [pyproject.toml](pyproject.toml), [uv.lock](uv.lock) — `vinta-django-orgs>=0.2,<0.3`
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py) — install `vinta_orgs`, `ORGANIZATION_MODEL` / `ORGANIZATION_MEMBERSHIP_MODEL`, `SHARED_SCHEMA_ORGANIZATIONS`, admin double-registration fix
- [organizations/models.py](organizations/models.py), [organizations/managers.py](organizations/managers.py), [organizations/querysets.py](organizations/querysets.py), [organizations/admin.py](organizations/admin.py), [organizations/permissions.py](organizations/permissions.py), `@organizations/slug_generation.py` (new)
- `@organizations/migrations/00XX_unwind_composite_pk.py`, `@organizations/migrations/00XX_backfill_slugs.py`, `@organizations/migrations/00XX_slug_not_null_and_check.py` (new)
- `@common/org_retrievers.py` (new)
- `user.organization_memberships` call sites across `organizations`, `payments`, `calendar_integration`, `public_api`
- `@organizations/tests/test_app_identity.py`, `@organizations/tests/test_membership_pk.py`, `@organizations/tests/test_slug_backfill.py`, `@organizations/tests/test_membership_manager.py`, `@organizations/tests/test_admin_registrations.py`, `@common/tests/test_org_retrievers.py`, `@calendar_integration/tests/test_membership_protect_fk.py` (new)

**Not touched** (withdrawn with the app rename — see **Amendments**): no app directory move, no `di_core/apps.py` change, no import sweep across the repo, no migration-graph rewrite, no content-type migration, no `audit` namespace backfill, no migration-history management command.

**Phase 2a**
- [calendar_integration/models.py](calendar_integration/models.py) — 28 models, 59 relations
- `@calendar_integration/migrations/` — index migrations, one per model
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py) — `STRICT_ORGANIZATION_FILTER`
- `@calendar_integration/tests/test_implicit_scoping.py`, `@calendar_integration/tests/test_safe_relation_joins.py` (new); query-count assertions across the existing suite

**Phase 2b**
- [audit/models.py](audit/models.py), [webhooks/models.py](webhooks/models.py), [public_api/models.py](public_api/models.py), `organizations/models.py`
- [common/utils/view_utils.py](common/utils/view_utils.py), [public_api/middlewares.py](public_api/middlewares.py)
- `organizations/managers.py`, `organizations/querysets.py` — delete `OrganizationModel`, `BaseOrganizationModelManager`, `BaseOrganizationModelQuerySet`
- `@common/tests/test_tenant_scoped_binding.py`, `@public_api/tests/test_system_user_scoping.py`, `@audit/tests/test_audit_scoping.py` (new)

**Phase 3**
- `organizations/models.py` (`Meta.permissions`), [payments/models.py](payments/models.py) (`Meta.permissions`)
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py) — `AUTHENTICATION_BACKENDS`
- `@organizations/migrations/00XX_seed_permission_groups.py`, `@organizations/migrations/00XX_backfill_membership_groups.py` (new)
- `organizations/services.py`, `organizations/querysets.py` (`billing_recipients`)
- `@organizations/tests/test_permission_backend.py`, `@organizations/tests/test_group_backfill_migration.py`, `@payments/tests/test_dunning_recipients.py` (new)

**Phase 4**
- `organizations/permissions.py`, [calendar_integration/permissions.py](calendar_integration/permissions.py), [public_api/permissions.py](public_api/permissions.py), [users/permissions.py](users/permissions.py), [users/models.py](users/models.py)
- `@organizations/tests/test_permissions_parity.py`, `@payments/tests/test_reseller_root_billing.py`, `@organizations/tests/test_branding_gate_parity.py` (new)

**Phase 5**
- `organizations/serializers.py`, `organizations/views.py`, `organizations/routes.py`, `organizations/graphql.py`
- [public_api/types.py](public_api/types.py), [public_api/queries.py](public_api/queries.py), `public_api/mutations.py`
- [schema.yml](schema.yml) (regenerated)
- `@.vinta-ai-workflows/client-handoffs/2026-XX-XX-membership-permissions.md` (new)
- `@organizations/tests/test_membership_api_surface.py`, `@organizations/tests/test_group_assignment_endpoint.py`, `@public_api/tests/test_invitation_groups.py` (new)

**Phase 6**
- `@organizations/migrations/00XX_drop_role_and_billing_owner.py` (new)
- [common/fields.py](common/fields.py) — delete `TenantSafeForeignKey`, `TenantSafeOneToOneField`, `SafeCompositePrimaryKey`, `_SafeCompositeAttribute`
- `organizations/services.py` — delete dual-write shims
- Dual-write-period tests across `organizations/tests/`

## Amendments

- **2026-08-14** — Removed application implementations already shipped by `vinta-django-orgs` `0.3.0`: scoped-manager `create()` / `bulk_create()`, membership resolution and its user stash, and `membership_holds_permission`. Request-aware code now reads `request.organization_membership`; off-request resolution uses the package directly and no longer silently chooses the oldest active membership. Affected phases: 3, 3.5, 6. Branches force-pushed: `plan/vinta-django-orgs-migration/phase-3`, `plan/vinta-django-orgs-migration/phase-3.5`, `chore/prune-package-tests`, `plan/vinta-django-orgs-migration/phase-4`, `plan/vinta-django-orgs-migration/phase-5`, `plan/vinta-django-orgs-migration/phase-6`.

- **2026-08-12** — Retargeted from `vinta-django-orgs` `0.1.1` to `0.2.0`, and **withdrew the `organizations` → `tenancy` app rename entirely**.

  **Why.** `0.1.1` shipped its Django app under the label `organizations`, identical to ours. The original plan resolved that collision by renaming *our* app to `tenancy` and pinning `db_table` on every model — and that rename, not the package adoption, is what generated Phase 1b in its entirety: a `django_content_type` / `auth_permission` relabel migration, an `audit.subject_type` namespace backfill (our audit rows persist the app label as a string, so the rename silently split audit history in two), and a `rename_organizations_migration_history` management command for databases seeded before the branch. `0.2.0` renames the *package's* Python packages and app labels to `vinta_orgs` / `vinta_orgs_custom_data` for precisely this reason. With no collision, every one of those artifacts is unnecessary.

  **Verified before acting**, by diffing the two wheels module-by-module with the package name normalized: `mixins`, `fields`, `managers`, `querysets`, `state`, `models`, `conf`, `settings`, `permissions`, `middleware`, `organization_retrievers`, `auth_backends`, `serializers`, `admin`, and `utils` differ **only** in import paths and docstrings. `0.2.0` is a pure rename — no behavioral change to the abstract bases, the safe-relation fields, or the scoping managers. Its migrations are squashed to a single `0001_initial` per app, and the top-level setting names (`ORGANIZATION_MODEL`, `ORGANIZATION_MEMBERSHIP_MODEL`, `SHARED_SCHEMA_ORGANIZATIONS`) are unchanged.

  **Affected phases**: 1a (withdrawn), 1b (withdrawn except `common/org_retrievers.py`, which was never rename-dependent), 1c (retained in substance, renumbered) — collapsed into a single **Phase 1**. Phase 0 is untouched: it predates the rename and imports nothing from the package. Phases 2a–6 change only in that paths read `organizations/` rather than `tenancy/` and permission codenames read `organizations.*` rather than `tenancy.*`.

  **Branches**: `plan/vinta-django-orgs-migration/phase-1a`, `phase-1b`, and `phase-1c` are abandoned in place for audit and their PRs (#255, #256, #257) closed unmerged; a new `phase-1` branches off `phase-0`. Nothing had been merged to `main`, no branch had more than one author, and no PR carried a review — which is what made withdrawing cheaper than building forward on top of a rename we no longer wanted. In-flight Phase 2a work was preserved on `salvage/phase-2a-pre-amend` before any rewrite.

  **What must not come back.** The withdrawn artifacts are named explicitly in the **App-label collision — none** Guiding Decision and in Phase 1's collapse note, and `organizations/tests/test_app_identity.py` is the regression gate: it asserts our app label and every table name, so a future change that reintroduces a rename or a table move fails loudly rather than quietly re-earning Phase 1b.

- **2026-08-15** — Retargeted from `vinta-django-orgs` `0.3.0` to `0.4.0`, which **deletes the function-based API** in favour of class-specialized generics.

  **What changed upstream.** `vinta_orgs.helpers` is gone; organization and membership operations are now `OrganizationService[Organization]` / `MembershipService[Organization, Membership]` subclasses that bind the project's swapped models once, so django-stubs infers concrete return types instead of the abstract bases. The module-level context functions (`get_current_organization`, `set_current_organization`, `clear_current_organization`, `reset_current_organization`, `organization_context`) are replaced by an `OrganizationState[Organization]` subclass exposing `.get()` / `.set()` / `.clear()` / `.reset()` / `.context()`. Two more breaking changes: organization ownership is immutable on existing rows unless a call passes `unsafe_organization_update=True`, and the zero-argument model getters are typed against the abstract bases. `0.4.0` also fixes three real bugs we inherit — a nullable `OrganizationSafeForeignKey` set to `None` no longer clears the row's `organization_id` (which would have let the next save stamp the row into whichever organization happened to be bound), reverse managers derive scope from the source instance rather than requiring an ambient organization, and `validate_unique()` / `validate_constraints()` probe unscoped so model forms validate outside a request.

  **Verified before acting.** Three conditions could have dragged the bump earlier and taken the whole stack with it; each was checked and rejected. No migration in this repo writes `organization` on an existing row, so the new immutability rule needs no opt-in anywhere. `ORGANIZATION_GROUP_SEEDERS` already points at our own callable, not the deleted package helper. And Phase 2a's re-export of the function API keeps working on its own branch, because Phases 0–2b pin `>=0.2,<0.3` — the first place their code runs under `0.4.0` is Phase 3's full-suite run, which is where the bump lands.

  **Affected phases**: 3 (carries the bump; rewrites `common/organization_context.py` as a state subclass; declares the service subclasses), 3.5 (`UNRESOLVED_ORGANIZATION` replaces the unmatchable-slug sentinel it invented), 6 (final sweep), plus rebases of `chore/prune-package-tests`, 4 and 5. **Phases 0, 1, 2a and 2b are untouched** — including the 101-file Phase 2a.

  **Branches force-pushed**: `plan/vinta-django-orgs-migration/phase-3`, `phase-3.5`, `chore/prune-package-tests`, `phase-4`, `phase-5`, `phase-6`. Nothing merged to `main`, no branch protected, single author, no PR carrying a review — the same conditions that made the previous two amendments affordable.

  **The lesson recorded rather than re-learned.** Three bumps in, the cost of each is set by how concentrated the coupling is. `0.4.0` deleted the binding API that ~260 call sites use, and it cost one module's rewrite because those call sites import `common/organization_context.py` rather than `vinta_orgs.state`. The three `accounts/` files that imported `vinta_orgs.helpers` directly are the counter-example and the per-file cost. See the **Alpha dependency** note in **Risk & Rollout Notes**.

- **2026-08-13** — Retargeted from `vinta-django-orgs` `0.2.0` to `0.3.0`, and **withdrew the four substrate pieces this plan had been building by hand**.

  **Why.** `0.3.0` upstreams them, having been written in response to this migration's findings. The membership `is_active` gate moves onto `AbstractOrganizationMembership` and is filtered *inside* `OrganizationModelBackend._get_membership` — better than our subclass, which filtered the result and left the per-organization cache holding a row nothing was allowed to use. `vinta_orgs.authorization` provides `has_organization_permission` / `membership_holds_permission` / `resolve_membership_permissions` with `include_global` and `allow_superuser` defaulting off, which is exactly the narrowing Phase 4's review arrived at after finding two privilege escalations. `vinta_orgs.drf.OrganizationScopedAPIViewMixin` plus `resolve_membership_for_user` provide the seam and the table Phase 3.5 built. `vinta_orgs.testing` provides the reseed fixture for the transactional-flush defect. `0.3.0` also fixes the Phase 2a blocker at source: `create()` / `bulk_create()` no longer scope the queryset they are built from.

  **Verified before acting.** Three conditions could have forced the bump into Phase 1 and dragged the whole stack with it; each was checked and rejected. `is_active` predates this plan on our concrete model, and a concrete model may legally override a field inherited from an *abstract* base, so our declaration wins and the model state is unchanged. We use neither `IsOrganizationOwner` nor `DjangoOrganizationModelPermissions`, so their new "no organization selected → `False`" behaviour reaches nothing. The `create()` / `bulk_create()` change only removes raises. `STRICT_ORGANIZATION_FILTER` was already set to `True` explicitly in Phase 2a, so the headline breaking change is a no-op — and it stays spelled out rather than inherited, because this plan's safety argument rests on it and a default is a weaker guarantee than a declaration.

  **Affected phases**: 3 (carries the bump; registers the package backend unsubclassed; seeds through `ORGANIZATION_GROUP_SEEDERS`), 3.5 (Defect 2 withdrawn entirely; Defect 1 becomes adoption — the phase keeps its id and shrinks), the `chore/prune-package-tests` chore (more tests become package-owned), 4 (`has_organization_permission` replaces the repo-owned helper), 5 (`resolve_membership_permissions` replaces the repo-owned batch projection), 6 (`vinta_orgs.testing` replaces the repo-owned conftest fixture). **Phases 0, 1, 2a and 2b are untouched** — 4 of 10 branches never move, including the 101-file Phase 2a.

  **Branches force-pushed**: `plan/vinta-django-orgs-migration/phase-3`, `phase-3.5`, `chore/prune-package-tests`, `phase-4`, `phase-5`, `phase-6`. Nothing had been merged to `main`, no branch had more than one author, no branch was protected, and **no PR carried a review** — the same conditions that made the `0.2.0` withdrawal cheap. Amending in place was chosen over appending a Phase 7 so the hand-rolled substrate never reaches `main` at all.

  **What must not come back.** The withdrawn code is named in the **Package owns the authorization substrate** Guiding Decision, which also draws the line for future work: domain stays here, infrastructure goes upstream, and a test that would pass against a stock package install belongs upstream too. The escalation rows in `organizations/tests/test_permissions_parity.py` are the regression gate — they assert `include_global` / `allow_superuser` are off rather than trusting the package's defaults, so an upstream change of default fails here loudly.

- **2026-08-13** — Inserted **Phase 3.5**, "Make the authorization substrate correct before migrating onto it", between Phases 3 and 4. It carries two defects found during implementation: the `check_permissions`-before-resolver ordering (surfaced in Phase 2b, pre-existing, ~15 permission-class call sites) and the auth backend's missing `is_active` filter (surfaced in Phase 3, which pinned it as observed behaviour). Both would have been silently inherited by Phase 4's mechanical `has_perm` swap — the first lets an admin-elsewhere pass a gate for an organization they are only a member of, the second would grant a deactivated admin full rights. Given its own phase rather than folded into Phase 4 so that "when the check runs" and "what is checked" stay separately reviewable, each with its own parity matrix. Also recorded Phase 4's test-fixture decision (shared helper, not a `post_save` signal). Affected phases: 3.5 (new), 4 (fixture note). No branches force-pushed.
