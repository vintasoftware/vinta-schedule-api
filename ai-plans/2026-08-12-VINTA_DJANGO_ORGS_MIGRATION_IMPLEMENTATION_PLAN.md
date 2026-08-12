# Migrate `organizations` onto vinta-django-orgs — Implementation Plan

Migrates this repo's bespoke multi-tenancy layer onto [vinta-django-orgs](https://github.com/vintasoftware/vinta-django-orgs) `0.1.1`, and replaces the flat `role` / `is_billing_owner` membership attributes with the package's `auth.Group` / `auth.Permission` model.

No SPEC sibling exists — this is a migration of existing behavior rather than a new feature, so the contract is "what the system does today, on a different foundation". Every behavioral change this plan *does* make is named in **Guiding Decisions** and nowhere else.

## 1. Goals

1. **Retire the bespoke tenancy layer.** `@common/fields.py`'s `TenantSafeForeignKey` / `OrganizationForeignKey` / `OrganizationOneToOneField` and `organizations.OrganizationModel` are replaced by the package's `OrganizationSafeForeignKey` / `OrganizationSafeOneToOneField` / `SingleOrganizationModelMixin` across all 34 scoped models and 65 declared relations.
2. **Adopt implicit, context-bound organization scoping.** The default manager on every scoped model scopes to the organization bound to the current `contextvars` context, with `STRICT_ORGANIZATION_FILTER = True` so an unbound query raises rather than silently returning nothing.
3. **Replace `role` / `is_billing_owner` with groups and permissions.** Three global `auth.Group` rows plus custom `Meta.permissions` back every authorization decision that today reads `membership.is_admin` or `membership.is_billing_owner`. Every check reads `has_perm`, never a group name.
4. **Keep the `X-Organization-Id` request contract intact.** A custom retriever plugged into `ORGANIZATION_RETRIEVERS` resolves the organization from the existing header; `TenantScopedViewMixin` keeps its membership-aware 400/403 resolution rules and binds the context.
5. **Expose resolved permissions on the API, drop `role`.** REST and public GraphQL surfaces report what the caller *may do*, not what they *are called*.

**Non-goals:**

- **`organizations_custom_data`.** The package's second app (per-organization dynamic tables, org-specific field definitions, per-table permission rows) is not installed, not migrated, and not referenced.
- **`OrganizationSite` and domain-based tenancy.** We do not do subdomain tenancy. `retrieve_by_domain` stays out of `ORGANIZATION_RETRIEVERS` and `CACHE_ORGANIZATION_RETRIEVAL` is irrelevant. The `OrganizationSite` model is *not* swappable, so installing the package app creates its table regardless — it stays empty and unread.
- **Per-organization custom roles.** Global groups only. Checks read permissions rather than group names specifically so a per-org layer can be added later without touching call sites, but no per-org group rows, name mangling, or tenant-facing role-management surface ships here.
- **Session-based and `Organization-Slug` header resolution.** `retrieve_by_session` and `retrieve_by_http_header` stay out of the retriever list.
- **Re-litigating which models are tenant-scoped.** The 34 models that are scoped today are the 34 that are scoped after. `payments` is the one genuine ambiguity — see **Open Questions**.
- **Reversibility guarantees per phase.** Pre-launch, no production tenants (see **Guiding Decisions**). Migrations are forward-only where a reverse would cost real work.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **App-label collision** | The package's app is literally labelled `organizations`, and so is ours. Ours is renamed to `tenancy`; the package keeps `organizations`. Every renamed model pins `db_table` to its existing `organizations_*` name, so the app label, migration graph, and `django_content_type` rows move while **no table does**. Chosen over renaming tables because the raw-SQL PROTECT FKs in `@calendar_integration/migrations/0026_calendarownership_membership_protect_fk.py` and `@audit/migrations/0001_initial.py` name tables as strings, and an `ALTER TABLE ... RENAME` takes `ACCESS EXCLUSIVE`. |
| **Composite primary key** | `OrganizationMembership.pk = SafeCompositePrimaryKey("user", "organization")` is unwound back to a surrogate `id`, because Django cannot hang a `ManyToManyField` off a composite-PK model and the package's `groups` / `permissions` fields are exactly that. **The `uniq_membership_user_organization` unique constraint is kept**, which is what makes this cheap: the three raw-SQL composite FKs target that *constraint*, not the PK, so they need no rebind and no data migration. |
| **Scoping semantics flip** | Our `BaseOrganizationModelManager` requires an explicit `filter_by_organization(org_id)`; the package's `objects` scopes implicitly and returns `.none()` when nothing is bound. This is the single most dangerous delta in the migration — an unbound query in a Celery task reads as "no data" rather than as a bug. Mitigated two ways: a behavior-neutral audit phase binds every call site *before* any model flips, and `STRICT_ORGANIZATION_FILTER = True` from the moment the first one does, so the failure is an exception rather than an empty list. |
| **Organization resolution** | A custom retriever reads `X-Organization-Id` (integer PK) and is the only entry in `ORGANIZATION_RETRIEVERS`. `TenantScopedViewMixin` is retained rather than replaced by `OrganizationMiddleware`: its 400-on-ambiguity / 403-on-non-member rules are *membership*-aware, and the package's middleware runs before DRF authentication has populated `request.user`. The mixin gains one responsibility — binding the resolved organization to the context. |
| **Slug becomes NOT NULL** | `AbstractOrganization` declares `slug = CharField(max_length=255, unique=True)`, NOT NULL. We inherit it rather than overriding, and backfill `slugify(name)` with a numeric disambiguator on collision, falling back to `org-<pk>` when the derived value fails `@organizations/slug_validation.py`'s reserved-word or confusable-character rules. Note the consequence: **slug is public** (it appears in branded login URLs), so a derived slug discloses the organization name. Accepted here because there is no production data to disclose. |
| **No feature flag** | This repo has no feature-flag module, and the change is not flag-shaped: a default-manager swap and an app rename cannot be gated per-request or per-tenant, because they are resolved at class-definition and migration time. Combined with pre-launch status and no production tenants, the flag would cost a PR and gate nothing. **The safety mechanism is the audit phase plus `STRICT_ORGANIZATION_FILTER`, not a flag.** There is consequently no flag-removal phase. |
| **Group scope** | Three global `auth.Group` rows — `organization_admin`, `organization_billing_owner`, `organization_member` — shared by every organization, seeded by a data migration. Every authorization check reads `user.has_perm("app.codename")`, **never** `membership.groups.filter(name=...)`, so introducing per-org groups later changes the seeding and nothing above the auth backend. This deliberately diverges from the package's own `IsOrganizationOwner`, which filters on `groups__name='organization_owner'` — we do not use that class. |
| **Permission catalog shape** | Custom `Meta.permissions` named for *capabilities* (`manage_billing`, `manage_members`, `manage_branding`), not for the model-CRUD triples `auth.Permission` defaults to. Our authorization questions are behavioral ("may this member change the plan"), and mapping them onto `change_subscription` would misrepresent them. |
| **Four rules stay hand-written** | `IsBillingOwnerOrAdmin`'s acting-reseller-root walk grants over organization B from a membership in A — the auth backend keys on the *current* organization (B) and cannot see a grant held in A. `OrganizationManagementPermission` gates on membership *absence*. Both branding gates are entitlement-driven, not role-driven. These four keep bespoke logic; what changes is the sub-check they compose with (`membership.is_admin` becomes `has_perm`). |
| **Pre-launch posture** | No production tenants. Data migrations are written to be idempotent because that costs nothing, but phases are not required to carry a tested reverse path, and the API break ships without a deprecation window. |
| **`meta` and timestamp indexes** | `BaseModel` gives `Organization` / `OrganizationMembership` a `meta` JSONField and `db_index=True` on `created` / `modified`; `AbstractOrganization` extends `TimeStampedModel`, which gives neither. `meta` is verifiably unused on both models (only `payments` reads it), so it is dropped. The two timestamp indexes are dropped with it — neither model is queried by timestamp range. |

## 3. Data Model Changes

### 3.1 `tenancy.Organization` (renamed from `organizations.Organization`)

```python
# tenancy/models.py
from organizations.models import AbstractOrganization


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
        db_table = "organizations_organization"   # pinned — no table rename
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="uniq_org_name_per_parent"),
        ]
```

Dropped: `BaseModel`'s `meta` JSONField, the `created` / `modified` indexes. Retained unchanged: `is_reseller()`, `get_branding_root()`, `resolve_branding()`, `resolve_branding_for_display()`.

`slug` is no longer nullable. `@organizations/slug_validation.py` moves to `tenancy/slug_validation.py` unchanged — the format, reserved-word, and confusable-character rules still apply at every write surface.

### 3.2 `tenancy.OrganizationMembership`

```python
# tenancy/models.py
from organizations.models import AbstractOrganizationMembership


class OrganizationMembership(AbstractOrganizationMembership):
    # Inherited: organization, user, groups (M2M auth.Group),
    # permissions (M2M auth.Permission), created, modified.
    is_active = models.BooleanField(default=True, db_default=True, db_index=True, ...)

    objects = OrganizationMembershipManager()   # see 3.3

    class Meta(AbstractOrganizationMembership.Meta):
        db_table = "organizations_organizationmembership"   # pinned
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"],
                                    name="uniq_membership_user_organization"),
        ]
```

Three structural changes, in dependency order:

1. **`pk` reverts to a surrogate `id`.** `SafeCompositePrimaryKey` is removed from the model; `@common/fields.py`'s `SafeCompositePrimaryKey` and `_SafeCompositeAttribute` are deleted in the final phase once nothing imports them.
2. **`uniq_membership_user_organization` is kept.** This is load-bearing. `@calendar_integration/migrations/0026_calendarownership_membership_protect_fk.py` and the `audit` equivalent declare `FOREIGN KEY (membership_user_id, organization_id) REFERENCES organization_membership(user_id, organization_id)` — a composite FK may target any unique constraint, so those FKs survive the PK change untouched.
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
| `tenancy.manage_members` | `OrganizationMembership` | `membership.is_admin` in `IsOrganizationAdmin`, `CalendarGroupPermission`, `User.is_organization_admin` |
| `tenancy.manage_organization` | `Organization` | `membership.is_admin` on organization-update paths |
| `tenancy.manage_branding` | `Organization` | the role half of the branding gates (the entitlement half is unchanged) |
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
+   "permissions": ["tenancy.manage_members", "tenancy.manage_organization",
+                   "tenancy.manage_branding", "payments.manage_billing"],
+   "can_manage_branding": true }
```

`can_manage_branding` stays a distinct field rather than folding into `permissions`: it is the *composite* of `tenancy.manage_branding` and the `white_label_branding` entitlement plus the parentless check, and collapsing it into the permission list would misreport an entitled-but-unpermitted caller.

`GET /organizations/mine/` gains the same `permissions` key per row and drops `role`. `GET /organization-members/` drops `role` and `is_billing_owner`.

### 4.2 Group assignment replaces role update

```
- POST /organization-members/{user_id}/update-role/   { "role": "admin" }
+ POST /organization-members/{user_id}/groups/        { "groups": ["organization_admin"] }
```

Write-side only, and the one place a group name is accepted — assigning a group is the act of choosing one, so there is nothing to abstract. Errors preserved from the old endpoint: setting the current value is an idempotent success, and demoting the last active member holding `tenancy.manage_members` in the organization is rejected (the "protect the last active admin" rule, restated in permission terms).

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

### Phase 1a — Rename our app to `tenancy`, keep the package uninstalled

**Goal**: our app answers to `tenancy`, with every table still at its old name and every model still behaving exactly as before. Nothing in the repo imports `vinta-django-orgs` yet, so its Django app is *not* installed this phase — that install is deferred to Phase 1c, which is the first phase that actually needs the package's abstract bases (see that phase's Changes list). Installing the app here bought nothing and forced the settings-level `SHARED_SCHEMA_ORGANIZATIONS` / swappable-model / admin-double-registration work to land as a Phase 1a deviation; it is corrected here so the phase matches its stated goal.

**Feature flag**: none.

Changes:

1. `@pyproject.toml`: add `vinta-django-orgs>=0.1.1,<0.2`. Pin the minor — the package is `0.1.1` and Development Status `Alpha`, so a minor bump may move the abstract bases. Declaring the dependency (and syncing `uv.lock`) is harmless on its own and Phase 1c needs it; only *installing its Django app* is deferred.
2. `@vinta_schedule_api/settings/base.py`: rename `organizations` to `tenancy` in `INTERNAL_INSTALLED_APPS`. The package's app is not added to `INSTALLED_APPS` this phase.
3. `git mv organizations/ tenancy/`, and add `label = "tenancy"` to its `AppConfig`.
4. Pin `db_table` on every model in `tenancy/models.py` to its current `organizations_*` name.
5. Mechanical sweep: 367 `from organizations...` imports and 223 non-migration `"organizations.X"` string references become `tenancy`. The 72 in-migration references are Phase 1b's problem, not this one.
6. `@di_core/apps.py` wires `container.wire(packages=INTERNAL_INSTALLED_APPS)` — the renamed package must still be wired.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `tenancy/tests/test_app_label.py` — `Organization._meta.app_label == "tenancy"`, `Organization._meta.db_table == "organizations_organization"`, and `makemigrations --check --dry-run` reports no pending model changes beyond the label move.
- The entire existing suite passes unchanged. That is the acceptance signal for a rename.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). High file count but each edit is a mechanical substitution with an exact precedent; the judgement is concentrated in the settings block.

**Reusable skills**: `add-env-var` is not needed (no new env vars). None otherwise.

Acceptance: `grep -rn "from organizations" --include="*.py" tenancy/ calendar_integration/ payments/ audit/ webhooks/ public_api/ users/ accounts/ common/` returns no hits (the package's app is not installed yet, so there is nothing to import it from), the suite is green, and no table was renamed.

---

### Phase 1b — Content types, the audit namespace split, the retriever, and the seeded-database migration path

**Goal**: Django's `django_content_type` / `auth_permission` state agrees our models live in `tenancy`; the `audit.subject_type` split Phase 1a's rename caused is repaired; the `X-Organization-Id` retriever exists (unwired); a database seeded before this branch can still `migrate` cleanly.

**Feature flag**: none.

**Re-scoped from the original text below.** Phase 1a's migration-graph rewrite (this phase's original change 1: rewriting the 72 in-migration `organizations.` references across 79 migrations) turned out to be **mandatory for Phase 1a itself** — `MigrationLoader.build_graph()` raises `NodeNotFoundError` on a repo where the app is renamed but its own migrations still declare `dependencies = [('organizations', ...)]` against a graph that no longer has an `organizations` app. Phase 1a therefore did that rewrite as part of its own change, and this phase's remaining scope is the five items below. See `ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md`'s "Current phase" section for the re-scope note and the Phase 1a carry-forwards it responds to.

**Note**: `ORGANIZATION_MODEL` / `ORGANIZATION_MEMBERSHIP_MODEL` and `SHARED_SCHEMA_ORGANIZATIONS` are **not** set in this phase — Phase 1c owns them, bundled with installing the package's Django app (see that phase's Changes list and the Phase 1a fixer note recorded in the tracking file). Setting a swappable-model target without the app installed to resolve it exercises nothing, so the two move together.

Changes:

1. **Content-type / permission data migration** (`tenancy/migrations/0023_move_content_types_to_tenancy.py`): update `django_content_type.app_label` from `organizations` to `tenancy` for the app's four models (`Organization`, `OrganizationMembership`, `OrganizationInvitation`, `OrganizationBranding`), and repoint the `auth_permission` rows that reference them. Idempotent both directions. Two cases, both handled: **no collision** (no `tenancy`-labelled row yet for a model) relabels the existing row in place, so every FK stays valid at the same id with zero further work; **collision** (both an `organizations`- and a `tenancy`-labelled row already exist — the shape produced by a `migrate` run against a database while the app was resolvable under both labels) merges the old row into the surviving `tenancy` one: a duplicate-codename permission is merged (its group/user grants re-pointed, the old permission row deleted), a permission with no codename match is simply re-pointed, and the old, now-empty content type row is deleted last. `organizationtier` / `subscriptionplan` (models deleted in `0015_remove_subscriptionplan_tier_and_more.py`) and `organizationsite` (the package's own model, not installed until Phase 1c) are explicitly out of scope. Reverse is best-effort per the "Pre-launch posture" Guiding Decision: it relabels back exactly in the no-collision case and recreates a fresh `organizations`-labelled row (new id) in the collision case rather than raising.
2. **Audit `subject_type` backfill** (`audit/migrations/0002_backfill_subject_type_namespace.py`): `audit/services.py::AuditService.subject_from_instance` persists `subject_type=f"{meta.app_label}.{instance.__class__.__name__}"`. Every write from `tenancy/services.py`, `tenancy/views.py`, and `public_api/mutations.py` stored `organizations.*` before Phase 1a's app rename and stores `tenancy.*` after it — `AuditRepository.query()` filters on that exact string, so audit history silently splits into two namespaces the moment Phase 1a ships, and pre-rename rows fall out of every subject-type-filtered query. Idempotent data migration, paired with change 1's content-type migration in the same pass: `UPDATE audit_audit SET subject_type = replace(subject_type, 'organizations.', 'tenancy.') WHERE subject_type LIKE 'organizations.%'` (table/column verified: `audit_audit.subject_type`, `character varying(255)`; every `subject_from_instance` caller across `tenancy`, `public_api`, `payments`, `calendar_integration`, and `legal` passes an instance whose app label is one of those five — never `organizations` — so `organizations.` cannot collide with a legitimately-prefixed value from any other app). Carried forward from the Phase 1a review.
3. Write `common/org_retrievers.py::retrieve_by_x_organization_id`, reading the header by integer PK. Not yet registered in `ORGANIZATION_RETRIEVERS` (that list lives inside `SHARED_SCHEMA_ORGANIZATIONS`, created in Phase 1c) and not yet consulted by anything — `TenantScopedViewMixin` starts using it in Phase 2b.
4. Add `OrganizationMiddleware` to `MIDDLEWARE`? **No** — deliberately omitted. Context binding happens in `TenantScopedViewMixin`, after authentication. Recorded here because its absence is a decision, not an oversight.
5. **Seeded-database migration path.** A database created before this branch has `django_migrations` rows keyed `('organizations', '0001_initial')` … `('0022_...')`. After the rename, `manage.py migrate` calls `loader.check_consistent_history(connection)` before computing any plan, and that raises `InconsistentMigrationHistory` — some already-applied migration (in `audit`, `calendar_integration`, `payments`, `public_api`, or `webhooks`) now has an unapplied dependency, because the `tenancy` migrations its graph edge points at are recorded under the old label. **Chose option (a)**: a management command, `python manage.py rename_organizations_migration_history` (`tenancy/management/commands/`), run once before `migrate` against any pre-branch database — `UPDATE django_migrations SET app = 'tenancy' WHERE app = 'organizations'`, idempotent, with a `--dry-run` flag. **Why a command and not a migration**: a migration cannot fix its own app's identity — the loader decides what is pending by reading `django_migrations` *before* running anything, so a `RunPython` step inside the `tenancy` graph would never get a chance to execute; the loader is already stuck on the inconsistency first. Operator runbook: run the command once against a pre-branch database, then `migrate` as normal.
6. Final audit pass over `*/migrations/*.py` for any remaining `organizations` → `tenancy` substitution that landed on something other than an app-label reference (`related_name`, `related_query_name`, `verbose_name`, index/constraint names, permission codenames, help text) or a raw-SQL literal table name that should have stayed `organizations_*`. Result: **none found** beyond the one already fixed and recorded in the Phase 1a review (`related_name='organizations'` restored on `tenancy/migrations/0002_initial.py`'s `OrganizationTier` FK).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `tenancy/tests/test_content_type_migration.py` — no-collision relabel, collision merge (permission repoint, group/user grant re-pointing, duplicate-codename deletion), idempotency in both cases, and the reverse (exact undo in the no-collision case, no-op when an `organizations` row already exists).
- **Integration**: `audit/tests/test_subject_type_migration.py` — every affected subject type is rewritten, the migration is idempotent, unrelated `subject_type` values (including one that merely contains the substring `organizations` without the exact prefix) are untouched, and reverse round-trips. Expected values are pinned as literals, not derived from the `Replace` expression under test.
- **Integration**: `common/tests/test_org_retrievers.py` — the retriever resolves a valid header, returns `None` on a missing, empty, or non-integer header, and returns `None` (never raises) on an unknown PK.
- **Integration**: `tenancy/tests/test_seeded_database_migration_path.py` — builds the pre-rename state by relabelling the test database's own applied `tenancy` migration rows back onto `organizations`, proves `loader.check_consistent_history` then raises `InconsistentMigrationHistory` (the real failure mode), runs the fix command, and proves `migrate` is a clean no-op afterward (empty plan, no raise, `check_consistent_history` passes) — plus the "from zero" case (the test database itself, built by `migrate` from empty before any test runs) and the command's own idempotency and `--dry-run` behavior.

**Suggested AI model**: Tier 3.

**Review models**: reviewer Tier 4 — a missed content-type row or a wrong seeded-database fix produces a migration graph that applies cleanly on an empty database and fails on a populated one, which is the failure mode CI is least likely to catch.

**Reusable skills**: `add-migration` — the content-type and audit-namespace data migrations both go through it.

Acceptance: `migrate` runs clean from zero on an empty database *and* from the pre-rename state on a seeded one (via the fix command); `makemigrations --check` reports nothing pending; the suite is green.

---

### Phase 1c — Unwind the composite PK, subclass the abstract bases, backfill slugs

**Goal**: `Organization` and `OrganizationMembership` are the package's models, carrying our extra fields, with `groups` and `permissions` M2Ms available and unused.

**Feature flag**: none.

Changes:

1. `@vinta_schedule_api/settings/base.py`: add `organizations.apps.OrganizationsConfig` to `INSTALLED_APPS` (kept separate from `INTERNAL_INSTALLED_APPS`, which drives di_core's DI wiring and names only this project's apps). Set `ORGANIZATION_MODEL = "tenancy.Organization"` and `ORGANIZATION_MEMBERSHIP_MODEL = "tenancy.OrganizationMembership"` so the package's own, identically-table-named models are `_meta.swapped` rather than colliding with ours (`models.E028`) or leaving a phantom CASCADE relation on `User.delete()`. Add the `SHARED_SCHEMA_ORGANIZATIONS` dict with `ORGANIZATION_RETRIEVERS` pointing at our retriever (written in this phase or Phase 1b — see that phase) and every non-goal retriever omitted.
2. Resolve the admin double-registration with a supported call rather than a `sys.modules` patch: after `django.contrib.admin.autodiscover()` has run (or via the relevant `AppConfig.ready()`), `admin.site.unregister(get_organization_membership_model())` / `admin.site.unregister(get_organization_model())` for whichever of the package's own admin registrations collide with `tenancy/admin.py`'s existing `ModelAdmin` registrations, then leave ours in place. No `sys.modules["organizations.admin"]` stub.
3. Remove `pk = SafeCompositePrimaryKey("user", "organization")` from `OrganizationMembership`; add back a surrogate `id`. Keep the `uniq_membership_user_organization` constraint — the raw-SQL PROTECT FKs target it and must not be touched.
4. Reparent both models onto `AbstractOrganization` / `AbstractOrganizationMembership` as shown in **Data Model Changes**. Drop `meta` and the two timestamp indexes.
5. `OrganizationMembershipManager` inherits `SingleOrganizationUnscopedManager`; set `default_manager_name = "objects"`. Getting this wrong scopes `user.memberships` and breaks every pre-selection lookup.
6. Rename the user-side reverse accessor: `user.organization_memberships` becomes `user.memberships` at every call site.
7. Slug backfill data migration: `slugify(name)`, numeric disambiguator on collision, `org-<pk>` fallback when the derived value fails `tenancy/slug_validation.py`. Then `ALTER COLUMN slug SET NOT NULL`.
8. `role` and `is_billing_owner` stay on the model, untouched and still read by every permission class. Nothing about authorization changes in this phase.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `tenancy/tests/test_membership_pk.py` — a membership round-trips through save / refresh / delete on the surrogate PK; `uniq_membership_user_organization` still rejects a duplicate `(user, organization)`.
- **Integration**: `calendar_integration/tests/test_membership_protect_fk.py` — deleting a membership with a `CalendarOwnership` still raises the raw-SQL `RESTRICT`, proving the FK survived the PK change.
- **Integration**: `tenancy/tests/test_slug_backfill.py` — collision disambiguation, reserved-word fallback, idempotent re-run, and NOT NULL after.
- **Unit**: `tenancy/tests/test_membership_manager.py` — `user.memberships` returns rows with no organization bound (the unscoped-manager contract).

**Suggested AI model**: Tier 4 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Unwinding a composite PK while keeping a constraint that raw SQL depends on, against a base class whose manager semantics are subtle, is the hardest single phase here.

**Review models**: reviewer Tier 4, fixer Tier 3 — the PROTECT-FK interaction is the kind of thing that passes tests and fails in production.

**Reusable skills**: `add-migration`; `add-model` for the reparenting conventions.

Acceptance: memberships have a surrogate PK, `groups` / `permissions` M2M tables exist and are empty, every organization has a valid non-null slug, the raw-SQL PROTECT FK still fires, and the suite is green with authorization behavior unchanged.

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

**Goal**: `audit`, `webhooks`, `public_api`, and `tenancy` finish the model layer; `TenantScopedViewMixin` binds the request's organization.

**Feature flag**: none.

Changes:

1. Same flip as Phase 2a for the remaining 6 models and 6 relations across `@audit/models.py`, `@webhooks/models.py`, `@public_api/models.py`, `@tenancy/models.py`.
2. `@common/utils/view_utils.py`: after `TenantScopedViewMixin.initial()` resolves the membership, bind the organization to the context and unbind on response. The resolution table (400 on ambiguity, 403 on non-member, per-action opt-outs via `active_org_resolution_optional` / `active_org_optional_actions`) is unchanged — only the binding is new.
3. `@public_api/middlewares.py::PublicApiSystemUserMiddleware` runs before DRF and resolves a system user; confirm it binds an organization before touching scoped models, or moves its scoped work behind the binding.
4. Delete `OrganizationModel`, `BaseOrganizationModelManager`, `BaseOrganizationModelQuerySet` from `tenancy/`. `@common/fields.py`'s `TenantSafeForeignKey` and friends stay until Phase 6 — `OrganizationMembershipForeignKey` still builds on them.

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
5. `@tenancy/services.py`: membership and invitation creation assigns groups *in addition to* setting `role`, so both representations stay consistent until Phase 6 drops one.
6. `billing_recipients()` switches to the permission-shaped query from **Manager plumbing**.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `tenancy/tests/test_permission_backend.py` — an admin's membership resolves the four permissions under a bound organization and **none** under a different bound organization; the union with global permissions works; an unbound context yields no organization permissions.
- **Integration**: `tenancy/tests/test_group_backfill_migration.py` — every combination of `role` × `is_billing_owner` maps to the right groups, and re-running changes nothing.
- **Integration**: `payments/tests/test_dunning_recipients.py` — `billing_recipients` returns the same set before and after the query change. Pin literal expected recipients rather than deriving the expectation from the same filter under test.

**Suggested AI model**: Tier 3. Established Django patterns, but the backend's per-organization cache semantics and the backfill's edge cases need care.

**Reusable skills**: `add-migration`.

Acceptance: `user.has_perm("payments.manage_billing")` is `True` under a bound organization where the membership is a billing owner and `False` under any other, every membership has exactly one group matching its old role, and no permission class reads groups yet.

---

### Phase 4 — Migrate the permission classes to `has_perm`

**Goal**: authorization decisions read permissions instead of `role` / `is_billing_owner`, with identical outcomes.

**Feature flag**: none.

Changes:

1. `@tenancy/permissions.py`: `IsOrganizationAdmin` reads `tenancy.manage_members`. `IsBillingOwnerOrAdmin`'s direct check reads `payments.manage_billing`; **its acting-reseller-root branch keeps its bespoke subtree walk** — the backend keys on the current organization and cannot see a grant held in an ancestor, so `is_target_in_subtree` and the `can_invite_organizations` check stay, with only the role sub-check swapped.
2. `OrganizationManagementPermission` keeps its membership-*absence* gate verbatim; nothing about it is permission-shaped.
3. Both branding gates keep their entitlement logic; only the role half becomes `tenancy.manage_branding`. `user_administers_branding_eligible_organization` (the S3Direct `auth` callable) iterates permitted memberships instead of `role=ADMIN` ones.
4. `@users/models.py::is_organization_admin(organization)` becomes a `has_perm` wrapper, keeping its signature so `@calendar_integration/permissions.py` needs no change beyond what it inherits.
5. Sweep the remaining classes across `@calendar_integration/permissions.py` (7 classes), `@public_api/permissions.py` (2), `@users/permissions.py` (1).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `tenancy/tests/test_permissions_parity.py` — for each of the 15 permission classes, a matrix of (membership state × target object) yields the same allow/deny as before the change. This is the phase's contract.
- **Integration**: `payments/tests/test_reseller_root_billing.py` — an admin of a reseller parent may still manage a descendant's billing while the bound organization is the descendant. This is the case `has_perm` alone gets wrong, so it gets its own test.
- **Integration**: `tenancy/tests/test_branding_gate_parity.py` — entitled-but-unpermitted and permitted-but-unentitled both still deny.

**Suggested AI model**: Tier 4. Fifteen classes, four of which do not fit the model being migrated to; the risk is silently widening a grant.

**Review models**: reviewer Tier 4 — every finding here is an authorization defect.

**Reusable skills**: none.

Acceptance: no permission class reads `role` or `is_billing_owner`, the parity matrix is green across all 15 classes, and the reseller-root case still passes.

---

### Phase 5 — Expose permissions on REST and GraphQL, drop `role` from the API

**Goal**: clients receive resolved permissions and assign groups; `role` leaves the contract.

**Feature flag**: none.

Changes:

1. `@tenancy/serializers.py`: `MyMembershipSerializer` and the `mine` serializer swap `role` for `permissions`; the member-list serializer drops `role` and `is_billing_owner`. `can_manage_branding` stays a distinct field for the reason given in **API Design**.
2. `@tenancy/views.py`: `update-role` becomes `POST /organization-members/{user_id}/groups/`, preserving idempotency and the last-admin protection restated as "the last active member holding `tenancy.manage_members`".
3. `@public_api/types.py`: delete `OrgRole`; the invitation input takes `groups`.
4. `@tenancy/graphql.py` and `@public_api/queries.py` / `mutations.py`: update the affected fields.
5. Regenerate `@schema.yml`.
6. Produce the client handoff via `handoff-to-client`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: `tenancy/tests/test_membership_api_surface.py` — responses carry `permissions` and no `role`; the permission list matches what the backend resolves.
- **Integration**: `tenancy/tests/test_group_assignment_endpoint.py` — assignment, idempotent re-assignment, rejection of the last-admin demotion, rejection of an unknown group name.
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
3. Delete the compatibility shims left in `@tenancy/services.py` that wrote both representations.
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

**Locks.** Table renames are avoided entirely by pinning `db_table`. The remaining DDL is index add/drop per scoped model, the membership PK change, `ALTER COLUMN slug SET NOT NULL`, and two column drops. Pre-launch, so lock duration is not a production concern — but the migrations are written as if it were, because that posture is cheap now and expensive to retrofit.

**Alpha dependency.** `vinta-django-orgs` is `0.1.1`, Development Status `Alpha`, first published to PyPI on 2026-08-11, and its `0.1.0` release notes already record a breaking change to `OrganizationMembership.objects` scoping. Pinned `<0.2`. Being the package's author is what makes this acceptable — a breaking upstream change is a decision, not a surprise.

**Rollback.** Pre-launch posture: no per-phase reverse path is guaranteed. The practical unit of rollback is the phase branch. Phases 0, 1a, and 1b are cleanly revertible (no destructive DDL); 1c onward are not, because the PK change and the slug NOT NULL constraint discard information.

**Backfills.** Three, all idempotent and all small enough to run in one transaction pre-launch: content types (Phase 1b), slugs (Phase 1c), group assignment (Phase 3). Written batched and resumable anyway — see `add-one-off-script`'s contract for the shape — so they remain usable if the pre-launch assumption changes.

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

**Phase 1a**
- [pyproject.toml](pyproject.toml), [uv.lock](uv.lock)
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py)
- `organizations/` → `tenancy/` (whole app: 19 modules, 23 migrations, 18 test modules)
- [di_core/apps.py](di_core/apps.py)
- 367 import sites and 223 string references across `calendar_integration`, `payments`, `audit`, `webhooks`, `public_api`, `users`, `accounts`, `common`
- `@tenancy/tests/test_app_label.py` (new)

**Phase 1b** (re-scoped — see the phase entry's "Re-scoped from the original text below" note; the 72 in-migration references across 79 migrations moved into Phase 1a's own change, out of necessity)
- `@tenancy/migrations/0023_move_content_types_to_tenancy.py` (new)
- `@audit/migrations/0002_backfill_subject_type_namespace.py` (new)
- `@common/org_retrievers.py` (new)
- `@tenancy/management/commands/rename_organizations_migration_history.py` (new)
- `@tenancy/tests/test_content_type_migration.py`, `@common/tests/test_org_retrievers.py`, `@audit/tests/test_subject_type_migration.py`, `@tenancy/tests/test_seeded_database_migration_path.py` (new)

**Phase 1c**
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py) — install the package's app, `ORGANIZATION_MODEL` / `ORGANIZATION_MEMBERSHIP_MODEL`, `SHARED_SCHEMA_ORGANIZATIONS`, admin double-registration fix
- [organizations/models.py](organizations/models.py) → `tenancy/models.py`, [organizations/managers.py](organizations/managers.py), [organizations/querysets.py](organizations/querysets.py)
- `@tenancy/migrations/00XX_unwind_composite_pk.py`, `@tenancy/migrations/00XX_backfill_slugs.py` (new)
- `user.organization_memberships` call sites across `tenancy`, `payments`, `calendar_integration`, `public_api`
- `@tenancy/tests/test_membership_pk.py`, `@tenancy/tests/test_slug_backfill.py`, `@tenancy/tests/test_membership_manager.py`, `@calendar_integration/tests/test_membership_protect_fk.py` (new)

**Phase 2a**
- [calendar_integration/models.py](calendar_integration/models.py) — 28 models, 59 relations
- `@calendar_integration/migrations/` — index migrations, one per model
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py) — `STRICT_ORGANIZATION_FILTER`
- `@calendar_integration/tests/test_implicit_scoping.py`, `@calendar_integration/tests/test_safe_relation_joins.py` (new); query-count assertions across the existing suite

**Phase 2b**
- [audit/models.py](audit/models.py), [webhooks/models.py](webhooks/models.py), [public_api/models.py](public_api/models.py), `tenancy/models.py`
- [common/utils/view_utils.py](common/utils/view_utils.py), [public_api/middlewares.py](public_api/middlewares.py)
- `tenancy/managers.py`, `tenancy/querysets.py` — delete `OrganizationModel`, `BaseOrganizationModelManager`, `BaseOrganizationModelQuerySet`
- `@common/tests/test_tenant_scoped_binding.py`, `@public_api/tests/test_system_user_scoping.py`, `@audit/tests/test_audit_scoping.py` (new)

**Phase 3**
- `tenancy/models.py` (`Meta.permissions`), [payments/models.py](payments/models.py) (`Meta.permissions`)
- [vinta_schedule_api/settings/base.py](vinta_schedule_api/settings/base.py) — `AUTHENTICATION_BACKENDS`
- `@tenancy/migrations/00XX_seed_permission_groups.py`, `@tenancy/migrations/00XX_backfill_membership_groups.py` (new)
- `tenancy/services.py`, `tenancy/querysets.py` (`billing_recipients`)
- `@tenancy/tests/test_permission_backend.py`, `@tenancy/tests/test_group_backfill_migration.py`, `@payments/tests/test_dunning_recipients.py` (new)

**Phase 4**
- `tenancy/permissions.py`, [calendar_integration/permissions.py](calendar_integration/permissions.py), [public_api/permissions.py](public_api/permissions.py), [users/permissions.py](users/permissions.py), [users/models.py](users/models.py)
- `@tenancy/tests/test_permissions_parity.py`, `@payments/tests/test_reseller_root_billing.py`, `@tenancy/tests/test_branding_gate_parity.py` (new)

**Phase 5**
- `tenancy/serializers.py`, `tenancy/views.py`, `tenancy/routes.py`, `tenancy/graphql.py`
- [public_api/types.py](public_api/types.py), [public_api/queries.py](public_api/queries.py), `public_api/mutations.py`
- [schema.yml](schema.yml) (regenerated)
- `@.vinta-ai-workflows/client-handoffs/2026-XX-XX-membership-permissions.md` (new)
- `@tenancy/tests/test_membership_api_surface.py`, `@tenancy/tests/test_group_assignment_endpoint.py`, `@public_api/tests/test_invitation_groups.py` (new)

**Phase 6**
- `@tenancy/migrations/00XX_drop_role_and_billing_owner.py` (new)
- [common/fields.py](common/fields.py) — delete `TenantSafeForeignKey`, `TenantSafeOneToOneField`, `SafeCompositePrimaryKey`, `_SafeCompositeAttribute`
- `tenancy/services.py` — delete dual-write shims
- Dual-write-period tests across `tenancy/tests/`
