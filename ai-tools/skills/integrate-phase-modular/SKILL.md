---
name: integrate-phase-modular
description: Internal integration step of [implement-plan] — NOT a standalone entry point. Pushes one reviewed phase along vinta_schedule_api's commit strategy and opens (or updates) its PR through the prs-context file + bundled open-pr.sh — the only PR-creation path. The conductor passes the resolved `WORKROOT` / `BASE_BRANCH` and the PR / inline-comment policy; do not invoke directly to push arbitrary work.
---

# Integrate one phase

Invoked by [implement-plan](../implement-plan/SKILL.md) after [review-phase](../review-phase/SKILL.md) returns clean. Pushes the phase and routes its PR through the context file. Subagents never push or open PRs — this orchestrator step does.

This is the **modular-commits** variant: one branch + one PR for the whole plan, with one atomic commit per logical unit. The conductor dispatches here when `run_options.commit_strategy_resolved = modular-commits`; for `stacked-branches` it dispatches to [integrate-phase-stacked](../integrate-phase-stacked/SKILL.md) instead.

## Inputs (passed by the conductor)

- `WORKROOT`, `BASE_BRANCH` — resolved once by the conductor.
- `PR creation policy: **agents create PRs** — every phase opens a PR via the bundled prs-context file + [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh).` policy + `run_options.generate_inline_comments`.
- The phase record + plan-level decisions (for the PR body).

**`WORKROOT` topology rule.** Every phase branches off the previous phase (first executed phase off `<BASE_BRANCH>`), and **every** `git` / lint / test / build / migrate call runs with `git -C <WORKROOT>` (or after `cd <WORKROOT>`). When `use_worktree = false`, `WORKROOT` is the main checkout and this is exactly today's in-place behavior; when `true`, `WORKROOT` is the worktree and branches / commits stack inside it, never touching the main checkout's working tree. One uniform path — no per-step worktree branching.

## Push to plan branch

Branch naming: `plan/{plan-id-kebab}` (one branch for the whole plan — no per-phase suffix).

**First executed phase** (branches from `<BASE_BRANCH>`, already made current by the conductor):

```bash
git -C <WORKROOT> checkout <BASE_BRANCH>
git -C <WORKROOT> checkout -b plan/{plan-id-kebab}
# subagent's atomic unit commits land on this branch
git -C <WORKROOT> push -u origin plan/{plan-id-kebab}
```

**Subsequent phases** (stay on the same plan branch — no new branch):

```bash
git -C <WORKROOT> checkout plan/{plan-id-kebab}
# subagent's atomic unit commits land on this branch
git -C <WORKROOT> push origin plan/{plan-id-kebab}
```

The branch carries every phase's commits in plan order. Reviewers read the commit log top-to-bottom as a table of contents of the implementation.

## Open PR via context file

This is the **only** PR-creation path. PRs always go through a single `.vinta-ai-workflows/prs-context/{feature-kebab}/plan.md` file (one PR per plan, not per phase) + the bundled [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh) script — even when inline comments are not requested. The file is the durable record; the script is the publisher. Subagents never open PRs themselves; the orchestrator does, after review passes.

**PR opens once — after Phase 1 passes review.** Subsequent phases push their atomic unit commits to the same plan branch (`plan/{plan-id-kebab}`); the orchestrator re-runs [open-pr.sh](../open-pr-from-context/scripts/open-pr.sh) against the same plan-level prs-context file at `.vinta-ai-workflows/prs-context/{feature-kebab}/plan.md`. The script is idempotent for already-open PRs — it updates the body, appends new inline comments, and posts a `Phase {N} complete — pushed M commits` PR comment.

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

3. **Pick comment targets** (only when `generate_inline_comments = true`). Read the phase diff via `git -C <WORKROOT> diff <BASE_BRANCH>...HEAD`. Select 3–10 spots that benefit from a one-paragraph context note — typically:
   - A subtle invariant the diff relies on (cite the plan's **Goals + Non-goals** / **Guiding Decisions** entries — by name, never use `§` shorthand).
   - A workaround for a known framework / library limitation.
   - A naming choice driven by an upstream contract.
   - The off-flag short-circuit when a feature flag is in **Guiding Decisions**.
   - Why a seemingly-cleaner refactor wasn't made (out of scope per **Goals + Non-goals**).
   - Cross-phase coupling (this hook is consumed by phase N+k).

   Skip lint/format churn, boilerplate matching nearby files, standard patterns from AGENTS.md, and self-explanatory test names. **A clean phase produces few comments — that's fine. Don't pad.**

   When `generate_inline_comments = false`: skip this step. The file's `# Comments` block stays empty.

4. **Write the prs-context file** at `.vinta-ai-workflows/prs-context/{feature-kebab}/plan.md`, following [resources/prs-context-template.md](../../prs-context-template.md). Frontmatter: `plan_id`, `feature_name`, `phase_id`, `phase_title`, `branch`, `base`, `created_at`, `status: pending`, empty `pr_url`. **`base` is the branch the PR opens against:** this strategy opens a single plan-level PR, so `base = <BASE_BRANCH>`. Body sections: `# Title` (single-line PR title), `# Description` (Markdown body — uses the project's PR template structure from step 2 when one exists), `# Comments` (YAML list of `{file, start_line, end_line?, side, body}` — empty list when comments are off).

5. **Confirm `.vinta-ai-workflows/prs-context/` is in `.gitignore`.** [vinta-install-ai-tools-setup](../vinta-install-ai-tools-setup/SKILL.md) runs the multi-vendor setup script which appends `.vinta-ai-workflows/prs-context/` on its first invocation. If an older bootstrap missed it, append it now.

6. **Run `open-pr.sh`** (only when policy = agents create PRs). Detect a usable CLI (`gh` for GitHub, `glab` for GitLab) plus the script's other deps (`yq`, `jq`):

   ```bash
   bash ai-tools/skills/open-pr-from-context/scripts/open-pr.sh .vinta-ai-workflows/prs-context/{feature-kebab}/plan.md
   ```

   The script opens the PR (or detects an existing one), posts each inline comment, rewrites the file's frontmatter to `status: published` + populated `pr_url`, appends a publish log. Exit codes:

   - `0` — PR up, all comments (if any) posted. Capture `pr_url` for the user update.
   - `1` — PR up, ≥1 comment failed. Surface the failed `(file:line)` list to the user; continue to the tracking step.
   - `2` — Hard failure (deps missing, branch not pushed, CLI unauthed, file invalid). Surface the script's stderr; treat the phase as having no PR. The file stays `status: pending` so the user can re-run after fixing the gap.

   When policy = "branches only": **don't run the script.** File stays `status: pending`.

7. **Skill wrapper** — [open-pr-from-context](../open-pr-from-context/SKILL.md) is available for ad-hoc invocation (after the run, on a different machine, etc.). The orchestrator can call the script directly here; the skill is for humans.

## Output

Return to the conductor: the branch pushed, and the PR-context file path with its `status` (`published` + `pr_url` when `open-pr.sh` ran; `pending` otherwise) plus the publish command when `pending`.
