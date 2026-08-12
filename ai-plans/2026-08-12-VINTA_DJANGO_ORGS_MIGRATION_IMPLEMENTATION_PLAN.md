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

| Decision | Resolution |
|---|---|
| **App-label collision — none** (amended, package `0.2.0`) | **There is no collision, so our app keeps the name `organizations` and nothing is renamed.** `0.1.1` labelled the package's own app `organizations`, identical to ours, and the original plan resolved that by renaming ours to `tenancy` with `db_table` pinned on every model. `0.2.0` renames the *package's* Python packages and app labels to `vinta_orgs` and `vinta_orgs_custom_data` for exactly this reason, so ours is unambiguous as it stands. Consequences of withdrawing the rename: no `git mv`, no `label = "tenancy"`, **no `db_table` pins at all** (the default `{app_label}_{model}` already resolves to each model's existing table name — verify with `makemigrations --check` rather than assuming, and pin only a model whose default would not match), no migration-graph rewrite across 79 migration files, no `django_content_type` / `auth_permission` relabel, no `audit.subject_type` namespace backfill, and no seeded-database migration-history command. Our imports read `from organizations...` exactly as they do today; the *package's* read `from vinta_orgs...`. The reason table renames were avoided in the first place — the **five** raw-SQL PROTECT FKs name tables as string literals — still holds and is now satisfied for free. (Corrected while verifying this amendment: the original text said the FKs lived in `calendar_integration` *and* `audit/migrations/0001_initial.py`. All five are in `calendar_integration` — `0026`, `0032`, `0036`, `0038`, `0040`; `audit` has no raw-SQL table literal at all.) |
| **Composite primary key** | `OrganizationMembership.pk = SafeCompositePrimaryKey("user", "organization")` is unwound back to a surrogate `id`, because Django cannot hang a `ManyToManyField` off a composite-PK model and the package's `groups` / `permissions` fields are exactly that. **The `uniq_membership_user_organization` unique constraint is kept**, which is what makes this cheap: the five raw-SQL composite FKs target that *constraint*, not the PK, so they need no rebind and no data migration. |
| **Scoping semantics flip** | Our `BaseOrganizationModelManager` requires an explicit `filter_by_organization(org_id)`; the package's `objects` scopes implicitly and returns `.none()` when nothing is bound. This is the single most dangerous delta in the migration — an unbound query in a Celery task reads as "no data" rather than as a bug. Mitigated two ways: a behavior-neutral audit phase binds every call site *before* any model flips, and `STRICT_ORGANIZATION_FILTER = True` from the moment the first one does, so the failure is an exception rather than an empty list. |
| **Organization resolution** | A custom retriever reads `X-Organization-Id` (integer PK) and is the only entry in `ORGANIZATION_RETRIEVERS`. `TenantScopedViewMixin` is retained rather than replaced by `OrganizationMiddleware`: its 400-on-ambiguity / 403-on-non-member rules are *membership*-aware, and the package's middleware runs before DRF authentication has populated `request.user`. The mixin gains one responsibility — binding the resolved organization to the context. |
| **Slug becomes NOT NULL** | `AbstractOrganization` declares `slug = CharField(max_length=255, unique=True)`, NOT NULL. We inherit it rather than overriding, and backfill `slugify(name)` with a numeric disambiguator on collision, falling back to `org-<pk>` when the derived value fails `@organizations/slug_validation.py`'s reserved-word or confusable-character rules. Note the consequence: **slug is public** (it appears in branded login URLs), so a derived slug discloses the organization name. Accepted here **only for the backfill** — the pre-existing rows the migration touches, on the grounds that there is no production data to disclose. |
| **Slug precondition for branding writes is retired** (review of the phase formerly numbered 1c) | `organizations.permissions.evaluate_branding_write_gate`'s third condition (`if not organization.slug: return NO_SLUG`) — "an eligible organization must also have picked a public slug before it may write branding" — is retired as a product rule. The NOT NULL slug above made it unsatisfiable for any organization reachable through a supported write path the moment this phase shipped; the Phase 1c review closed the one remaining loophole (an out-of-band `queryset.update(slug="")` past `save()`, which nothing prevented at the database level) with `models.CheckConstraint(condition=~models.Q(slug=""), name="organization_slug_not_blank")` on `Organization.Meta.constraints`, making the retirement permanent rather than merely untested. The `BrandingWriteGateReason.NO_SLUG` enum member, its `BRANDING_GATE_EXCEPTIONS` entry, and the `if not organization.slug` check are kept dead-with-reason rather than deleted immediately — they are still part of the gate's public contract — and are scheduled for removal in **Phase 4** (see that phase's Changes list). |
| **Runtime slug default is opaque, not name-derived** (review of the phase formerly numbered 1c) | The row above's disclosure trade-off was accepted **only for the Phase 1 slug backfill** of pre-existing rows (no production data to disclose at that moment) — it was never sanctioned as `Organization.save()`'s permanent runtime default, which would make name disclosure permanent for every organization saved without an explicit slug from this deploy on. `Organization.save()`'s fallback (used when a caller left `slug` blank) now derives the opaque `org-<token>` form (`organizations.slug_generation.derive_organization_slug(..., disclose_name=False)`), not `slugify(name)`. Name-derivation remains sanctioned for exactly one runtime write path — `OrganizationService.create_organization`, the self-serve "create my own organization" flow, where the human caller explicitly chose `name` for their own, about-to-be-public organization — which now computes and passes an explicit, name-derived `slug` itself rather than relying on the model's fallback. |
| **No feature flag** | This repo has no feature-flag module, and the change is not flag-shaped: a default-manager swap and a change of base class cannot be gated per-request or per-tenant, because they are resolved at class-definition and migration time. Combined with pre-launch status and no production tenants, the flag would cost a PR and gate nothing. **The safety mechanism is the audit phase plus `STRICT_ORGANIZATION_FILTER`, not a flag.** There is consequently no flag-removal phase. |
| **Group scope** | Three global `auth.Group` rows — `organization_admin`, `organization_billing_owner`, `organization_member` — shared by every organization, seeded by a data migration. Every authorization check reads `user.has_perm("app.codename")`, **never** `membership.groups.filter(name=...)`, so introducing per-org groups later changes the seeding and nothing above the auth backend. This deliberately diverges from the package's own `IsOrganizationOwner`, which filters on `groups__name='organization_owner'` — we do not use that class. |
| **Permission catalog shape** | Custom `Meta.permissions` named for *capabilities* (`manage_billing`, `manage_members`, `manage_branding`), not for the model-CRUD triples `auth.Permission` defaults to. Our authorization questions are behavioral ("may this member change the plan"), and mapping them onto `change_subscription` would misrepresent them. |
| **Four rules stay hand-written** | `IsBillingOwnerOrAdmin`'s acting-reseller-root walk grants over organization B from a membership in A — the auth backend keys on the *current* organization (B) and cannot see a grant held in A. `OrganizationManagementPermission` gates on membership *absence*. Both branding gates are entitlement-driven, not role-driven. These four keep bespoke logic; what changes is the sub-check they compose with (`membership.is_admin` becomes `has_perm`). |
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
- `get_active_organization_membership()` in `@organizations/models.py` keeps its signature and its `_UNSET` sentinel contract. Its fallback branch (`user.organization_memberships.filter(is_active=True)`) becomes `user.memberships.filter(is_active=True)`.
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
4. Delete `OrganizationModel`, `BaseOrganizationModelManager`, `BaseOrganizationModelQuerySet` from `organizations/`. `@common/fields.py`'s `TenantSafeForeignKey` and friends stay until Phase 6 — `OrganizationMembershipForeignKey` still builds on them.

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

**Feature flag**: none.

Changes:

1. Declare the four custom permissions from the **Permission catalog** as `Meta.permissions` on `Organization`, `OrganizationMembership`, and `Subscription`.
2. Add `organizations.auth_backends.OrganizationModelBackend` to `AUTHENTICATION_BACKENDS`. It unions global and per-organization permissions and keys the org half on `get_current_organization()` — which Phase 2b now guarantees is bound during a request.
3. Data migration: seed the three global groups and their permission mappings.
4. Data migration: assign groups from existing state — `role == ADMIN` → `organization_admin`, `is_billing_owner` → `organization_billing_owner`, everything else → `organization_member`. Idempotent; `role` and `is_billing_owner` are read, not written.
5. `@organizations/services.py`: membership and invitation creation assigns groups *in addition to* setting `role`, so both representations stay consistent until Phase 6 drops one.
6. `billing_recipients()` switches to the permission-shaped query from **Manager plumbing**.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `organizations/tests/test_permission_backend.py` — an admin's membership resolves the four permissions under a bound organization and **none** under a different bound organization; the union with global permissions works; an unbound context yields no organization permissions.
- **Integration**: `organizations/tests/test_group_backfill_migration.py` — every combination of `role` × `is_billing_owner` maps to the right groups, and re-running changes nothing.
- **Integration**: `payments/tests/test_dunning_recipients.py` — `billing_recipients` returns the same set before and after the query change. Pin literal expected recipients rather than deriving the expectation from the same filter under test.

**Suggested AI model**: Tier 3. Established Django patterns, but the backend's per-organization cache semantics and the backfill's edge cases need care.

**Reusable skills**: `add-migration`.

Acceptance: `user.has_perm("payments.manage_billing")` is `True` under a bound organization where the membership is a billing owner and `False` under any other, every membership has exactly one group matching its old role, and no permission class reads groups yet.

---

### Phase 4 — Migrate the permission classes to `has_perm`

**Goal**: authorization decisions read permissions instead of `role` / `is_billing_owner`, with identical outcomes.

**Feature flag**: none.

Changes:

1. `@organizations/permissions.py`: `IsOrganizationAdmin` reads `organizations.manage_members`. `IsBillingOwnerOrAdmin`'s direct check reads `payments.manage_billing`; **its acting-reseller-root branch keeps its bespoke subtree walk** — the backend keys on the current organization and cannot see a grant held in an ancestor, so `is_target_in_subtree` and the `can_invite_organizations` check stay, with only the role sub-check swapped.
2. `OrganizationManagementPermission` keeps its membership-*absence* gate verbatim; nothing about it is permission-shaped.
3. Both branding gates keep their entitlement logic; only the role half becomes `organizations.manage_branding`. `user_administers_branding_eligible_organization` (the S3Direct `auth` callable) iterates permitted memberships instead of `role=ADMIN` ones.
4. `@users/models.py::is_organization_admin(organization)` becomes a `has_perm` wrapper, keeping its signature so `@calendar_integration/permissions.py` needs no change beyond what it inherits.
5. Sweep the remaining classes across `@calendar_integration/permissions.py` (7 classes), `@public_api/permissions.py` (2), `@users/permissions.py` (1).
6. Delete the dead-with-reason `BrandingWriteGateReason.NO_SLUG` (and its `BRANDING_GATE_EXCEPTIONS` entry and the `if not organization.slug` check in `evaluate_branding_write_gate`) from `@organizations/permissions.py` — retired in Phase 1 (see the plan's Guiding Decisions "Slug precondition for branding writes is retired" row) once `organization_slug_not_blank` made it permanently unreachable through any supported write path. `OrganizationSlugRequiredForBrandingError` in `@organizations/exceptions.py` goes with it once nothing references it.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `organizations/tests/test_permissions_parity.py` — for each of the 15 permission classes, a matrix of (membership state × target object) yields the same allow/deny as before the change. This is the phase's contract.
- **Integration**: `payments/tests/test_reseller_root_billing.py` — an admin of a reseller parent may still manage a descendant's billing while the bound organization is the descendant. This is the case `has_perm` alone gets wrong, so it gets its own test.
- **Integration**: `organizations/tests/test_branding_gate_parity.py` — entitled-but-unpermitted and permitted-but-unentitled both still deny.

**Suggested AI model**: Tier 4. Fifteen classes, four of which do not fit the model being migrated to; the risk is silently widening a grant.

**Review models**: reviewer Tier 4 — every finding here is an authorization defect.

**Reusable skills**: none.

Acceptance: no permission class reads `role` or `is_billing_owner`, the parity matrix is green across all 15 classes, and the reseller-root case still passes.

---

### Phase 5 — Expose permissions on REST and GraphQL, drop `role` from the API

**Goal**: clients receive resolved permissions and assign groups; `role` leaves the contract.

**Feature flag**: none.

Changes:

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

**Feature flag**: none.

Changes:

1. Drop the `role` and `is_billing_owner` columns from `OrganizationMembership`; delete `OrganizationRole`.
2. Delete from `@common/fields.py`: `TenantSafeForeignKey`, `TenantSafeOneToOneField`, `SafeCompositePrimaryKey`, `_SafeCompositeAttribute`. Keep `OrganizationMembershipForeignKey` (see **Open Questions**), reparented onto the package's field classes.
3. Delete the compatibility shims left in `@organizations/services.py` that wrote both representations.
4. `grep -rn "OrganizationRole\|is_billing_owner\|OrganizationModel\b" --include="*.py"` across the repo returns nothing outside migrations.
5. Remove tests that exercised the dual-write period.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- The full suite passes with no reference to the dropped fields.

**Suggested AI model**: Tier 1. Mechanical deletion once the greps are clean.

**Reusable skills**: `add-migration` for the column drops.

Acceptance: the grep in change 4 returns nothing outside migrations, the suite is green, and `makemigrations --check` reports nothing pending.

---

## 6. Risk & Rollout Notes

**No feature flag, by decision.** Justified in **Guiding Decisions**. The substitutes are Phase 0's behavior-neutral binding pass, `STRICT_ORGANIZATION_FILTER = True`, and the parity test matrices in Phases 3–5. There is no flag-removal phase because no flag is declared.

**The scoping flip is the top risk.** Phase 2a changes the default manager on 28 models at once. Its failure mode is a query that returns nothing where it used to return rows, which strict mode converts into an exception — loud, but only on a code path that actually executes. Phase 0's binding pass and its tripwire fixture exist because CI coverage of Celery tasks and management commands is thinner than of views.

**Query plans change.** `AUTO_DEFER_SAFE_JOINS` defaults to `True`, splitting `select_related` on safe relations into a second query — the package's own [benchmarks](https://github.com/vintasoftware/vinta-django-orgs/blob/main/benchmarks/RESULTS.md) explain why (PostgreSQL costs the key and organization conditions as independent when they are not). The `class_prepared` receiver also replaces each FK's single-column index with `(organization, pk)`. Both are improvements in the general case; neither is free on a specific hot query. Review the index migration per model rather than accepting the autodetector's output.

**Locks.** No table is renamed and no `db_table` pin is needed — the app label never changes, so every table stays at the name Django already derives for it. The remaining DDL is index add/drop per scoped model, the membership PK change, `ALTER COLUMN slug SET NOT NULL`, the `organization_slug_not_blank` CHECK, and two column drops. Pre-launch, so lock duration is not a production concern — but the migrations are written as if it were, because that posture is cheap now and expensive to retrofit.

**Alpha dependency.** `vinta-django-orgs` is `0.2.0`, Development Status `Alpha`, first published to PyPI on 2026-08-11. Pinned `>=0.2,<0.3`. Two minor releases have each carried a breaking change already — `0.1.0` to `OrganizationMembership.objects` scoping, and `0.2.0` renaming both app labels — so the minor pin is doing real work. Being the package's author is what makes this acceptable: a breaking upstream change is a decision, not a surprise. **This plan was itself amended once for exactly such a bump** (see **Amendments**), which is the empirical case for keeping the pin tight and re-reading the diff on every bump rather than trusting semver alone.

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

- **2026-08-12** — Retargeted from `vinta-django-orgs` `0.1.1` to `0.2.0`, and **withdrew the `organizations` → `tenancy` app rename entirely**.

  **Why.** `0.1.1` shipped its Django app under the label `organizations`, identical to ours. The original plan resolved that collision by renaming *our* app to `tenancy` and pinning `db_table` on every model — and that rename, not the package adoption, is what generated Phase 1b in its entirety: a `django_content_type` / `auth_permission` relabel migration, an `audit.subject_type` namespace backfill (our audit rows persist the app label as a string, so the rename silently split audit history in two), and a `rename_organizations_migration_history` management command for databases seeded before the branch. `0.2.0` renames the *package's* Python packages and app labels to `vinta_orgs` / `vinta_orgs_custom_data` for precisely this reason. With no collision, every one of those artifacts is unnecessary.

  **Verified before acting**, by diffing the two wheels module-by-module with the package name normalized: `mixins`, `fields`, `managers`, `querysets`, `state`, `models`, `conf`, `settings`, `permissions`, `middleware`, `organization_retrievers`, `auth_backends`, `serializers`, `admin`, and `utils` differ **only** in import paths and docstrings. `0.2.0` is a pure rename — no behavioral change to the abstract bases, the safe-relation fields, or the scoping managers. Its migrations are squashed to a single `0001_initial` per app, and the top-level setting names (`ORGANIZATION_MODEL`, `ORGANIZATION_MEMBERSHIP_MODEL`, `SHARED_SCHEMA_ORGANIZATIONS`) are unchanged.

  **Affected phases**: 1a (withdrawn), 1b (withdrawn except `common/org_retrievers.py`, which was never rename-dependent), 1c (retained in substance, renumbered) — collapsed into a single **Phase 1**. Phase 0 is untouched: it predates the rename and imports nothing from the package. Phases 2a–6 change only in that paths read `organizations/` rather than `tenancy/` and permission codenames read `organizations.*` rather than `tenancy.*`.

  **Branches**: `plan/vinta-django-orgs-migration/phase-1a`, `phase-1b`, and `phase-1c` are abandoned in place for audit and their PRs (#255, #256, #257) closed unmerged; a new `phase-1` branches off `phase-0`. Nothing had been merged to `main`, no branch had more than one author, and no PR carried a review — which is what made withdrawing cheaper than building forward on top of a rename we no longer wanted. In-flight Phase 2a work was preserved on `salvage/phase-2a-pre-amend` before any rewrite.

  **What must not come back.** The withdrawn artifacts are named explicitly in the **App-label collision — none** Guiding Decision and in Phase 1's collapse note, and `organizations/tests/test_app_identity.py` is the regression gate: it asserts our app label and every table name, so a future change that reintroduces a rename or a table move fails loudly rather than quietly re-earning Phase 1b.
