---
name: implement-plan
description: Execute a phased implementation plan from `ai-plans/` in vinta_schedule_api by orchestrating one subagent per phase (using whatever model the plan suggests and the runtime supports), pushing one stacked branch per phase to GitHub, and tracking progress. Use when the user says "implement the plan", "execute plan X", "start implementation", "run phase N of plan Y", "implement {feature} plan", or asks to drive a `*_IMPLEMENTATION_PLAN.md` file phase-by-phase. NOT for one-off changes, single-file edits, or work that doesn't have an existing plan. Agents push branches and open PRs via `open-pr-from-context` after review passes.
---

# Implement Plan

Drive a phased plan in [`ai-plans/`](ai-plans/) to completion: spawn one subagent per phase (whichever model plan recommends + runtime can run), run lint / typecheck / unit / e2e (where applicable), push one stacked GitHub branch per phase, keep a progress tracking file as context handoff between phases. Harness-agnostic — claude-code, OpenAI Codex, Google's runtime, or any framework with a "spawn subagent with model + prompt" primitive.

Execution counterpart to [plan-feature](../plan-feature/SKILL.md). Plan = contract; this skill = build pipeline.

## Working assumptions

- Repo: vinta_schedule_api (Django 6 + DRF + Strawberry GraphQL + Celery, multi-tenant (OrganizationModel), Postgres, deployed to Render). Conventions: [AGENTS.md](AGENTS.md).
- Plan files: [`ai-plans/YYYY-MM-DD-FEATURE_NAME_IMPLEMENTATION_PLAN.md`](ai-plans/).
- Lint: `docker compose run --rm api uv run ruff check ./`. Format: `docker compose run --rm api uv run ruff format ./`.
- Type / build gate: `docker compose run --rm api uv run python manage.py check --deploy` plus full mypy via `docker compose run --rm api uv run mypy .`.
- Unit / integration tests: `docker compose run --rm api uv run pytest -n auto` (everything); per-app via `docker compose run --rm api uv run pytest <app>/tests/ -n auto`.

- Migrations: `docker compose run --rm api uv run python manage.py makemigrations --check` (gate) + `docker compose run --rm api uv run python manage.py migrate` (apply). Raw-SQL DB code (functions, views, materialized views, triggers, procedures) routes through `common/raw_sql_migration_managers.py` — see [add-migration](../add-migration/SKILL.md). Deploy target: Render — long-running migrations run via the Render dashboard's job runner; Celery workers + beat are separate services on Render and must be restarted alongside web after a deploy.
- Code host: **GitHub**. PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).
- Co-author trailer policy: **forbidden**. Commits must not include `Co-Authored-By:` AI trailers.

## Step 0 — Locate + parse plan

Parse once, reuse for every phase:

1. **Identify plan file.** Ask user which plan (path or feature name). Feature name: `ls ai-plans/` + grep; confirm before proceeding.
2. **Extract structured fields**, in order:
   - **Feature name** + **plan id** — derived from filename's `FEATURE_NAME` portion only: strip `YYYY-MM-DD-` prefix + `_IMPLEMENTATION_PLAN.md` suffix. Kebab variant for branch names.
   - **Goals + Non-goals** section — verbatim, used in every phase prompt.
   - **Guiding Decisions** section — verbatim. Pay attention to: feature flag (key, scope, default, flip-on criterion), storage shape, tenant scoping, API contract decisions.
   - **Data Model Changes** section — keep full body; later phases reference earlier subsections.
   - **Phased Rollout** section — parse into phase records: `{ id, title, goal, body, spec_use_case, suggested_model_tier, reusable_skills, has_e2e, acceptance, is_cross_repo, is_flag_removal }`.
   - **Risk & Rollout Notes**, **Open Questions**, **Touch List** sections — keep available; include in phase prompts only when relevant.
3. **Classify each phase**: `is_cross_repo`, `is_flag_removal` — orchestrator does NOT auto-execute these.
4. **Ask the user three opt-in questions** via `AskUserQuestion`. Defaults are project-specific (see below); record every answer in tracking under `run_options`:

   a. **Pause between phases?** *"Do you want me to pause and wait for confirmation after each phase, before starting the next one? Lets you review the diff / branch / PR / tracking summary before moving on."* Options: `Auto-flow (default) — keep going phase to phase`, `Pause between phases — wait for go after each one`.

   b. **Draft inline review comments per phase?** *"On top of the standard PR description, do you want me to scan each phase's diff and add 3–10 inline comments calling out non-obvious decisions (subtle invariants, feature-flag short-circuits, cross-phase coupling, upstream-contract naming)? Off by default — say yes when reviewers will appreciate annotated diffs."* Options: `Yes — include inline comments`, `No — PR description only`.

   c. **Run phases in a worktree?** *"Do you want every phase's subagent to work inside an isolated git worktree (its own runnable copy of the app with its own dev + test DB, env files, docker-compose project name) instead of sharing your main checkout? Lets you keep using `main` for unrelated work while this plan runs; survives parallel plans on the same repo without DB / port / docker collisions. Costs one extra checkout's worth of disk + the time it takes [prepare-worktree](../prepare-worktree/SKILL.md) to provision it."* Options: `No — run in current checkout`, `Yes — provision one shared worktree for the whole plan`. Default = value of `run_options.implement-plan.use_worktree` in `.vinta-ai-workflows.yaml` (`Yes` for this project).

      When `Yes`: **the same worktree is used for every executable phase** — all phase branches stack inside it. The skill never provisions a second worktree mid-plan. If the user wants per-phase worktrees, that's a different workflow (split the plan into independent plans).

      Skip this question entirely when `foundation_skills.prepare-worktree` is `disabled` in `.vinta-ai-workflows.yaml`: record `run_options.use_worktree = false`; surface a one-line note that worktree isolation is available if the team opts in via [vinta-sync-ai-tools](../../skills/vinta-sync-ai-tools/SKILL.md).

   PR opening itself is **not** asked here — it's governed by the project's PR creation policy captured at bootstrap (see `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` above). When that policy = "agents create PRs", the [Open PR via context file](#1f-open-pr-via-context-file) step always opens the PR via [open-pr.sh](../foundation-skills/open-pr-from-context/scripts/open-pr.sh) regardless of the comment opt-in.

   d. **Commit strategy?** *"This project's commit_strategy is set to ask. Pick one for this run: one branch + one PR per phase (stacked), or one branch + one PR for the whole plan with one atomic commit per logical unit (modular)?"* Options: `Stacked branches — one branch + PR per phase`, `Modular commits — atomic commits, one PR for whole plan`. Cache answer in tracking under `run_options.commit_strategy_resolved`.

5. **Confirm with user before starting.** Show plan path, phase list (id + title + tier + cross-repo/flag-removal flags + e2e flag), phases this skill will execute vs defer, branch naming pattern (depends on `run_options.commit_strategy_resolved` — resolved at Step 0), captured `run_options.pause_between_phases` + `run_options.generate_inline_comments` + `run_options.use_worktree` + `run_options.commit_strategy_resolved`, and that each phase will push its stacked branch and open a PR on GitHub.

   Wait for "go". After that, the per-phase pause behavior follows `run_options.pause_between_phases`. Inline-comment drafting follows `run_options.generate_inline_comments`. Worktree isolation follows `run_options.use_worktree`. Commit-strategy behavior follows `run_options.commit_strategy_resolved`.

## Step 0.5 — Provision worktree (when `run_options.use_worktree = true`)

Skip when `run_options.use_worktree = false`; jump to Step 1.

When true, invoke [prepare-worktree](../prepare-worktree/SKILL.md) **once**, before any phase runs:

1. **Inputs.** Plan path (so prepare-worktree can read it for deps / migrations / env / compose churn — see prepare-worktree's **Plan inspection** step), suggested worktree name = `plan-{plan-id-kebab}`, plan-driven mode.
2. **Pre-run sanity.** Confirm no existing worktree at the target path (`git worktree list | grep <name>` — refuse if collision). Confirm `git -C <main> status` of the main checkout (warn if dirty; defer to prepare-worktree's **Sanity checks** step for the call).
3. **Run prepare-worktree.** Pass the plan file + worktree name. The skill returns:
   - `worktree_path` — absolute path the phase subagents will `cd` into.
   - `worktree_branch` — the base branch prepare-worktree created; phase branches stack off this.
   - `worktree_summary` — `.vinta-ai-workflows/worktrees/<name>.yaml` (read by teardown).
   - `sandbox_tier` — `enforced` (the [Filesystem sandbox](../prepare-worktree/SKILL.md#step-55--filesystem-sandbox-os-level-write-guard) step found `sandbox-exec` / `bwrap` and will OS-block main-checkout writes) or `none` (no sandbox tool — prevention degrades to the Layer 1 backstop). Drives the [Spawn subagent](#1c-spawn-subagent) wrapping + Layer 1 below.
4. **Persist to tracking.** Write `run_options.worktree_path`, `run_options.worktree_branch`, `run_options.worktree_summary`, `run_options.sandbox_tier` into `ai-plans/TRACKING_{plan-id}.md` (Step 1g schema gains these fields). All later phases read them — never re-provision mid-plan.
5. **Report to user.** Quote the prepare-worktree summary back: which dirs symlinked vs copied vs forked; which DB(s) forked + their names; compose project name; teardown command. Hold here until the user confirms (`AskUserQuestion`: `Looks good — start phase 1`, `Stop — let me adjust`).

**Worktree topology rule.** When `use_worktree = true`, every phase branches off the previous phase **inside the worktree**, not off `main` in the main checkout. The first phase branches off `<worktree_branch>` (which prepare-worktree based on `origin/main`); subsequent phases stack as usual. All `git` calls in the **Push branch** step use `git -C <worktree_path>` (or `cd` into the worktree first). All inner / outer test commands in the [Prepare agent prompt](#1a-prepare-agent-prompt-token-efficient) step's working-instructions block run inside the worktree so they hit the forked DB / env / compose stack.

Failure modes:
- **prepare-worktree returns an error** (disk full, branch exists, DB clone failed) → surface to the user; do NOT fall back to "just run in the main checkout" silently — that defeats the opt-in. Ask: `Retry`, `Run in main checkout instead (flip use_worktree to false)`, `Stop`.
- **User cancels at the confirmation gate** → tear the worktree down (run the teardown command from prepare-worktree's report) before exiting, so the next run starts clean.

## Step 1 — Per-phase loop

For each phase that's `not is_cross_repo and not is_flag_removal`, in plan order:

### 1a. Prepare agent prompt (token-efficient)

Compose with **only what the agent needs**:

```
You are implementing {phase.id}: {phase.title} of plan {plan.id}.

## Repo
vinta_schedule_api (Django 6 + DRF + Strawberry GraphQL + Celery, multi-tenant (OrganizationModel), Postgres, deployed to Render).

{If run_options.use_worktree = true:}
  ## Worktree
  Work entirely inside this worktree: `<run_options.worktree_path>`.
  `cd` into it before any command. Every `git`, every lint / test / build / migrate
  call runs there, and every lint / test / typecheck / format / migrate command runs
  through this worktree's own compose stack — `docker compose run --rm api uv run …`
  (e.g. `docker compose run --rm api uv run pytest -n auto`). Because the worktree
  exports its own `COMPOSE_PROJECT_NAME`, those commands hit the worktree's **own
  compose `db` container** — never the host and never the main checkout's database.
  Do NOT run these tools on the host, and do NOT touch the main checkout — its DB,
  env, and compose stack are intentionally separated. See
  `<run_options.worktree_path>/WORKTREE.md` for what's forked vs shared (deps, dev DB,
  test DB, compose project name, env file).
  {If run_options.sandbox_tier = enforced:} Writes to the main checkout are
  OS-blocked — if you see `Operation not permitted` / `EROFS` on a write, you
  used a main-checkout path by mistake; redo it against this worktree path.
  Branch base for this phase: `<phase-specific base>` — orchestrator already
  created your phase branch there; commit straight to it.

## Read first
1. AGENTS.md — repo conventions.
2. ai-plans/{plan-filename}, the **Goals + Non-goals**, **Guiding Decisions**, **Data Model Changes** sections and YOUR phase body inside **Phased Rollout**.
{If run_options.use_worktree = true:} 3. `WORKTREE.md` at the worktree root — fork map (which dirs symlink to main vs are independent copies).

## Plan-level decisions (from Goals + Non-goals + Guiding Decisions)
{Goals + Non-goals verbatim}
{Guiding Decisions table verbatim}
{If feature flag declared:}
  Feature flag: `{flag-key}` — scope `{per-tenant|per-request}`, default `{false|true}`.
  Wire reads + writes per the plan's **Guiding Decisions** entry. Off-flag path = byte-for-byte pre-feature behavior.

## What was already implemented in prior phases
{Tracking file "Completed Phases" section. First executed phase: "Nothing yet — this is the first phase."}

## Your tasks (Phase {id} only)
{phase.body verbatim, including Goal / Spec use-case / Feature flag / Changes / Tests / Acceptance lines}

## Reusable skills you SHOULD invoke
{phase.reusable_skills — for each, instruct the agent to first read ai-tools/skills/{name}/SKILL.md, then follow that pattern.}

Project skills available: plan-feature, create-spec, create-qa-use-cases, open-pr-from-context, prepare-worktree, implement-plan, amend-plan, systematic-debugging, add-env-var, add-one-off-script, add-model, add-migration, create-graphql-public-query, create-postgres-function, create-postgres-view, create-rest-endpoint, run-one-off-script-django

## Adding new third-party dependencies

Before running any install command (`npm add`, `pnpm add`, `yarn add`, `pip install`, `poetry add`, `uv add`, `cargo add`, `go get`, `gem install`, equivalents), check the package's SPDX license against the project's forbidden list — see the **Dependency licenses** section in [AGENTS.md](AGENTS.md) for the full list, the per-package overrides, and any project-specific notes.

Quick lookup:

- **npm / pnpm / yarn**: `npm view <pkg> license`.
- **PyPI**: `pip index versions <pkg>` then read the project metadata, or open `https://pypi.org/project/<pkg>/`.
- **Cargo**: `cargo metadata --format-version 1 | jq '.packages[] | select(.name=="<pkg>") | .license'` (after a temporary `cargo add` in a scratch dir, or read `Cargo.toml` upstream).
- **Go**: open the module's repo `LICENSE` file directly.
- **Gem**: `gem specification <pkg> licenses`.

If the license is in the forbidden list AND the `(package, license)` pair is **not** listed under **Approved overrides** in AGENTS.md:

1. Stop. Do not run the install command.
2. Surface the violation to the user with: package name, SPDX identifier, why it's forbidden, link to the upstream license.
3. Offer alternatives (search the ecosystem for an MIT / Apache-2.0 / BSD-licensed equivalent) before asking for an override.
4. If the user grants a one-off override, the orchestrator must record it in `policies.dependency_licenses.allowed_overrides[]` of `.vinta-ai-workflows.yaml` (package + SPDX + one-line reason) before re-running the install. Undocumented overrides leak into the diff and the reviewer agent will flag them.

**License unknown / undeclared.** When the lookup above returns no license, an empty value, `UNKNOWN`, `SEE LICENSE IN <file>`, or only an unstructured `LICENSE` file in the repo with no SPDX identifier, treat it as a **policy decision the user owns** — don't guess, don't auto-infer, don't fall back to "assume MIT". The package may be unlicensed (all-rights-reserved by default in most jurisdictions), proprietary, or simply missing metadata.

1. Stop. Do not run the install command.
2. Surface to the user: package name, what was found (e.g. "the `license` field is absent in `package.json`", "PyPI metadata returned `UNKNOWN`", "no LICENSE file in the repo"), the upstream repo / registry URL so the user can verify.
3. Ask via `AskUserQuestion`: `Skip — find a licensed alternative`, `Treat as forbidden — refuse install`, `Treat as allowed — record an override` (the third option only when the user has independently confirmed the license off-channel; record the resolved SPDX in `allowed_overrides[]` with the source in the `reason` field, e.g. `"unlicensed but author confirmed MIT via GitHub issue #42"`).
4. Don't add the dep until the user picks one of the three.

Transitive deps follow the same rule, but checking every transitive license at install time is impractical — the project's CI (or a separate license-audit run) handles the deep walk. The subagent's responsibility is the **direct** add.

## Working instructions
1. Read existing code paths your changes touch — do not write before reading.
2. Implement using Read/Edit/Write. Match existing patterns.
3. **Inner loop — fast iteration.** Scoped to files/apps you touched:
   a. `docker compose run --rm api uv run ruff check ./` until clean.
   b. `docker compose run --rm api uv run pytest <new-test-path> -vs` for new tests individually.
   c. Scoped suite: `docker compose run --rm api uv run pytest <app>/tests/ -n auto`.
4. Iterate 2–3 until **new tests pass individually** and the scoped suite is green. Do **not** advance to step 5 with red scoped tests.
5. **Outer gate — full local verification, only after step 4 is green.** All MUST pass before staging:
   a. **Type / build:** `docker compose run --rm api uv run python manage.py check --deploy`.
   b. **Full test suite:** `docker compose run --rm api uv run pytest -n auto`.
   
6. Outer gate fails → return step 2 (fix regression), re-run inner loop, then 5a/5b/5c. **Never** commit, push, or proceed while any gate is red.

**If `run_options.commit_strategy_resolved = "modular-commits"`:**

7. **Plan commit units before staging.** List the logical units this phase produces (e.g. `3 services + 1 use case update + 1 init export`). Each unit = **one** commit. Tests for that unit travel **in the same commit** as the code they test — never a separate commit.
8. For each unit, in order:
   a. Stage exactly that unit's files: `git add <explicit paths>` (NEVER `git add -A` — repo root holds untracked .env / .env.docker, generated schema.yml / schema-auth.yml, .coverage, mailpit-data — `git add -A` will sweep them in). Tests for the unit go in the same `git add`.
   b. Commit with the repo's commit_style — see the **Commit Boundaries** + **Commit Message Format** tables below.
   c. Don't bundle two units in one commit. If the commit message needs the word "and" to cover the diff, **split** — see **Red Flags** below.
9. Do **not** add `Co-Authored-By: Claude` (or any other AI) trailer to commits — the project forbids them.
10. Stop after the commit. Orchestrator owns the branch, the push, and the PR. — push all unit commits at once at end of phase.

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

7. Stage right files (NEVER `git add -A` — repo root holds untracked .env / .env.docker, generated schema.yml / schema-auth.yml, .coverage, mailpit-data — `git add -A` will sweep them in). Stage explicitly: `git add <explicit paths>`.
8. Commit with the repo's style — look at `git log -10 --oneline` first. Conventional Commits format: `type(scope): subject` — e.g. `feat(calendar): add bundle availability filter`, `fix(public_api): correct organization scope on bookings query`.
9. Do **not** add `Co-Authored-By: Claude` (or any other AI) trailer to commits — the project forbids them.
10. Stop after the commit. Orchestrator owns the branch, the push, and the PR.

## Required output (single final report)
- Status: SUCCESS or FAILURE (and why).
- Files created/modified (paths only).
- 5–15 line summary of what you implemented and key decisions.

- Deviations from the plan body and reasoning.
- Anything you couldn't do (with explanation).
```

**Don't** dump the full plan into every prompt. Tracking summaries replace prior phases as context. Always include the **Goals + Non-goals** and **Guiding Decisions** sections plus the relevant **Data Model Changes** subsection — load-bearing decisions; phases reach back frequently.

### 1b. Pick model from plan's per-phase suggestion

**Plan owns model selection — this skill does not re-derive tiers, doesn't assume vendor.** Each phase carries `**Suggested AI model**:` listing one model per vendor.

Pick:

1. Read line, parse out **all** vendor suggestions.
2. **Filter to what's actually available in the runtime.** Different harnesses expose different sets.
3. From surviving suggestions, **pick the cheapest / fastest** the runner can use.
4. Translate the chosen model to whatever form the runner's spawning tool expects.
5. Phase suggestion straddles tiers → pick the higher-tier suggestion.
6. Line missing / malformed → **ask the user**. Don't silently re-derive tier.

**Retry escalation (no user prompt):** picked model fails on a clear capability gap → step **one tier up** + retry once. After Tier 4 fails, STOP. Update tracking with `❌`, post the agent's report to the user, ask how to proceed.

Record the **model actually used** + the **plan's suggested tier** in tracking.

### 1c. Spawn subagent

Use whatever agent-spawning primitive the runtime exposes. Pass:

- Descriptive label (e.g. `"{plan.id} {phase.id}: {phase.title}"`).
- Model from the [Pick model from plan's per-phase suggestion](#1b-pick-model-from-plans-per-phase-suggestion) step, translated.
- Phase prompt from the [Prepare agent prompt](#1a-prepare-agent-prompt-token-efficient) step.
- The right **agent type**.

**Sandbox the spawn (only when `run_options.use_worktree = true`).** The prompt tells the subagent to stay in the worktree, but that's cooperative — a smaller model can resolve a path back to the main checkout and silently write there (the failure Layer 1 below catches reactively). When the runtime spawns subagents as **subprocesses** (it shells out to an agent CLI — e.g. `codex exec …`, a `claude -p …` child, a custom runner), wrap that launch command in the worktree's bundled guard so the OS blocks main-checkout writes regardless of harness:

```bash
ai-tools/skills/prepare-worktree/scripts/sandbox-run.sh \
  --deny  <main-checkout-root> \
  --allow <run_options.worktree_path> \
  --allow <main-checkout-root>/.vinta-ai-workflows \
  -- <the agent spawn command>
```

`<main-checkout-root>` is the repo root the skill was invoked from (never `worktree_path`). A stray write then fails with `Operation not permitted` / `EROFS`; the subagent retries against the worktree.

- **In-process subagent runtimes** (the orchestrator and subagent share one OS process — e.g. claude-code's Task tool) can't wrap a single spawn. Two options: (a) install a runtime pre-write guard hook scoped to the worktree, or (b) run the **entire** implement-plan invocation under `sandbox-run.sh` with the same `--deny` / `--allow` set (the deny-main model leaves `.vinta-ai-workflows` writable for the orchestrator's own tracking / prs-context writes). Pick whichever the runtime supports.
- **`run_options.sandbox_tier = none`** (no sandbox tool on the machine) → skip wrapping; prevention falls back entirely to the Layer 1 stray-write check below. Surface this once to the user so the weaker guarantee is explicit.

**Agent type per phase.** Project agents in [`ai-tools/agents/`](ai-tools/agents/) (exposed to claude-code via `.claude/agents` symlink):

| Phase shape | Agent type |
|---|---|
| Default — any phase whose primary risk is correct execution of the Changes / Tests / Acceptance | `implementer` |
| Migration-heavy — phase introduces Django schema migrations, raw-SQL DB code (functions, views, materialized views, triggers, procedures via `common/raw_sql_migration_managers.py`), or lock-sensitive operations on hot tables | `migration-author` |
| Review-only (rare; usually a Layer 3 dispatch from inside the loop, not a whole phase) | `reviewer` |
| Fix-up (dispatched by the review loop, not by phase routing) | `fixer` |

Phase combines shapes → agent type stays `implementer`, prompt lists every relevant SKILL.md. Agent type changes only when a stack-specialist's risk is the primary one.

**Avoid bouncing the same phase between multiple agents.** Wanting to "hand off" mid-phase → the plan should have split into sub-phases instead.

### 1d. Thorough review

Three layers, all required, in order. The orchestrator never edits — every issue surfaces as a fix-up subagent task.

#### Layer 1 — Mechanical checks

1. `git status` + `git diff --stat`: confirm file list matches the agent's report.
2. **Read the full diff** for every changed file using `git diff`. Spot-checking is not enough.
3. **Verify the outer gate** ran + green. Look in the report for explicit confirmation that `docker compose run --rm api uv run python manage.py check --deploy` AND `docker compose run --rm api uv run pytest -n auto` were executed + passed. Vague confirmation → **re-run yourself**.
4. **Scope creep**: file touched outside expected surface area? Unrelated formatting churn? Surface it.
5. **No-secrets scan**: `git diff` for `password|secret|token|api_key|AKIA|BEGIN [A-Z]+ KEY`.
6. **Stray main-checkout writes (only when `run_options.use_worktree = true`)**: a subagent is told to work inside the worktree, but a buggy agent (often a smaller model) can resolve an absolute path back to the **main checkout** and silently edit files there (worktrees have independent working trees, so those edits never reach the phase commit — they sit as uncommitted thrash in the main checkout, and the "missing" edits read as a silent fixer/implementer failure). **When `run_options.sandbox_tier = enforced` the OS sandbox from the [Spawn subagent](#1c-spawn-subagent) step already blocks these writes — this check becomes a cheap backstop (a clean `git status` is the expected result; non-empty output means the sandbox was bypassed or a path slipped through, still a BLOCKER).** When `sandbox_tier = none` it's the *only* line of defense — run it religiously. Either way, after **every** implementer **and** fixer subagent returns, run `git -C <main-checkout-path> status --short | grep -vE '^\?\?'` (tracked modifications only). Any output is a BLOCKER for this phase:
   - Diff the stray files (`git -C <main-checkout-path> diff -- <path>`) to recover intent.
   - If the edit belongs in the worktree, re-dispatch the fixer/implementer with an explicit instruction to write to the worktree path (the change is missing from the phase commit until it does).
   - Once recovered (or confirmed superseded by the correctly-committed worktree version), discard the stray edits with `git -C <main-checkout-path> restore -- <path>` so the main checkout returns clean. Never leave the main checkout dirty between phases — a later phase's Layer 1 can't tell new thrash from old.
   `<main-checkout-path>` is the repo root the skill was invoked from (NOT `run_options.worktree_path`). Skip this check entirely when `run_options.use_worktree = false`.
7. **Dependency license scan**: `git diff package.json pyproject.toml ...` (project-relevant manifests) — for every added dep look up its SPDX license (`npm view <pkg> license`, PyPI metadata, repo `LICENSE`). A license in `policies.dependency_licenses.forbidden_spdx` and not in `allowed_overrides` is a BLOCKER (when `block`) or a SHOULD-FIX (when `warn`). A missing / `UNKNOWN` / undeclared license is **always a BLOCKER** regardless of enforcement mode — there is no override to silently bless undisclosed terms.
8. **No AI co-author trailer**: `git log -<n>..HEAD --format=%B | grep -i 'Co-Authored-By'` — any AI trailer is a BLOCKER.

#### Layer 2 — Plan compliance walkthrough

Open phase body alongside diff and walk:

1. **Every numbered "Changes" item implemented.**
2. **Every "Tests" entry materialized**, with assertions actually exercising the called-out behavior.
3. **Acceptance line satisfiable** by the diff.
4. **Repo conventions** from AGENTS.md.
5. **Reusable-skill compliance.**

6. **Feature-flag wiring** if the plan's **Guiding Decisions** declared a flag — flag-OFF byte-for-byte pre-feature behavior, ≥1 test asserts.
7. **Cross-phase consistency** with prior tracking summaries.

#### Layer 3 — Independent reviewer subagent

After Layers 1–2 pass, spawn a **separate** subagent (different session, no implementation context) using the project's `reviewer` agent type ([ai-tools/agents/reviewer.md](ai-tools/agents/reviewer.md)). Read-only by design.

Reviewer prompt template — see the reviewer agent's body for the standard form. Triage findings:
- **BLOCKER**: must fix before the **Push branch** step below.
- **SHOULD-FIX**: fix in-phase if cheap; else follow-up issue + tracking note.
- **NIT**: ignore unless trivially cheap.

Reviewer finds nothing on a >300-LoC multi-file phase → suspicious. Read once more.

#### Fix loop

1. Spawn a **new** subagent — project's `fixer` agent type ([ai-tools/agents/fixer.md](ai-tools/agents/fixer.md)). Fix prompt quotes the finding verbatim.
2. The `fixer`'s system prompt mandates re-running the inner loop + outer gate.
3. After fixer returns, redo Layer 1 in full + the affected portion of Layer 2.
4. Loop until Layers 1, 2, 3 are clean.

### 1e. Push branch

**Worktree path rule (applies to whichever commit strategy the block below renders).** When `run_options.use_worktree = false`, every `git` command runs in the main checkout exactly as written. When `true`, run every `git` command below inside the worktree — prefix with `git -C <run_options.worktree_path>` (or `cd` into it first), and the first executed phase branches off `<run_options.worktree_branch>` instead of `main`. Branches / commits stack inside the worktree; nothing touches the main checkout's working tree.

**If `run_options.commit_strategy_resolved = "modular-commits"`:**

Branch naming: `plan/{plan-id-kebab}` (one branch for the whole plan — no per-phase suffix).

**First executed phase** (branches from `main`):
```bash
git checkout main
git pull --ff-only
git checkout -b plan/{plan-id-kebab}
# subagent's atomic unit commits land on this branch
git push -u origin plan/{plan-id-kebab}
```

**Subsequent phases** (stay on the same plan branch — no new branch):
```bash
git checkout plan/{plan-id-kebab}
# subagent's atomic unit commits land on this branch
git push origin plan/{plan-id-kebab}
```

The branch carries every phase's commits in plan order. Reviewers read the commit log top-to-bottom as a table of contents of the implementation.

**Else (`run_options.commit_strategy_resolved = "stacked-branches"`):**

Branch naming: `plan/{plan-id-kebab}/phase-{phase.id}`.

**First executed phase** (branches from `main`):
```bash
git checkout main
git pull --ff-only
git checkout -b plan/{plan-id-kebab}/phase-{phase.id}
# subagent's commits land on this branch
git push -u origin plan/{plan-id-kebab}/phase-{phase.id}
```

**Subsequent phases** (stacked on the previous phase's branch):
```bash
git checkout plan/{plan-id-kebab}/phase-{prev.id}
git checkout -b plan/{plan-id-kebab}/phase-{phase.id}
git push -u origin plan/{plan-id-kebab}/phase-{phase.id}
```

PR opening lives in the [Open PR via context file](#1f-open-pr-via-context-file) step below (single flow — context file + `open-pr.sh`). Subagents never open PRs themselves; the orchestrator does, after review passes.

### 1f. Open PR via context file

This is the **only** PR-creation path. PRs always go through either `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md` or `.vinta-ai-workflows/prs-context/{feature-kebab}/plan.md` depending on `run_options.commit_strategy_resolved` + the bundled [open-pr.sh](../foundation-skills/open-pr-from-context/scripts/open-pr.sh) script — even when inline comments are not requested. The file is the durable record; the script is the publisher.

**If `run_options.commit_strategy_resolved = "modular-commits"`:**

**PR opens once — after Phase 1 passes review.** Subsequent phases push their atomic unit commits to the same plan branch (`plan/{plan-id-kebab}`); the orchestrator re-runs [open-pr.sh](../foundation-skills/open-pr-from-context/scripts/open-pr.sh) against the same plan-level prs-context file at `.vinta-ai-workflows/prs-context/{feature-kebab}/plan.md`. The script is idempotent for already-open PRs — it updates the body, appends new inline comments, and posts an `Phase {N} complete — pushed M commits` PR comment.

**Else (`run_options.commit_strategy_resolved = "stacked-branches"`):**

(PR opens per phase after review — current behavior.)

Two project-level signals decide the actual behavior:

| `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` policy | `run_options.generate_inline_comments` | What this **Open PR via context file** step does |
|---|---|---|
| agents create PRs | false | Write minimal context file (`# Title`, `# Description`, empty `# Comments`). Run `open-pr.sh` → PR opened, no inline comments. |
| agents create PRs | true  | Write full context file (title + description + 3–10 inline comments). Run `open-pr.sh` → PR opened, all comments posted. |
| branches only     | false | **Skip this step entirely.** Human will open PR manually from the pushed branch. |
| branches only     | true  | Write full context file (durable record). **Don't run `open-pr.sh`.** Human can publish later from a CLI-equipped session via [open-pr-from-context](../foundation-skills/open-pr-from-context/SKILL.md). Surface this in the [Send brief update](#1h-send-brief-update-to-user) step. |

#### Steps

1. **Skip if neither column applies** (policy = branches only AND `generate_inline_comments = false`). Jump to the [Update tracking file](#1g-update-tracking-file) step.

2. **Honor existing PR / MR templates.** Read `project.pr_template_paths` from `.vinta-ai-workflows.yaml`. For each entry:
   - **One template** → load it; the prs-context `# Description` body must follow that template's section structure verbatim. Fill each section with phase-specific content drawn from the plan's **Goals + Non-goals**, **Guiding Decisions**, and the phase body. Preserve any `<!-- HTML comments -->` placeholders; do not strip the template's checklists. Sections you can't fill from phase data → leave the template's placeholder/prompt untouched (don't fabricate).
   - **Multiple templates** (`PULL_REQUEST_TEMPLATE/` directory) → ask once via `AskUserQuestion`: list each template + its filename, ask which to use for this run. Cache the choice in tracking under `run_options.pr_template_used` so subsequent phases of the same plan use the same one without re-asking.
   - **Empty array** → free-form description. Default sections: `## Summary` (1–3 sentences), `## Plan reference` (link / phase id), `## Test plan` (commands the reviewer can run).

   When the project's PR template includes a checkbox checklist (e.g. `- [ ] Tests added`, `- [ ] Docs updated`), tick the boxes the phase's diff actually satisfies and leave unsatisfied ones unticked — never auto-tick everything.

   GitHub also honors `?template=<name>` in the PR-create URL when the project has a multi-template directory. `gh pr create --body-file` writes the body directly so the URL trick isn't needed; the body must match the chosen template's structure regardless.

3. **Pick comment targets** (only when `generate_inline_comments = true`). Read the phase diff via `git diff main...HEAD` (or the previous phase branch for stacked phases). Select 3–10 spots that benefit from a one-paragraph context note — typically:
   - A subtle invariant the diff relies on (cite the plan's **Goals + Non-goals** / **Guiding Decisions** entries — by name, never use `§` shorthand).
   - A workaround for a known framework / library limitation.
   - A naming choice driven by an upstream contract.
   - The off-flag short-circuit when a feature flag is in **Guiding Decisions**.
   - Why a seemingly-cleaner refactor wasn't made (out of scope per **Goals + Non-goals**).
   - Cross-phase coupling (this hook is consumed by phase N+k).

   Skip lint/format churn, boilerplate matching nearby files, standard patterns from AGENTS.md, and self-explanatory test names. **A clean phase produces few comments — that's fine. Don't pad.**

   When `generate_inline_comments = false`: skip this step. The file's `# Comments` block stays empty.

4. **Write `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md`** following [resources/prs-context-template.md](../prs-context-template.md). Frontmatter: `plan_id`, `feature_name`, `phase_id`, `phase_title`, `branch`, `base`, `created_at`, `status: pending`, empty `pr_url`. Body sections: `# Title` (single-line PR title), `# Description` (Markdown body — uses the project's PR template structure from step 2 when one exists), `# Comments` (YAML list of `{file, start_line, end_line?, side, body}` — empty list when comments are off).

5. **Confirm `.vinta-ai-workflows/prs-context/` is in `.gitignore`.** [vinta-install-ai-tools-setup](../../../vinta-install-ai-tools-setup/SKILL.md) runs the multi-vendor setup script which appends `.vinta-ai-workflows/prs-context/` on its first invocation. If an older bootstrap missed it, append it now.

6. **Run `open-pr.sh`** (only when policy = agents create PRs). Detect a usable CLI (`gh` for GitHub, `glab` for GitLab) plus the script's other deps (`yq`, `jq`):

   ```bash
   bash ai-tools/skills/open-pr-from-context/scripts/open-pr.sh .vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md
   ```

   Script opens the PR (or detects an existing one), posts each inline comment, rewrites the file's frontmatter to `status: published` + populated `pr_url`, appends a publish log. Exit codes:

   - `0` — PR up, all comments (if any) posted. Capture `pr_url` for the [Send brief update](#1h-send-brief-update-to-user) step.
   - `1` — PR up, ≥1 comment failed. Surface the failed `(file:line)` list to the user; continue to the [Update tracking file](#1g-update-tracking-file) step.
   - `2` — Hard failure (deps missing, branch not pushed, CLI unauthed, file invalid). Surface the script's stderr; treat the phase as having no PR. The file stays `status: pending` so the user can re-run after fixing the gap.

   When policy = "branches only": **don't run the script.** File stays `status: pending`.

7. **Skill wrapper** — [open-pr-from-context](../foundation-skills/open-pr-from-context/SKILL.md) is available for ad-hoc invocation (after the run, on a different machine, etc.). The orchestrator can call the script directly here; the skill is for humans.

### 1g. Update tracking file

Tracking lives at `ai-plans/TRACKING_{plan-id}.md`. Commit on the **current** phase's branch — deletion in Step 3.

Schema: feature-name, plan path, started/last-updated dates, optional feature-flag info, **run options** (`pause_between_phases`, `generate_inline_comments`, `use_worktree`, `worktree_path`, `worktree_branch`, `worktree_summary`, `sandbox_tier` — last four only when `use_worktree = true`), **If `run_options.commit_strategy_resolved = "modular-commits"`:** top-level `plan_branch:` field **Else (`stacked-branches`):** (per-phase branch lives under the per-phase fields), completed-phases (with status, model**If `run_options.commit_strategy_resolved = "stacked-branches"`:** `, branch, base` **Else:** (no per-phase branch under modular), 5–15 line summary), current phase, remaining phases, deferred phases.

The orchestrator writes this from the git diff + the agent's summary — not from the agent's narration.

### 1h. Send brief update to user

One short paragraph: phase N done, branch pushed, PR opened, what got built, and — when the [Open PR via context file](#1f-open-pr-via-context-file) step ran — the PR-context file path with its `status` (`published` + URL when `open-pr.sh` opened the PR; `pending` when the script wasn't run because PR policy = branches only or deps were missing). When `status: pending`, mention how to publish later (`bash ai-tools/skills/open-pr-from-context/scripts/open-pr.sh <path>`). Moving to phase N+1. No long retrospective — tracking file is the durable record.

### 1i. Per-phase pause gate (opt-in)

`run_options.pause_between_phases = false` (default) → **immediately spawn the next phase**. Do not wait.

`run_options.pause_between_phases = true` → ask the user via `AskUserQuestion`:

- `Continue — start phase N+1`
- `Pause — stop here, I'll resume later by re-invoking the skill` (orchestrator exits cleanly; tracking file already records progress so the next invocation resumes mid-plan per the "Re-running mid-plan" section).
- `Stop — abort the plan run` (orchestrator stops; user decides next steps manually).

Wait for the answer. Don't spawn anything in the meantime. The pause is the user's review window — they may inspect the diff, the branch, the PR-context file, or the tracking file before agreeing to continue.

## Cross-repo phases

Phase in another repo:
1. **Do not implement.**
2. Mark in tracking under "Deferred Phases".
3. Continue to the next in-repo phase. Don't block on cross-repo work.

## Flag-removal phase (always out of scope)

Plan declared a flag → last phase is `Phase N — Remove the {flag-key} feature flag`. This skill **never** executes that phase. Flag removal is gated on real-world soak signal + is the exclusive responsibility of a dedicated flag-removal skill (separate skill).

What this skill does instead:
1. Identify the phase during Step 0; always exclude.
2. Mark in tracking as deferred.
3. End the run with a `/schedule` offer pointing at the dedicated flag-removal skill.
4. Refuse + redirect if user asks this skill to remove the flag.

## Re-running mid-plan

User invokes the skill against a partially-done plan:

1. Read `ai-plans/TRACKING_{plan-id}.md` if present. Extract `run_options.*` — including `worktree_path` / `worktree_branch` / `worktree_summary` when set. Never re-prompt the Step 0 opt-in questions on resume; the original answers stick.
2. **Worktree resume.** When `run_options.use_worktree = true`:
   - Confirm the worktree still exists (`git worktree list | grep <worktree_path>`). Missing → ask user: `Reprovision (run prepare-worktree again with the same name)`, `Switch to main checkout (flip use_worktree to false for the rest of the run)`, `Stop`.
   - Confirm the worktree summary file still parses; if not, regenerate from the existing worktree state.
   - **Re-probe `sandbox_tier`** (`command -v sandbox-exec || command -v bwrap`) — a resume may run on a different machine than the original provisioning. Update `run_options.sandbox_tier` in tracking before spawning; the [Spawn subagent](#1c-spawn-subagent) wrapping follows the re-probed value.
   - All resumed phases use the existing worktree — do not provision a second one.
3. `git -C <path> branch -a | grep plan/{plan-id-kebab}` to detect already-pushed phase branches (`<path>` = main checkout or worktree per `run_options.use_worktree`).
4. Cross-reference with the plan's phase list.
5. Confirm resumption point with the user.

## Step 2 — Final report

After all executable phases complete:

1. **Delete `TRACKING_{plan-id}.md`** on the last phase's branch. Commit. The plan file stays.
2. Send the user a final summary: **If `run_options.commit_strategy_resolved = "modular-commits"`:** single plan branch `plan/{plan-id-kebab}` with commit log organized by phase **Else (`stacked-branches`):** branches pushed (with bases, in stack order); deferred phases (cross-repo + flag-removal); next steps for the human. When `run_options.use_worktree = true`: include the worktree path + branch + summary file path + the teardown command (`git worktree remove <path>` + the per-engine drop-db / `docker compose -p <project> down -v` lines from `<worktree_summary>`). Do NOT auto-run teardown — the user may still want the worktree to debug review feedback or land follow-ups.
3. PR URLs for each phase, in stack order.
4. Flag-removal phase deferred → end with `/schedule` offer for the dedicated flag-removal skill.

## Important rules

- **Read AGENTS.md** in every phase prompt.
- **Stage explicitly.** No `git add -A`.
- **Subagents work in fresh sessions.** Each phase = a new subagent. Tracking + plan files = the context handoff.
- **Orchestrator owns git topology.** Subagents commit but never branch, push, or open PRs themselves.
- **No AI co-author trailers in commits.** The project forbids them; treat any AI trailer as a BLOCKER.
- **Trust the plan's per-phase model suggestion.**
- **Don't re-implement what a project skill encodes.**

- **Two-tier verification, in order, every phase.** Inner scoped, outer full repo.
- **Three-layer review, every phase, no exceptions.**
- **Orchestrator never edits code.**
- **Feature flags = gates, not toggles for tests.**
- **Never remove a feature flag from this skill.**
- **Stop on Tier-4 failure.**
- **Honor opt-in flags.** `run_options.pause_between_phases` controls the [Per-phase pause gate](#1i-per-phase-pause-gate-opt-in); `run_options.generate_inline_comments` controls whether the [Open PR via context file](#1f-open-pr-via-context-file) step drafts inline comments (always writes the file when that step runs at all — empty comments when off); `run_options.use_worktree` controls whether the [Provision worktree](#step-05--provision-worktree-when-run_optionsuse_worktree--true) step runs and whether every later `git` / lint / test / build / migrate call uses the worktree path.
- **One worktree per plan run.** When `use_worktree = true`, provision once in the [Provision worktree](#step-05--provision-worktree-when-run_optionsuse_worktree--true) step and reuse for every phase. Never spawn a second worktree mid-plan; never silently fall back to the main checkout on prepare-worktree failure (ask the user).
- **Don't auto-tear-down the worktree.** Step 2 surfaces the teardown command; the user runs it when they're ready (after reviewer feedback is addressed, after the PR merges, etc.).
- **Prevent stray main-checkout writes at the OS layer when you can; catch them always.** When `use_worktree = true` and a sandbox tool exists (`sandbox_tier = enforced`), wrap every subprocess subagent spawn in `prepare-worktree`'s `sandbox-run.sh` (deny main checkout, allow worktree + `.vinta-ai-workflows`) so main-checkout writes are blocked by the kernel regardless of harness — see [Spawn subagent](#1c-spawn-subagent). This is *prevention*; the prompt instruction alone is not. Regardless of tier, Layer 1 runs `git -C <main-checkout-path> status --short` after every implementer and fixer as the backstop; any tracked modification is a BLOCKER — recover the intent, re-dispatch to the worktree, then restore the main checkout clean. `<main-checkout-path>` is the repo root the skill was invoked from, never `run_options.worktree_path`.
- **PR-context file + `open-pr.sh` is the only PR-creation path.** No raw `gh pr create` / `glab mr create` calls outside the bundled script. The file is durable; the script is the publisher.
- **License check before any new dep.** Refuse `npm add` / `pnpm add` / `pip install` / `poetry add` / `uv add` / `cargo add` / `go get` when the package's SPDX license is in the forbidden list — see AGENTS.md **Dependency licenses**. User can grant a one-off override after acknowledging the violation; record the override in `policies.dependency_licenses.allowed_overrides` before re-running.
- **[Open PR via context file](#1f-open-pr-via-context-file) gating** = combination of project PR policy (PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).) and `generate_inline_comments`. See the matrix in that step. Skip it entirely only when policy = branches only AND comments = off.
- **Never use `§N` shorthand to point at sections** — neither in this skill body nor in any rendered file (tracking, prs-context, branch description). Always use the section's full name with a markdown link when possible. `§N` references are hard to read for humans and brittle when section numbering shifts.

## Quick checklist (orchestrator, per phase)

- [ ] Plan parsed; structured fields cached.
- [ ] Cross-repo + flag-removal phases identified + deferred.
- [ ] `run_options.use_worktree` resolved; [Provision worktree](#step-05--provision-worktree-when-run_optionsuse_worktree--true) ran when `true` (worktree provisioned + summary captured + `sandbox_tier` probed + user confirmed); skipped when `false`.
- [ ] Current phase: prompt composed with **Goals + Non-goals** + **Guiding Decisions** + relevant **Data Model Changes** subsection + tracking summaries + this phase's body (plus the worktree block when `use_worktree = true`).
- [ ] Model picked from `**Suggested AI model**:` line (cheapest available); plan tier recorded.
- [ ] Subagent spawned, report received. When `use_worktree = true` AND `sandbox_tier = enforced`: subprocess spawn wrapped in `sandbox-run.sh` (deny main, allow worktree + `.vinta-ai-workflows`), or the whole run sandboxed for in-process runtimes.
- [ ] Inner loop green: scoped lint + new tests individually + scoped suite.
- [ ] **Outer gate green:** `docker compose run --rm api uv run python manage.py check --deploy` AND `docker compose run --rm api uv run pytest -n auto` both passed.
- [ ] Layer 1 review: full diff read; no scope creep; no secrets; outer gate confirmed; no AI co-author trailer; when `use_worktree = true`, `git -C <main-checkout-path> status --short` clean (no stray main-checkout writes) after the implementer and after every fixer.
- [ ] Layer 2 review: every "Changes" ticked; every "Tests" materialized; acceptance line satisfiable; conventions, reusable skills, flag wiring all checked.
- [ ] Layer 3 review: adversarial review run; BLOCKERs fixed; SHOULD-FIX either fixed or noted.
- [ ] After any fix-up: Layers 1 + 2 + outer gate re-run.
- [ ] **If `run_options.commit_strategy_resolved = "modular-commits"`:** Plan branch updated with phase commits; pushed. **Else (`stacked-branches`):** Stacked branch created; pushed.

**If `run_options.commit_strategy_resolved = "modular-commits"`:**

- [ ] Commit units listed upfront before any staging.
- [ ] Each commit covers exactly one logical unit (no "and" in commit messages).
- [ ] Tests landed in the same commit as the code they cover (never a separate test-only commit).
- [ ] All unit commits pushed to `plan/{plan-id-kebab}` at end of phase.

- [ ] **Open PR via context file** decision applied per matrix (PR policy + `generate_inline_comments`):
  - [ ] PR-context file written when at least one of policy=create / comments=true holds.
  - [ ] `open-pr.sh` run when policy=create AND deps available; PR URL captured.
  - [ ] Per-comment failures (exit 1) surfaced with `(file:line)` list.
  - [ ] Hard failure (exit 2) surfaced; file left `status: pending`.
- [ ] `TRACKING_{plan-id}.md` updated.
- [ ] One-paragraph user update sent (PR URL or pending-file path included).
- [ ] If `run_options.pause_between_phases = true`: prompted user (`Continue` / `Pause` / `Stop`); honored answer.
- [ ] If `run_options.pause_between_phases = false`: next phase started immediately.
- [ ] On final phase: tracking file deleted; final summary lists branches with PR URLs; any `status: pending` PR-context files listed with publish command; `/schedule` offer for flag-removal if applicable.
