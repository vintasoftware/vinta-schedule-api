---
name: integrate-phase-stacked
description: Internal integration step of [implement-plan] — NOT a standalone entry point. Pushes one reviewed phase along vinta_schedule_api's commit strategy and opens (or updates) its PR through the prs-context file + bundled open-pr.sh — the only PR-creation path. The conductor passes the resolved `WORKROOT` / `BASE_BRANCH` and the PR / inline-comment policy; do not invoke directly to push arbitrary work.
disable-model-invocation: true
---

# Integrate one phase

Invoked by [implement-plan](../implement-plan/SKILL.md) after [review-phase](../review-phase/SKILL.md) returns clean. Pushes the phase and routes its PR through the context file. Subagents never push or open PRs — this orchestrator step does.

This is the **stacked-branches** variant: one branch + one PR per phase. The conductor dispatches here when `run_options.commit_strategy_resolved = stacked-branches`; for `modular-commits` it dispatches to [integrate-phase-modular](../integrate-phase-modular/SKILL.md) instead.

## Inputs (passed by the conductor)

- `WORKROOT` — **this lane's**, resolved by the conductor. Under a parallel run several integrate steps may be in flight at once, each on its own lane; nothing here is shared between them.
- `phase.base_branch` — the phase's dependency-derived base, computed by the conductor ([Lane branch topology](../implement-plan/SKILL.md#lane-branch-topology)). This is the branch the phase was cut from **and** the PR's `base`. The plan-level `BASE_BRANCH` is only the base of phases that declare no dependencies.
- `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` policy + `run_options.generate_inline_comments`.
- The phase record + plan-level decisions (for the PR body).

**`WORKROOT` topology rule.** Every phase branches off **its own computed base** — the branch derived from that phase's `**Depends on**:` set, which is `<BASE_BRANCH>` for a phase with no dependencies (see [Lane branch topology](../implement-plan/SKILL.md#lane-branch-topology)) — and **every** `git` / lint / test / build / migrate call runs with `git -C <WORKROOT>` (or after `cd <WORKROOT>`). When `use_worktree = false`, `WORKROOT` is the main checkout and phases run one at a time in place; when `true`, `WORKROOT` is a worktree and branches / commits live inside it, never touching the main checkout's working tree. Under parallel execution `WORKROOT` is **this lane's** worktree and nothing else — a lane never reads or writes a sibling lane's tree. One uniform path — no per-step worktree branching.

## Push stacked branch

Branch naming: `plan/{plan-id-kebab}/phase-{phase.id}` (one branch + one PR per phase, stacked on its **dependencies** rather than on plan order).

Each phase branches from `<phase.base_branch>` — the branch the conductor computed from that phase's `**Depends on**:` set (see [Lane branch topology](../implement-plan/SKILL.md#lane-branch-topology)): `<BASE_BRANCH>` for a phase with no dependencies, the single dependency's phase branch for one, `plan/{plan-id-kebab}/integ-{phase.id}` for several. The conductor creates the branch when it assigns the phase to a lane:

```bash
git -C <WORKROOT> checkout <phase.base_branch>
git -C <WORKROOT> checkout -b plan/{plan-id-kebab}/phase-{phase.id}
# subagent's commits land on this branch
git -C <WORKROOT> push -u origin plan/{plan-id-kebab}/phase-{phase.id}
```

A chain-shaped plan reproduces the classic stack exactly — each phase depends on the one before it, so `<phase.base_branch>` *is* the previous phase's branch. A plan with independent phases produces several stacks rooted at `<BASE_BRANCH>`, reunited by the wave integration branches.

**PR base per phase** (the `base` field written into the prs-context frontmatter — this is what `gh pr create --base` / `glab mr create --target-branch` opens the PR against; getting it wrong makes the PR diff include every upstream phase and the review unusable):

- `base = <phase.base_branch>`, always. Never `<BASE_BRANCH>` for a phase that has dependencies.

**Every branch the plan lands through gets a PR.** Phase PRs alone do not reach `<BASE_BRANCH>`: a phase based on `integ-{phase.id}` targets a branch that nothing else targets, and the conflict resolutions made during wave merges are on no phase branch at all. So a stacked run opens three kinds of PR:

| PR | `branch` (head) | `base` | prs-context file | Opened |
|---|---|---|---|---|
| Phase | `plan/{plan-id-kebab}/phase-{phase.id}` | `<phase.base_branch>` | `phase-{phase.id}.md` | when the phase passes review |
| Integration | `plan/{plan-id-kebab}/integ-{phase.id}` | `<BASE_BRANCH>` | `integ-{phase.id}.md` (`kind: integration`) | just before the phase PR, only for a phase with two or more dependencies |
| Plan | the final `plan/{plan-id-kebab}/wave-{N}` | `<BASE_BRANCH>` | `plan.md` (`kind: plan`) | once, at run end, after the final wave branch is built |

The **integration PR** is based on `<BASE_BRANCH>` because the `integ-` branch has several parents and none of them is "below" it. Its diff starts as all of its dependencies and shrinks, as their PRs merge, to the merge commits and any conflict resolution the fixer made. Its description says: merge the dependencies' PRs first, then this one, then retarget the phase PR to `<BASE_BRANCH>`.

The **plan PR** is the one branch known to hold the whole plan. Its description lists every phase and integration PR in an order that merges (wave by wave; inside a wave, plan order; each integration PR right before its phase PR) and offers two ways to land: merge the plan PR alone, or merge the listed PRs in order and the plan PR last — by then its diff is only what no other PR carries. Either way, **merge commits, not squash**: a squash makes every PR stacked above it repeat the changes below it.

## Open PR via context file

This is the **only** PR-creation path. PRs always go through `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md` + the bundled [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh) script — even when inline comments are not requested. The file is the durable record; the script is the publisher. Subagents never open PRs themselves; the orchestrator does, after review passes.

One PR per phase — the [Open PR via context file](#open-pr-via-context-file) step runs after this phase passes review, writing `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md`.

Two project-level signals decide the actual behavior:

| `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` policy | `run_options.generate_inline_comments` | What this step does |
|---|---|---|
| agents create PRs | false | Write minimal context file (`# Title`, `# Description`, empty `# Comments`). Run `open-pr.sh` → PR opened, no inline comments. |
| agents create PRs | true  | Write full context file (title + description + 3–10 inline comments). Run `open-pr.sh` → PR opened, all comments posted. |
| branches only     | false | **Skip this step entirely.** Human will open the PR manually from the pushed branch. |
| branches only     | true  | Write full context file (durable record). **Don't run `open-pr.sh`.** Human can publish later from a CLI-equipped session via [open-pr-from-context](../open-pr-from-context/SKILL.md). Surface this in the user update. |

### Steps

1. **Skip if neither column applies** (policy = branches only AND `generate_inline_comments = false`). Return to the conductor's tracking step.

2. **Honor existing PR / MR templates.** Read `project.pr_template_paths` from `.vinta-ai-workflows.yaml`. For each entry:
   - **One template** → load it; the prs-context `# Description` body must follow that template's section structure verbatim. Fill each section with phase-specific content drawn from the plan's **Goals + Non-goals**, **Guiding Decisions**, and the phase body. Preserve any `<!-- HTML comments -->` placeholders; do not strip the template's checklists. Sections you can't fill from phase data → leave the template's placeholder/prompt untouched (don't fabricate).
   - **Multiple templates** (`PULL_REQUEST_TEMPLATE/` directory) → ask once via `AskUserQuestion`: list each template + its filename, ask which to use for this run. Cache the choice in tracking under `run_options.pr_template_used` so subsequent phases of the same plan use the same one without re-asking.
   - **Empty array** → free-form description. Default sections: `## Summary` (1–3 sentences), `## Plan reference` (link / phase id), `## Test plan` (commands the reviewer can run).

   When the project's PR template includes a checkbox checklist (e.g. `- [ ] Tests added`, `- [ ] Docs updated`), tick the boxes the phase's diff actually satisfies and leave unsatisfied ones unticked — never auto-tick everything.

   GitHub also honors `?template=<name>` in the PR-create URL when the project has a multi-template directory. `gh pr create --body-file` writes the body directly so the URL trick isn't needed; the body must match the chosen template's structure regardless.

3. **Pick comment targets** (only when `generate_inline_comments = true`). Read the phase diff via `git -C <WORKROOT> diff <BASE_BRANCH>...HEAD` (or the previous phase branch for stacked phases). Select 3–10 spots that benefit from a one-paragraph context note — typically:
   - A subtle invariant the diff relies on (cite the plan's **Goals + Non-goals** / **Guiding Decisions** entries — by name, never use `§` shorthand).
   - A workaround for a known framework / library limitation.
   - A naming choice driven by an upstream contract.
   - The off-flag short-circuit when a feature flag is in **Guiding Decisions**.
   - Why a seemingly-cleaner refactor wasn't made (out of scope per **Goals + Non-goals**).
   - Cross-phase coupling (this hook is consumed by phase N+k).

   Skip lint/format churn, boilerplate matching nearby files, standard patterns from AGENTS.md, and self-explanatory test names. **A clean phase produces few comments — that's fine. Don't pad.**

   When `generate_inline_comments = false`: skip this step. The file's `# Comments` block stays empty.

4. **Write the prs-context file** at `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md`, following [resources/prs-context-template.md](../../prs-context-template.md). Frontmatter: `plan_id`, `feature_name`, `phase_id`, `phase_title`, `branch`, `base`, `created_at`, `status: pending`, empty `pr_url`. **`base` is the branch the PR opens against — resolve it per the commit strategy, never default it to `<BASE_BRANCH>` blindly:** for stacked branches a phase's PR bases on its dependency-derived `<phase.base_branch>` — `<BASE_BRANCH>` only for a phase with no dependencies (see the PR-base rule under the Push stacked branch step above); an integration PR (`kind: integration`) and the plan PR (`kind: plan`) base on `<BASE_BRANCH>`. For a single plan-level PR (modular / one-PR strategies) `base = <BASE_BRANCH>`. Body sections: `# Title` (single-line PR title), `# Description` (Markdown body — uses the project's PR template structure from step 2 when one exists), `# Comments` (YAML list of `{file, start_line, end_line?, side, body}` — empty list when comments are off).

5. **Deslop the prose.** Run the `deslop-comments` skill ([deslop-comments](../deslop-comments/SKILL.md)) over the file you just wrote, passing `.vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md` as the explicit scope. `# Title`, the `# Description` body, and every `# Comments` `body` must read as Simple English: one idea per sentence, current behavior stated directly instead of "not X" framing, and no AI-slop vocabulary (`gate`, `guard`, `backstop`, `surface`, `leverage`, `plumb`, `canonical`, …). Keep precise domain terms, and keep the project's PR-template structure intact — section headings, `` placeholders, and checklist lines stay exactly as step 2 wrote them. This pass rewrites prose only: it never touches the frontmatter, the section layout, or which file and lines a comment targets. Do it before `open-pr.sh` runs, since the published PR body and comments come straight from this file.

6. **Confirm `.vinta-ai-workflows/prs-context/` is in `.gitignore`.** [vinta-install-ai-tools-setup](../vinta-install-ai-tools-setup/SKILL.md) runs the multi-vendor setup script which appends `.vinta-ai-workflows/prs-context/` on its first invocation. If an older bootstrap missed it, append it now.

7. **Run `open-pr.sh`** (only when policy = agents create PRs). Detect a usable CLI (`gh` for GitHub, `glab` for GitLab) plus the script's other deps (`yq`, `jq`):

   ```bash
   bash ai-tools/skills/open-pr-from-context/scripts/open-pr.sh .vinta-ai-workflows/prs-context/{feature-kebab}/phase-{phase.id}.md
   ```

   The script opens the PR (or detects an existing one), posts each inline comment, rewrites the file's frontmatter to `status: published` + populated `pr_url`, appends a publish log. Exit codes:

   - `0` — PR up, all comments (if any) posted. Capture `pr_url` for the user update.
   - `1` — PR up, ≥1 comment failed. Surface the failed `(file:line)` list to the user, with the `gh error:` / `glab error:` line the script printed above each one; continue to the tracking step.
   - `2` — Hard failure (deps missing, branch not pushed, CLI unauthed, file invalid). Surface the script's stderr; treat the phase as having no PR. The file stays `status: pending` so the user can re-run after fixing the gap.

   When policy = "branches only": **don't run the script.** File stays `status: pending`.

   {If integrations.pr-review-canvas = enabled in `.vinta-ai-workflows.yaml`:} **Review canvas.** After exit `0` or `1`, the PR is up. Generate its review canvas with the `pr-review-canvas` skill ([pr-review-canvas](../pr-review-canvas/SKILL.md)). Pass the PR number from the end of `pr_url` (`…/pull/<n>` or `…/merge_requests/<n>`), as in `/pr-review-canvas <n>`. Run it again each time this step re-runs `open-pr.sh` on the same PR, for example after each phase under one plan-level PR. The tool updates the canvas incrementally. The skill posts the canvas as a PR comment unless the project's `pr-review.config.yml` turns sharing off. Add its local review URL and the comment link to the user update. A canvas that fails never fails the phase. Report the skill's error, and point at `pr-review doctor` when the error suggests setup. When the `pr-review-canvas` skill is not installed, say so once, point at `vinta-install-ai-tools-setup`, and skip.

8. **Skill wrapper** — [open-pr-from-context](../open-pr-from-context/SKILL.md) is available for ad-hoc invocation (after the run, on a different machine, etc.). The orchestrator can call the script directly here; the skill is for humans.

## Output

Return to the conductor: the branch pushed (plus the `integ-` branch when one was pushed), and each PR-context file path written — the integration file first when there is one — with its `status` (`published` + `pr_url` when `open-pr.sh` ran; `pending` otherwise) plus the publish command when `pending`.
