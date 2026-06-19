# Audit Trail — Implementation Plan

> No `..._SPEC.md` sibling exists for this feature. Requirements were captured through a Step-0 interrogation (recorded inline in **Guiding Decisions**). If a spec is authored later, reconcile this plan against it.

## 1. Goals

1. Ship a self-contained, injectable `audit` Django app that records actions taken by **memberships** (`OrganizationMembership`), **system users** (`public_api.SystemUser`), **single-use codes** (`calendar_integration.CalendarManagementToken`), and the **system** itself.
2. Define a backend-agnostic `AuditRepository` interface (read + append only — no update, no delete) with a first concrete `DjangoORMAuditRepository`, both wired through the existing `dependency-injector` container so a non-ORM backend can be swapped in later without touching callers.
3. Expose a DI-injected `AuditService.record(...)` that other modules call explicitly; it snapshots mutable actor context synchronously and persists asynchronously via Celery.
4. Provide a **read-only, repository-backed Django admin** (filters, search, detail view, CSV export) that works against any `AuditRepository` implementation — never creating, editing, or deleting records.

**Non-goals:**
- Instrumenting existing write paths (calendar, payments, organizations, public_api, webhooks). This plan builds the engine + admin only; each owning module wires its own `record(...)` calls in follow-up PRs.
- Table partitioning, retention/purge jobs, or archival. v1 is a plain indexed table; revisit when volume is known.
- REST or public GraphQL surface for audits. Read access is Django admin only.
- A second (non-ORM) repository implementation. The interface is designed for it; only the ORM backend ships now.
- Feature flag machinery (the repo has no flag system, and this is purely additive surface — see **Guiding Decisions**).
- Backfilling historical actions that occurred before the audit app shipped.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Scope** | Engine + admin only. No call-site instrumentation in this plan — proves the pattern without dragging every module's regression surface into one feature. Instrumentation is per-module follow-up. |
| **Write path** | **Async via Celery.** `AuditService.record(...)` validates + snapshots synchronously, then enqueues a task that persists via the repository. Keeps audit latency/failure off the action's critical path. |
| **Snapshot-at-emit** | Because persistence is async, all *mutable* actor context (`actor_role`, `system_user_scopes`, `system_user_scoped_to_membership`) is captured **synchronously inside `record()`** and passed in the task payload — never re-read in the worker, where the membership/system-user could have changed or been deleted. |
| **Emit contract** | Explicit `AuditService.record(...)` call, service injected via DI (`Provide["audit_service"]`). Most precise; intent + actor are unambiguous at the call site. No signals (lose intent, fire on every ORM write), no decorators (too implicit for an audit trail). |
| **Tenancy** | Audit is **org-scoped** (`OrganizationModel`, `organization` FK). Matches every other hot table; system-actor records still carry org context. Truly global actions are out of scope for v1. |
| **Subject storage** | **Soft reference**: `subject_type` (`"app_label.ModelName"` string) + `subject_id` (string) + optional `subject_label` (human-readable snapshot). Portable across any backend, survives row deletion, no contenttypes coupling. No DB-enforced integrity — accepted for an append-only log. |
| **Actor storage** | `actor_type` enum (`SYSTEM`, `MEMBERSHIP`, `SYSTEM_USER`, `SINGLE_USE_CODE`) + nullable `actor_id` (BigInteger). Portable, extensible to new actor kinds without a schema change per kind. `actor_id` is null for `SYSTEM`. |
| **`affected_memberships` storage** | **M2M through table** (`AuditAffectedMembership`, tenant-safe via `OrganizationForeignKey`) in the *ORM* repository. The `AuditRepository` interface and `record(...)` payload exchange a plain `affected_membership_ids: list[int]`, so a non-ORM backend stores it however it likes. May be empty. |
| **`action` enum** | Central, extensible `AuditAction(models.TextChoices)` in the audit app. Single source of truth, validated on write, gives admin readable labels. Owning modules contribute new members over time. |
| **`diff`** | Nullable JSON in `{field: {"old": ..., "new": ...}}` shape. A shipped `compute_diff(before, after)` helper produces it from two state dicts so callers don't reimplement it; callers may also pass a pre-built dict. |
| **System-user snapshot** | `system_user_scopes` = JSON `list[str]` of `PublicAPIResources` values captured at emit time; `system_user_scoped_to_membership` = the membership id (BigInteger), null if the token is org-wide. Soft ids, not FKs — consistent with the snapshot model. |
| **`created_at`** | Explicit `created_at = DateTimeField(auto_now_add=True, db_index=True)` is the canonical contract field exposed by the DTO. `OrganizationModel`/`BaseModel` also supply `created`/`modified`, but the portable interface uses `created_at`. |
| **Admin backend-agnosticism** | The admin is a **custom, repository-driven** set of admin-site views (not a plain ORM `ModelAdmin`) so it works identically whether the repository is ORM-backed or not. All reads go through `AuditRepository.query(...)` / `.get(...)`. |
| **Volume / partitioning / retention** | Plain indexed table for v1. Indexes target the admin's filter/search predicates. Partitioning + retention deferred. |
| **No feature flag** | The repo has no feature-flag system, and every artifact here is *new additive surface* (new app, new table, new admin views, no existing code path reads/writes it). Per the planning rules this is a legitimate flag skip — there is consequently **no flag-removal phase**. |

## 3. Data Model Changes

### 3.1 New `audit/constants.py` — enums

```python
from django.db import models


class AuditActorType(models.TextChoices):
    SYSTEM = "system", "System"
    MEMBERSHIP = "membership", "Membership"
    SYSTEM_USER = "system_user", "System user"
    SINGLE_USE_CODE = "single_use_code", "Single-use code"


class AuditAction(models.TextChoices):
    # Central, extensible. Owning modules append members as they instrument call sites.
    # Seed with a few generic verbs so the enum + admin are usable on day one.
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"
    # ... modules extend (e.g. CALENDAR_EVENT_RESCHEDULE = "calendar.event.reschedule", ...)
```

### 3.2 New `audit/models.py` — `Audit` (`OrganizationModel`)

```python
class Audit(OrganizationModel):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=100, choices=AuditAction.choices, db_index=True)

    actor_type = models.CharField(max_length=20, choices=AuditActorType.choices, db_index=True)
    actor_id = models.BigIntegerField(null=True, blank=True)  # null for SYSTEM
    actor_role = models.CharField(  # snapshot of membership role at emit time; null unless MEMBERSHIP
        max_length=20, choices=OrganizationRole.choices, null=True, blank=True,
    )

    system_user_scopes = models.JSONField(null=True, blank=True)  # list[str] of PublicAPIResources; null unless SYSTEM_USER
    system_user_scoped_to_membership = models.BigIntegerField(null=True, blank=True)  # membership id snapshot; null if org-wide

    subject_type = models.CharField(max_length=255, db_index=True)  # "app_label.ModelName"
    subject_id = models.CharField(max_length=255, db_index=True)    # soft ref, string for PK-shape portability
    subject_label = models.CharField(max_length=255, null=True, blank=True)  # human-readable snapshot

    diff = models.JSONField(null=True, blank=True)  # {field: {"old": ..., "new": ...}}, null unless an update

    affected_memberships = models.ManyToManyField(
        "organizations.OrganizationMembership",
        through="audit.AuditAffectedMembership",
        related_name="+",
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["actor_type", "actor_id"]),
            models.Index(fields=["subject_type", "subject_id"]),
        ]
```

Notes:
- Append-only: no `save()`-driven mutation API exposed; the repository never updates/deletes.
- `OrganizationModel` supplies `organization` + the tenant-scoped default manager. The admin/repository read path must use an **unscoped manager** (staff context has no active-membership header) with `organization` exposed as a filter — see Phase 3.

### 3.3 New `audit/models.py` — `AuditAffectedMembership` (`OrganizationModel`, through table)

```python
class AuditAffectedMembership(OrganizationModel):
    audit = OrganizationForeignKey(Audit, on_delete=models.CASCADE, related_name="affected_membership_links")
    membership = OrganizationForeignKey(
        "organizations.OrganizationMembership", on_delete=models.CASCADE, related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["audit", "membership"], name="uniq_audit_membership"),
        ]
        indexes = [models.Index(fields=["membership"])]  # "audits affecting membership X"
```

### 3.4 New `audit/types.py` — portable DTOs + query object

These are the backend-agnostic data carriers the interface and Celery payload speak in (plain dataclasses, JSON-serializable):

```python
@dataclass(frozen=True)
class ActorSnapshot:
    actor_type: str                       # AuditActorType value
    actor_id: int | None
    actor_role: str | None = None         # OrganizationRole value, MEMBERSHIP only
    system_user_scopes: list[str] | None = None          # SYSTEM_USER only
    system_user_scoped_to_membership: int | None = None  # SYSTEM_USER only

@dataclass(frozen=True)
class SubjectRef:
    subject_type: str
    subject_id: str
    subject_label: str | None = None

@dataclass(frozen=True)
class AuditRecordData:        # what record() builds and the task persists
    organization_id: int
    action: str
    actor: ActorSnapshot
    subject: SubjectRef
    affected_membership_ids: list[int] = field(default_factory=list)
    diff: dict | None = None

@dataclass(frozen=True)
class AuditRecord:           # what the repository returns to readers (incl. id, created_at)
    id: int
    created_at: datetime
    # ... all AuditRecordData fields flattened

@dataclass(frozen=True)
class AuditQuery:            # the single filter/search object the admin passes to any backend
    organization_id: int | None = None
    actions: list[str] | None = None
    actor_type: str | None = None
    actor_id: int | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    affected_membership_id: int | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    has_diff: bool | None = None
    search: str | None = None

@dataclass(frozen=True)
class AuditPage:
    items: list[AuditRecord]
    total: int
```

### 3.5 New `audit/repositories.py` — interface

```python
class AuditRepository(abc.ABC):
    @abc.abstractmethod
    def add(self, data: AuditRecordData) -> AuditRecord: ...
    @abc.abstractmethod
    def get(self, audit_id: int) -> AuditRecord | None: ...
    @abc.abstractmethod
    def query(self, q: AuditQuery, *, offset: int = 0, limit: int = 50,
              ordering: str = "-created_at") -> AuditPage: ...
    # No update. No delete. Append + read only.
```

## 4. API Design

No external API surface. The only read interface is the Django admin (Phases 7–10) and the only write interface is the DI-injected `AuditService.record(...)` (Phase 5). The repository interface itself (`add` / `get` / `query`) is the internal contract.

## 5. Phased Rollout

### Phase 0 — Scaffold the `audit` app

**Goal**: Empty, installed `audit` app skeleton wired into DI. No behavior. (Ship value: none on its own — required foundation so every later phase has a home and the container can wire it.)

**Feature flag**: none — pure scaffolding.

Changes:
1. `python manage.py startapp audit` → trim to `apps.py`, `__init__.py`, `models.py`, `admin.py`, `migrations/`.
2. Register `"audit"` in `INTERNAL_INSTALLED_APPS` in [base.py:30](../vinta_schedule_api/settings/base.py#L30) (placed after `organizations`, before `public_api`, since it references both).
3. Confirm `di_core/apps.py` `container.wire(packages=...)` already covers `INTERNAL_INSTALLED_APPS` so `audit` is auto-wired ([apps.py:14](../di_core/apps.py#L14)).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `audit/tests/test_app_config.py` — `apps.get_app_config("audit")` resolves; app imports cleanly.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Mechanical app scaffold, exact precedent across existing apps.

**Reusable skills**: none.

Acceptance: `python manage.py check` passes with `audit` in `INSTALLED_APPS`; app contributes no migrations yet.

---

### Phase 1 — Enums, DTOs, and the `AuditRepository` interface

**Goal**: The backend-agnostic contract exists — enums, DTOs, query object, and the abstract repository. No DB, no persistence. (Ship value: none on its own — it's the seam every other phase depends on.)

**Feature flag**: none — additive, no reachable behavior.

Changes:
1. `audit/constants.py`: `AuditActorType`, `AuditAction` (see Data Model Changes 3.1).
2. `audit/types.py`: `ActorSnapshot`, `SubjectRef`, `AuditRecordData`, `AuditRecord`, `AuditQuery`, `AuditPage` (3.4).
3. `audit/repositories.py`: `AuditRepository` ABC (3.5).
4. Export the public names from `audit/__init__.py`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `audit/tests/test_types.py` — DTOs construct, are frozen, and round-trip to/from JSON-able dicts (needed because the Celery payload serializes them).
- **Unit**: `audit/tests/test_repository_interface.py` — `AuditRepository` cannot be instantiated; a stub subclass implementing all abstract methods can.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` if DTO/JSON plumbing gets fiddly) / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: none.

Acceptance: importing `audit` exposes the enums, DTOs, and ABC; the interface has no `update`/`delete` methods; tests green.

---

### Phase 2 — `Audit` model + through table + migration

**Goal**: The ORM storage exists: `Audit`, `AuditAffectedMembership`, indexes, and the migration.

**Feature flag**: none — additive new tables, no existing code reads/writes them.

Changes:
1. `audit/models.py`: `Audit` (3.2) and `AuditAffectedMembership` (3.3), both `OrganizationModel`.
2. Generate the migration; confirm indexes + the `uniq_audit_membership` constraint land. Plain `makemigrations` (no raw-SQL/lock-aware work — brand-new tables, no hot-table contention).
3. Register nothing in admin yet (Phase 7 owns the custom admin).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `audit/tests/test_models.py` — create an `Audit` via the ORM, attach affected memberships, assert the unique constraint rejects a duplicate `(audit, membership)`; assert `created_at` auto-populates.
- **Integration**: `audit/tests/test_migrations.py` (or migration smoke via the project's standard migration test) — migration applies and reverses cleanly.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` with iteration / `gpt-5-mini` / `gemini-2.5-flash`. Standard model + migration; precedent in `public_api/models.py` and `organizations/models.py`.

**Reusable skills**: `add-model` (new `OrganizationModel` + through table, manager, factory); `add-migration` (the schema migration + reverse path).

Acceptance: `migrate` creates both tables with all indexes and the unique constraint; a factory-built `Audit` persists and reads back.

---

### Phase 3 — `DjangoORMAuditRepository` implementation

**Goal**: A working ORM repository: `add`, `get`, `query` (filters + search + pagination + ordering + total count), backed by the model.

**Feature flag**: none — additive.

Changes:
1. `audit/repositories.py`: `DjangoORMAuditRepository(AuditRepository)`.
   - `add(data)`: create `Audit` + bulk-create `AuditAffectedMembership` rows in one transaction; map to `AuditRecord`.
   - `get(id)`: fetch via the **unscoped** manager (admin/staff context has no active-membership tenant scope), prefetch affected memberships, map to `AuditRecord`; return `None` if absent.
   - `query(q, offset, limit, ordering)`: translate `AuditQuery` → ORM filters (`actions__in`, `actor_type`, `actor_id`, `subject_type`/`subject_id`, `affected_memberships=id`, `created_at__gte/lt`, `diff__isnull` for `has_diff`, and a `search` `Q(...)` across `actor_id`/`subject_type`/`subject_id`/`subject_label`); return `AuditPage(items, total)`.
2. Map model ↔ `AuditRecord`/`AuditRecordData` in one place (a `_to_record` function) so the non-ORM backend can mirror it.

Spec use-case: shared scaffolding — powers the admin read use-cases (Phases 7–10) and the write path (Phase 5).

Tests:
- **Unit**: `audit/tests/test_orm_repository.py` — `add` persists actor snapshot + affected ids + diff and returns a correct `AuditRecord`; `get` round-trips; `get` of a missing id returns `None`.
- **Integration**: `audit/tests/test_orm_repository_query.py` — each `AuditQuery` filter narrows correctly; `search` matches subject + actor; pagination `offset`/`limit` + `total` are correct; `ordering` honored; cross-org rows are visible (unscoped) and `organization_id` filter narrows them.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Multi-method repository with non-trivial query translation + count semantics.

**Reusable skills**: `write-tests` (repository unit + integration tests against factories).

Acceptance: every `AuditQuery` field filters as specified, pagination + total are correct, and `add`→`get` round-trips a full record including affected memberships and diff.

---

### Phase 4 — DI wiring + `compute_diff` helper

**Goal**: The repository and (placeholder) service are resolvable from the container, and the `compute_diff(before, after)` helper produces the canonical diff shape.

**Feature flag**: none — additive.

Changes:
1. `di_core/containers.py`: add `audit_repository = providers.Singleton(DjangoORMAuditRepository)` and a `audit_service = providers.Factory(AuditService, repository=audit_repository)` placeholder (service body lands in Phase 5) — follows the existing `providers.*` pattern at [containers.py:42](../di_core/containers.py#L42).
2. `audit/diff.py`: `compute_diff(before: dict, after: dict) -> dict` → `{field: {"old", "new"}}` for changed keys only; returns `{}`/`None` when nothing changed. Handles added/removed keys.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `audit/tests/test_diff.py` — changed/added/removed keys; no-change returns empty/None; nested values compared by equality; non-dict-serializable values guarded.
- **Integration**: `audit/tests/test_container_wiring.py` — `container.audit_repository()` returns a `DjangoORMAuditRepository`; provider is wired by name.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Pure helper + DI registration with clear precedent.

**Reusable skills**: `write-tests`.

Acceptance: `compute_diff` returns the exact `{field:{old,new}}` shape across add/change/remove/no-change cases; the repository resolves from the DI container.

---

### Phase 5 — `AuditService.record(...)` + Celery persistence task

**Goal**: Other modules can call an injected `AuditService.record(...)`; it snapshots actor context synchronously and persists asynchronously via Celery.

**Feature flag**: none — additive; no existing caller is modified in this plan.

Changes:
1. `audit/services.py`: `AuditService` (DI-injected `repository`), with:
   - `record(*, organization_id, action, actor: ActorSnapshot, subject: SubjectRef, affected_membership_ids=(), diff=None)` — validates, builds `AuditRecordData`, serializes to a JSON-safe dict, enqueues `persist_audit_record.delay(payload)`. Returns nothing (fire-and-forget).
   - Actor builders that capture snapshots **synchronously**: `actor_from_membership(membership)` (→ `actor_role=membership.role`), `actor_from_system_user(system_user)` (→ `system_user_scopes=[r.resource_name for r in system_user.available_resources.all()]`, `system_user_scoped_to_membership=system_user.scoped_to_membership_id`), `actor_from_single_use_code(token)` (→ `actor_id=token.id`), `system_actor()`.
2. `audit/tasks.py`: `@shared_task persist_audit_record(payload: dict)` — rebuilds `AuditRecordData`, resolves the repository from the DI container, calls `repository.add(...)`. Registered with the project Celery app at [celery.py](../vinta_schedule_api/celery.py).
3. Finalize the `audit_service` provider in `di_core/containers.py`.

Spec use-case: the write contract other modules consume (instrumentation itself is out of scope).

Tests:
- **Unit**: `audit/tests/test_service.py` — each actor builder captures the correct snapshot (role for membership, scopes + scoped-to for system user, id for single-use code, nulls for system); `record(...)` enqueues with a JSON-serializable payload (assert via mocked `.delay`).
- **Integration**: `audit/tests/test_task.py` — running `persist_audit_record` (eager) writes a correct `Audit` through the real ORM repository, including affected memberships and diff; a snapshot taken before a membership role change persists the *old* role (proves snapshot-at-emit).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Service + task + DI + serialization with the snapshot-correctness edge.

**Reusable skills**: `write-tests`.

Acceptance: `AuditService.record(...)` for each actor kind results (after the eager task runs) in a persisted `Audit` with the correct snapshot fields, affected memberships, and diff; the action caller is never blocked by repository errors (task failure is isolated/logged).

---

### Phase 6 — Repository-backed admin: list + filters

**Goal**: A read-only admin changelist for audits, backed by `AuditRepository.query(...)`, with the core filters (action, actor_type, created_at range, has_diff) — usable against any backend, never creating/editing/deleting.

**Feature flag**: none — additive new admin views.

Changes:
1. `audit/admin.py`: register custom admin-site views (a repository-driven changelist, not a plain ORM `ModelAdmin`) under the admin site, rendering rows from `AuditService`/`AuditRepository.query(...)`. Resolve the repository from the DI container.
2. Filter controls map to `AuditQuery` fields: `action`, `actor_type`, `created_after`/`created_before`, `has_diff`. `organization` exposed as a filter (unscoped read).
3. Pagination via `query(offset, limit, total)`. `has_add_permission` / `has_change_permission` / `has_delete_permission` → `False`.
4. Template extends `admin/base_site.html` so it inherits the admin look.

Spec use-case: read use-case **Filter audits**.

Tests:
- **Integration** (Django admin test client): `audit/tests/test_admin_list.py` — staff user loads the changelist; each filter narrows results via the repository; add/change/delete are forbidden (no buttons, POSTs rejected); pagination works. Asserts the view goes through `AuditRepository.query` (swap in a stub repository to prove backend-agnosticism).
- **E2E**: N/A — Django admin is internal staff UI rendered server-side, covered by the admin test client above; it is not part of the Next.js frontend the Playwright suite targets. (No `QA_USE_CASES.md` entry needed — no browser e2e for staff admin.)

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Custom repository-driven admin views are the novel/architectural part of this plan.

**Reusable skills**: `write-tests`.

Acceptance: a staff user sees a paginated, filterable audit list sourced entirely through `AuditRepository.query(...)`; no create/edit/delete is possible; swapping the repository for a stub still renders the list.

---

### Phase 7 — Admin search

**Goal**: Add a search box over actor id, subject type+id, and affected membership id to the audit changelist.

**Feature flag**: none — additive.

Changes:
1. `audit/admin.py`: wire the changelist search input to `AuditQuery.search` (and `affected_membership_id` when the term is a membership id). No new query logic in the admin — `DjangoORMAuditRepository.query` already implements `search` (Phase 3).

Spec use-case: read use-case **Search audits**.

Tests:
- **Integration**: `audit/tests/test_admin_search.py` — search by actor id, by subject type/id, and by affected membership id each returns the expected rows through the repository; empty/garbage terms return empty without error.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Thin wiring over an existing repository capability.

**Reusable skills**: `write-tests`.

Acceptance: searching the changelist narrows results by actor/subject/affected-membership, routed through `AuditRepository.query(search=...)`.

---

### Phase 8 — Admin read-only detail view

**Goal**: A clickable, fully read-only detail page per audit showing all fields with a pretty-printed `diff` and `system_user_scopes`, sourced via `AuditRepository.get(...)`.

**Feature flag**: none — additive.

Changes:
1. `audit/admin.py` + template: a detail view calling `repository.get(audit_id)`; pretty-render `diff` (`{field:{old,new}}`) and the scopes list; render the affected-memberships list. All fields read-only; no change form, no save.
2. Link changelist rows to the detail view.

Spec use-case: read use-case **View an audit record**.

Tests:
- **Integration**: `audit/tests/test_admin_detail.py` — detail page renders all fields incl. formatted diff + scopes for each actor kind; a missing id 404s; the page exposes no edit/save/delete controls and rejects mutating POSTs; reads go through `AuditRepository.get`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` if diff/scopes formatting is involved) / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `write-tests`.

Acceptance: clicking an audit opens a read-only detail page with a human-readable diff + scopes; no mutation is possible; data comes from `AuditRepository.get(...)`.

---

### Phase 9 — Admin CSV export

**Goal**: An admin action to export the currently filtered/searched audit queryset to CSV.

**Feature flag**: none — additive.

Changes:
1. `audit/admin.py`: a changelist export action that re-runs the active `AuditQuery` through `repository.query(...)` (streaming/paged to bound memory) and streams a CSV (`StreamingHttpResponse`) with one row per audit (flattened actor snapshot, subject ref, affected membership ids, diff as JSON). Export is read-only — no record mutation.

Spec use-case: read use-case **Export audits to CSV**.

Tests:
- **Integration**: `audit/tests/test_admin_export.py` — export respects active filters + search; CSV header + row shape correct; affected membership ids and diff serialize; large result set streams without loading all rows at once.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Standard streamed-CSV admin action over an existing query path.

**Reusable skills**: `write-tests`; `create-data-export` if its streamed-CSV conventions apply to the admin action.

Acceptance: the export action downloads a CSV of exactly the filtered/searched audits, generated through `AuditRepository.query(...)`, with no record mutation.

## 6. Risk & Rollout Notes

- **Feature flag**: none. All surface is additive (new app, new tables, new admin views, DI providers); no existing code path is altered. If a future phase instruments an existing flow, that PR — not this plan — owns its flag decision.
- **Migration safety**: Phase 2 creates two brand-new tables with their indexes/constraints; no hot-table locks, no rewrites, no backfill. Reverse path is a clean drop. No raw-SQL framework needed.
- **Async durability**: Celery persistence is fire-and-forget — a broker/worker loss can drop an audit write. Accepted for v1 (decision: write path = async). Task failures must be isolated and logged so they never surface to the action caller. If stronger durability is later required, revisit (sync best-effort or transactional outbox).
- **Snapshot correctness**: the chief async hazard is re-reading mutable actor state in the worker. Mitigated by capturing all of it synchronously in `record()` (Phase 5) and asserting it in `test_task.py`.
- **Tenant scoping in admin**: `Audit` is an `OrganizationModel`, but staff admin has no active-membership tenant context. The repository read path uses an **unscoped** manager with `organization` exposed as a filter; Phase 3 tests assert cross-org visibility so admin doesn't silently show nothing.
- **Volume**: no partitioning/retention in v1. The table grows unbounded — flag for a follow-up (partition-by-org or scheduled purge) once production write rates are known. Indexes target admin predicates to keep filtered reads sane in the interim.
- **Rollback**: revert the app from `INSTALLED_APPS`, reverse the Phase 2 migration (drops the two tables). No data in existing tables is touched, so rollback is clean at any phase.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Retention window + partitioning once volume is known. | Defer to a follow-up; start measuring write rate after Phase 5 ships. | Eng leadership |
| Should `SYSTEM`-actor records ever be org-less (truly global actions)? | No for v1 — every record carries an `organization`. Revisit if a global action surfaces. | Product/Eng |
| Who owns extending `AuditAction` as modules instrument call sites — central review or free PRs? | Central enum, additions reviewed in the instrumenting module's PR. | Eng leadership |
| Should CSV export be capped (row limit) to protect the admin worker? | Stream with no hard cap for v1; add a cap if exports get abused. | Eng |
| Does any compliance requirement need audit immutability at the DB level (e.g. revoke `UPDATE`/`DELETE` grants)? | App-level append-only for v1; add DB grants if compliance demands. | Security/Eng |

## 8. Touch List

**Phase 0 — Scaffold**
- @audit/__init__.py, @audit/apps.py, @audit/models.py, @audit/admin.py, @audit/migrations/__init__.py (new)
- [base.py](../vinta_schedule_api/settings/base.py#L30) (edit — add `"audit"` to `INTERNAL_INSTALLED_APPS`)
- @audit/tests/test_app_config.py (new)

**Phase 1 — Contract**
- @audit/constants.py, @audit/types.py, @audit/repositories.py (new)
- @audit/__init__.py (edit — exports)
- @audit/tests/test_types.py, @audit/tests/test_repository_interface.py (new)

**Phase 2 — Model + migration**
- @audit/models.py (edit — `Audit`, `AuditAffectedMembership`)
- @audit/migrations/0001_initial.py (new)
- @audit/factories.py (new — test factory)
- @audit/tests/test_models.py, @audit/tests/test_migrations.py (new)

**Phase 3 — ORM repository**
- @audit/repositories.py (edit — `DjangoORMAuditRepository`)
- @audit/tests/test_orm_repository.py, @audit/tests/test_orm_repository_query.py (new)

**Phase 4 — DI + diff helper**
- [containers.py](../di_core/containers.py#L42) (edit — `audit_repository`, `audit_service` providers)
- @audit/diff.py (new)
- @audit/tests/test_diff.py, @audit/tests/test_container_wiring.py (new)

**Phase 5 — Service + task**
- @audit/services.py, @audit/tasks.py (new)
- [containers.py](../di_core/containers.py#L42) (edit — finalize `audit_service`)
- @audit/tests/test_service.py, @audit/tests/test_task.py (new)

**Phase 6 — Admin list + filters**
- @audit/admin.py (edit — repository-backed changelist views)
- @audit/templates/admin/audit/ (new — changelist template)
- @audit/tests/test_admin_list.py (new)

**Phase 7 — Admin search**
- @audit/admin.py (edit — search wiring)
- @audit/tests/test_admin_search.py (new)

**Phase 8 — Admin detail**
- @audit/admin.py (edit — detail view)
- @audit/templates/admin/audit/ (edit — detail template)
- @audit/tests/test_admin_detail.py (new)

**Phase 9 — Admin CSV export**
- @audit/admin.py (edit — export action)
- @audit/tests/test_admin_export.py (new)
