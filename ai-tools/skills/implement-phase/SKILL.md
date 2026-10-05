---
name: implement-phase
description: Internal execution step of [implement-plan] / [amend-plan] — NOT a standalone entry point. Given one already-classified plan phase plus the resolved `WORKROOT` / `BASE_BRANCH` / `SANDBOX_TIER`, it composes a token-efficient implementer prompt, picks the model from the phase's own suggestion, and spawns exactly one implementer subagent, returning that agent's report. Do not invoke directly for ad-hoc edits or in response to a user's raw feature request; the conductor invokes it once per executable phase in vinta_schedule_api.
disable-model-invocation: true
---

# Implement one phase

Execution unit invoked by [implement-plan](../implement-plan/SKILL.md) (and by [amend-plan](../amend-plan/SKILL.md) for `amend-existing` rewrites). One phase in → one implementer report out. This skill does **not** review, branch, push, or open PRs — those are [review-phase](../review-phase/SKILL.md) and the resolved integrate-phase variant ([integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) / [integrate-phase-modular](../integrate-phase-modular/SKILL.md)). It also does **not** decide whether a phase runs — the conductor already filtered cross-repo / flag-removal phases.

## Not for an agent that was handed one phase

This skill **orchestrates**: it composes prompts, picks models and spawns other agents. Run it only when you are the session a user or a scheduler invoked to drive a plan.

If you are reading it because something handed you a single phase — a prompt naming your phase id, a branch already cut for you, a worktree you were told to stay inside, or an orchestrator such as [vinta-ai-maestro](https://github.com/vintasoftware/vinta-ai-workflows/tree/main/packages/vinta-ai-maestro) that spawned you — then the conductor this skill describes **is already running, and it is what spawned you**. Do not start a second one underneath it. Do the work in your own session, report back the way your prompt asked, and take from here only what it says about this repository's conventions, gates and commit rules.

The duplication is the smaller cost. A dispatched agent is deliberately reused — the same session takes the review findings, the chore over its own diff, and often the next phase — and that reuse is worth something only because the session that read the codebase is the session that gets the next turn. Hand your phase to a sub-agent and its reading of the code dies with it: you are left holding a summary, and every turn after yours starts cold.

## Inputs (passed by the conductor as data — this skill re-derives none of them)

- `phase` record: `{ id, title, goal, body, spec_use_case, depends_on, wave, base_branch, crew_member, crew_tier, suggested_model_tier, reusable_skills, acceptance }`. `crew_member` / `crew_tier` come from the phase's `**Assigned to**:` line and the plan's **Crew** table; `suggested_model_tier` is the legacy path and is set only on a plan with no roster. Exactly one of the two is populated.
- Plan-level decisions: **Goals + Non-goals**, **Guiding Decisions**, the relevant **Data Model Changes** subsection.
- **Dependency-closure summaries** — the `phase-{id}.md` tracking entries for this phase's transitive dependencies, and only those. Not "everything finished so far": under parallel execution a sibling lane's work is not in this phase's base branch, and describing it as done makes the implementer code against files it cannot see.
- `WORKROOT`, `SANDBOX_TIER` — **this lane's**, resolved by the conductor ([Resolve WORKROOT step](../implement-plan/SKILL.md#step-05--resolve-workroot)). `phase.base_branch` — the branch the conductor already created this phase's branch from, derived from `depends_on`.
- `run_options.full_test_suite` — resolves the outer gate's test scope in the composed prompt's `{If run_options.full_test_suite = true:}` marker (false = scoped suite only; true = full repo suite).

## 1. Compose the agent prompt (token-efficient)

Compose with **only what the agent needs**:

```
You are implementing {phase.id}: {phase.title} of plan {plan.id}.

## Repo
vinta_schedule_api (Django 6 + DRF + Strawberry GraphQL + Celery, multi-tenant (SingleOrganizationModelMixin), Postgres, deployed to AWS ECS/Fargate).

## Working location
Work entirely inside `<WORKROOT>`. `cd` into it before any command. Every `git`,
every lint / test / build / migrate call runs there.
{If run_options.use_worktree = true:}
  `<WORKROOT>` is an isolated git worktree — do NOT touch the main checkout; its DB,
  env, and compose stack are intentionally separated. See `<WORKROOT>/WORKTREE.md` for
  what's forked vs shared (deps, dev DB, test DB, compose project name, env file).
  {If run_options.sandbox_tier = enforced:} Writes outside this worktree are OS-blocked —
  if you see `Operation not permitted` / `EROFS` on a write, you used a path outside
  `<WORKROOT>` by mistake; redo it against this worktree path.
{If run_options.parallel_phases = true:}
  Other phases of this plan are being implemented **right now**, in sibling worktrees
  next to yours. Never read or write any path outside `<WORKROOT>` — a sibling's tree is
  mid-edit and mid-test, and a write there corrupts someone else's phase. Anything you
  need from another phase is either already in your base branch or is a dependency the
  plan failed to declare — say so in your report rather than reaching for it.
Branch base for this phase: `<phase.base_branch>` — derived from this phase's
**Depends on** set, not from plan order. The orchestrator already created your phase
branch there; commit straight to it.

## Read first
1. AGENTS.md — repo conventions.
2. ai-plans/{plan-filename}, the **Goals + Non-goals**, **Guiding Decisions**, **Data Model Changes** sections and YOUR phase body inside **Phased Rollout**.
{If run_options.use_worktree = true:} 3. `WORKTREE.md` at the worktree root — fork map (what is forked, copied, or shared with main). Dependency dirs are always this worktree's own copy. Install / cache writes there never reach main.

## Plan-level decisions (from Goals + Non-goals + Guiding Decisions)
{Goals + Non-goals verbatim}
{Guiding Decisions table verbatim}
{If feature flag declared:}
  Feature flag: `{flag-key}` — scope `{per-tenant|per-request}`, default `{false|true}`.
  Wire reads + writes per the plan's **Guiding Decisions** entry. Off-flag path = byte-for-byte pre-feature behavior.

## What your phase builds on
{The `phase-{id}.md` tracking summaries for this phase's transitive dependencies, in
wave order. No dependencies: "Nothing yet — this phase starts from `<BASE_BRANCH>`."
Sibling phases running in parallel are deliberately NOT listed: their work is not in
your base branch and you must not code against it.}

## Your tasks (Phase {id} only)
{phase.body verbatim, including Goal / Spec use-case / Feature flag / Changes / Tests / Acceptance lines}

## Reusable skills you SHOULD invoke
{phase.reusable_skills — for each, instruct the agent to first read ai-tools/skills/{name}/SKILL.md, then follow that pattern.}

Project skills available: plan-feature, create-spec, open-pr-from-context, prepare-worktree, implement-plan, implement-phase, review-phase, integrate-phase-stacked, integrate-phase-modular, amend-plan, systematic-debugging, deslop-comments, handoff, handoff-to-client, thermo-nuclear-code-quality-review, write-unit-test, pr-review-canvas, add-env-var, add-one-off-script, add-model, add-migration, create-graphql-public-query, create-postgres-function, create-postgres-view, create-rest-endpoint, run-one-off-script-django

## Adding new third-party dependencies

Before running any install command (`npm add`, `pnpm add`, `yarn add`, `pip install`, `poetry add`, `uv add`, `cargo add`, `go get`, `gem install`, equivalents), check the package's SPDX license against the project's forbidden list — see the **Dependency licenses** section in [AGENTS.md](../../../AGENTS.md) for the full list, the per-package overrides, and any project-specific notes.

Quick lookup:

- **npm / pnpm / yarn**: `npm view <pkg> license`.
- **PyPI**: `pip index versions <pkg>` then read the project metadata, or open `https://pypi.org/project/<pkg>/`.
- **Cargo**: `cargo metadata --format-version 1 | jq '.packages[] | select(.name=="<pkg>") | .license'` (after a temporary `cargo add` in a scratch dir, or read `Cargo.toml` upstream).
- **Go**: open the module's repo `LICENSE` file directly.
- **Gem**: `gem specification <pkg> licenses`.

If the license is in the forbidden list AND the `(package, license)` pair is **not** listed under **Approved overrides** in AGENTS.md:

1. Stop. Do not run the install command.
2. Search the ecosystem for an MIT / Apache-2.0 / BSD-licensed equivalent first, so the question can name one.
3. Ask the human (see **Asking the human** in [AGENTS.md](../../../AGENTS.md)). As a sub-agent you cannot ask directly: return `status: NEEDS_INPUT` with one question. Its text carries the package name, SPDX identifier, why it's forbidden, and the upstream license link. Options: `Use <alternative> (Recommended)` (omit when none was found), `Implement without it`, `Record an override`. The orchestrator shows it as a clickable prompt and resumes you with the answer.
4. If the user grants a one-off override, the orchestrator must record it in `policies.dependency_licenses.allowed_overrides[]` of `.vinta-ai-workflows.yaml` (package + SPDX + one-line reason) before re-running the install. Undocumented overrides leak into the diff and the reviewer agent will flag them.

**License unknown / undeclared.** When the lookup above returns no license, an empty value, `UNKNOWN`, `SEE LICENSE IN <file>`, or only an unstructured `LICENSE` file in the repo with no SPDX identifier, treat it as a **policy decision the user owns** — don't guess, don't auto-infer, don't fall back to "assume MIT". The package may be unlicensed (all-rights-reserved by default in most jurisdictions), proprietary, or simply missing metadata.

1. Stop. Do not run the install command.
2. Ask the human. As a sub-agent, return `status: NEEDS_INPUT` with one question (the orchestrator relays it as a clickable prompt; see **Asking the human** in [AGENTS.md](../../../AGENTS.md)). The question text carries the package name, what was found (e.g. "the `license` field is absent in `package.json`", "PyPI metadata returned `UNKNOWN`", "no LICENSE file in the repo"), and the upstream repo / registry URL so the user can verify. Options: `Find alternative (Recommended)`, `Treat as forbidden`, `Record an override` (only when the user has independently confirmed the license off-channel; record the resolved SPDX in `allowed_overrides[]` with the source in the `reason` field, e.g. `"unlicensed but author confirmed MIT via GitHub issue #42"`).
3. Don't add the dep until the user picks one of the three.

Transitive deps follow the same rule, but checking every transitive license at install time is impractical — the project's CI (or a separate license-audit run) handles the deep walk. The subagent's responsibility is the **direct** add.

## When you need a human decision
You run as a subagent. You cannot reach the human, and a question written into
your report gets lost in the transcript. When you hit a decision the plan does not
settle and you should not make alone, do not guess and do not finish with a prose
question. Examples: an ambiguous or contradictory requirement, a dependency the
license policy blocks, a change outside this phase's scope, a destructive or
irreversible step.

Stop at a clean point. Finished, verified work may stay committed; leave
unfinished work uncommitted. Then return this as your whole final report:

    status: NEEDS_INPUT
    blocked_on: <one line: the decision you need>
    done_so_far: <one line: what is finished, and which files it touched>
    questions:        # 1-4 questions; each must make sense without the transcript
      - header: <12 chars max, e.g. "License">
        question: <full question ending in "?", with the evidence needed to answer it: file:line, package, error line>
        multi_select: false
        options:      # 2-4 options; recommended first, its label ending in " (Recommended)"
          - label: <1-5 words>
            description: <what happens if the human picks this>
          - label: <1-5 words>
            description: <what happens if the human picks this>

Do not add an "Other" option. The human always gets a free-text field. The
orchestrator shows your questions as a clickable prompt, then resumes you (or
spawns a new agent) with the answers.

## Working instructions
1. Read existing code paths your changes touch — do not write before reading.
2. Implement using Read/Edit/Write. Match existing patterns.
3. **Inner loop — fast iteration.** Scoped to files/apps you touched:
   a. `docker compose run --rm api uv run ruff check ./` until clean.
   b. `docker compose run --rm api uv run pytest <new-test-path> -vs` for new tests individually.
   c. Scoped suite: `docker compose run --rm api uv run pytest <app>/tests/ -n auto`.
4. Iterate 2–3 until **new tests pass individually** and the scoped suite is green. Do **not** advance to step 5 with red scoped tests.
5. **Outer gate — local verification, only after step 4 is green.** All MUST pass before staging:
   a. **Type / build:** `docker compose run --rm api uv run python manage.py check --deploy` — repo-wide, always.
   b. **Tests:** by default run only the **scoped suite** `docker compose run --rm api uv run pytest <app>/tests/ -n auto` for the apps/files you touched — the new tests already passed individually in step 4b, so this re-confirms the touched surface without paying for the whole repo. When that line contains `{changed_files}` or `{touches}`, it is a template: replace `{changed_files}` with the files this phase changed against its base (`git diff --name-only --diff-filter=d <base>...HEAD` plus uncommitted and untracked work) and `{touches}` with the phase's Touch List, each path shell-quoted and space-separated. If a placeholder would be empty, run `docker compose run --rm api uv run pytest -n auto` instead.
      {If run_options.full_test_suite = true:} run the **full test suite** `docker compose run --rm api uv run pytest -n auto` instead of the scoped suite — this phase guards against regressions in untouched code too.

6. Outer gate fails → return step 2 (fix regression), re-run inner loop, then 5a/5b. **Never** commit, push, or proceed while any gate is red.

## Do this work yourself

You are the agent that implements this phase, not an orchestrator for one. Do not spawn,
dispatch or delegate to a sub-agent (claude-code's Task/Agent tool, or whatever your
runtime calls the same thing) for any part of it: not the implementation, not a search of
the codebase, not a second opinion on your own output. Read, run and write yourself.

The orchestrator reuses this session — for the review findings, for a chore over your own
diff, often for the next phase — precisely because by then you know where this codebase
keeps things and how its suite is run. A sub-agent's reading of the code ends when the
sub-agent does, so a delegated phase leaves you holding its summary and nothing else, and
every turn after this one starts cold.

A project skill that tells you to spawn an implementer, reviewer or fixer —
`implement-plan`, `implement-phase`, `review-phase`, `amend-plan`, anything shaped like
them — is written for the orchestrator that dispatched you, not for you. Take what it says
about conventions, gates and commit rules; never follow its spawn steps.
**If `run_options.commit_strategy_resolved = "modular-commits"`:**

7. **Plan commit units before staging.** List the logical units this phase produces (e.g. `3 services + 1 use case update + 1 init export`). Each unit = **one** commit. Tests for that unit travel **in the same commit** as the code they test — never a separate commit.
8. For each unit, in order:
   a. Stage exactly that unit's files: `git add <explicit paths>` (NEVER `git add -A` — repo root holds untracked .env / .env.docker, generated schema.yml / schema-auth.yml, .coverage, mailpit-data — `git add -A` will sweep them in). Tests for the unit go in the same `git add`.
   b. Commit with the repo's commit_style — see the **Commit Boundaries** + **Commit Message Format** tables below.
   c. Don't bundle two units in one commit. If the commit message needs the word "and" to cover the diff, **split** — see **Red Flags** below.
9. Do **not** add `Co-Authored-By: Claude` (or any other AI) trailer to commits — the project forbids them.
10. Stop after the commit. The orchestrator owns the branch, the push, and the PR. — push all unit commits at once at end of phase.

### Modular-commits discipline (load-bearing — re-read every phase)

Commit each logical unit independently as you complete it. One service = one commit. One use-case update = one commit. Tests travel with the code they test.

The commit list becomes a **table of contents** for reviewers — they can read the commit titles before touching any code and already understand the shape and sequence of the implementation.

#### Commit Boundaries

| Unit | When to commit | Example commit message |
|------|---------------|------------------------|
| New service | Service + its unit tests complete | `feat(record-copy): add service to copy files between records` |
| Use case update | Use case wires in new services, with integration tests | `feat(record-copy): wire optional fields into record copy use case` |
| Init / exports | After exposing new symbols | `chore(record-copy): expose new services in init file` |
| Serializer field | Field + validation + tests | `feat(record-copy): add copy flag for tags to serializer` |
| Refactor / cleanup | Standalone cleanup pass only | `refactor(record-copy): apply shared batch size to copy services` |
| Bug fix | Fix + regression test | `fix(reports): include archived rows in summary section` |

Tests for a unit belong **in the same commit** as that unit. Never commit tests separately.

#### Commit Message Format

Spec: [conventionalcommits.org](https://www.conventionalcommits.org/en/v1.0.0/)

```
<type>(<scope>): <description>

[optional body: non-obvious why, constraints, or side effects — omit if obvious]
```

| Type | Use for |
|------|---------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code restructuring without behavior change |
| `chore` | Maintenance — init files, exports, config |
| `docs` | Documentation only |

**Scope:** the feature area or module (e.g. `reports`, `record-copy`). Optional but recommended.

**Breaking changes:** append `!` before the colon — `feat(auth)!: replace session tokens`.

```
feat(record-copy): add service to copy files between records
feat(record-copy): add service to copy tags between records
feat(record-copy): wire optional fields into record copy use case
chore(record-copy): expose new services in init file
refactor(record-copy): apply shared batch size to copy services
```

#### Bad

```
WIP
add stuff
Implement full record copy feature   ← too broad, should be split
```

#### Red Flags — Split the Commit

- Commit message needs "and" to cover everything in it.
- You are staging files from two different units.
- A reviewer cannot understand the diff without seeing the other commits first.

#### Common Rationalizations

| Rationalization | Reality |
|----------------|---------|
| "I'll commit everything at the end" | Reviewers read commit-by-commit; one giant diff hides intent. |
| "The user can squash later" | Squashing destroys the logical history this discipline exists to preserve. |
| "It's faster to do one commit" | Planning units takes 2 minutes; reviewing a 2000-line blob takes much longer. |
| "The changes are all related" | Related ≠ same unit. Services that depend on each other still get separate commits. |

**Else (`run_options.commit_strategy_resolved = "stacked-branches"`):**

7. Stage the right files (NEVER `git add -A` — repo root holds untracked .env / .env.docker, generated schema.yml / schema-auth.yml, .coverage, mailpit-data — `git add -A` will sweep them in). Stage explicitly: `git add <explicit paths>`.
8. Commit with the repo's style — look at `git log -10 --oneline` first. Conventional Commits format: `type(scope): subject` — e.g. `feat(calendar): add bundle availability filter`, `fix(public_api): correct organization scope on bookings query`.
9. Do **not** add `Co-Authored-By: Claude` (or any other AI) trailer to commits — the project forbids them.
10. Stop after the commit. The orchestrator owns the branch, the push, and the PR.

## Required output (single final report)
- Status: SUCCESS, FAILURE (and why), or NEEDS_INPUT (with the `questions:` block above).
- Files created/modified (paths only).
- 5–15 line summary of what you implemented and key decisions.
- Deviations from the plan body and reasoning.
- Anything you couldn't do (with explanation).
```

**Don't** dump the full plan into every prompt. Dependency-closure tracking summaries replace prior phases as context. Always include the **Goals + Non-goals** and **Guiding Decisions** sections plus the relevant **Data Model Changes** subsection — load-bearing decisions; phases reach back frequently.

## Pick the model from the phase's crew assignment

**The plan owns the *implementer* model — this skill does not re-derive tiers and doesn't assume a vendor.** It owns it as a **roster**: the plan's **Crew** table names the agents it is staffed with and the tier each is staffed at, and every phase carries an `**Assigned to**:` line naming one of them.

Pick:

1. Read the phase's `**Assigned to**:` line for the crew id, then that id's row in the plan's **Crew** table for the tier.
2. Open the tier in [`ai-tools/skills/plan-feature/resources/ai-models.yaml`](../plan-feature/resources/ai-models.yaml) and take its models.
3. **Filter to what's actually available in the runtime.** Different harnesses expose different sets.
4. From the survivors, **pick the cheapest / fastest** the runner can use, and translate it to whatever form the runner's spawning tool expects.
5. Tier with no runtime-available vendor → step one tier up and say so once. Never hard-fail a phase over a model-selection miss.
6. `**Assigned to**:` missing or naming an agent the **Crew** table does not list → **ask the user** via `AskUserQuestion` (header `Crew`), quoting the line as found, one option per **Crew** member (id + tier), the likeliest fit first with ` (Recommended)`. Don't silently re-derive a tier from the phase body; the roster is the plan's arithmetic about how many agents this feature needs, and inventing a member changes it.

**A legacy plan carries `**Suggested AI model**:` and no Crew table.** Read the tier straight off that line and continue — same resolution, one less indirection. Don't invent a roster for it.

### Reuse the agent the plan staffed

**A crew member is an agent, and it is still alive.** If this member has already taken a phase on this run, continue that sub-agent rather than spawning a new one: it is standing in the same worktree — a member keeps one for the whole run — and it already knows where this codebase keeps things, how its suite is run and what its conventions are. Rediscovering that is most of what a cold agent's first turn costs.

Because it is new work, the continuation gets the phase's **full brief**, not a delta. Precede it with the re-orientation described in [Re-orienting a member after the reset](../implement-plan/SKILL.md#re-orienting-a-member-after-the-reset): same agent and same directory, whether the previous phase's work is in this tree, and which files differ from what it last saw.

Start cold instead when any of these hold:

- the member has taken no phase yet on this run;
- **their previous phase failed** — that session is the context that failed with it;
- the runtime cannot continue a finished sub-agent at all.

Record which of those applied, so a phase that was unexpectedly slow can be read later without guessing.

**Retry escalation (no user prompt):** the picked model fails on a clear capability gap → step **one tier up** and retry once. After Tier 4 fails, STOP. Update tracking with `❌` and hand the failure back to the conductor, which asks the user how to proceed with a structured question (see its **Model escalation** rule).

Record the **model actually used**, the **crew member** it came from, and **whether that member is the one the plan assigned** — a phase run by a covering peer is the difference between a run that cost what the plan said and one that did not.

## 3. Spawn the subagent

Use whatever agent-spawning primitive the runtime exposes. Pass:

- A descriptive label (e.g. `"{plan.id} {phase.id}: {phase.title}"`).
- The model from the [Pick the model](#pick-the-model-from-the-plans-per-phase-suggestion) step, translated to the runner's form.
- The phase prompt from the [Compose the agent prompt](#1-compose-the-agent-prompt-token-efficient) step.
- The right **agent type** (below).

**Sandbox the spawn — only when `SANDBOX_TIER = enforced`.** The prompt tells the subagent to stay in `WORKROOT`, but that's cooperative — a smaller model can resolve a path back to the main checkout and silently write there (the review-phase stray-write check catches this reactively). When `SANDBOX_TIER = enforced` **and** the runtime spawns subagents as **subprocesses** (it shells out to an agent CLI — e.g. `codex exec …`, a `claude -p …` child, a custom runner), wrap that launch command in the worktree's bundled guard so the OS blocks main-checkout writes regardless of harness:

```bash
ai-tools/skills/prepare-worktree/scripts/sandbox-run.sh \
  --deny  <main_checkout> \
  --deny  <pool_root> \
  --allow <WORKROOT> \
  --allow <main_checkout>/.vinta-ai-workflows \
  --allow <main_checkout>/.git \
  -- <the agent spawn command>
```

`<main_checkout>` is the repo root the skill was invoked from (never `WORKROOT` when a worktree is in use). A stray write then fails with `Operation not permitted` / `EROFS`; the subagent retries against the worktree. `<main_checkout>/.git` must be allowed because git worktrees write commits into the main repo's `.git` (shared objects/refs, `.git/worktrees/<name>/index.lock`); omitting it makes the subagent's own `git commit` fail.

`<pool_root>` is the directory that holds the lane worktrees (the worktree root prepare-worktree provisioned into). Denying it and allowing back only this lane's `WORKROOT` blocks writes into **sibling lanes** — under parallel execution the more dangerous stray write, because a sibling's tree is being edited and tested at that moment. Omit the `--deny <pool_root>` line only when the run has a single lane and no pool exists.

- **In-process subagent runtimes** (orchestrator and subagent share one OS process — e.g. claude-code's Task tool) can't wrap a single spawn. Two options: (a) install a runtime pre-write guard hook scoped to `WORKROOT` (prepare-worktree ships `scripts/claude-worktree-write-guard.py` + `scripts/gen-claude-sandbox-settings.sh` for claude-code); or (b) run the **entire** invocation under `sandbox-run.sh` with the same `--deny` / `--allow` set. Pick whichever the runtime supports.
- **`SANDBOX_TIER = none`** (no sandbox tool, or `use_worktree = false`) → skip wrapping; prevention falls back entirely to the review-phase stray-write check. Surface this once to the user when a worktree run is unsandboxed so the weaker guarantee is explicit.

**Agent type per phase.** Project agents in [`ai-tools/agents/`](ai-tools/agents/) (exposed to claude-code via `.claude/agents` symlink):

| Phase shape | Agent type |
|---|---|
| Default — any phase whose primary risk is correct execution of the Changes / Tests / Acceptance | `implementer` |
| Migration-heavy — phase introduces Django schema migrations, raw-SQL DB code (functions, views, materialized views, triggers, procedures via `common/raw_sql_migration_managers.py`), or lock-sensitive operations on hot tables | `migration-author` |
| Review-only (rare; usually a Layer 3 dispatch from inside the loop, not a whole phase) | `reviewer` |
| Fix-up (dispatched by the review loop, not by phase routing) | `fixer` |

A phase that combines shapes → the agent type stays `implementer`, and the prompt lists every relevant SKILL.md. The agent type changes only when a stack-specialist's risk is the primary one.

**Avoid bouncing the same phase between multiple agents.** Wanting to "hand off" mid-phase → the plan should have split into sub-phases instead.

**Concurrent invocations are expected.** The conductor may have several lanes in flight, each running its own copy of this skill against a different phase. Nothing here is shared: the prompt, the model pick, the spawn, and the returned report all belong to one phase in one `WORKROOT`. Never read another lane's worktree, branch, or tracking entry — if this phase needs something from another phase, that is a dependency edge the plan should have declared.

## Relay a sub-agent's questions (`NEEDS_INPUT`)

A spawned sub-agent cannot reach the human. When its report says `status: NEEDS_INPUT` (the contract every phase-work prompt carries), the orchestrator turns it into a clickable prompt:

1. **Don't answer for the human, and don't ask in prose.** Don't paste the report and end the turn with "how should I proceed?". Don't re-spawn the agent hoping the question goes away.
2. **Ask with `AskUserQuestion`** (the harness's structured question tool — see **Asking the human** in [AGENTS.md](../../../AGENTS.md)). Pass the report's `questions:` block through unchanged: header, question, options (label + description), multi-select. Above the call, write one line naming the blocked phase and agent, plus its `blocked_on` and `done_so_far`. When the block is malformed (no options, more than 4 questions, an "Other" option), fix the shape and keep the wording. Never fall back to prose.
3. **Record the answer** in the conductor's tracking file when one exists, under the phase's `decisions` list (question header, chosen option or free-text answer). A resumed run reads it and doesn't ask again.
4. **Resume the work.** When the runtime can continue the same sub-agent session (for example Claude Code's `SendMessage` to the agent id), send the answers there. Otherwise spawn a fresh agent of the same type and model with the original prompt plus an `## Answers from the human` section that quotes each question, the answer, and the previous agent's `done_so_far`.
5. **Escalate plan-level answers.** When an answer changes the plan itself (a **Guiding Decisions** row, a phase's scope or acceptance line), ask before resuming: `Amend the plan first (Recommended)` (stop and hand over to [amend-plan](../amend-plan/SKILL.md)), `Apply to this phase only` (record the deviation in tracking and resume).

## Output

When the implementer returns `NEEDS_INPUT`, relay it (above) and resume until it returns `SUCCESS` or `FAILURE`. Then return the implementer's single final report verbatim to the conductor (status, files, summary, deviations, blockers), plus the relayed decisions so the conductor can record them in tracking. The conductor — not this skill — writes tracking from the git diff + the report.
