# Implementation Tracking — Audit Trail

- **Feature**: Audit Trail (injectable audit module + repository-backed read-only admin)
- **Plan**: [ai-plans/2026-06-19-AUDIT_TRAIL_IMPLEMENTATION_PLAN.md](2026-06-19-AUDIT_TRAIL_IMPLEMENTATION_PLAN.md)
- **Started**: 2026-06-19
- **Last updated**: 2026-06-19
- **Feature flag**: none (purely additive surface; repo has no flag system)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `worktree_path`: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-audit-trail
- `worktree_branch`: plan/audit-trail
- `worktree_summary`: .vinta-ai-workflows/worktrees/plan-audit-trail.yaml
- `commit_strategy_resolved`: modular-commits
- `plan_branch`: plan/audit-trail
- `pr_creation`: agents-create
- `pr_template_used`: none (project pr_template_paths is empty → free-form)

## Completed phases
### Phase 0 — Scaffold the `audit` app ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, 0 should-fix, 1 nit ignored)
- **Model used**: claude-haiku-4-5 (plan tier: Tier 1)
- **Commit**: `3d8f8bf chore(audit): scaffold audit app`
- **Summary**: Created the `audit` Django app (`apps.py` with `AuditConfig(name="audit")`, empty `models.py`/`admin.py` docstrings, `migrations/` + `tests/` packages). Registered `"audit"` in `INTERNAL_INSTALLED_APPS` right after `organizations`. DI auto-wires via existing `di_core/apps.py`. No migration generated. Full suite green (2613 passed); `check --deploy` clean (only pre-existing warnings).

### Phase 1 — Enums, DTOs, AuditRepository interface ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, 3 should-fix — all fixed via fixer commit)
- **Model used**: claude-haiku-4-5 (plan tier: Tier 2)
- **Commits**: `483b889` enums, `6606c84` DTOs, `c518906` interface+exports, `d167066` test(audit) JSON-serializability fix
- **Summary**: `audit/constants.py` (`AuditActorType`, `AuditAction` TextChoices), `audit/types.py` (frozen DTOs: `ActorSnapshot`, `SubjectRef`, `AuditRecordData`, `AuditRecord`, `AuditQuery`, `AuditPage` — pure, no Django imports, JSON-serializable payload proven by test), `audit/repositories.py` (`AuditRepository` ABC, append+read only, no update/delete), `audit/__init__.py` exports. Full suite green (2644 passed); no migration.

### Phase 2 — Audit model + through table + migration ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, several should-fix applied; admin-stub finding rejected — plan defers admin to Phase 6 as repository-backed read-only)
- **Model used**: claude-sonnet-4-6 (plan tier: Tier 2 — bumped to Tier 3 model given the bespoke multi-tenant OrganizationForeignKey + migration risk)
- **Commits**: `f367e1d` model+migration+factory+tests, `ad1a29e` org-leading indexes/constraint + tenant-boundary tests
- **Summary**: `Audit(OrganizationModel)` + `AuditAffectedMembership(OrganizationModel)` through table (tenant-safe `OrganizationForeignKey` dual-field pattern, `through_fields=("audit_fk","membership_fk")`). Migration `0001_initial` with 4 org-leading `Audit` indexes, through-table `(organization, membership_fk)` index, and `uniq_audit_membership` on `(organization, audit_fk, membership_fk)`. `AuditFactory`/`AuditAffectedMembershipFactory` (model_bakery). Full suite green (2661 passed); reverse path clean.
- **Plan deviation**: index list refined to lead every composite key with `organization` (project convention the plan's index list overlooked). Unscoped reads (Phase 3/admin) use the established `Audit.original_manager`. Cross-org through-table links persist without rejection (documented project-wide ForeignObject limitation; scoped reads via the ForeignObject won't surface them).

### Phase 3 — DjangoORMAuditRepository ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, several should-fix — all applied)
- **Model used**: claude-sonnet-4-6 (plan tier: Tier 3)
- **Commits**: `4a0fc94` add+get, `f2274d5` query, `91bbed7` harden add()/lock diff invariant/strengthen tests
- **Summary**: `DjangoORMAuditRepository(AuditRepository)` — `add` (atomic Audit + bulk_create through rows, dedup ids), `get` (unscoped `original_manager`, prefetch), `query` (full AuditQuery translation: actions__in, actor_type/id, subject_type/id, affected_membership via through-join + distinct, created_at gte/lt, has_diff, int-safe search, whitelisted ordering, total before pagination), single-source `_to_record`. 54 repo tests; full suite green (2715 passed).
- **Cross-phase contract locked**: `diff` is `None` or a non-empty dict — `add()` normalizes `{}`→`None` so `has_diff` (diff__isnull) is meaningful; Phase 4's `compute_diff` must return `None` for no-change.
- **Note for Phase 6**: `_ALLOWED_ORDERING_FIELDS` only allows `created_at`/`-created_at`; admin must extend it for other orderings.

### Phase 4 — DI wiring + compute_diff ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, 3 should-fix — all applied)
- **Model used**: claude-haiku-4-5 (plan tier: Tier 2)
- **Commits**: `c5788cf` compute_diff, `e0e05c3` wire audit_repository into DI, `fa14d63` test edge cases
- **Summary**: `audit/diff.py` `compute_diff(before, after)` → `{field:{old,new}}` for changed/added/removed keys, **returns `None` for no-change** (upholds locked invariant), None-for-absent convention documented. `audit_repository = providers.Singleton(DjangoORMAuditRepository)` wired in `di_core/containers.py`. 21 diff tests + 2 container tests; full suite green (2738 passed).
- **Deviation**: `audit_service` provider deferred to Phase 5 (where `AuditService` is defined) — wiring it here would import a nonexistent symbol and break the container.

### Phase 5 — AuditService.record + Celery task ✅
- **Status**: complete, all 3 review layers passed (reviewer: 1 BLOCKER [missing transaction.on_commit] + several should-fix — all fixed)
- **Model used**: claude-sonnet-4-6 (plan tier: Tier 3)
- **Commits**: `261b4e3` AuditService, `6530150` Celery task, `4bba9c1` wire audit_service, `c13fbe0` on_commit dispatch + harden error handling
- **Summary**: `AuditService` (DI Factory) with synchronous actor builders (`actor_from_membership`/`_system_user`/`_single_use_code`/`system_actor` — scopes eagerly evaluated from `available_resources`), `record(...)` builds a JSON-safe payload (`dataclasses.asdict`) and dispatches `persist_audit_record` via `transaction.on_commit` (enqueue errors swallowed+logged inside the callback). `audit/tasks.py` `persist_audit_record` resolves the repository from the DI container at runtime (avoids @inject import-before-wiring issue), reconstructs the DTO defensively, logs+swallows failures. `audit_service` wired in DI. 34 service/task tests incl. snapshot-at-emit proof; full suite green (2772 passed).
- **Key fixes**: BLOCKER — wrapped dispatch in `transaction.on_commit` (record() runs under ATOMIC_REQUESTS; a bare `.delay()` could persist audits for rolled-back actions); removed redundant `@inject` (was emitting DIWiringWarning); broadened task except; reworked tests to use `django_capture_on_commit_callbacks`.

### Phase 6 — Repository-backed admin: list + filters ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, 1 real bug + test-quality should-fix — all fixed)
- **Model used**: claude-sonnet-4-6 (plan tier: Tier 3)
- **Commits**: `1bdfb47` admin changelist + filters + tests, `5b172ef` fix empty-filter + harden tests
- **Summary**: `AuditAdmin(ModelAdmin)` registered for `Audit` as a shell (auth/nav/perms) but with `changelist_view` fully overridden to read via `container.audit_repository().query(...)` and render a custom template (`admin/audit/audit/change_list.html` extending `admin/base_site.html`) — Django's ORM ChangeList bypassed. Read-only (add/change/delete perms all False; POST to change/delete rejected). Filters: action, actor_type, created_at range, has_diff, organization_id; pagination via page/per_page → offset/limit. Backend-agnosticism proven by a stub-repository override test (also asserts GET→AuditQuery translation). 30 admin tests; full suite green (2802 passed).
- **Bug fixed**: empty submitted filter (`actor_type=`) was filtering to 0 results; `_first` now normalizes empty→None.
- **Note**: `get_queryset` uses `original_manager` only for Django's internal change/delete plumbing (read-only); the LIST is exclusively repository-driven. Phase 8 replaces the per-object view with a repository-driven detail.

### Phase 7 — Admin search ✅
- **Status**: complete, all 3 review layers passed (reviewer: 0 blockers, 1 DRY should-fix — fixed)
- **Model used**: claude-haiku-4-5 (plan tier: Tier 2)
- **Commits**: `c44bfad` search + affected-membership filter, `d?` dedup stub (test(audit): deduplicate stub repository)
- **Summary**: Wired `search` (→ AuditQuery.search; repo ORs subject_* + numeric actor_id) and `affected_membership_id` (→ through-join filter) into the admin changelist — `_build_audit_query` + `active_filters` + `base_querystring` (pagination preserves them) + two template inputs. No new query logic (repository handles it). 18 search tests; full suite green (2820 passed).

## Current phase
- Phase 8 — Admin read-only detail (Tier 2, implementer) — NEXT (after DI amend)

## Amendment applied (2026-06-19) — DI method-argument injection ✅
- **Trigger**: user request to use `@inject` + `Provide` method/constructor-argument injection like the project's other services/tasks, instead of runtime `di_core.containers.container` resolution.
- **Path**: amend-plan refuses modular-commits force-push, so resolved as a **forward corrective commit** on `plan/audit-trail` (no history rewrite). Plan amended (Guiding Decisions "DI injection style" row + Phase 5/6 bodies + Amendments log).
- **Commits**: `b6aed1a` plan amendment, `2037969` DI rework (service/task/admin/container), `07f37c9` wiring test + DI test polish.
- **Result**: `AuditService.__init__` injects `repository` via `@inject` (container = `providers.Factory(AuditService)`, no explicit dep — `webhook_service` pattern); `persist_audit_record` task uses `@app.task`+`@inject` (`webhooks/tasks.py` pattern) with a `None` guard; admin `changelist_view` injects via `@inject`. All runtime container resolution removed. **Root cause of the original failure found**: `from __future__ import annotations` (PEP 563) stringified annotations so dependency-injector couldn't see `Provide[...]` markers — removed from the 3 modules. DIWiringWarning eliminated. Full suite green (2822 passed).
- **Reviewer note rejected**: restoring explicit `repository=audit_repository` on the Factory was declined — it would reintroduce the DIWiringWarning and diverge from the `webhook_service` convention; the single-global-container model is project-wide and `.override()` works on it (stub tests pass).

## Remaining phases
- Phase 2 — Audit model + through table + migration (Tier 2, migration-author)
- Phase 3 — DjangoORMAuditRepository (Tier 3, implementer)
- Phase 4 — DI wiring + compute_diff (Tier 2, implementer)
- Phase 5 — AuditService.record + Celery task (Tier 3, implementer)
- Phase 6 — Admin list + filters (Tier 3, implementer)
- Phase 7 — Admin search (Tier 2, implementer)
- Phase 8 — Admin read-only detail (Tier 2, implementer)
- Phase 9 — Admin CSV export (Tier 2, implementer)

## Deferred phases
_(none — no cross-repo phases, no flag-removal phase)_
