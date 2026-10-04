---
name: amend-plan
description: Adjust an existing implementation plan in `ai-plans/` after implementation has started or finished. Updates the plan file (revising existing phases or appending new ones), then for each affected phase that was already implemented adjusts its commits (`git commit --amend` or new commits) on the phase branch, force-pushes the rewritten branch, rebases every phase branch in the rewritten phase's dependency closure, force-pushes each, and refreshes the PR-context files. Use when the user says "amend the plan", "update phase N", "add a phase to plan X", "the spec changed, fix the plan", or "rewrite the implementation for phase N". NOT for one-off changes to a single file unrelated to a plan; use the regular implement skill for that. Agents push branches and open PRs via `open-pr-from-context` after review passes.
disable-model-invocation: true
---

# Amend Plan

Revise a plan in [`ai-plans/`](ai-plans/) after work has begun. Companion conductor to [implement-plan](../implement-plan/SKILL.md): it reuses the same sub-skills ([implement-phase](../implement-phase/SKILL.md) for the body change, [review-phase](../review-phase/SKILL.md) for the gates) — but the orchestrator's job here is **history rewriting** instead of forward execution.

The flow is destructive (force-push). Every modification is gated on user confirmation. Default disposition for any ambiguous case is "stop and ask" — never force-push without an explicit per-branch `Confirm` from the user.

## Unsupported commit strategy

**This skill does not yet support `commit_strategy = modular-commits`.** Detected from `.vinta-ai-workflows.yaml` `policies.commit_strategy` (or `TRACKING_{plan-id}.md` `run_options.commit_strategy_resolved` when the project policy is `ask` and a run is already in flight).

This project's `policies.commit_strategy` is `ask` — at amend time, read `TRACKING_{plan-id}.md` `run_options.commit_strategy_resolved` first. When it resolves to `stacked-branches`, the full amend flow below runs unchanged. When it resolves to `modular-commits`, refuse with the guidance in this section.

Amending under modular commits requires rewriting an arbitrary number of inline atomic commits across a shared `plan/{plan-id-kebab}` branch. The git topology is fundamentally different from the per-phase stacked branches this skill is designed around — the rewrite plan, force-push targets, and downstream rebase fan-out all differ. Full support is tracked as a follow-up.

**Resolve the amendment one of three ways:**

1. **Append a new phase** — extend the plan with the change as a new `Phase N+1`, then run [implement-plan](../implement-plan/SKILL.md). Cleanest path; preserves the existing commit log.
2. **Hand-craft the amendment** — `git rebase -i plan/{plan-id-kebab}` (or `git commit --fixup` + `git rebase --autosquash`) on the plan branch, force-push, and re-run review manually. Skip this skill entirely.
3. **Re-run the plan from scratch on a new branch** — abandon the in-flight commits (leave them for audit), regenerate the plan with [plan-feature](../plan-feature/SKILL.md), implement forward.

Refuse with this guidance; do not proceed.

## Not for an agent that was handed one phase

This skill **orchestrates**: it composes prompts, picks models and spawns other agents. Run it only when you are the session a user or a scheduler invoked to drive a plan.

If you are reading it because something handed you a single phase — a prompt naming your phase id, a branch already cut for you, a worktree you were told to stay inside, or an orchestrator such as [vinta-ai-maestro](https://github.com/vintasoftware/vinta-ai-workflows/tree/main/packages/vinta-ai-maestro) that spawned you — then the conductor this skill describes **is already running, and it is what spawned you**. Do not start a second one underneath it. Do the work in your own session, report back the way your prompt asked, and take from here only what it says about this repository's conventions, gates and commit rules.

The duplication is the smaller cost. A dispatched agent is deliberately reused — the same session takes the review findings, the chore over its own diff, and often the next phase — and that reuse is worth something only because the session that read the codebase is the session that gets the next turn. Hand your phase to a sub-agent and its reading of the code dies with it: you are left holding a summary, and every turn after yours starts cold.

## Working assumptions

- Repo: vinta_schedule_api (Django 6 + DRF + Strawberry GraphQL + Celery, multi-tenant (SingleOrganizationModelMixin), Postgres, deployed to AWS ECS/Fargate). Conventions: [AGENTS.md](../../../AGENTS.md).
- Plan files: [`ai-plans/YYYY-MM-DD-FEATURE_NAME_PLAN.md`](ai-plans/).
- Lint: `docker compose run --rm api uv run ruff check ./`. Format: `docker compose run --rm api uv run ruff format ./`.
- Type / build gate: `docker compose run --rm api uv run python manage.py check --deploy` plus full mypy via `docker compose run --rm api uv run mypy .`.
- Unit / integration tests: `docker compose run --rm api uv run pytest -n auto`; per-app via `docker compose run --rm api uv run pytest <app>/tests/ -n auto`.
- Migrations: `docker compose run --rm api uv run python manage.py makemigrations --check` (gate) + `docker compose run --rm api uv run python manage.py migrate` (apply). Raw-SQL DB code (functions, views, materialized views, triggers, procedures) routes through `common/raw_sql_migration_managers.py` — see [add-migration](../add-migration/SKILL.md). Deploy target: AWS ECS/Fargate — the staging deploy runs a release task (`migrate` then `collectstatic`) before it rolls the web, worker and beat services, so a failed migration stops the deploy (`scripts/deploy/ecs_deploy.sh`). Production has no deploy job yet.
- Code host: **GitHub**. PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).
- Co-author trailer policy: **forbidden**. Commits must not include `Co-Authored-By:` AI trailers.
- Default branch: `main`.
- Branch naming convention (set by [implement-plan](../implement-plan/SKILL.md)): `plan/{plan-id-kebab}/phase-{phase.id}`.
- **`WORKROOT`.** Resolve once, same as the [implement-plan Resolve WORKROOT step](../implement-plan/SKILL.md#step-05--resolve-workroot): the main checkout by default, or the plan's worktree when `run_options.use_worktree = true` in `run.md`. When the run used a **lane pool**, amend in the **integration worktree** — lanes are sized for forward implementation and may still hold state from their last phase. Every `git` call below runs with `git -C <WORKROOT>`; when no worktree is in play, `WORKROOT` is the main checkout and the commands read exactly as in-place git.
- **Amend only when no implementation is in flight.** A rewrite force-pushes branches other lanes may be based on. If `run.md` shows any phase `running`, stop and tell the user to let the run finish (or stop it) first.

## When to use

- A spec change forces a phase body rewrite (different acceptance, different decisions in the plan's **Guiding Decisions**).
- A phase was implemented but the resulting diff is wrong / incomplete (caught post-merge to the phase branch but pre-merge to `main`).
- A new phase needs to slot in between two existing ones, or be appended.
- A guiding decision in **Guiding Decisions** changed and it cascades into multiple phases.

## When NOT to use

- **Phase already merged to `main`.** History cannot be retroactively rewritten on `main`. The change must go in as a new phase appended to the plan, implemented forward via [implement-plan](../implement-plan/SKILL.md). This skill detects this case and refuses to force-push a merged branch.
- **Single-file change unrelated to a plan.** Use the regular implement skill / direct edit.
- **The plan never started.** No commits to amend; just edit the plan file and run [implement-plan](../implement-plan/SKILL.md) normally.

## Step 0 — Understand the change + parse the plan

1. **Identify the plan file.** Same logic as the [implement-plan "Locate + parse plan" step](../implement-plan/SKILL.md#step-0--locate--parse-plan): an `AskUserQuestion` (header `Plan file`) offering the 2–4 best-matching plans from `ls -t ai-plans/` (most recent first; the free-text field covers any other path).

2. **Capture the requested change.** The user's prompt is the source. If vague, interview via `AskUserQuestion`:
   - *"Which phases are affected?"* — enumerate phase ids from the plan's **Phased Rollout** section.
   - *"Is this a body rewrite of an existing phase, a new phase to slot in, or a **Guiding Decisions** change that cascades?"*
   - *"What's the new acceptance criterion / change list?"* — verbatim.
   Don't infer scope. The plan-amend is the user's contract, not yours.

3. **Parse the plan.** Same structured fields as [implement-plan's "Extract structured fields" step](../implement-plan/SKILL.md#step-0--locate--parse-plan): plan id, **Goals + Non-goals** / **Guiding Decisions** / **Data Model Changes** / phase records from **Phased Rollout** / **Risk & Rollout Notes** through **Touch List**.

4. **Read the tracking directory** `ai-plans/TRACKING_{plan-id}/` if present: `run.md` carries the `run_options` (including worktree state → `WORKROOT`, and the resolved dependency graph), each `phase-{id}.md` carries that phase's branch, base, and model, and `waves/wave-{N}.md` records which lane branches were merged where. A plan run before the directory layout has a single `TRACKING_{plan-id}.md` — read it the same way. If neither exists → `git -C <WORKROOT> branch -a | grep plan/{plan-id-kebab}` to enumerate pushed phase, `integ-`, and `wave-` branches.

5. **Build a per-phase state map.** For every phase in the plan, record:

   | Field | Source |
   |---|---|
   | `phase.id`, `phase.title` | plan's **Phased Rollout** section |
   | `state` | one of `not-started` / `in-progress` / `implemented-not-merged` / `merged-to-default` |
   | `branch` | tracking file or git, pattern `plan/{plan-id-kebab}/phase-{id}` |
   | `base` | `phase-{id}.md`, or `git -C <WORKROOT> merge-base origin/<branch> <base-branch>`. **The base is the phase's dependency-derived branch, not the previous phase in plan order** — read `**Depends on**:` from the plan and resolve it per [Lane branch topology](../implement-plan/SKILL.md#lane-branch-topology). A phase with no dependencies bases on `main`. |
   | `dependents` | every phase whose `**Depends on**:` set contains this one, transitively. This — not "every phase with a higher number" — is the set a rewrite cascades into. |
   | `pr_status` | `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{id}.md` frontmatter (`pending` / `published`) when the file exists |
   | `merged_to_default` | `git -C <WORKROOT> branch --merged origin/main | grep` against the branch |

   `merged-to-default = true` blocks any commit rewrite for that phase — see [Step 3 — Refuse force-pushes that can't work](#step-3--refuse-force-pushes-that-cant-work) below.

6. **Classify the requested change** by phase impact, in priority order:

   - **`body-rewrite`** — existing phase keeps its id; body changes. Cascades into its `dependents` closure because rewritten commits get new SHAs. Phases outside that closure are untouched — under a parallel plan that is often most of them.
   - **`insert-new`** — new phase slotted in. Cascades into whichever existing phases the user makes depend on it (and their closure). A new phase nobody depends on cascades into nothing.
   - **`append-new`** — new phase with no existing dependents. No cascade. Implementation runs forward via [implement-plan](../implement-plan/SKILL.md) — this skill hands off after editing the plan file.
   - **`dependency-change`** — the change is to a phase's `**Depends on**:` line rather than its body. Adding an edge to an already-implemented phase means its branch has the wrong base: it must be rebased onto the new base and its own closure re-cascaded. Removing an edge is safe to leave as-is (the branch simply carries more history than it needs) — say so and let the user decide whether a re-cut is worth it.
   - **`guiding-decisions-change`** — change inside the plan's **Guiding Decisions** section. Cascades into every phase that referenced the decision.

7. **Evaluate amendment blast radius — recommend restart when too big.** Amending in place stops being a good deal once the rewrite work approaches re-implementation. Compute these signals from the per-phase state map + the requested change:

   | Signal | Threshold suggesting RESTART |
   |---|---|
   | Phases needing `body-rewrite` ÷ total implemented phases | ≥ 50% |
   | `guiding-decisions-change` cascading into | ≥ 50% of implemented phases |
   | Phases in `merged-to-default` (immutable, must be `append-new`) | ≥ 2, AND remaining work also rewrites earlier phases |
   | New body materially changes the data-model contract from **Data Model Changes** | rewrite of >2 phases hinges on it |
   | Estimated touched LoC across rewrites | ≥ 70% of original implementation diff size (rough estimate via `git -C <WORKROOT> diff --stat <base>..<branch>` summed across affected branches) |
   | Multi-author phase branches in the rewrite queue | ≥ 1 (force-push erases collaborator local state) |
   | Approved PRs in the rewrite queue | ≥ 2 (re-review burden becomes non-trivial) |

   Any **two** signals tripping → mark the amendment as `high-blast-radius`. **Three or more** → mark as `restart-recommended`.

   When `high-blast-radius` or `restart-recommended`, surface to the user via `AskUserQuestion` **before** showing the standard step-8 confirmation:

   - *"This amendment looks large enough that restarting from scratch may cost less than rewriting in place. Tripping signals: <list>. Restarting means: rewrite the plan as a fresh `YYYY-MM-DD-FEATURE_NAME_PLAN.md` (today's date), abandon the current phase branches (leave them in place for audit), run [implement-plan](../implement-plan/SKILL.md) on the new plan from scratch. Amending in place keeps history but force-pushes <N> branches and re-spawns implementer/reviewer/fixer agents per phase."*

   Options:

   - `Restart — draft a new plan, abandon current branches`
   - `Amend in place — proceed knowing the cost (you'll show me the force-push plan next)`
   - `Stop — let me think / talk to the team first`

   On `Restart`:

   1. Help the user draft a new `YYYY-MM-DD-FEATURE_NAME_PLAN.md` with today's date (paired with the spec, same `FEATURE_NAME`). This skill does not write the new plan body — point at [plan-feature](../plan-feature/SKILL.md) (or [create-spec](../create-spec/SKILL.md) first if the spec also changed).
   2. Annotate the **old** plan: at the top, add `**Superseded YYYY-MM-DD by ../YYYY-MM-DD-FEATURE_NAME_PLAN.md** — reason: <one line>`. Append the same line under `## Amendments`.
   3. Leave the old phase branches alone — useful audit trail, no force-push needed.
   4. Update `TRACKING_{plan-id}/run.md` to mark the plan superseded; preserve every `phase-{id}.md` entry.
   5. Hand off to [plan-feature](../plan-feature/SKILL.md). This skill exits.

   On `Amend in place`: proceed to step 8 (the original confirmation gate, renumbered). On `Stop`: exit cleanly; nothing written.

   When the signals show `low-blast-radius` (≤1 tripping), skip the recommendation entirely and go straight to step 8. Don't pad easy amendments with restart questions.

8. **Confirm with user before any write.** Show: requested change, classified type, list of affected phase branches with their state + merge status, downstream branches that will be rebased, the force-push plan. Use `AskUserQuestion`:

   - `Proceed — I authorize the force-pushes listed`
   - `Refine — let me adjust scope` (loop back to step 2)
   - `Stop`

   Anything in `merged-to-default` state shown explicitly with a warning. **Do not** include those in the force-push list — user must convert those to `append-new` phases instead.

## Step 1 — Edit the plan file

Always the first write. Plan file is durable; commits get rewritten next.

1. **Body rewrites** — replace the affected `## Phase {id}` block inside **Phased Rollout** verbatim with the new body. Keep `phase.id` stable so branch naming stays valid.

2. **Inserts** — choose a new id. Two conventions are common:
   - Decimal: `1.5` between `1` and `2` (matches existing patterns in some Vinta plans). Branch becomes `plan/{plan-id-kebab}/phase-1.5`.
   - Letter: `1b` between `1` (relabeled `1a`) and `2`. Requires renaming `1` → `1a` inside **Phased Rollout** + updating downstream references.
   Ask via `AskUserQuestion` (header `Phase id`): `Decimal id (Recommended)` (no rename of existing ids), `Letter id` (relabels the neighbouring phase).

3. **Appends** — new `## Phase N+1` block at end of **Phased Rollout**. Same shape as siblings: Goal, **Assigned to**, optional Review models, reusable_skills, Changes, Tests, Acceptance. An appended phase is staffed off the existing **Crew** table; adding a member is a change to the plan's staffing arithmetic and needs the same `Takes`-column update as any other.

4. **Guiding Decisions changes** — rewrite the affected row. Add a one-line note at the top of **Guiding Decisions** ("**Amended YYYY-MM-DD**: replaced storage shape from X to Y; affects phases 2, 3, 4.") so reviewers see what shifted. Reference the changed row by its **Decision** column name, not by a `§N.M` shorthand.

5. **Bump the amendment log.** At the bottom of the plan, under `## Amendments`, append:

   ```markdown
   - **YYYY-MM-DD** — <one-line summary of change>. Affected phases: <ids>. Branches force-pushed: <branch-list>.
   ```

   Create the section if it doesn't exist. This is the audit trail; preserve every entry.

6. Commit the plan edit on `main` (or wherever the plan file lives — the plan file itself is not branched per phase). Commit message: `Amend plan: <summary>`. Conventional Commits format: `type(scope): subject` — e.g. `feat(calendar): add bundle availability filter`, `fix(public_api): correct organization scope on bookings query`.

## Step 2 — Build the rewrite queue

For each phase classified as needing commit rewrites (`body-rewrite` for already-implemented phases, plus each rewritten phase's `dependents` closure for `insert-new` / `body-rewrite` / `dependency-change` / `guiding-decisions-change`), build a queue in **topological order of the dependency graph**: a phase is rebased only after every phase it depends on has been. Phases outside the closure are never touched — leave their branches and PRs alone.

A phase with several dependencies rebases onto a **rebuilt** `integ-{id}` branch: re-merge its dependency branches in plan order first, then rebase the phase onto that. Rebasing it onto only one dependency silently drops the others.

For each entry record:

- `branch`, `base` (parent in the stack — may be `main` or another phase branch).
- `change_kind`: `amend-existing` (modify the phase body's effect on the diff) or `rebase-only` (parent moved, no body change for this phase).
- `commits_to_amend`: list of SHAs the orchestrator may rewrite (look at `git -C <WORKROOT> log <base>..<branch>`).

Phases in `not-started` state are deferred to [implement-plan](../implement-plan/SKILL.md) — not rewritten here.

## Step 3 — Refuse force-pushes that can't work

Before any write to remote, **block on these conditions**:

1. **Phase merged to `main`.** History on `main` is immutable in practice. Explain that the amendment must be a new phase appended to the plan, not a rewrite, then ask via `AskUserQuestion` (header `Merged`): `Re-classify as append-new (Recommended)` (continue this run with classification `append-new`; execute it later via [implement-plan](../implement-plan/SKILL.md)), `Stop`.

2. **Branch's PR was reviewed and approved.** Force-pushing destroys reviewer context. Surface: list approved PRs by URL, ask `AskUserQuestion`:
   - `Proceed — I'll re-request review after force-push`
   - `Stop — too disruptive, redesign as forward phase`

3. **Branch protection rules block force-push.** `gh api repos/{owner}/{repo}/branches/{branch}/protection` (or `glab` equivalent). If the branch is protected, force-push will fail noisily — surface the rule, stop.

4. **Multiple authors on the branch.** `git -C <WORKROOT> log --pretty=format:%ae <base>..<branch> | sort -u | wc -l` > 1 → other developers committed too. Force-push erases their local state. Ask via `AskUserQuestion` (header `Co-authors`), naming the other authors: `Stop — convert to a forward phase (Recommended)`, `I've coordinated with <names>`.

If any block triggers and the user can't dismiss it: stop. Don't proceed further. Tell the user the rewrite path is unavailable; suggest an `append-new` phase as the fallback.

## Step 4 — Per-phase rewrite loop

For each entry in the rewrite queue, in stack order:

### 4a. Check out the branch

```bash
git -C <WORKROOT> fetch origin
git -C <WORKROOT> checkout {branch}
git -C <WORKROOT> reset --hard origin/{branch}
```

### 4b. For `change_kind = amend-existing` only — apply the body change

Spawn an implementer subagent. The prompt mirrors [implement-phase](../implement-phase/SKILL.md#1-compose-the-agent-prompt-token-efficient) but records the change differently (new commit on top by default, never a push). Compose:

```
You are amending {phase.id}: {phase.title} of plan {plan.id}.

## Repo
vinta_schedule_api (Django 6 + DRF + Strawberry GraphQL + Celery, multi-tenant (SingleOrganizationModelMixin), Postgres, deployed to AWS ECS/Fargate).

## Working location
Work inside `<WORKROOT>`. `cd` into it before any command.

## Read first
1. AGENTS.md — repo conventions.
2. ai-plans/{plan-filename}, the **Goals + Non-goals**, **Guiding Decisions**, and **Data Model Changes** sections, plus the rewritten phase body inside **Phased Rollout**.
3. The current diff: `git -C <WORKROOT> diff {base}...HEAD` — what's already on this branch.

## What changed in the plan (verbatim)
{Diff between old phase body and new — produce via `diff <(old-body) <(new-body)`. Or, if the plan was rewritten in place, the new body verbatim with a note "this replaces what was here before".}

## Your task
Bring the diff on this branch into compliance with the new phase body. You may:
- Edit existing files this branch already modified.
- Add new files when the new body requires them.
- Remove files this branch added that the new body no longer needs.

## How to record the change
Default: ADD A NEW COMMIT on top of the existing branch. Title: "Amend phase {phase.id}: <summary>". This preserves the original implementation as a separate commit and makes the amendment auditable in `git log`.

Use `git commit --amend` ONLY when:
- The branch has exactly one commit, AND
- The amendment is small (≤30% of the original diff size), AND
- The user explicitly authorized amend in Step 0.

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
```

Then splice in the shared "return your questions" contract and the inner/outer verification loop verbatim:

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

…and close the prompt with the amend-specific staging tail:

```
7. Stage explicitly: `git add <explicit paths>`.
8. Commit. Conventional Commits format: `type(scope): subject` — e.g. `feat(calendar): add bundle availability filter`, `fix(public_api): correct organization scope on bookings query`.
9. Do **not** add `Co-Authored-By: Claude` (or any other AI) trailer to commits — the project forbids them.
10. **Do NOT push. Do NOT force-push.** The orchestrator owns the remote.

## Required output
- Status: SUCCESS, FAILURE, or NEEDS_INPUT (with the `questions:` block).
- New commit SHA(s) added (or amended SHA).
- 5–15 line summary.
- Deviations from new body + reasoning.
```

When the amend implementer (or any fixer below) returns `NEEDS_INPUT`, relay it as a clickable prompt before continuing — see [Relay a sub-agent's questions](#relay-a-sub-agents-questions-needs_input).

For `change_kind = rebase-only` (downstream phase whose parent moved): skip the agent. The work is purely git topology.

### 4c. Run the three-layer review

Invoke [review-phase](../review-phase/SKILL.md) against the rewritten branch, passing the **new** phase body to walk against, `WORKROOT`, and the `reviewer` / `fixer` agent types with their `agent_models` tiers plus this phase's `reviewer_model_tier` / `fixer_model_tier` overrides (parsed from the rewritten body's `**Review models**:` line, null when absent). Layer 2 walks: every "Changes" item in the new body, every "Tests" entry, the new acceptance line.

Skip this step only when `change_kind = rebase-only` (no body change → no compliance walk). Even then, spot-run review-phase's Layer 1 mechanical checks to verify the rebase didn't lose unrelated work.

### 4d. Rebase onto the (possibly-rewritten) parent

```bash
# parent's tip may have moved if it was rewritten earlier in the queue.
git -C <WORKROOT> fetch origin
PARENT_TIP=$(git -C <WORKROOT> rev-parse origin/{base})
git -C <WORKROOT> rebase $PARENT_TIP
```

Conflicts:

1. **Spawn a fixer subagent** with the conflict body + new phase body + parent's tip diff. Same fixer agent type as [review-phase](../review-phase/SKILL.md#fix-loop).
2. Fixer resolves, runs inner + outer gate (in `<WORKROOT>`).
3. Orchestrator continues the rebase: `git -C <WORKROOT> rebase --continue`.

Repeat until the rebase finishes clean. If the fixer can't resolve after one retry → stop; do not push a half-rebased branch. Show the conflicting files and ask via `AskUserQuestion` (header `Rebase`): `Abort rebase, stop the run (Recommended)` (`git rebase --abort`; branch stays as it was), `Retry with guidance` (the free-text answer goes into a new fixer prompt), `I'll resolve it by hand` (leave the rebase in progress and exit).

### 4e. Force-push (with confirmation)

`AskUserQuestion`:

- `Force-push <branch> now (Recommended)` (was authorized in Step 0)
- `Pause — let me look at the local state first`

On confirm:

```bash
git -C <WORKROOT> push --force-with-lease origin {branch}
```

**Use `--force-with-lease`, not `--force`.** It refuses to overwrite if the remote moved since the last fetch — protects against another developer's pushes the orchestrator didn't see.

If `--force-with-lease` rejects: another developer pushed. Stop. Re-fetch, re-apply, re-confirm.

### 4f. Refresh the PR-context file (when present)

For the rewritten branch, look for `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md`:

- **File missing** — skip; nothing to refresh.
- **File `status: pending`** — rewrite the file to reflect new title / description / comments per the [prs-context-template](../../prs-context-template.md). Status stays `pending`. The user will publish later via [open-pr-from-context](../open-pr-from-context/SKILL.md).
- **File `status: published`** — the existing PR is auto-updated by the force-push (GitHub/GitLab pick up the new tip). But:
  - Inline comments may now reference SHAs that no longer exist. They'll appear as "outdated" in the PR UI.
  - If the new diff has materially different comment-worthy spots, regenerate the `# Comments` block, set `status: pending`, and re-run [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh) on the file. The script reuses the existing PR, posts new comments. Old "outdated" comments stay visible in the PR for audit; that's the platform's behavior.

Whenever you rewrite any of `# Title`, `# Description`, or `# Comments`, finish by running the `deslop-comments` skill ([deslop-comments](../deslop-comments/SKILL.md)) over the file with the file path as the explicit scope, so the rewritten text stays in Simple English same as [integrate-phase](../integrate-phase-stacked/SKILL.md) does when it first writes the file. Structure, frontmatter, and comment line targets stay untouched.

When rewriting the `# Description` body, **honor `project.pr_template_paths`** from `.vinta-ai-workflows.yaml` — same rule as [integrate-phase](../integrate-phase-stacked/SKILL.md)'s **Open PR via context file** step: follow the project's PR template structure, fill new sections with phase-specific content from the rewritten body, leave un-fillable placeholders untouched. If the prior file used a different template than the project now declares, prefer the current `pr_template_paths` choice — surface the change to the user when the body shape shifts visibly.

Always include in the publish-log block at the bottom of the file:

```markdown
- YYYY-MM-DDThh:mm:ssZ — branch force-pushed (amend-plan); old SHA <x>, new SHA <y>
```

### 4g. Update tracking

Update `ai-plans/TRACKING_{plan-id}/phase-{id}.md` for the rewritten phase (and `run.md` when the graph itself changed):
- Append to its `Completed Phases` entry: `Amended YYYY-MM-DD: <summary>; new SHA <x>; force-pushed`.
- Don't remove the original summary — keep history.

## Step 5 — Final report

After every queue entry processes:

1. Print a per-branch summary:

   ```
   plan/{plan-id-kebab}/phase-1   amended  (commits added: 1; force-pushed)
   plan/{plan-id-kebab}/phase-2   rebased  (no body change; force-pushed)
   plan/{plan-id-kebab}/phase-3   rebased  (no body change; force-pushed)
   plan/{plan-id-kebab}/phase-4   pending  (not yet implemented; will use new parent next implement-plan run)
   ```

2. List any PR-context files now at `status: pending` that need re-publishing.
3. List any phases blocked from rewrite (Step 3 refusals) with the recommended forward path.
4. Reminder: reviewers on existing PRs need a re-review request — force-push erases context. Send a short comment on each affected PR (the orchestrator can do this via the PR CLI if the project's PR policy = "agents create PRs"; otherwise hand off to the human).

## Relay a sub-agent's questions (`NEEDS_INPUT`)

A spawned sub-agent cannot reach the human. When its report says `status: NEEDS_INPUT` (the contract every phase-work prompt carries), the orchestrator turns it into a clickable prompt:

1. **Don't answer for the human, and don't ask in prose.** Don't paste the report and end the turn with "how should I proceed?". Don't re-spawn the agent hoping the question goes away.
2. **Ask with `AskUserQuestion`** (the harness's structured question tool — see **Asking the human** in [AGENTS.md](../../../AGENTS.md)). Pass the report's `questions:` block through unchanged: header, question, options (label + description), multi-select. Above the call, write one line naming the blocked phase and agent, plus its `blocked_on` and `done_so_far`. When the block is malformed (no options, more than 4 questions, an "Other" option), fix the shape and keep the wording. Never fall back to prose.
3. **Record the answer** in the conductor's tracking file when one exists, under the phase's `decisions` list (question header, chosen option or free-text answer). A resumed run reads it and doesn't ask again.
4. **Resume the work.** When the runtime can continue the same sub-agent session (for example Claude Code's `SendMessage` to the agent id), send the answers there. Otherwise spawn a fresh agent of the same type and model with the original prompt plus an `## Answers from the human` section that quotes each question, the answer, and the previous agent's `done_so_far`.
5. **Escalate plan-level answers.** When an answer changes the plan itself (a **Guiding Decisions** row, a phase's scope or acceptance line), ask before resuming: `Amend the plan first (Recommended)` (stop and hand over to [amend-plan](../amend-plan/SKILL.md)), `Apply to this phase only` (record the deviation in tracking and resume).

## Important rules

- **Never `--force`. Always `--force-with-lease`.** Protects against silent overwrites.
- **Plan file edit is the first write.** Commit the new plan body before any branch rewrite. The plan is the contract; the branches are the artifact.
- **Recommend restart when blast radius is high.** The blast-radius step inside Step 0 evaluates signals; ≥2 tripping signals → surface a restart option to the user before any force-push plan is shown. Don't quietly amend a half-rewrite of the whole plan.
- **Never use `§N` shorthand to point at sections** — neither in this skill body, the rewritten plan body, the amendment log entry, nor any prs-context refresh. Always use the section's full name (and link when possible).
- **Phases merged to `main` are immutable.** Convert to `append-new` phases. Refuse to attempt rewrites.
- **Confirm every force-push individually.** No batch "confirm all".
- **Every stop for human input is a structured question.** `AskUserQuestion` with 2–4 concrete options, the recommended (safest) one first — see **Asking the human** in [AGENTS.md](../../../AGENTS.md). Never end a turn with a prose question. Sub-agents return `NEEDS_INPUT`; the orchestrator relays it.
- **`WORKROOT` is resolved once, used everywhere.** Every `git` call takes `git -C <WORKROOT>`; no per-step worktree branching.
- **Three-layer review on every rewritten branch.** Same standard as [implement-plan](../implement-plan/SKILL.md) — via [review-phase](../review-phase/SKILL.md). The amendment isn't done until Layer 3 passes.
- **PR-context file is a derived artifact.** Refresh it after the rewrite; never edit the file as a substitute for fixing the diff.
- **Subagents commit but never push.** Orchestrator owns force-push. Subagents never open PRs either — PRs go through the PR-context file + `open-pr.sh`.
- **No AI co-author trailers in commits.** The project forbids them; treat any AI trailer as a BLOCKER.
- **License check before any new dep.** Refuse `npm add` / `pnpm add` / `pip install` / `poetry add` / `uv add` / `cargo add` / `go get` when the package's SPDX license is in the forbidden list — see AGENTS.md **Dependency licenses**. User can grant a one-off override after acknowledging the violation; record the override in `policies.dependency_licenses.allowed_overrides` before re-running.
- **Stop on Tier-4 failure** (model escalation, same rules as [implement-phase](../implement-phase/SKILL.md#pick-the-model-from-the-plans-per-phase-suggestion)).
- **Stop on rebase failure** the fixer can't resolve in one retry. Don't ship half-rebased branches.

## Quick checklist (orchestrator, per amendment run)

- [ ] User-requested change captured verbatim; classification determined (`body-rewrite` / `insert-new` / `append-new` / `guiding-decisions-change`).
- [ ] Plan parsed; per-phase state map built (state, branch, base, pr_status, merged_to_default); `WORKROOT` resolved from tracking `run_options`.
- [ ] Blast-radius signals computed; `low` → straight to confirmation, `high-blast-radius` / `restart-recommended` → user offered `Restart` / `Amend in place` / `Stop` before the force-push plan is shown.
- [ ] On `Restart` choice: new plan drafted (or hand-off to [plan-feature](../plan-feature/SKILL.md)); old plan annotated `Superseded`; tracking marked; this skill exits.
- [ ] Step 3 refusals surfaced; force-push plan confirmed by user.
- [ ] Plan file edited; amendment log entry appended; committed on `main`.
- [ ] Rewrite queue ordered by stack depth (parent first).
- [ ] For each entry: body change applied (when `amend-existing`); inner + outer gate green.
- [ ] [review-phase](../review-phase/SKILL.md) run on each rewritten branch; BLOCKERs fixed; SHOULD-FIX noted.
- [ ] Rebase onto rewritten parent; conflicts resolved via fixer; tests re-run.
- [ ] `--force-with-lease` push confirmed and executed per branch.
- [ ] PR-context file refreshed (pending or republished).
- [ ] Tracking file updated with amendment notes.
- [ ] Final summary lists every branch state, all `pending` PR-context files, all blocked rewrites with forward-phase suggestions, and a reviewer re-request reminder.
- [ ] If any phase was `append-new`: hand off to [implement-plan](../implement-plan/SKILL.md) for forward execution. This skill does NOT execute new phases.

## What this skill does NOT do

- **Does not execute new (`not-started`) phases.** That's [implement-plan](../implement-plan/SKILL.md)'s job. Edit the plan, then hand off.
- **Does not rewrite history on `main`.** Refuses up front.
- **Does not auto-bypass branch protection.** Surface the rule, stop.
- **Does not amend commits made by humans on the branch unless explicitly authorized.** Multi-author branches require explicit confirmation.
- **Does not delete the plan file or its branches** even when an amendment makes some phases obsolete. Mark obsolete phases in the plan body with `**Superseded YYYY-MM-DD by phase {new-id}**`; leave their branches alone (or let the user delete manually).
