---
name: implement-plan
description: Execute a phased implementation plan from `ai-plans/` in vinta_schedule_api by orchestrating one subagent per phase (using whatever model the plan suggests and the runtime supports), running independent phases concurrently in their own worktree lanes when the plan's dependency graph allows it, pushing one branch per phase to GitHub, and tracking progress. Use when the user says "implement the plan", "execute plan X", "start implementation", "run phase N of plan Y", "implement {feature} plan", or asks to drive a `*_IMPLEMENTATION_PLAN.md` file phase-by-phase. NOT for one-off changes, single-file edits, or work that doesn't have an existing plan. Agents push branches and open PRs via `open-pr-from-context` after review passes.
disable-model-invocation: true
---

# Implement Plan

Drive a phased plan in [`ai-plans/`](ai-plans/) to completion. This skill is a **thin conductor**: it parses the plan once, builds the phase dependency graph, resolves a `WORKROOT` per lane, then runs a fixed three-step pipeline per phase — **several phases at a time when the graph allows it** — delegating the real work to focused sub-skills:

1. [implement-phase](../implement-phase/SKILL.md) — compose prompt, pick model, spawn the implementer.
2. [review-phase](../review-phase/SKILL.md) — the thermo-nuclear review loop over the phase diff.
3. the resolved integrate-phase variant — [integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) when `run_options.commit_strategy_resolved = stacked-branches`, else [integrate-phase-modular](../integrate-phase-modular/SKILL.md) — push the branch + open the PR via context file.

The conductor itself owns only: plan parsing, the dependency graph, phase classification, `WORKROOT` resolution, the scheduler, the progress-tracking directory, wave integration, the pause gate, and the final report. Harness-agnostic — claude-code, OpenAI Codex, Google's runtime, or any framework with a "spawn subagent with model + prompt" primitive.

**Parallel by default when the plan allows it.** The plan gives every phase a `**Depends on**:` line; phases whose dependencies are all green run **concurrently**, each in its own worktree lane. A plan whose graph is a straight chain runs exactly as it always did — one phase at a time. Sequential execution is the `max_parallel_lanes = 1` case of the same machinery, not a separate code path.

Execution counterpart to [plan-feature](../plan-feature/SKILL.md). Plan = contract; this skill = build pipeline.

## Not for an agent that was handed one phase

This skill **orchestrates**: it composes prompts, picks models and spawns other agents. Run it only when you are the session a user or a scheduler invoked to drive a plan.

If you are reading it because something handed you a single phase — a prompt naming your phase id, a branch already cut for you, a worktree you were told to stay inside, or an orchestrator such as [vinta-ai-maestro](https://github.com/vintasoftware/vinta-ai-workflows/tree/main/packages/vinta-ai-maestro) that spawned you — then the conductor this skill describes **is already running, and it is what spawned you**. Do not start a second one underneath it. Do the work in your own session, report back the way your prompt asked, and take from here only what it says about this repository's conventions, gates and commit rules.

The duplication is the smaller cost. A dispatched agent is deliberately reused — the same session takes the review findings, the chore over its own diff, and often the next phase — and that reuse is worth something only because the session that read the codebase is the session that gets the next turn. Hand your phase to a sub-agent and its reading of the code dies with it: you are left holding a summary, and every turn after yours starts cold.

## Working assumptions

- Repo: vinta_schedule_api (Django 6 + DRF + Strawberry GraphQL + Celery, multi-tenant (SingleOrganizationModelMixin), Postgres, deployed to AWS ECS/Fargate). Conventions: [AGENTS.md](../../../AGENTS.md).
- Plan files: [`ai-plans/YYYY-MM-DD-FEATURE_NAME_IMPLEMENTATION_PLAN.md`](ai-plans/).
- Lint: `docker compose run --rm api uv run ruff check ./`. Format: `docker compose run --rm api uv run ruff format ./`.
- Type / build gate: `docker compose run --rm api uv run python manage.py check --deploy` plus full mypy via `docker compose run --rm api uv run mypy .`.
- Unit / integration tests: `docker compose run --rm api uv run pytest -n auto` (everything); per-app via `docker compose run --rm api uv run pytest <app>/tests/ -n auto`.
- Migrations: `docker compose run --rm api uv run python manage.py makemigrations --check` (gate) + `docker compose run --rm api uv run python manage.py migrate` (apply). Raw-SQL DB code (functions, views, materialized views, triggers, procedures) routes through `common/raw_sql_migration_managers.py` — see [add-migration](../add-migration/SKILL.md). Deploy target: AWS ECS/Fargate — the staging deploy runs a release task (`migrate` then `collectstatic`) before it rolls the web, worker and beat services, so a failed migration stops the deploy (`scripts/deploy/ecs_deploy.sh`). Production has no deploy job yet.
- Code host: **GitHub**. PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).
- Co-author trailer policy: **forbidden**. Commits must not include `Co-Authored-By:` AI trailers.

## Step 0 — Locate + parse plan

Parse once, reuse for every phase:

1. **Identify plan file.** When the user's prompt names a path, use it. Otherwise `ls -t ai-plans/*_IMPLEMENTATION_PLAN.md` (grep for the feature name when one was given) and ask via `AskUserQuestion` (header `Plan file`), offering the 2–4 best matches, most recent first, each described by its feature name + date + whether a `TRACKING_*.md` exists. The free-text field covers any other path. A single unambiguous match still gets the question, with that plan as the only option plus `Cancel`.
2. **Extract structured fields**, in order:
   - **Feature name** + **plan id** — derived from filename's `FEATURE_NAME` portion only: strip `YYYY-MM-DD-` prefix + `_IMPLEMENTATION_PLAN.md` suffix. Kebab variant for branch names.
   - **Goals + Non-goals** section — verbatim, used in every phase prompt.
   - **Guiding Decisions** section — verbatim. Pay attention to: feature flag (key, scope, default, flip-on criterion), storage shape, tenant scoping, API contract decisions.
   - **Data Model Changes** section — keep full body; later phases reference earlier subsections.
   - **Phased Rollout** section — parse into phase records: `{ id, title, goal, body, spec_use_case, depends_on, wave, base_branch, crew_member, crew_tier, suggested_model_tier, reusable_skills, acceptance, is_cross_repo, is_flag_removal }`. `depends_on` comes from the phase's `**Depends on**:` line; `wave` and `base_branch` are **derived**, never read from the plan. `crew_member` comes from the phase's `**Assigned to**:` line, and its tier from the **Crew** table row of the same id; on a legacy plan with no Crew table, read `suggested_model_tier` off `**Suggested AI model**:` instead and leave `crew_member` null.
   - **Risk & Rollout Notes**, **Open Questions**, **Touch List** sections — keep available; include in phase prompts only when relevant. The **Touch List** additionally feeds the file-overlap warning in the graph step below.
3. **Classify each phase**: `is_cross_repo`, `is_flag_removal` — the conductor does NOT auto-execute these (see [Cross-repo phases](#cross-repo-phases) + [Flag-removal phase](#flag-removal-phase-always-out-of-scope)).
4. **Build the dependency graph** — see [Build the phase dependency graph](#build-the-phase-dependency-graph) below. Do this before the opt-in questions: the answer to the parallel-execution question depends on whether the graph has any wave wider than one phase.
5. **Ask the user the opt-in questions** via `AskUserQuestion`, batched into as few calls as the tool allows (Claude Code takes 4 questions per call; Codex takes 3). Put each question's default first with ` (Recommended)` on its label. Defaults are project-specific (see below); record every answer in tracking under `run_options`:

   a. **Pause between phases?** *"Do you want me to pause and wait for confirmation after each phase, before starting the next one? Lets you review the diff / branch / PR / tracking summary before moving on."* Options: `Auto-flow (default) — keep going phase to phase`, `Pause between phases — wait for go after each one`.

   b. **Draft inline review comments per phase?** *"On top of the standard PR description, do you want me to scan each phase's diff and add 3–10 inline comments calling out non-obvious decisions (subtle invariants, feature-flag short-circuits, cross-phase coupling, upstream-contract naming)? Off by default — say yes when reviewers will appreciate annotated diffs."* Options: `Yes — include inline comments`, `No — PR description only`.

   c. **Run phases in a worktree?** *"Do you want every phase's subagent to work inside an isolated git worktree (its own runnable copy of the app with its own dev + test DB, env files, docker-compose project name) instead of sharing your main checkout? Lets you keep using `main` for unrelated work while this plan runs; survives parallel plans on the same repo without DB / port / docker collisions. Costs one extra checkout's worth of disk + the time it takes to provision it."* Provisioning runs the project's `commands.worktree_prepare` when set, else the [prepare-worktree](../prepare-worktree/SKILL.md) skill — see [Pick the provisioner once](#step-05--resolve-workroot). Options: `No — run in current checkout`, `Yes — provision one shared worktree for the whole plan`. Default = value of `run_options.implement-plan.use_worktree` in `.vinta-ai-workflows.yaml` (`Yes` for this project).

      When `Yes` **and the run is sequential**: one worktree serves every executable phase — all phase branches live inside it. When `Yes` **and the run is parallel**: the conductor provisions a **pool** of worktrees, one per lane, plus one integration worktree — see [Provision the lane worktree pool](#provision-the-lane-worktree-pool). Either way the pool is sized once, at Step 0.5, and never grown mid-run.

      Skip this question entirely when worktrees are unavailable — `foundation_skills.prepare-worktree` is `disabled` **and** `commands.worktree_prepare` is unset in `.vinta-ai-workflows.yaml`: record `run_options.use_worktree = false`; surface a one-line note that worktree isolation is available if the team enables the skill via [vinta-sync-ai-tools](../../skills/vinta-sync-ai-tools/SKILL.md) or sets `commands.worktree_prepare` to its own provisioning script. When worktrees are unavailable, question (e) below is also skipped and the run is sequential — parallel execution has a hard worktree requirement.

   d. **Full test suite each phase?** *"Each phase's outer gate always runs the repo-wide type/build gate. For tests, do you want the quick path (run only the scoped suite covering the apps/files that phase touched — faster phases) or the full repo test suite every phase (slower, but guards against regressions in untouched code)? New tests still pass individually in the inner loop either way."* Options: `Quick — scoped tests only each phase (default)`, `Full — run the whole test suite each phase`. Default = value of `run_options.implement-plan.full_test_suite` in `.vinta-ai-workflows.yaml` (`Quick`/false for this project). Records `run_options.full_test_suite` (`true` only for the `Full` answer).

   e. **Run independent phases in parallel?** — **ask only when the graph has at least one wave wider than one phase.** *"{N} of this plan's phases have no dependency on each other, so they can be implemented at the same time — each in its own worktree with its own DB and compose stack. Widest point is {W} phases at once. Running them in parallel finishes the plan much faster; it costs one extra runnable checkout per lane and makes the run harder to watch step by step."* Options: `Yes — run up to {min(W, 3)} lanes at a time (default)`, `Yes — but cap at 2 lanes`, `No — one phase at a time`. Default = value of `run_options.implement-plan.parallel_phases` in `.vinta-ai-workflows.yaml` (**`true` when unset** — a plan that declares a parallel graph is asking to be run that way). Records `run_options.parallel_phases` + `run_options.max_parallel_lanes`.

      **Skip the question and record `parallel_phases = false`, `max_parallel_lanes = 1` when**: every wave holds exactly one phase (nothing to parallelize — say so in one line, don't ask), or worktrees are unavailable (skill `disabled` and no `commands.worktree_prepare`), or the user answered `No` to question (c). In the last two cases, when the graph *was* parallelizable, tell the user what they are giving up and why: parallel execution cannot share one working tree.

      `max_parallel_lanes` is capped by the widest wave and by `run_options.implement-plan.max_parallel_lanes` (default `3`). More lanes than the graph can keep busy just burns disk.

   PR opening itself is **not** asked here — it's governed by the project's PR creation policy captured at bootstrap (see `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` above). When that policy = "agents create PRs", the resolved integrate-phase variant — [integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) when `run_options.commit_strategy_resolved = stacked-branches`, else [integrate-phase-modular](../integrate-phase-modular/SKILL.md) step always opens the PR via [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh) regardless of the comment opt-in.

   f. **Commit strategy?** *"This project's commit_strategy is set to ask. Pick one for this run: one branch + one PR per phase (stacked), or one branch + one PR for the whole plan with one atomic commit per logical unit (modular)?"* Options: `Stacked branches — one branch + PR per phase (Recommended)`, `Modular commits — atomic commits, one PR for whole plan`. Cache answer in tracking under `run_options.commit_strategy_resolved`.

6. **Confirm with user before starting.** Show plan path, phase list (id + title + tier + cross-repo/flag-removal flags), phases this skill will execute vs defer, **the wave schedule** (which phases run together, and what each one waits on), branch naming pattern (depends on `run_options.commit_strategy_resolved` — resolved at Step 0), captured `run_options.pause_between_phases` + `run_options.generate_inline_comments` + `run_options.use_worktree` + `run_options.full_test_suite` + `run_options.parallel_phases` + `run_options.max_parallel_lanes` + `run_options.commit_strategy_resolved`, and that each phase will push its branch and open a PR on GitHub.

   Then ask via `AskUserQuestion` (header `Start run`): `Start the run (Recommended)` (name the phases the scheduler dispatches first), `Change run options` (re-ask the opt-in questions, then show this summary again), `Cancel` (exit; nothing written). Never ask the user to type "go". After that, the per-phase pause behavior follows `run_options.pause_between_phases`. Inline-comment drafting follows `run_options.generate_inline_comments`. Worktree isolation follows `run_options.use_worktree`. Outer-gate test scope follows `run_options.full_test_suite`. Concurrency follows `run_options.parallel_phases` + `run_options.max_parallel_lanes`. Commit-strategy behavior follows `run_options.commit_strategy_resolved`.

## Build the phase dependency graph

The plan's **Phased Rollout** section opens with an **Execution graph** table and gives every phase a `**Depends on**:` line ([plan-feature](../plan-feature/SKILL.md) authors both). Parse them into a DAG once, alongside the rest of the plan:

1. **Read each phase's `**Depends on**:` line** into `phase.depends_on` — a list of phase ids, or `[]` for `nothing`. Ignore the prose after the em dash; it explains *why* the edge exists and is passed through to the implementer prompt, not to the scheduler.
2. **Compute `phase.wave`** — longest path from a root: `wave(P) = 1` when `depends_on` is empty, else `1 + max(wave(d) for d in P.depends_on)`. Waves are **derived, never read from the plan**; a plan whose **Execution graph** table disagrees with the computed waves is a plan bug — show the mismatch in the start-run summary and go with the computed values.
3. **Validate the graph.** Each failure below stops the run before any phase is dispatched. Every "ask" in this step and the next two is an `AskUserQuestion` call, batched up to 4 problems per call, each question naming the phase and quoting the offending line:
   - **Unknown id** in a `**Depends on**:` line → `Stop — I'll fix the plan (Recommended)`, `Drop the unknown edge` (write the corrected line back into the plan).
   - **Cycle** → print the cycle (`Phase 3 → Phase 5 → Phase 3`) in the question; `Stop — I'll fix the plan (Recommended)`, plus one `Drop edge <a> → <b>` option per edge in the cycle (up to 3).
   - **Missing line** on any executable phase → do **not** guess. Ask: `Depends on everything before it (Recommended)` (safe, serializing), `Depends on nothing` (parallel). The free-text field takes an explicit id list. Write the answer back into the plan file before starting, so a resume reads the same graph.
   - **Dependency on a deferred phase** (cross-repo or flag-removal, which this skill never executes) → that dependent is also deferred, transitively. Report both.
4. **Warn on file overlap.** For every pair of phases in the same wave, intersect their **Touch List** entries. A non-empty intersection means two lanes will edit the same file concurrently and the wave merge will conflict. Put the pairs and the shared paths in the question, then ask: `Serialize them (Recommended)` (add a dependency edge and re-derive waves), `Run anyway — I'll take the merge conflict`, `Stop — let me fix the plan`. This is a warning, not a hard stop: two phases legitimately touching one `__init__.py` merge fine, two rewriting the same use case do not.
5. **Read the plan's staffing, and check it against the graph you just computed.** The **Crew** table and the phases' `**Assigned to**:` lines are one decision in two places, and the arithmetic they claim is checkable before anything is dispatched:
   Each "stop and ask" below offers `Stop — I'll fix the plan (Recommended)` plus the concrete repair when one exists (`Assign to <member>`, `Drop <member> from the roster`), and writes an accepted repair back into the plan.
   - **An `**Assigned to**:` naming an agent the table does not list** → stop and ask. Don't invent a member: the roster is the plan's answer to "how many agents does this feature need", and adding one changes it.
   - **A phase with no `**Assigned to**:` line in a plan that has a Crew table** → stop and ask, for the same reason. Half a roster means two staffing rules running at once.
   - **A declared member assigned no phase** → stop and ask. That is an agent the plan budgeted for and never uses.
   - **Reviewer rows on the table** (a plan written for the older review) → not an error. Reviews now run one tier above each phase's implementer, so those rows staff nothing: leave them out of the roster, and say so once in the pool report.
   - **A wave the roster cannot staff** → warn. Sort the wave's assigned tiers and the roster's tiers, compare one for one: a wave of two Tier 3 phases needs two members at Tier 3 or above, and a Tier 1 member on the roster does not help because the floor forbids handing them one. Such a wave still runs — it serializes — so say so now rather than letting it look like a slow machine later.
   - **A plan with no Crew table at all** is a legacy plan. Read each phase's `**Suggested AI model**:` tier instead and skip every check above.
6. **Record the resolved graph and the roster** in `run.md` (see [Update tracking](#1d-update-tracking)) so a resume rebuilds the identical schedule and the identical staffing without re-asking anything.

**The graph decides ordering — plan order does not.** Phase numbering is a reading aid for humans. A phase with no dependencies runs in wave 1 no matter how high its number is.

## Agent models — reviewer, conflict fixer, and mechanical steps

The per-phase **implementer** model stays plan-owned: the plan's **Crew** table names the agents and their tiers, and each phase's `**Assigned to**:` line names which one takes it (see [implement-phase](../implement-phase/SKILL.md)). The **reviewer** is derived, never configured: [review-phase](../review-phase/SKILL.md) runs it one tier above the tier the phase's implementer actually ran at. The conflict-fixer and mechanical-step models are `.vinta-ai-workflows.yaml`'s `agent_models` only, and never plan-owned. Read that section once in [Step 0](#step-0--locate--parse-plan) alongside `run_options`. A plan written for the older review may still carry a `Role` column and reviewer rows on its **Crew** table, or `**Review models**:` lines on its phases: ignore all three.

## Resolve a tier to a spawn model

Every model choice below is a **tier** (1–4) into the same table the per-phase implementer uses — [`ai-tools/skills/plan-feature/resources/ai-models.yaml`](../plan-feature/resources/ai-models.yaml). Where the tier comes from depends on the role:

- **`reviewer`** — one tier above the phase's implementer, derived by [review-phase](../review-phase/SKILL.md). Never configured and never plan-named. (`agent_models.reviewer` in older configs, and a plan's `**Review models**:` line or reviewer rows on its **Crew** table, are ignored.)
- **`fixer`** — `agent_models.fixer`, for the merge-conflict fixer. A review finding is not this role's: the phase's own implementer fixes it, at its own tier.
- **`worktree_prep`, `integrate`** — `agent_models.<role>`, for the mechanical steps below. Never plan-named.

To turn the tier into the model a spawn actually uses:

1. **No tier** (an `agent_models` key unset, or the whole section absent) → do not force a model. Spawn with the runtime's default model (today's behavior). Skip the rest.
2. Open [`ai-tools/skills/plan-feature/resources/ai-models.yaml`](../plan-feature/resources/ai-models.yaml), take that tier's `models`, **filter to the vendors the runtime actually exposes**, pick the cheapest/fastest survivor, and translate it to the runner's spawn form — the same resolution [implement-phase](../implement-phase/SKILL.md) runs for the implementer.
3. `ai-models.yaml` missing, or the tier has no runtime-available vendor → fall back to the runtime default and surface the fallback once. Never hard-fail a phase over a model-selection miss.
4. The resolved model is out of quota or credits and its `ai-models.yaml` entry carries a `fallback:` → spawn on the fallback instead, by the same **Out of quota** rule the implementer follows in [implement-phase](../implement-phase/SKILL.md).

Record the **model actually used** in tracking: for the reviewer, alongside the review note in the phase's record; for the mechanical steps, in the phase's tracking row next to the branch/PR fields.

## Delegate a mechanical step to a configured model

Two steps the conductor would otherwise run **inline in its own (usually pricier) session** — provisioning the worktree with the [prepare-worktree](../prepare-worktree/SKILL.md) skill (a project's `commands.worktree_prepare` always runs inline instead, since a shell command needs no model) and integrating a phase ([integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) / [integrate-phase-modular](../integrate-phase-modular/SKILL.md): push the branch + open/update the PR through the bundled `open-pr.sh`) — are mechanical, precedent-driven work that a cheap model handles fine. The `agent_models.worktree_prep` / `agent_models.integrate` tiers let a project push that work down.

- **Tier set** (`worktree_prep` / `integrate`) → **spawn exactly one subagent** at the [resolved model](#resolve-an-agent_models-tier-to-a-spawn-model), hand it the step's SKILL.md plus the same inputs the conductor would use, and consume its returned report exactly as if the conductor had done the work inline. This subagent is a **labor delegate, not a decision-maker**: the conductor still owns git topology (which branch stacks on which base) and still holds every value the step returns (`WORKROOT` / `BASE_BRANCH` / worktree summary for `worktree_prep`; branch + PR-context path + `status` for `integrate`). The delegate executes and reports those back.
- **Tier unset** → run the step inline in the conductor's own session — today's behavior, no subagent.

Rules that hold **regardless of who runs the step**:

- The **PR-context file + `open-pr.sh` is still the only PR-creation path.** An `integrate` delegate uses the bundled script; it never calls raw `gh pr create` / `glab mr create`.
- The delegate is `read-write` (worktree provisioning writes dirs/DBs; integrate pushes + writes the PR-context file) but **makes no plan or code decisions** — a malformed or failed delegate report is surfaced to the user, never worked around.
- This delegation is **separate from the phase-work sub-agents** (implementer / reviewer / conflict fixer). Those still never branch, push, or open PRs — that prohibition is about code-authoring agents, not the dedicated mechanical delegate the conductor spawns to run the integrate step itself.

## Step 0.5 — Resolve `WORKROOT`

Resolve three values **once per lane**, before any phase runs, and record them in tracking. Every later step uses them as data — no step re-derives worktree state. A sequential run has exactly one lane, so "once per lane" and "once" are the same thing.

| Value | `run_options.use_worktree = false` | `run_options.use_worktree = true` |
|---|---|---|
| `WORKROOT` | the main checkout root (the repo the skill was invoked from) | `<worktree_path>` returned by the provisioner — **this lane's** path under parallel execution |
| `BASE_BRANCH` | `main` | `<worktree_branch>` the provisioner created |
| `SANDBOX_TIER` | `none` | `enforced` or `none` (probed per lane) |

**Pick the provisioner once.** `.vinta-ai-workflows.yaml` decides how a worktree gets made:

| Config | Provisioner |
|---|---|
| `commands.worktree_prepare` set | **The project's own command** — see [Provisioning with the project's command](#provisioning-with-the-projects-command). It wins even when the skill is also enabled; the skill is then only offered as a fallback when the command fails. |
| `commands.worktree_prepare` unset, `foundation_skills.prepare-worktree: enabled` | **The [prepare-worktree](../prepare-worktree/SKILL.md) skill.** |
| neither | None — worktrees are unavailable, and Step 0 already recorded `use_worktree = false`. |

Record the choice as `run_options.worktree_provisioner: command | skill` in tracking. Both provisioners hand back the same four values (`worktree_path`, `worktree_branch`, `worktree_summary`, `sandbox_tier`), so no later step checks which one ran.

**When `run_options.parallel_phases = true`:** skip the single-worktree path below entirely and provision the pool per [Provision the lane worktree pool](#provision-the-lane-worktree-pool). That step resolves one `WORKROOT` / `SANDBOX_TIER` per lane plus one integration worktree, and refuses the run outright when worktrees are unavailable. `BASE_BRANCH` stays plan-level (`main`, or the worktree base when the whole plan hangs off one); each phase's *own* base is derived from its dependencies, not from `BASE_BRANCH` — see [Lane branch topology](#lane-branch-topology).

**When `use_worktree = false`:** set `WORKROOT` = main checkout, `BASE_BRANCH = main`, `SANDBOX_TIER = none`. Make `BASE_BRANCH` current + up to date: `git -C <WORKROOT> checkout main && git -C <WORKROOT> pull --ff-only`. Jump to Step 1.

**When `use_worktree = true`:** provision **once**, with the provisioner picked above.

- **Skill.** This is a mechanical step: when `agent_models.worktree_prep` is set, **delegate it to a subagent** per the [Delegate a mechanical step to a configured model](#delegate-a-mechanical-step-to-a-configured-model) pattern (hand the subagent prepare-worktree's SKILL.md + the inputs below; consume its returned `worktree_path` / `worktree_branch` / `worktree_summary` / `sandbox_tier` report). When the tier is unset, run it inline.
- **Command.** Run it inline, always. It is a shell command, so there is nothing for a model to do, and `agent_models.worktree_prep` is ignored.

The steps below read the same either way:

1. **Inputs.** Plan path (so the provisioner can read it for deps / migrations / env / compose churn — see prepare-worktree's **Plan inspection** step), suggested worktree name = `plan-{plan-id-kebab}`, plan-driven mode. The command receives these as `VINTA_*` environment variables.
2. **Pre-run sanity.** Confirm no existing worktree at the target path (`git worktree list | grep <name>` — refuse if collision). Confirm `git -C <main_checkout> status` of the main checkout (warn if dirty; with the skill, defer to prepare-worktree's **Sanity checks** step for the call).
3. **Run the provisioner.** The skill gets the plan file + worktree name; the command gets [its contract](#provisioning-with-the-projects-command). Either one yields:
   - `worktree_path` → `WORKROOT`.
   - `worktree_branch` → `BASE_BRANCH` (based on `origin/main`, so it is already current).
   - `worktree_summary` — `<summary_dir>/<name>.yaml` (read by teardown and lane reset). `null` when the project's command wrote none.
   - `sandbox_tier` → `SANDBOX_TIER`: `enforced` (`sandbox-exec` / `bwrap` was found and the [Filesystem sandbox](../prepare-worktree/SKILL.md#step-55--filesystem-sandbox-os-level-write-guard) wrapper will OS-block main-checkout writes) or `none` (no sandbox tool — prevention degrades to the review-phase stray-write backstop).
4. **Persist to tracking.** Write `run_options.worktree_provisioner`, `run_options.worktree_path`, `run_options.worktree_branch`, `run_options.worktree_summary`, `run_options.sandbox_tier` into `ai-plans/TRACKING_{plan-id}/run.md`. All later phases read them — never re-provision mid-plan.
5. **Report to user.** With the skill, quote its summary back: which dirs copied vs reinstalled vs forked (dependency dirs are always copied or reinstalled, never symlinked); which DB(s) forked + their names; compose project name; teardown command. With the command, quote the last ~20 lines of its output, the summary YAML when it wrote one, and the teardown command. Hold here until the user confirms (`AskUserQuestion`: `Looks good — start phase 1 (Recommended)`, `Stop — let me adjust`).

Failure modes:
- **The provisioner fails** (disk full, branch exists, DB clone failed, the command exited non-zero or failed its post-checks) → surface to the user; do NOT fall back to "just run in the main checkout" silently — that defeats the opt-in. Ask via `AskUserQuestion` (header `Worktree`), quoting the error line: `Retry (Recommended)`, `Run in main checkout instead (flip use_worktree to false)`, `Stop`. When the failed provisioner was the command **and** `foundation_skills.prepare-worktree` is `enabled`, add `Provision with the prepare-worktree skill instead` as the second option. Before a retry or the skill fallback, tear down whatever the command left behind (see [Teardown with the project's command](#teardown-with-the-projects-command)).
- **User cancels at the confirmation gate** → tear the worktree down (the skill's reported teardown command, or [the command teardown](#teardown-with-the-projects-command)) before exiting, so the next run starts clean.

### Provisioning with the project's command

`commands.worktree_prepare` is a shell command the team owns. The conductor runs it once per worktree, from `<main_checkout>`, with stdin closed, through `sh -c '<commands.worktree_prepare>'`, and with these variables set in its environment:

| Variable | Value |
|---|---|
| `VINTA_WORKTREE_NAME` | The worktree name: `plan-{plan-id-kebab}` for a single worktree, or the lane / integration name from the [pool step](#provision-the-lane-worktree-pool). Distinct per worktree, so the command should derive every DB name, compose project name, cache key and port from it. |
| `VINTA_WORKTREE_PATH` | Absolute path to create the worktree at: `skills.prepare-worktree.worktree_root` (default `.claude/worktrees`) resolved against `<main_checkout>`, then `/<name>`. A root that ends in `-` (the `../<repo>-wt-` sibling convention) takes `<name>` with no slash. |
| `VINTA_WORKTREE_BRANCH` | The new branch to create: `plan/{plan-id-kebab}/wt` for a single worktree, `plan/{plan-id-kebab}/wt-<name>` for a pool lane or the integration worktree. Phase branches are cut inside the worktree later. |
| `VINTA_WORKTREE_BASE_REF` | `origin/main`. |
| `VINTA_WORKTREE_KIND` | `single`, `lane` or `integration`. |
| `VINTA_MAIN_CHECKOUT` | Absolute path of `<main_checkout>`. |
| `VINTA_PLAN_PATH` | Absolute path of the plan file. |
| `VINTA_WORKTREE_SUMMARY` | Absolute path `<main_checkout>/<summary_dir>/<name>.yaml` (`summary_dir` from `skills.prepare-worktree.summary_dir`, default `.vinta-ai-workflows/worktrees`). |

**The command must:**

- Create a git worktree at `VINTA_WORKTREE_PATH`, on a new branch `VINTA_WORKTREE_BRANCH` based on `VINTA_WORKTREE_BASE_REF`. Fetching first is its call.
- Leave it runnable. The project's lint, test, build and migrate commands must work inside it without sharing writable state with the main checkout or with another worktree: its own dependency dirs, env files, databases and compose project, whatever the project needs.
- Exit non-zero on any failure. Never report success for a half-made worktree.
- Never write to the main checkout's tracked files, and never prompt for input.
- Succeed when run again for the same name after its teardown ran. Re-provisioning a lane depends on this.

**The command may** write a summary YAML at `VINTA_WORKTREE_SUMMARY`. If it does, the file must follow the summary shape in prepare-worktree's [Write the summary file](../prepare-worktree/SKILL.md#step-6--write-the-summary-file) step in full: every key present, `null` for "none", and only the listed values for each closed set. Other tools parse this file, so a partial summary is worse than none. When the skill is not installed, the shape is documented in the [vinta-ai-workflows source](https://github.com/vintasoftware/vinta-ai-workflows/blob/main/skills/vinta-derive-skills/resources/foundation-skills/prepare-worktree/SKILL.md#step-6--write-the-summary-file). The conductor reads `state.dev_db.reset_cmd` / `state.test_db.reset_cmd` from it for lane reuse. Without a summary, the lane has no reset command, and the [lane reset](#re-orienting-a-member-after-the-reset) re-provisions it instead of reusing it across a migration boundary.

**Check the result. Never trust the exit code alone.** Capture `git -C <main_checkout> status --short` before the command runs; after it exits 0, all three checks must hold:

```bash
git -C "$VINTA_WORKTREE_PATH" rev-parse --abbrev-ref HEAD                       # prints $VINTA_WORKTREE_BRANCH
wt_real="$(cd "$VINTA_WORKTREE_PATH" && pwd -P)"                                # git lists resolved paths
git -C <main_checkout> worktree list --porcelain | grep -Fx "worktree $wt_real"
git -C <main_checkout> status --short                                           # identical to the capture taken before the command
```

If any check fails, the provisioning failed; handle it with the failure modes above. Fill the four values:

- `worktree_path` = `VINTA_WORKTREE_PATH`; `worktree_branch` = `VINTA_WORKTREE_BRANCH`.
- `worktree_summary` = `VINTA_WORKTREE_SUMMARY` when the file exists, else `null`.
- `sandbox_tier`: probe it yourself, because the sandbox governs how agents are spawned, not how the worktree was made. It is `enforced` when `ai-tools/skills/prepare-worktree/scripts/sandbox-run.sh` exists in the main checkout **and** `sandbox-exec` (macOS) or `bwrap` (Linux) is on `PATH`. Otherwise it is `none`. With the skill disabled, the sandbox scripts are not installed, so the tier is `none` and the review-phase stray-write check is the only guard. Say so once in the report.

**Pools run the command one lane at a time.** The command runs `git worktree add` itself, and concurrent adds against one repository corrupt `.git/worktrees/`. The skill path can overlap the expensive per-lane work. The command path cannot, because the add and the rest of the work happen inside one opaque command.

### Teardown with the project's command

Run `commands.worktree_teardown` from `<main_checkout>` with the same `VINTA_*` environment the worktree was provisioned with. When it is unset, teardown is `git -C <main_checkout> worktree remove <worktree_path>`. That removes only the checkout, so say in the report that anything the prepare command created outside it (forked DBs, compose volumes) is left behind.

**Re-provisioning a lane** runs teardown, then the prepare command with the same `VINTA_WORKTREE_NAME` and `VINTA_WORKTREE_PATH` and a fresh `VINTA_WORKTREE_BRANCH` (`<branch>-r<n>`): the old branch outlives `git worktree remove`, and `git worktree add -b` refuses an existing branch.

The final report prints each worktree's teardown as one ready-to-run line, with the environment inline:

```bash
VINTA_WORKTREE_NAME=<name> VINTA_WORKTREE_PATH=<path> VINTA_WORKTREE_BRANCH=<branch> VINTA_WORKTREE_KIND=<kind> VINTA_MAIN_CHECKOUT=<main_checkout> VINTA_PLAN_PATH=<plan> VINTA_WORKTREE_SUMMARY=<summary> sh -c '<commands.worktree_teardown>'
```

## Provision the lane worktree pool

Parallel execution **requires** a worktree provisioner: the project's `commands.worktree_prepare`, or the [prepare-worktree](../prepare-worktree/SKILL.md) skill (see [Pick the provisioner once](#step-05--resolve-workroot)). Two agents cannot write two branches in one working tree, and two concurrent test runs cannot share one dev / test database or one compose project name.

**Hard gate — refuse rather than degrade.** When `run_options.parallel_phases = true` but worktrees are unavailable — `foundation_skills.prepare-worktree` is `disabled` and `commands.worktree_prepare` is unset, or the user answered `No` to the worktree question, or provisioning fails — **stop and tell the user why**. Do not silently fall back to sequential; the plan's whole schedule was built around concurrency. Offer: `Enable worktrees and continue in parallel`, `Run this plan sequentially instead (max_parallel_lanes = 1)`, `Stop`. The user picks; the conductor never picks for them.

**Pool, don't provision per phase.** Provisioning a runnable worktree costs a dep install plus a DB fork. A plan with 14 phases must not pay that 14 times. Provision **`max_parallel_lanes` worktrees once** and reuse each one across the phases assigned to it:

1. **Size the pool by the roster: one worktree per *implementer***, named for the member rather than numbered. A plan with no **Crew** table falls back to `lanes = min(run_options.max_parallel_lanes, widest_wave)`.

   **An implementer keeps their worktree for the whole run**, and that is the point rather than a detail. A sub-agent can only be continued into a directory it is already standing in, so a member that moved between phases would have to start cold every time — which is most of a phase's first turn spent rediscovering a codebase the same agent read an hour ago. Pinning the directory is what makes "reuse the agent" possible at all.

   It costs a checkout and a set of forked databases per implementer, including for members idle in most waves. That is the trade, and it is worth stating to the user in the pool report rather than discovering on a full disk.

   **The reviewer works in the lane it is reviewing.** [review-phase](../review-phase/SKILL.md) spawns it per phase, in that phase's `WORKROOT`, and it gets no worktree of its own.
2. **Provision each lane.** Run the provisioner once per lane, plan-driven, with worktree name `plan-{plan-id-kebab}-crew-{implementer-id}` (or `plan-{plan-id-kebab}-lane-{i}` on a plan with no roster).
   - **Skill:** this is the mechanical `worktree_prep` step. Delegate all of them per the [Delegate a mechanical step to a configured model](#delegate-a-mechanical-step-to-a-configured-model) pattern when `agent_models.worktree_prep` is set, and **dispatch the provisioning calls concurrently**, because they are independent.
   - **Command:** run `commands.worktree_prepare` inline with `VINTA_WORKTREE_KIND=lane`, **one lane at a time**, and post-check each one per [Provisioning with the project's command](#provisioning-with-the-projects-command).
3. **Provision the integration worktree.** One more, named `plan-{plan-id-kebab}-integ` (`VINTA_WORKTREE_KIND=integration` for the command). The conductor merges lane branches into wave integration branches here (see [Lane branch topology](#lane-branch-topology)) so a merge never disturbs a lane that is still working.
4. **Record the pool** in `run.md`: for each lane, `workroot`, `branch`, `worktree_summary`, `sandbox_tier`, plus `current_phase` (null when idle). `SANDBOX_TIER` is probed **per lane** — a mixed result is possible in principle and each lane's spawn wrapping follows its own tier.
5. **Report once, then hold.** Show the user the pool (paths, DB names, compose project names, teardown commands) and the computed wave schedule together. `AskUserQuestion`: `Looks good — start (Recommended)`, `Fewer lanes`, `Stop — let me adjust`.

### Re-orienting a member after the reset

A lane worktree carries state from the phase it just ran — most dangerously **applied migrations** in its forked dev / test DB. The next phase assigned to that lane branches from a different base, which may not contain those migrations, and a leftover schema silently invalidates its test run.

**And the agent standing in it has a memory of the old tree.** Continuing a member across phases is only safe if it is told what moved, so the message that starts its next phase is the phase's **full brief** — it is new work, not a delta — preceded by three facts:

1. **It is the same agent in the same directory**, and everything it knows about the repository's layout, conventions and tooling still holds. Say so: an agent told only that things changed will re-read work it does not need to.
2. **Whether the previous phase's own work is in this tree** — true exactly when this phase depends on it. This is the sentence that matters most. An agent that remembers writing a model and does not know it is absent will code against something that is not there.
3. **Which files differ from what it last saw**, as a list:

   ```bash
   git -C <lane.workroot> diff --name-only plan/{plan-id-kebab}/phase-{prior.id} HEAD
   ```

   A list is checkable; "things may have changed" invites the agent to decide for itself what to trust. If the diff cannot be computed, say the set is **unknown** and to re-read before editing — never that nothing changed, which is the one wording that would stop it re-reading.

**Start a member cold when their previous phase failed.** Their session is the context that failed with it, and whatever wrong turn it took is exactly what a continuation preserves. Re-reading a repository is cheaper than inheriting a wrong conclusion about it.

Before handing a lane to its next phase:

```bash
git -C <lane.workroot> checkout <phase.base_branch>
git -C <lane.workroot> checkout -b plan/{plan-id-kebab}/phase-{phase.id}
```

then **reset that lane's databases to the new base** using the `reset_cmd`s recorded under `state.dev_db` / `state.test_db` in the lane's `worktree_summary` (prepare-worktree writes them, and so may the project's command — drop + recreate from the template, or re-run migrations from zero, per engine). A lane with no `reset_cmd` (or no summary at all), and whose outgoing or incoming phase touches the **Data Model Changes** section, is not safe to reuse: re-provision that lane instead (with the command: [teardown, then prepare again](#teardown-with-the-projects-command)). Never reuse a lane across a migration boundary without the reset.

Dep churn matters less but is real: when the incoming phase's plan body installs dependencies, re-run the project's install command in that lane before dispatching.

### Teardown

The pool is torn down **only at the end of the run, and only by the user**. [Step 2](#step-2--final-report) prints every lane's teardown command plus the integration worktree's. Do not auto-run them — a failed lane's worktree is the only place its state survives.

**`WORKROOT` topology rule.** Every phase branches off **its own computed base** — the branch derived from that phase's `**Depends on**:` set, which is `<BASE_BRANCH>` for a phase with no dependencies (see [Lane branch topology](../implement-plan/SKILL.md#lane-branch-topology)) — and **every** `git` / lint / test / build / migrate call runs with `git -C <WORKROOT>` (or after `cd <WORKROOT>`). When `use_worktree = false`, `WORKROOT` is the main checkout and phases run one at a time in place; when `true`, `WORKROOT` is a worktree and branches / commits live inside it, never touching the main checkout's working tree. Under parallel execution `WORKROOT` is **this lane's** worktree and nothing else — a lane never reads or writes a sibling lane's tree. One uniform path — no per-step worktree branching.

## Lane branch topology

Every phase still gets its own branch. What changes under a DAG is **what that branch is based on** — no longer "the previous phase in plan order", but the phase's own declared dependencies.

**Base branch of phase `P`:**

| `P.depends_on` | `P.base_branch` |
|---|---|
| empty | `<BASE_BRANCH>` |
| exactly one phase `Q` | `plan/{plan-id-kebab}/phase-{Q.id}` |
| two or more phases | `plan/{plan-id-kebab}/integ-{P.id}` — built by merging every dependency's branch (see below) |

**Multi-dependency base.** Built in the **integration worktree**, before the phase is dispatched:

```bash
git -C <integ.workroot> checkout -B plan/{plan-id-kebab}/integ-{P.id} plan/{plan-id-kebab}/phase-{first-dep.id}
git -C <integ.workroot> merge --no-ff plan/{plan-id-kebab}/phase-{next-dep.id}   # once per remaining dep, in plan order
git -C <integ.workroot> push -u origin plan/{plan-id-kebab}/integ-{P.id}
```

**Wave integration branches** are the durable spine. `plan/{plan-id-kebab}/wave-0` is `<BASE_BRANCH>`. When every phase at wave `N` has passed review, the conductor builds `plan/{plan-id-kebab}/wave-{N}` in the integration worktree by merging each wave-`N` lane branch into `wave-{N-1}` with `--no-ff`, in plan order. Wave branches are what a resume anchors on, what the final report points at, and what a phase whose dependency set covers an entire earlier wave may use directly as its base.

**A phase does not wait for its wave — it waits for its dependencies.** Wave branches are built behind the scheduler, not in front of it: a wave-3 phase whose two dependencies are both green starts immediately, even while other wave-2 phases are still running. The wave branch is bookkeeping and integration; the `depends_on` set is the gate.

### Merge conflicts during integration

The orchestrator **never edits code**, including merge conflicts. On a conflicted `merge`:

1. Capture `git -C <integ.workroot> diff --name-only --diff-filter=U`.
2. Spawn a **fixer** subagent (the project's `fixer` agent type, at the `agent_models.fixer` model) inside the integration worktree. Its prompt carries: the conflicted paths, both phases' bodies from the plan, both phases' `phase-{id}.md` summaries, and the instruction to resolve for **both** intents — never to `--ours` / `--theirs` a conflict away.
3. After the fixer returns, re-run the **outer gate** (`docker compose run --rm api uv run python manage.py check --deploy` plus the test scope `run_options.full_test_suite` selects) in the integration worktree. Red → loop back to step 2 with the failure.
4. Green → commit the merge, push the branch, and record the conflict + resolution in `waves/wave-{N}.md`.

A conflict that survives its fixer-round budget (default **two**) is a **plan defect**, not a code problem: two phases in the same wave own the same code. Stop and ask via `AskUserQuestion` (header `Wave merge`), naming both phases and the paths: `Serialize them (Recommended)` (add the edge, re-derive waves, re-run the loser), `I'll resolve it by hand`.

The budget is an **integration-level** setting, not either phase's `max_fix_rounds`. A conflict belongs to a *pair* of phases, so deriving it from one of them would make the answer depend on which phase happened to merge second.

**Confirming a fix requires reading the files, not asking git.** `git add` clears a path's unmerged flag whether or not `<<<<<<<` is still sitting in it, so git cannot tell you whether the fixer actually resolved anything. Scan the conflicted paths for conflict markers before committing the merge. Skip this and a fixer that did nothing produces a merge commit full of markers that passes straight into the wave branch.

### PRs: one per phase, plus what it takes to reach `<BASE_BRANCH>`

The PR `base` written into the prs-context frontmatter is the phase's **computed `base_branch`** — `<BASE_BRANCH>`, a single dependency's branch, or the `integ-{P.id}` branch. Never `<BASE_BRANCH>` for a phase that has dependencies; a wrong base makes the PR diff include every upstream phase and the review is unusable.

Phase PRs are review units, and on their own they do not land the plan. Under a per-phase PR strategy the conductor also opens:

- **An integration PR per `integ-{P.id}` branch**, into `<BASE_BRANCH>`, just before `P`'s own PR. Without it `P`'s PR targets a branch nothing targets, and nothing stacked on `P` can reach `<BASE_BRANCH>`.
- **A plan PR at run end**, from the final `wave-{N}` branch into `<BASE_BRANCH>`. It is the only branch carrying the conflict resolutions made during wave merges, so it is merged last whichever way the plan lands. Its description lists every phase and integration PR in merge order.

The commit strategy's PR-open timing says which files to write and when.

## Step 1 — Scheduler loop

Every phase that's `not is_cross_repo and not is_flag_removal` goes through the same three-step pipeline. **What the scheduler decides is when** — a phase becomes eligible the moment every id in its `depends_on` is green, and it runs as soon as a lane is free.

### Dispatch loop

Continuous, dependency-driven. A phase starts the moment its dependencies are green and a lane is free — it does **not** wait for its wave to fill or drain.

```
DONE = {}            # phase ids that passed review + integrate
RUNNING = {}         # lane -> phase currently in flight
BLOCKED = {}         # phase ids whose upstream failed
PENDING = every executable phase (not cross-repo, not flag-removal)

while PENDING or RUNNING:
    ready = [p for p in PENDING
             if set(p.depends_on) <= DONE
             and p.id not in BLOCKED]

    while ready and some lane is idle:
        p = ready.pop(0)                      # plan order breaks ties
        agent = claim_agent(p)                # the plan's member, a qualified peer, or None
        if agent is None: continue            # every hand at or above p's tier is busy
        lane = claim_idle_lane()
        reset_lane(lane, p)                   # checkout base + branch + DB reset
        dispatch(lane, p, agent)              # 1a → 1b → 1c, concurrently with other lanes
        RUNNING[lane] = p ; PENDING.remove(p)

    if not RUNNING:                           # nothing running, nothing ready
        break                                 # deadlock or done — checked below

    wait for ANY lane to return
    on success: DONE.add(p.id) ; free the lane ; write phase tracking ; maybe build a wave branch
    on failure: mark p failed ; BLOCKED |= transitive_dependents(p) ; free the lane
```

**Tie-breaking is plan order.** When more phases are ready than lanes are free, dispatch in the order they appear in **Phased Rollout**. Prefer a ready phase that unblocks the most dependents when the user has asked for throughput — but do not invent a scoring function; plan order is the default and is what the user can predict.

### Claiming an agent

`claim_agent(p)` is the staffing half of a dispatch, and it runs **before** the lane is taken. A lane is disk; who is holding it is what the phase costs and whether the result is any good.

1. **The member the plan assigned, if they are free.** The common case, and the one the plan's cost estimate is written against.
2. **Otherwise the cheapest free member at or above that member's tier.** A wave should not serialize behind one agent when a qualified peer is idle. Reach for the *cheapest* qualified one, not the best available — covering for a peer must not quietly promote the phase to the top tier, or a busy wave silently runs every Tier 2 phase on the Tier 4 member's model.
3. **Otherwise nobody, and the phase waits** — even with a lane free. This is the one place staffing costs throughput, and it is deliberate: a phase run below its tier does not fail cleanly. It produces plausible code that fails review two rounds later, by which point nothing points back at the staffing decision.

An implementer is held for the **whole phase**, not one turn of it: answering the review's findings is the implementer continuing its own session, so handing the phase to someone else mid-flight would hand it to an agent with no session to continue. Release it when the phase settles.

**The wait cannot deadlock.** The floor is the assigned member's own tier, so a waiting phase is always waiting on somebody who is *holding another phase* — never on a qualification nobody on the roster has. If you find yourself with every agent idle and a phase that cannot be staffed, the plan assigned it to a member the **Crew** table does not list; stop and ask.

**Record every claim**, and record whether it was the plan's own assignment or a peer covering. A run where half the phases were covered by a dearer peer is a run that cost more than the plan said while every phase came back green — and that is invisible in the phase statuses.

**Deadlock check.** Loop exits with `PENDING` non-empty and nothing running → every remaining phase is blocked. Report each blocked phase with the failed upstream that blocks it.

**Failure containment.** A failed phase does **not** abort the run. Let every already-dispatched lane finish (killing a lane mid-implementation leaves a half-written worktree nobody can resume). Mark the failure's transitive dependents `BLOCKED`, keep dispatching everything still reachable, and report the whole picture at the end. The exception is the [Tier-4 escalation stop](#1a-implement) — after Tier 4 fails on a phase, that phase stops, but sibling lanes still run to completion.

**Per-lane pause gate.** `run_options.pause_between_phases = true` under parallel execution means: **stop dispatching new phases** once every in-flight lane has returned, then ask. It does not mean pausing lanes individually — a per-lane prompt with three lanes running is unreadable. Options stay `Continue`, `Pause`, `Stop`.

**Concurrency is a cap, not a target.** A graph that is a straight chain runs one lane at a time and that is correct — do not reorder or bundle phases to fill idle lanes. The same goes for the roster: an agent idle for three waves is not a reason to hand them work above their tier.

**The per-phase pipeline.** Each dispatched lane runs the steps below for its own phase, concurrently with (and independently of) every other lane. Everywhere below, `WORKROOT` means **this lane's** workroot.

### 1a. Implement

Invoke [implement-phase](../implement-phase/SKILL.md), passing the phase record, the plan-level decisions (**Goals + Non-goals**, **Guiding Decisions**, the relevant **Data Model Changes** subsection), the **tracking summaries of this phase's transitive dependencies only** (see below), `run_options.full_test_suite`, and this lane's `WORKROOT` / `SANDBOX_TIER` plus the phase's computed `base_branch`. It returns the implementer's report.

**Prior-phase context is dependency-scoped, not chronological.** A lane must not be told about a sibling phase that happens to have finished first — that work is not in its base branch, so describing it as "already implemented" makes the implementer code against files it cannot see. Pass the `phase-{id}.md` summaries for the phase's **transitive dependency closure**, and nothing else. A wave-1 phase gets "Nothing yet — this phase starts from `<BASE_BRANCH>`."

**Model escalation.** implement-phase escalates one tier + retries once on a clear capability gap. After Tier 4 fails, it stops and hands back the failure — update tracking with `❌`, post a 3–5 line digest of the report (what failed, last gate output, files touched), then ask via `AskUserQuestion` (header `Phase fail`): `Amend the plan (Recommended)` (hand over to [amend-plan](../amend-plan/SKILL.md) to split or reshape the phase), `Retry with guidance` (the free-text answer is appended to the phase prompt as `## Guidance from the human`), `Skip phase` (mark deferred in tracking, continue — only when later phases don't depend on it), `Stop the run`. Don't silently re-derive tier.

**Sub-agent questions.** implement-phase and review-phase relay any `status: NEEDS_INPUT` report as a clickable prompt before they return (see [Relay a sub-agent's questions](../implement-phase/SKILL.md#relay-a-sub-agents-questions-needs_input)). The conductor never sees a prose question from a sub-agent; if one slips through (a report that ends in a question without the `NEEDS_INPUT` block), apply the same relay: turn it into an `AskUserQuestion` call with 2–4 options drawn from the report, never forward it as prose.

### 1b. Review

Invoke [review-phase](../review-phase/SKILL.md) against the phase diff, passing the phase body (the stated requirement), the phase's `base_branch`, the plan's **Goals + Non-goals** and **Guiding Decisions**, this lane's `WORKROOT` / `SANDBOX_TIER`, `main_checkout`, **every sibling lane's workroot** (for the stray-write check after each fix round), the phase's implementer sub-agent to continue, and the tier that implementer actually ran at — after any escalation, and the covering member's tier when a peer took the phase. review-phase runs the thermo-nuclear review loop with a reviewer one tier above that, and returns `PASS` only once the reviewer explicitly approves, or `STOPPED` when the human stopped the loop. Do not proceed to integrate unless it returned `PASS`. Record in `phase-{id}.md` the reviewer's model, the iterations, the rejected findings and the settled decisions it returns.

### 1c. Integrate

Invoke the resolved integrate-phase variant — [integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) when `run_options.commit_strategy_resolved = stacked-branches`, else [integrate-phase-modular](../integrate-phase-modular/SKILL.md), passing this lane's `WORKROOT`, the phase's computed `base_branch` (**not** the plan-level `BASE_BRANCH` — see [Lane branch topology](#lane-branch-topology)), the `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` policy, and `run_options.generate_inline_comments`. It pushes the branch and routes the PR through the context file, returning the branch + PR-context path + status. This is a mechanical step: when `agent_models.integrate` is set, run it as a delegated subagent per the [Delegate a mechanical step to a configured model](#delegate-a-mechanical-step-to-a-configured-model) pattern (the delegate pushes + writes the PR-context file + runs `open-pr.sh`, then reports the branch / path / status back); when unset, run it inline. Either way the PR-context file + `open-pr.sh` is the only PR-creation path.

### 1d. Update tracking

Tracking lives in a **directory**, not a single file: `ai-plans/TRACKING_{plan-id}/`.

```
ai-plans/TRACKING_{plan-id}/
├─ run.md                 # conductor-owned run state
├─ phase-{phase.id}.md    # one per executed phase
└─ waves/wave-{N}.md      # one per completed wave integration
```

**Why a directory.** Concurrent lanes commit on different branches that later merge. A single shared tracking file would conflict on **every** wave merge, for no reason — the lanes are appending unrelated records. Splitting by owner makes the merges trivially clean, because no two branches ever touch the same path.

**Ownership rules — these are what make the merges clean. Do not relax them:**

| Path | Written by | Committed on |
|---|---|---|
| `run.md` | the conductor only | the integration worktree, on the current wave branch |
| `phase-{id}.md` | the lane that ran phase `{id}`, once, after it passes review | that phase's own lane branch, in the phase's final commit |
| `waves/wave-{N}.md` | the conductor only | `plan/{plan-id-kebab}/wave-{N}`, as part of the merge commit |

**No lane ever writes, edits, or deletes another lane's file.** A lane that needs a sibling's summary *reads* it — the conductor passes prior-phase summaries into the prompt as data (see [Implement](#1a-implement)); the lane does not go looking in the tracking dir itself.

**`run.md`** carries: feature name, plan path, started / last-updated dates, optional feature-flag info, **run options** (`pause_between_phases`, `generate_inline_comments`, `full_test_suite`, `use_worktree`, `parallel_phases`, `max_parallel_lanes`), the **resolved dependency graph** (phase id → `depends_on` + computed wave), the **lane pool** (per lane: `workroot`, `branch`, `worktree_summary`, `sandbox_tier`, `current_phase`), the **crew roster** (per member: id, role, tier, resolved model, its worktree for an implementer, the phases the plan assigned them, and the phases they actually took), the integration worktree, **If `run_options.commit_strategy_resolved = "modular-commits"`:** top-level `plan_branch:` field **Else (`stacked-branches`):** (per-phase branch lives inline under the per-phase fields), and per-phase status (`done` / `running` / `blocked` / `failed` / `deferred`) with the lane each ran on.

**`phase-{id}.md`** carries: status, the crew member that took it + the model actually used + whether that member is the one the plan assigned + whether its session was continued from an earlier phase or started cold (and why, when cold) + the reviewer's model, the review iterations, the rejected findings and the settled decisions, branch (stacked-branches only), base branch, wave, `depends_on`, and the 5–15 line summary the conductor writes **from the git diff plus the agent's report** — not from the agent's narration.

**`waves/wave-{N}.md`** carries: which lane branches were merged, in what order, any conflicts and how they were resolved, and the outer-gate result on the merged tree.

**Migrating a legacy single-file tracking.** A plan started before this layout has `ai-plans/TRACKING_{plan-id}.md`. On resume: create the directory, split the existing content (run options + graph → `run.md`; each completed-phase entry → its own `phase-{id}.md`), `git rm` the old file, and continue. Say so in the resume report.

**Deletion.** [Step 2](#step-2--final-report) deletes the whole directory (`git rm -r`) on the final integration branch, in one commit. The plan file stays.

### 1e. Wave integration (when this phase completes a wave)

After a phase passes review + integrate, check whether **every** phase at its wave is now green. If so, build `plan/{plan-id-kebab}/wave-{N}` in the integration worktree per [Lane branch topology](#lane-branch-topology), write `waves/wave-{N}.md`, and push. If not, do nothing — the wave branch is built once, by whichever lane happens to finish last.

Wave integration runs **in the integration worktree**, never in a lane. It must not block the scheduler: dispatch it and keep filling free lanes with ready phases while it runs.

### 1f. Send brief update to user

One short paragraph: which phase finished on which lane, branch pushed, PR opened, what got built, and — when the [Integrate](#1c-integrate) step ran — the PR-context file path with its `status` (`published` + URL when `open-pr.sh` opened the PR; `pending` when the script wasn't run because PR policy = branches only or deps were missing). When `status: pending`, mention how to publish later (`bash ai-tools/skills/open-pr-from-context/scripts/open-pr.sh <path>`). Then what the scheduler picked up next, and what is still running on the other lanes. No long retrospective — the tracking directory is the durable record.

Under parallel execution, send **one update per phase completion**, not a merged digest — the user needs to be able to interrupt on a specific lane.

### 1g. Pause gate (opt-in)

`run_options.pause_between_phases = false` (default) → **immediately dispatch the next ready phase**. Do not wait.

`run_options.pause_between_phases = true` → stop dispatching, let every in-flight lane finish, then ask the user via `AskUserQuestion`:

- `Continue — dispatch the next ready phases`
- `Pause — stop here, I'll resume later by re-invoking the skill` (conductor exits cleanly; the tracking directory already records progress so the next invocation resumes mid-plan per [Re-running mid-plan](#re-running-mid-plan)).
- `Stop — abort the plan run` (conductor stops; user decides next steps manually).

Wait for the answer. Don't spawn anything in the meantime — with lanes idle, the pause is genuinely a stop. The pause is the user's review window; they may inspect any lane's diff, branch, PR-context file, or tracking entry before agreeing to continue.

## Cross-repo phases

Phase in another repo:
1. **Do not implement.**
2. Mark it deferred in `run.md`.
3. Keep scheduling every in-repo phase. Don't block on cross-repo work.
4. **Any phase that depends on it is deferred too**, transitively — its base branch would never exist. The [graph step](#build-the-phase-dependency-graph) already computed that closure; report the whole set together so the user sees the real cost of the cross-repo edge, not just one phase.

## Flag-removal phase (always out of scope)

Plan declared a flag → last phase is `Phase N — Remove the {flag-key} feature flag`. This skill **never** executes that phase. Flag removal is gated on real-world soak signal + is the exclusive responsibility of a dedicated flag-removal skill (separate skill).

What this skill does instead:
1. Identify the phase during Step 0; always exclude.
2. Mark in tracking as deferred.
3. End the run with a `/schedule` offer pointing at the dedicated flag-removal skill.
4. Refuse + redirect if the user asks this skill to remove the flag.

## Re-running mid-plan

User invokes the skill against a partially-done plan:

1. Read `ai-plans/TRACKING_{plan-id}/run.md` plus every `phase-*.md` beside it. Extract `run_options.*` — including the lane pool and the resolved dependency graph. Never re-prompt the Step 0 opt-in questions on resume; the original answers stick. **Legacy single-file tracking** (`TRACKING_{plan-id}.md`) → migrate it into the directory first, per [Tracking directory](#1d-update-tracking).
2. **Rebuild the graph from the plan file and diff it against the recorded one.** The plan may have been edited between runs. A changed `**Depends on**:` line on a phase that is already `done` is a warning (its branch is already based on the old graph — surface it); on a pending phase it simply takes effect.
3. **Lane pool resume.** When `run_options.use_worktree = true`, for **every** lane in the pool plus the integration worktree:
   - Confirm it still exists (`git worktree list | grep <workroot>`). Missing → ask via `AskUserQuestion`: `Reprovision that lane`, `Shrink the pool and carry on with fewer lanes`, `Stop`.
   - Confirm its summary file still parses; if not, regenerate from the existing worktree state.
   - **Re-probe `SANDBOX_TIER`** (`command -v sandbox-exec || command -v bwrap`) — a resume may run on a different machine than the original provisioning. Update each lane's `sandbox_tier` in `run.md` before spawning; the implement-phase spawn wrapping follows the re-probed value.
   - **Reset each lane's DB before reuse**, exactly as a mid-run reassignment would ([Resetting a lane worktree between phases](#resetting-a-lane-worktree-between-phases)). A lane resumed with a half-applied migration set is the single most likely way a resumed run goes wrong.
   - Never grow the pool on resume beyond what `run.md` recorded — a wider pool changes the schedule the user approved.
4. `git -C <integ.workroot> branch -a | grep plan/{plan-id-kebab}` to detect already-pushed phase, `integ-`, and `wave-` branches. A phase whose branch exists and whose `phase-{id}.md` says `done` is green; a phase whose branch exists without a `phase-{id}.md` was interrupted mid-flight — treat it as **not** done, delete the branch, and re-run it.
5. Cross-reference with the plan's phase list, recompute the ready set, and confirm the resumption point via `AskUserQuestion` (header `Resume`) — the question shows which phases are done, which are blocked by a failure, and what the scheduler will dispatch first. Options: `Resume (Recommended)`, `Re-run <phase id>` (when a phase was interrupted or failed), `Stop`. Decisions already recorded in tracking (the `decisions` lists from relayed sub-agent questions) are not asked again.

## Step 2 — Final report

After the scheduler loop exits — every executable phase is `done`, `failed`, or `blocked`:

1. **Build the final wave branch** if the last completed wave has no branch yet, so one branch carries the whole plan.
2. **Delete the `TRACKING_{plan-id}/` directory** (`git rm -r`) on that final wave branch, in one commit. The plan file stays.
3. **Open the plan PR** (per-phase PR strategies only — skip when the commit strategy already keeps one plan-level PR). Only when every phase is `done`: a plan with failed or blocked phases is not ready to land, so list the gap instead. Push the final wave branch, write `.vinta-ai-workflows/prs-context/{feature-kebab}/plan.md` with `kind: plan`, `branch: plan/{plan-id-kebab}/wave-{N}` (the final wave), `base: <BASE_BRANCH>`, and a `# Description` listing every phase and integration PR in merge order (wave by wave; plan order inside a wave; each integration PR right before its phase PR), with the two ways to land it — merge this PR alone, or merge the listed PRs in order and this one last — and the rule to use merge commits, not squash. Run `open-pr.sh` on it under the same PR policy as the phase PRs. This is the PR that lands the plan; a run that ends without it leaves the user to rebuild the merges by hand.
4. Send the user a final summary: the **plan PR URL** first (or why it was not opened — failed phases, PR policy, missing deps); **If `run_options.commit_strategy_resolved = "modular-commits"`:** single plan branch `plan/{plan-id-kebab}` with commit log organized by phase (parallel runs: plus the lane branches merged into it, per wave) **Else (`stacked-branches`):** branches pushed (with bases, grouped by wave); the **wave branches** in order, and which phase branches merged into each; phases that **failed** and the dependents each one **blocked**; deferred phases (cross-repo + flag-removal); next steps for the human. When `run_options.use_worktree = true`: include **every lane's** path + branch + summary file path + teardown command, and the integration worktree's. With the skill, build the teardown from that lane's `<worktree_summary>`: `git worktree remove <path>`, the per-engine drop-db line for each forked DB, and, when `state.compose` is set, `docker compose -p <state.compose.project_name> down`, then `docker volume rm` for each `state.compose.forked_volumes[].forked_name`, then `rm <state.compose.override_path>`. Never `down -v`: it also deletes the volumes listed in `state.compose.shared_volumes`, which belong to the main checkout (see prepare-worktree's **Teardown** step). With the project's command, it is the ready-to-run line from [Teardown with the project's command](#teardown-with-the-projects-command). Do NOT auto-run teardown — the user may still want a lane to debug review feedback or land follow-ups, and a failed phase's lane is the only place its state survives.
   PR URLs: every phase and integration PR, grouped by wave.
5. Flag-removal phase deferred → end with `/schedule` offer for the dedicated flag-removal skill.

## Important rules

- **Read AGENTS.md** in every phase prompt.
- **Stage explicitly.** No `git add -A`.
- **Subagents work in fresh sessions.** Each phase = a new subagent. The plan file plus the phase's dependency-closure tracking files = the context handoff.
- **Conductor owns git topology.** Phase-work subagents (implementer / conflict fixer) commit, and the reviewer never edits; none of them branch, push, or open PRs. The one exception is a **mechanical `integrate` delegate** spawned per `agent_models.integrate` — it exists precisely to run the conductor's integrate step (push + PR via `open-pr.sh`) on a cheaper model, and the conductor still dictates the branch/base topology it uses.
- **No AI co-author trailers in commits.** The project forbids them; treat any AI trailer as a BLOCKER.
- **Trust the plan's per-phase model suggestion.** Implementer model selection lives in [implement-phase](../implement-phase/SKILL.md); the conductor never re-derives tiers.
- **The plan never names the reviewer, conflict-fixer or mechanical-step models.** The reviewer runs one tier above the phase's implementer; the others come from `agent_models` (see [Agent models](#agent-models--reviewer-conflict-fixer-and-mechanical-steps)), and an unset key means the spawn uses the runtime default.
- **Don't re-implement what a project skill encodes.**
- **Two-tier verification, in order, every phase.** Inner scoped, then the outer gate — enforced inside [implement-phase](../implement-phase/SKILL.md). The outer gate always runs the repo-wide type/build gate; its test scope follows `run_options.full_test_suite` (scoped suite by default, full repo suite when opted in).
- **The review loop, every phase, no exceptions** — [review-phase](../review-phase/SKILL.md) is not optional and not inlined here, and a phase merges only on the reviewer's explicit approval.
- **Orchestrator never edits code.**
- **Feature flags = gates, not toggles for tests.**
- **Never remove a feature flag from this skill.**
- **Stop on Tier-4 failure.**
- **Every stop for human input is a structured question.** Use `AskUserQuestion` (see **Asking the human** in [AGENTS.md](../../../AGENTS.md)) with 2–4 concrete options, the recommended one first. Never end a turn with a prose question or "reply go". Sub-agents return `NEEDS_INPUT`; the orchestrator relays it.
- **Honor opt-in flags.** `run_options.pause_between_phases` controls the [pause gate](#1g-pause-gate-opt-in); `run_options.generate_inline_comments` controls whether the resolved integrate-phase variant — [integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) when `run_options.commit_strategy_resolved = stacked-branches`, else [integrate-phase-modular](../integrate-phase-modular/SKILL.md) drafts inline comments (always writes the file when that step runs at all — empty comments when off); `run_options.use_worktree` controls whether the [Resolve WORKROOT step](#step-05--resolve-workroot) provisions worktrees and thus what `WORKROOT` / `SANDBOX_TIER` resolve to; `run_options.full_test_suite` controls the outer-gate test scope ([Implement](#1a-implement)) — scoped suite by default, full repo suite when `true`; `run_options.parallel_phases` + `run_options.max_parallel_lanes` control how many phases the [scheduler](#dispatch-loop) keeps in flight.
- **The graph decides order, not the plan's numbering.** Never run a phase before every id in its `**Depends on**:` set is green, and never serialize two phases the graph says are independent just because one has a lower number.
- **A lane only ever knows its own dependencies.** Pass a phase the tracking summaries of its transitive dependency closure and nothing more. Telling a lane about a sibling's work that is not in its base branch makes it code against files it cannot see.
- **One worktree pool per plan run.** Size it once in the [pool step](#provision-the-lane-worktree-pool) and reuse each lane across phases. Never grow the pool mid-run; never silently fall back to the main checkout when provisioning fails (skill or project command), and never fall back to sequential without asking — parallel execution requires worktrees and refusing is the correct move.
- **Reset a lane's DB before reusing it.** A lane carrying a previous phase's migrations silently invalidates the next phase's tests. No `reset_cmd` in the lane's summary (or no summary) and a migration on either side → re-provision that lane instead.
- **Don't auto-tear-down any worktree.** Step 2 surfaces every lane's teardown command; the user runs them when ready. A failed phase's lane is the only place its state survives.
- **`WORKROOT` is resolved once per lane, used everywhere.** Every sub-skill takes `WORKROOT` / `SANDBOX_TIER` as data — no step re-derives worktree state, and no step reads another lane's. OS-level prevention (sandbox wrap in implement-phase when `SANDBOX_TIER = enforced`, denying the whole pool root) plus the stray-write backstop across the main checkout and every sibling lane, run after the implementer and after every fix round, keep foreign writes out; see [worktree-seam](../implement-phase/SKILL.md#3-spawn-the-subagent).
- **The orchestrator never edits code — merge conflicts included.** A conflicted wave or `integ-` merge goes to a fixer subagent in the integration worktree, then back through the outer gate.
- **A failed phase blocks its dependents, not the run.** Let in-flight lanes finish, mark the transitive dependents blocked, keep dispatching what is still reachable, and report the whole picture.
- **PR-context file + `open-pr.sh` is the only PR-creation path.** No raw `gh pr create` / `glab mr create` calls outside the bundled script.
- **License check before any new dep.** Refuse `npm add` / `pnpm add` / `pip install` / `poetry add` / `uv add` / `cargo add` / `go get` when the package's SPDX license is in the forbidden list — see AGENTS.md **Dependency licenses**. User can grant a one-off override after acknowledging the violation; record the override in `policies.dependency_licenses.allowed_overrides` before re-running.
- **Never use `§N` shorthand to point at sections** — neither in this skill body nor in any rendered file (tracking, prs-context, branch description). Always use the section's full name with a markdown link when possible.

## Quick checklist (conductor — once per run, then per phase)

- [ ] Plan parsed; structured fields cached.
- [ ] Cross-repo + flag-removal phases identified + deferred, **with their transitive dependents**.
- [ ] Dependency graph built + validated (no cycles, no unknown ids, no missing `**Depends on**:` line); waves computed; file-overlap warnings resolved with the user.
- [ ] `WORKROOT` / `SANDBOX_TIER` resolved per lane ([Resolve WORKROOT step](#step-05--resolve-workroot)); lane pool + integration worktree provisioned, summaries captured, schedule confirmed by the user when `use_worktree = true`.
- [ ] This phase dispatched only after every id in its `depends_on` was green; its `base_branch` computed from that set (not from plan order).
- [ ] Lane reset before reuse: base checked out, phase branch created, DB reset via `db_reset_cmd`.
- [ ] [implement-phase](../implement-phase/SKILL.md) run: prompt composed with **Goals + Non-goals** + **Guiding Decisions** + relevant **Data Model Changes** subsection + **dependency-closure** tracking summaries + this phase's body; agent claimed off the roster and model resolved from their tier (cheapest available); implementer report received.
- [ ] [review-phase](../review-phase/SKILL.md) run: the reviewer explicitly approved; every fix round re-ran the outer gate and was committed; questions only a person could settle were asked and recorded; when worktrees are in use, `git -C <tree> status --short` clean after the implementer and after every fix round, for the main checkout **and every sibling lane**.
- [ ] the resolved integrate-phase variant — [integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) when `run_options.commit_strategy_resolved = stacked-branches`, else [integrate-phase-modular](../integrate-phase-modular/SKILL.md) run: **If `run_options.commit_strategy_resolved = "modular-commits"`:** Plan branch updated with phase commits (directly, or via the lane branch's wave merge); pushed. **Else (`stacked-branches`):** Phase branch created from its dependency-derived base; pushed. PR opened via the context file + `open-pr.sh`; PR URL captured.
  - [ ] When `run_options.commit_strategy_resolved = "modular-commits"`:
    - [ ] Commit units listed upfront before any staging.
    - [ ] Each commit covers exactly one logical unit (no "and" in commit messages).
    - [ ] Tests landed in the same commit as the code they cover (never a separate test-only commit).
    - [ ] All unit commits pushed at end of phase — to `plan/{plan-id-kebab}` when sequential, to `plan/{plan-id-kebab}/lane-{phase.id}` when parallel.
    - [ ] Parallel runs only: lane branch merged into `plan/{plan-id-kebab}` with `--no-ff` at the wave boundary; never squashed.
  - [ ] When `run_options.commit_strategy_resolved = "stacked-branches"`:
    - [ ] Phase branch created from `<phase.base_branch>` (dependency-derived), not from plan order.
    - [ ] PR `base` in the prs-context frontmatter equals `<phase.base_branch>`.
    - [ ] Phase based on an `integ-` branch: the `integ-` branch pushed and its integration PR (`integ-{phase.id}.md`, `base: <BASE_BRANCH>`) opened before the phase PR.
    - [ ] Run end: plan PR (`plan.md`, final wave branch → `<BASE_BRANCH>`) opened, listing every phase and integration PR in merge order.
  - [ ] **Open PR via context file** decision applied per matrix (PR policy + `generate_inline_comments`): file written when at least one of policy=create / comments=true holds; `open-pr.sh` run when policy=create AND deps available (PR URL captured); per-comment failures (exit 1) and hard failures (exit 2) surfaced.
- [ ] `TRACKING_{plan-id}/phase-{id}.md` written on this phase's own branch; `run.md` updated by the conductor only; no lane touched another lane's file.
- [ ] Wave branch built + `waves/wave-{N}.md` written when this phase completed its wave; merge conflicts resolved by a fixer, never by the conductor, and the outer gate re-run on the merged tree.
- [ ] One-paragraph user update sent per phase completion (PR URL or pending-file path included; lane named; what the scheduler picked up next).
- [ ] Every `NEEDS_INPUT` report relayed as an `AskUserQuestion` prompt; answers recorded in tracking; no prose question left at the end of a turn.
- [ ] If `run_options.pause_between_phases = true`: stopped dispatching, let in-flight lanes drain, prompted user (`Continue` / `Pause` / `Stop`); honored answer. Else: next ready phase dispatched immediately.
- [ ] On run end: final wave branch built; tracking directory deleted; plan PR opened (final wave branch → `<BASE_BRANCH>`, every phase + integration PR listed in merge order) when every phase is done and the strategy opens per-phase PRs; final summary leads with the plan PR URL and lists wave + phase branches with PR URLs, failed phases with the dependents they blocked, every lane's teardown command; any `status: pending` PR-context files listed with publish command; `/schedule` offer for flag-removal if applicable.
