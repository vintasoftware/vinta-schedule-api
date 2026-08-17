# Tracking — External Client Identifiers

- **Feature**: External Client Identifiers
- **Plan**: `ai-plans/2026-08-17-EXTERNAL_CLIENT_IDENTIFIERS_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-08-17
- **Last updated**: 2026-08-17
- **Feature flag**: none. The plan's "No feature flag" decision stands in a
  no-op-when-omitted regression test on every phase that touches an existing write path.

## Run options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config default (auto-flow) |
| `generate_inline_comments` | `false` | config default |
| `full_test_suite` | `false` (scoped) | user answer |
| `commit_strategy_resolved` | `stacked-branches` | user answer |
| `use_worktree` | `true` | config default |
| `worktree_path` | `.claude/worktrees/plan-external-client-identifiers` | prepare-worktree |
| `worktree_branch` | `plan-external-client-identifiers` | prepare-worktree |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-external-client-identifiers.yaml` | prepare-worktree |
| `sandbox_tier` | `enforced` (not wrappable — see below) | prepare-worktree probe |

**Sandbox caveat.** `sandbox-exec` is available, but this runtime spawns subagents
in-process, so the per-spawn `sandbox-run.sh` wrap does not apply. Main-checkout
write prevention falls back to the review-phase stray-write check: the conductor runs
`git -C <main_checkout> status --short` after every implementer and every fixer.

**Worktree fixes applied before Phase 1** (both were provisioning gaps that would
have failed every phase):

1. `virtualenv` compose volume was left shared with the main checkout, so `uv run`
   died with `failed to remove directory /opt/venv: Permission denied`. Forked it to
   `vinta-schedule_wt_plan-external-client-identifiers_virtualenv`, matching
   `shared_volumes: []` in `.vinta-ai-workflows.yaml`.
2. `vinta_schedule_api/settings/local.py` is gitignored and was not copied, so the
   `backend-schema-local` pre-commit hook raised `ModuleNotFoundError` on every
   commit. Copied it in.

Verified after the fixes: `migrate` applies cleanly and `pytest` collects 2042 tests.

## Model assignments

Implementer models come from each phase's `**Suggested AI model**:` line; reviewer /
fixer / integrate come from `agent_models` in `.vinta-ai-workflows.yaml`. Only the
anthropic vendor is exposed by this runtime.

| Role | Tier | Model |
|---|---|---|
| Phase 1 implementer | 2 (stepped up — touches 6 files) | `claude-sonnet-5` |
| Phase 2 implementer | 3 | `claude-sonnet-5` |
| Phase 3 implementer | 3 | `claude-sonnet-5` |
| Phase 4 implementer | 2 (stepped up — touches 5 files) | `claude-sonnet-5` |
| Phase 5 implementer | 3 | `claude-sonnet-5` |
| Phase 6 implementer | 3 | `claude-sonnet-5` |
| Reviewer (default) | 3 | `claude-sonnet-5` |
| Reviewer (Phase 6 override) | 4 | `claude-opus-4-8` |
| Fixer | 2 | `claude-haiku-4-5` |
| Integrate | 1 | `claude-haiku-4-5` |

## Completed phases

### Phase 1 — Add the ExternalClientIdentifier table ✅

- **Branch**: `plan/external-client-identifiers/phase-1` (base: `plan-external-client-identifiers`)
- **Implementer**: `claude-sonnet-5` (plan Tier 2, stepped up — 6 files)
- **Reviewer**: `claude-sonnet-5` (Tier 3) — 0 BLOCKER, 4 SHOULD-FIX, 2 NIT
- **Fixer**: `claude-haiku-4-5` (Tier 2)
- **Commits**: `61f7cf0b` (table), `fa0dfc89` (normalize_system fixes)
- **Diff**: 6 files, +575

Shipped the `ExternalClientIdentifier` table exactly as **Data Model Changes** specifies:
`SingleOrganizationModelMixin` + `SafeRelationNullInitMixin` + `BaseModel`, a
`content_type` / `identified_key` generic FK pair, both unique constraints
(`extclientid_uniq_target_system`, `extclientid_uniq_system_ident`) and the lookup index
(`extclientid_org_ct_key_idx`). `GenericRelation` on `CalendarEvent` and
`ExternalAttendee` gives the cascade, including the parent-driven case
(`Calendar` → `CalendarEvent` → identifiers) that a `post_delete` signal would miss.
Also: `IDENTIFIABLE_MODELS` + `normalize_system`, an org-scoped factory, and an editable
admin whose `clean_system` normalizes.

`makemigrations` folded the constraints and indexes into a single `CreateModel`, which is
Django's normal output for a brand-new table (precedent: `0039_bookingpolicy.py`). No
`AddIndexConcurrently` and no `atomic = False`, as the phase requires.

**Fixes applied from review** (all in `normalize_system`, the function both unique
constraints depend on):

- It was **not idempotent**. `https://crm.example.com//` normalized to
  `https://crm.example.com/`, which normalized again to `https://crm.example.com` — so one
  logical system could occupy two rows, defeating the uniqueness the feature is built on.
  Now strips all trailing slashes.
- `netloc.lower()` **mangled userinfo**, lowercasing credentials. Now lowercases only the
  host. Verified live: `https://User:Pass@CRM.Example.com/api` →
  `https://User:Pass@crm.example.com/api`; port, path, query and fragment case all preserved.
- Tests for both, plus an idempotency test. 27 tests in the file.

**Verification** (all re-run by the conductor inside the worktree):
`ruff check` clean · `ruff format --check` 642 files · `makemigrations --check` no changes ·
`check --deploy` 0 errors (5 pre-existing dev warnings) · `mypy` 291 pre-existing errors,
0 new, new module clean · **`pytest calendar_integration/tests/` → 2069 passed, 0 failed**
(2042 baseline + 27) · migration **reverses and reapplies** cleanly.

**Declined from review — admin `get_queryset` uses the org-scoped manager.** The reviewer
proposed switching to `original_manager`, citing `audit/admin.py`. Not applied, for three
reasons: the plan's **Risk & Rollout Notes** call a new read path reaching for
`original_manager` a blocker; `AuditAdmin`'s bypass is justified by its being read-only
(all three permission methods return `False`) whereas this admin is editable by plan
decision, so the same bypass would expose cross-organization editing; and it is not a
regression — all 11 `get_queryset` overrides in `calendar_integration/admin.py` behave
this way and none use `original_manager` (only `organizations/` and `audit/` do). Tracked
as a follow-up below.

**NITs not actioned**: `extclientid_uniq_target_system` is exactly 30 characters, Django's
limit — it applies fine, but leaves no headroom for a future rename. `normalize_system`'s
docstring still says "strip a trailing slash" (singular) and does not mention userinfo
handling; it matches the plan's own wording but now understates the behavior.

### Phase 2 — Identifier write service, normalization, validation, audit ✅

- **Branch**: `plan/external-client-identifiers/phase-2` (base: `plan/external-client-identifiers/phase-1`)
- **Implementer**: `claude-sonnet-5` (plan Tier 3)
- **Reviewer**: `claude-sonnet-5` (Tier 3) — 0 BLOCKER, 3 SHOULD-FIX, 2 NIT
- **Fixers**: `claude-sonnet-5` (escalated from Tier 2 — ordering logic inside an existing
  reconciliation loop, not a mechanical edit)
- **Commits**: `68e5cf67` (service), `a5d937fb` (attendee-ordering BLOCKER), `13a8e818` (review fixes)
- **Diff**: 9 files, +1969 / −11

`ExternalClientIdentifierService` owns `replace_for_target(target, identifiers | None)` and
`get_for_targets(targets)`. It validates the target's allowlist and organization, normalizes
`system`, rejects blank / whitespace-only / over-length identifiers, then diffs the stored set
against the incoming one and applies a minimal delete + create. Registered in
`di_core/containers.py` and threaded through `CalendarServiceContext` — never direct-imported.
`create_event` and `update_event` call it for the event and each external attendee. The audit
diff gains an `external_client_identifiers` key only when the set actually changed.

**BLOCKER found by the conductor and fixed** (`a5d937fb`). `update_event` applied
external-attendee identifiers *before* deleting the attendees being replaced.
`extclientid_uniq_system_ident` is unique on `(organization, content_type, system, identifier)`
and is not scoped to a single target, so a re-sent attendee whose id the caller omitted —
deleted and recreated by the reconciliation loop — collided with its own about-to-be-deleted
predecessor:

```
UniqueViolation: duplicate key value violates unique constraint "extclientid_uniq_system_ident"
```

Re-sending an attendee with an unchanged CRM id is the ordinary case. The phase's original test
for this path passed only because it changed the identifier value (`old-contact` → `new-contact`),
which sidesteps the collision. Reproduced with a probe that differed by exactly that one value.
The fix moves identifier application after the stale attendees are deleted, and adds a clear-pass
before the write-pass so two attendees swapping identifiers in one payload also work — that second
case turned out to be real, not hypothetical. Both have regression tests, and the conductor
verified both **fail** against the pre-fix service file and **pass** after.

**Review fixes** (`13a8e818`):

- **N+1 on the omitted path.** The write-pass called `replace_for_target(attendee, None)`
  unconditionally, and the service runs its SELECT before the `None` check — so every
  `update_event` gained one query per attendee even when the caller never mentions identifiers,
  undercutting the "byte-identical when omitted" requirement. Guarded at the call sites, not
  inside the service, because the service deliberately validates org and allowlist even on no-op
  calls and two tests pin that. Pinned by a `django_assert_num_queries` test (28 with the guard,
  29 without).
- **Duplicate `system` in one payload now raises** `ExternalClientIdentifierDuplicateSystemError`
  instead of silently collapsing to last-wins. Under last-wins a client sending
  `[{crm, "A"}, {crm, "B"}]` got `B` stored while believing `A` was set, and a later lookup by `A`
  returned nothing. Phase 3 hands this list straight through from an external API token, so the
  ambiguity had to surface as an error before that ships. Comparison is on the normalized system,
  so the same system spelled two ways in one payload is caught. **This is a deliberate addition to
  the plan's Phase 3 error list** — Phase 3 must document it.
- **`get_for_targets` cross-product guard** now has the test that makes it load-bearing. The query
  filters content types and pks as separate `__in` sets; only an `if key in result` guard stops one
  target's rows leaking into another's. The query itself was left alone — the per-pair `Q` rewrite
  is an optimization with no caller until Phase 3.
- Two docstring corrections, including one where a test's docstring contradicted what the adjacent
  test asserts.

**Verification** (all re-run by the conductor inside the worktree): `ruff check` clean ·
`ruff format --check` 645 files · `makemigrations --check` no changes · `check --deploy` 0 errors ·
`mypy` 291 pre-existing, 0 new · **`pytest calendar_integration/tests/` → 2107 passed, 0 failed**
(2102 + 5). Duplicate-system rejection verified live against a running Django shell.

### Phase 3 — Public GraphQL read, create-time write, and lookup ✅

- **Branch**: `plan/external-client-identifiers/phase-3` (base: `plan/external-client-identifiers/phase-2`)
- **Implementer**: `claude-sonnet-5` (plan Tier 3)
- **Reviewer**: `claude-sonnet-5` (Tier 3) — 1 BLOCKER, 6 SHOULD-FIX, 2 NIT
- **Fixer**: `claude-sonnet-5` (escalated from Tier 2 — resolver control flow + recurrence semantics)
- **Commits**: `ccae1fe2` (GraphQL surface), `3ce99787` (BLOCKER + review fixes)
- **Diff**: 7 files, +981 / −2 (plus the fix commit)

`ExternalClientIdentifierGraphQLType` is exposed on `CalendarEventGraphQLType` and
`ExternalAttendeeGraphQLType`. `ScheduleEventInput` and `ScheduleEventExternalAttendeeInput`
gain `strawberry.UNSET`-defaulted identifier fields mapped to `None` for the Phase 2
dataclasses. `calendarEvents` gains the `(system, identifier)` filter pair with
both-or-neither validation, applied after owner-scope narrowing, plus a new standalone
identifier-only lookup mode.

**BLOCKER — N+1 on the two list branches.** The `userId` and `calendarId` branches return
already-materialized `list[CalendarEvent]`, so strawberry-django's optimizer has no lazy
queryset to attach the prefetch to and the field-level `prefetch_related=[...]` hints are
inert there. Selecting identifiers over N events issued N extra queries — precisely what
Phase 3 item 3 exists to prevent, in the branches where it matters most, and untested.
Root cause ran deeper than the call sites: `optimize_queryset` was applied only to the
recurring-master queryset, never to the non-recurring one, so one-off events N+1'd
regardless. Fixed by applying it to both, and passing a prefetch callable from both
resolver call sites. **Conductor-verified**: with the service fix reverted the regression
test fails with `Expected to perform 9 queries but 11 were done`; restored, it passes.

**The identifier filter was silently dead for recurring series.** The expansion methods
deliberately exclude the recurring **master** from their output, and the master is the row
that carries the identifier — so `e.id in matching_event_ids` dropped everything. A
consumer who tagged a recurring series and filtered by it with a `calendarId` and a window
got an empty list, indistinguishable from "no such identifier". Rather than failing loud
(the reviewer's suggestion), the filter now means what a consumer intends: an occurrence
survives when its master matches. Occurrences come in two shapes — persisted
modified-occurrence exceptions carry `parent_recurring_object_fk_id`, plain generated
occurrences carry `recurrence_rule_fk_id` — so both are matched, with no per-occurrence
query. Safe because `recurrence_rule` is a `OneToOneField`, so a rule id maps to exactly
one master.

**Other fixes**: coverage for the combined `calendarId`/`userId` + identifier modes (which
had none); an explicit `external_client_identifiers__organization` filter on the identifier
join (defense in depth — the reviewer confirmed no live leak, since the driving table is
org-filtered and pks are globally unique); documentation of all five error cases on the
input type and fields so introspection surfaces them; two NITs.

**Verification** (conductor re-ran): `ruff check` clean · `ruff format --check` 646 files ·
`makemigrations --check` no changes · `check --deploy` 0 errors · `mypy` 291 pre-existing,
0 new · **full repo `pytest -n auto` → 6068 passed, 0 failed** (6063 + 5). The full suite
was run instead of the scoped one because this phase modified a shared service method used
beyond `public_api`.

**Process notes on this phase** (no code impact, but worth knowing):

1. The implementer's report **contradicted its own diff**, claiming the `userId`/`calendarId`
   branches left the identifier filter unapplied when both branches apply it. The code was
   correct; the report was not. Caught by reading the diff rather than the summary.
2. The implementer **silently extended Phase 2's already-shipped service** with URL-format
   validation (`ExternalClientIdentifierInvalidSystemError`) inside Phase 3's commit. The
   change is necessary and correct — the plan's error list requires it, and `bulk_create`
   bypasses `full_clean()` so the model's own `URLField` validators never ran on any write
   path — but it should have been declared as an amendment to Phase 2 rather than folded in.

**Deferred from review**: the reviewer recommended extracting a shared
`_apply_owner_scope(qs, request, org)` helper and splitting `calendar_events` into per-mode
helpers — the resolver now has four branches with a duplicated owner-scope block, and it
warned that a future edit to one copy and not the other is how an owner-scope bypass gets
introduced. Deferred so the BLOCKER fix stayed reviewable. See follow-ups.

### Phase 4 — Carry event identifiers in webhook payloads ✅

- **Branch**: `plan/external-client-identifiers/phase-4` (base: `plan/external-client-identifiers/phase-3`)
- **Implementer**: `claude-sonnet-5` (plan Tier 2, stepped up — 5 files)
- **Reviewer**: `claude-sonnet-5` (Tier 3) — 0 BLOCKER, 3 SHOULD-FIX, 2 NIT
- **Fixer**: `claude-sonnet-5`
- **Commits**: `51867b76` (payload plumbing), `15665283` (query fix + tests)

`CalendarEventWebhookPayload` gains `external_client_identifiers`. Both builders in
`calendar_service_utils.py` populate it — the ORM path from the prefetched generic relation,
the input path from the input data. `_serialize_event` always emits a list: `[]` when there
are none, never `null`, never absent. `WEBHOOK_EVENT_DESCRIPTIONS` updated for the three
event webhooks, and (from review) for the three attendee webhooks, whose embedded event
object now carries the field.

**The delete-snapshot trap.** `delete_event` already serialized before deleting, so the real
risk was subtler: `serialize_event` must **materialize** identifiers at call time. A lazy
queryset stored on `CalendarEventData` would only evaluate later, inside the
`transaction.on_commit` callback that builds the payload — by which point the
`GenericRelation` cascade has removed the rows and every deletion webhook would ship `[]`.
Proven by substituting the lazy expression and watching the test go red.

**Review caught a real regression hiding behind a bumped assertion.** Phase 4 raised a
Phase 2 test's pinned query count 28 → 29, justified as "one unavoidable query". It was not:

- The captured query feeds `can_perform_update`, which never reads identifiers — so it is
  wasted, not unavoidable.
- The assertion is not wrapped in `django_capture_on_commit_callbacks`, so it never ran the
  deferred `call_side_effects()` closure at all. That is where the real cost lives:
  `_serialize_event(event)` runs once for the update **plus once per attendee dispatch**,
  each re-issuing an uncached identifier query. A production `update_event` with N attendee
  dispatches paid N extra queries that no test measured.

Fixed by prefetching once **inside** the closure, after `replace_for_target` has written the
new identifiers. The fixer's first attempt put the prefetch at the top of `update_event` and
caught its own error via the test — that placement caches the *pre-write* identifiers, so
the first update that sets them would ship an empty payload. **Conductor-verified**: the new
on-commit test pins 75 queries and reports 77 with the prefetch removed. The saving is flat
(1 query) regardless of attendee count, where the old code was linear.

Also added: a test for the `parent_recurring_object` fallback (a webhook for a modified
occurrence of a tagged series carries the master's identifiers), which had zero coverage.

The 28 → 29 bump on the synchronous test was **kept**, not reverted — that path legitimately
reads pre-write identifiers and cannot share the closure's cache. Its docstring now says
exactly which query it is and why it cannot be removed.

**Verification** (conductor re-ran): `ruff check` clean · `ruff format --check` 647 files ·
`makemigrations --check` no changes · `check --deploy` 0 errors · `mypy` 291 pre-existing,
0 new · **full repo `pytest -n auto` → 6087 passed, 0 failed**.

**Ship value is currently dormant — see follow-up 4.** Phase 4's code is correct and the
reviewer confirmed it is not masking another defect, but calendar webhooks do not dispatch
at all in the container-wired path today.

## Current phase

**Phase 5 — Internal REST read, write and filtering**
- Branch: `plan/external-client-identifiers/phase-5`
- Base: `plan/external-client-identifiers/phase-4`
- Status: not started

## Follow-ups (not blocking this plan)

1. **`calendar_integration/admin.py` org-scoping under `STRICT_ORGANIZATION_FILTER`.** All
   11 admins in that file call `super().get_queryset(request)`, which resolves to the
   org-scoped default manager. With no organization bound to a staff request, the
   changelist raises `OrganizationNotFoundError` (500) rather than listing rows.
   Pre-existing and repo-wide within that file; deserves one uniform fix across all 11,
   not a per-model bypass. `audit/admin.py` documents the failure mode in detail.

2. **`CalendarEventGraphQLType.id` is non-nullable, but generated recurring occurrences
   have `id=None`.** Selecting `id` on a pk-less occurrence through the public GraphQL API
   raises `Cannot return null for non-nullable field`. Surfaced while writing Phase 3's
   recurring tests, which had to query `title` / `startTime` instead. **Entirely
   pre-existing** — `id: strawberry.auto` (`calendar_integration/graphql.py:347`) is
   untouched by any branch in this plan, and no prior test exercised recurring occurrences
   through a real unmocked expansion over GraphQL. It means any consumer selecting `id`
   while listing a calendar containing a recurring series gets an error. Worth its own
   investigation; out of scope here.

3. **Refactor `calendar_events` in `public_api/queries.py`.** Four branches plus a
   pre-computed `matching_event_ids`, with the owner-scope block duplicated across two of
   them. Extract `_apply_owner_scope(qs, request, org)` and consider per-mode private
   helpers. Flagged by the Phase 3 reviewer as the shape in which an owner-scope bypass
   would eventually be introduced by editing one copy and not the other.

4. **Calendar-event webhooks never dispatch — `di_core/containers.py:225`.** HIGH IMPACT,
   and a production decision rather than a code one. The provider is wired as

   ```python
   side_effects_pipeline=(webhook_calendar_side_effects_service,)   # tuple, not providers.List
   ```

   `dependency_injector` only auto-resolves a provider passed as a direct kwarg; nested in a
   plain tuple it stays unresolved. Confirmed live by both the conductor and the Phase 4
   reviewer: resolving the container yields a raw `Factory` object in the pipeline, and
   `isinstance(handler, OnCreateEventHandler)` is `False`. Every dispatch method guards on
   exactly that `isinstance`, so the handler is silently skipped. Nothing outside the
   container constructs `CalendarSideEffectsService`, so there is no other production path,
   and every existing test passes a `Mock()` or `None` — which is why it went unnoticed.
   `git log -S` dates the line to **2025-09-08**. Untouched by this plan.

   All six calendar-event webhook types (created, updated, deleted, attendee
   added/removed/updated) appear dead end to end.

   **Deliberately not fixed here.** The change is one line, but flipping it on would begin
   delivering webhooks that have been silent for ~11 months; partners with registered
   subscriptions could see a sudden burst. That needs a product and partner-communication
   decision, plus a check on whether any queue drains retroactively. Until it is fixed,
   Phase 4 ships correct-but-dormant code — the reviewer confirmed the payload logic itself
   is right and is not masking a second defect.

5. **Webhook and GraphQL disagree about recurring occurrences.** A webhook for a persisted
   modified occurrence of a tagged series carries the master's identifiers (via
   `serialize_event`'s `parent_recurring_object` fallback), but the same occurrence read
   through `CalendarEventGraphQLType.external_client_identifiers` returns `[]` — that field
   has no parent fallback. Systemic on that type rather than identifier-specific:
   `attendances`, `external_attendances` and `resources` lack the same fallback. Not fixed
   in Phase 4, because making identifiers the lone exception would trade one inconsistency
   for a subtler one. Worth resolving uniformly across the type.

6. **`delete_event` has the same serialize-per-dispatch shape** as `update_event` did, in
   its own `call_side_effects` closure. Moot today — delete does not reconcile attendees, so
   there are no extra dispatches — but the pattern is copy-pasted and would pay the same
   per-dispatch cost if that ever changes.

## Remaining phases

| Phase | Title | Depends on |
|---|---|---|
| 5 | Internal REST read, write and filtering | 1, 2 |
| 6 | Public `updateCalendarEvent` mutation | 1, 2, 3 |

## Deferred phases

None. The plan declares no cross-repo phase and no feature flag, so there is no
flag-removal phase to defer.
