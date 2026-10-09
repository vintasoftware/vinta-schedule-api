---
name: review-phase
description: Internal review gate of [implement-plan] / [amend-plan] / [systematic-debugging] — NOT a standalone entry point. Runs the thermo-nuclear review loop over one phase's diff in vinta_schedule_api, with this skill as the loop's host: it spawns one reviewer sub-agent one tier above the phase's implementer, hands each round of findings to the phase's own implementer to verify, fix or reject with evidence, puts the questions only a person can settle to the human, and loops until the reviewer explicitly approves. The invoking conductor passes the diff, the phase body (the stated requirement) and the resolved `WORKROOT`; do not invoke directly to "review my code" — use the `thermo-nuclear-review-loop` skill for that.
disable-model-invocation: true
---

# Review one phase

The single review implementation shared by every plan-execution conductor: [implement-plan](../implement-plan/SKILL.md) (after an implementer runs), [amend-plan](../amend-plan/SKILL.md) (after a rewrite), and [systematic-debugging](../systematic-debugging/SKILL.md) (against the fix diff). It runs the [thermo-nuclear-review-loop](../thermo-nuclear-review-loop/SKILL.md) skill. Read-only orchestration: this skill **never edits code** — the reviewer reports, and the phase's own implementer fixes. (The one exception is a change systematic-debugging wrote in its own session: there the conductor is the fixer, as the skill is written.)

## Not for an agent that was handed one phase

This skill **orchestrates**: it composes prompts, picks models and spawns other agents. Run it only when you are the session a user or a scheduler invoked to drive a plan.

If you are reading it because something handed you a single phase — a prompt naming your phase id, a branch already cut for you, a worktree you were told to stay inside, or an orchestrator such as [vinta-ai-maestro](https://github.com/vintasoftware/vinta-ai-workflows/tree/main/packages/vinta-ai-maestro) that spawned you — then the conductor this skill describes **is already running, and it is what spawned you**. Do not start a second one underneath it. Do the work in your own session, report back the way your prompt asked, and take from here only what it says about this repository's conventions, gates and commit rules.

The duplication is the smaller cost. A dispatched agent is deliberately reused — the same session takes the review findings, the chore over its own diff, and often the next phase — and that reuse is worth something only because the session that read the codebase is the session that gets the next turn. Hand your phase to a sub-agent and its reading of the code dies with it: you are left holding a summary, and every turn after yours starts cold.

## Inputs (passed by the conductor)

- The phase diff (on the current branch inside `WORKROOT`) and the phase's `base_branch`, which is the loop's baseline.
- The phase body — the loop's **stated requirement** (the **new** body when invoked by amend-plan; the bug report and the fix's intent when invoked by systematic-debugging).
- The plan's **Goals + Non-goals** and **Guiding Decisions**, when the conductor has a plan.
- `WORKROOT`, `SANDBOX_TIER` — **this lane's**, resolved by the conductor. `main_checkout` and `sibling_workroots`, for the stray-write check after each fix round.
- The phase's implementer sub-agent, to continue, and `author_tier` — the tier it actually ran at. systematic-debugging passes neither: it wrote the fix itself.

## Resolve the reviewer model

The reviewer runs **one tier above the implementer**: `min(author_tier + 1, 4)`.

`author_tier` is the tier the implementer **actually ran at** — after implement-phase's escalation, and the covering crew member's tier when a peer took the phase — not the tier the plan wrote down. The review is a judgement of the work, and it should come from a model at least as capable as the one that did it. A Tier 4 implementer is reviewed at Tier 4, since nothing sits above it; the reviewer's independence then comes from being a separate agent with no implementation context, which it always is. When the conductor cannot say which tier the implementer ran at, review at Tier 4.

The fix does not take the reviewer's tier: the phase's own implementer fixes, at its own tier, because it is the agent that knows why the code is the way it is.

Feed that tier into the resolution below, which turns a tier into the model the spawn uses.

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

## Review

Run the [thermo-nuclear-review-loop](../thermo-nuclear-review-loop/SKILL.md) skill over the phase diff. Read it before the first phase you review: it holds the loop's procedure, the gate for questions only a person can settle, and the Review Standard the reviewer applies.

The skill is written for one host session that both fixes the code and talks to the reviewer. A conductor cannot be that host as written: it never edits code, and a sub-agent cannot spawn sub-agents of its own, so the phase's implementer cannot run the loop either. The roles split three ways instead:

| The skill's role | Played by |
|---|---|
| **Host**: spawns the reviewer, relays between reviewer and fixer, runs the gate, counts iterations, asks the human | this skill, in the conductor's session |
| **Reviewer**: reads, runs commands, never edits, and approves or returns blockers | one sub-agent, spawned here at the [resolved model](#resolve-the-reviewer-model) and kept for the whole loop |
| **Fixer**: verifies each finding, chooses the remedy, fixes, rejects with counter-evidence, verifies, commits | the phase's own implementer sub-agent, continued |

Where the skill and this section disagree, this section wins.

### 1. Establish the scope

- **Scope**: the phase's diff, `git -C <WORKROOT> diff <base_branch>...HEAD`, plus anything still uncommitted in `<WORKROOT>`.
- **Baseline**: the phase's `base_branch`.
- **Stated requirement**: the phase body, verbatim. Add the plan's **Goals + Non-goals** and **Guiding Decisions** when the conductor passed them, under a heading that says they bound the whole plan rather than describe this phase.
- Record `git -C <WORKROOT> diff --shortstat <base_branch>` for the final report.

### 2. Spawn the reviewer

Spawn **one** sub-agent at the [resolved model](#resolve-the-reviewer-model), with no implementation context, and give it the skill's reviewer prompt filled in with the scope, the baseline and the stated requirement. It works in `<WORKROOT>`, the lane under review, and reads nothing outside it.

- **Use a general-purpose agent, not the project's `reviewer` agent type.** The reviewer applies one standard: the project's `REVIEW.md` when it has one, otherwise the skill's Review Standard. An agent type that carries review rules of its own would hand it two.
- **Check that it read the standard**, as the skill says: the verdict opens with a line naming the standard and quoting its first and last lines. Without that line, ask it to read the standard and resend before acting on any finding.
- **Keep it for the whole loop**, and continue it on every later pass (for example Claude Code's `SendMessage` to the agent id), so it keeps what it already checked. Where the runtime cannot continue a finished sub-agent, spawn a fresh one each pass and hand it the previous findings, the rejected findings with their counter-evidence, and the settled decisions.
- **It never edits.** Record `git -C <WORKROOT> rev-parse HEAD` and `git -C <WORKROOT> status --porcelain` before each pass and compare them after. A change the reviewer made stops the loop. Ask with `AskUserQuestion` (header `Review`), naming the files it touched: `Stop the review (Recommended)` (leave its changes in place for you to look at, and return `STOPPED`), `Keep the changes and continue` (the implementer verifies them like any other finding).

### 3. Hand the findings to the implementer

Continue the phase's implementer sub-agent with a **delta**: the reviewer's findings verbatim, the settled decisions so far, and the skill's fixer instructions — its **Verify before fixing** and **Implement and verify** sections, quoted or pointed at by path. Do not re-send the phase body, the plan sections or the dependency summaries: the implementer was given all of that when it started, and repeating it invites a re-implementation rather than a fix.

The implementer must:

1. Verify every finding before acting on it, and reject the unsupported ones with concrete counter-evidence.
2. Fix the justified ones as one coherent change, re-run the inner loop and the outer gate in `<WORKROOT>`.
3. Commit the round the way the phase commits its work (the project's commit strategy), with a message naming the findings it addresses.
4. Report each finding as fixed (and how), rejected (and the counter-evidence), or gated — a decision that is not the implementer's to make, with the skill's trigger, the evidence, the reviewer's recommendation and its own.

It never asks the human itself: a question comes back as `status: NEEDS_INPUT`, which this skill relays (see [Relay a sub-agent's questions](#relay-a-sub-agents-questions-needs_input)), then continues the implementer with the answers.

After it returns, run the stray-write check from [implement-phase](../implement-phase/SKILL.md) (main checkout and sibling lanes) before the next pass: a fix written into the wrong tree is missing from the commit the reviewer reads.

**When the change was written in the conductor's own session** — [systematic-debugging](../systematic-debugging/SKILL.md) run outside a plan — there is no implementer to continue. The conductor is the fixer itself, exactly as the skill is written, and follows the fixer instructions directly.

**Where the runtime cannot continue a finished sub-agent**, spawn a fresh agent of the project's `fixer` type at the implementer's own tier, and give it the phase body, the plan-level sections and the findings, because it has none of them. Note in the phase's tracking record that the fix was a cold hand-off.

### 4. Run the gate

A gated item goes to the human, never into the code and never decided by the conductor. Once per iteration, after the implementer has fixed everything else, ask with `AskUserQuestion` (header `Review`), one question per gated item: the finding with `file:line`, the evidence, the reviewer's recommendation in its own words, and the implementer's. Offer 2–4 options drawn from the item, the implementer's recommendation first with ` (Recommended)`. For a scenario nothing reaches, or a check on data already validated upstream, that is normally `Reject the finding (Recommended)` next to `Handle it in this phase`. A destructive option is never the recommended one.

Record each answer as a **settled decision** with the date, in the phase's tracking record. Every later reviewer and implementer message carries the settled decisions. The reviewer may re-raise one only with new evidence, which it names. Gate interviews do not count as iterations.

An answer that changes the plan itself (a **Guiding Decisions** row, the phase's scope or acceptance line) is escalated the way the relay escalates one: `Amend the plan first (Recommended)` or `Apply to this phase only`.

### 5. Re-review

Send the same reviewer the round's commits, the implementer's report (a summary to check against the diff, not one to trust), the rejected findings with their counter-evidence, and the settled decisions. Tell it to re-read the whole diff and reapply the full standard under the Review Standard's **Pass two and later** rules.

### 6. The budget: 20 iterations

Count each pass that returns blockers as one unsuccessful iteration. After **20**, pause before the 21st. Give the human, in a few lines: what changed across the iterations; which blockers remain and whether the implementer verified, disputed or considers each one diminishing returns; its view of whether more work is worth it; and the risks that remain. Then ask with `AskUserQuestion` (header `Review`): `Continue for 20 more`, `Stop — hand the phase back unapproved`, `Amend the plan` (stop and hand over to [amend-plan](../amend-plan/SKILL.md)). Put first, as recommended, the option the report supports. Silence is not permission to continue.

### 7. When the loop ends

It ends successfully **only when the reviewer explicitly approves**. Passing gates are not approval, and neither is a reviewer that ran out of things to say. Return `PASS` with the reviewer's approval line, the iteration commits, the `--shortstat` at the start and the end, the rejected findings and the settled decisions, so the conductor can record them in tracking.

If the human stopped the loop, return `STOPPED` with the blockers that remain. The conductor does not integrate a phase that did not pass.

## Relay a sub-agent's questions (`NEEDS_INPUT`)

A spawned sub-agent cannot reach the human. When its report says `status: NEEDS_INPUT` (the contract every phase-work prompt carries), the orchestrator turns it into a clickable prompt:

1. **Don't answer for the human, and don't ask in prose.** Don't paste the report and end the turn with "how should I proceed?". Don't re-spawn the agent hoping the question goes away.
2. **Ask with `AskUserQuestion`** (the harness's structured question tool — see **Asking the human** in [AGENTS.md](../../../AGENTS.md)). Pass the report's `questions:` block through unchanged: header, question, options (label + description), multi-select. Above the call, write one line naming the blocked phase and agent, plus its `blocked_on` and `done_so_far`. When the block is malformed (no options, more than 4 questions, an "Other" option), fix the shape and keep the wording. Never fall back to prose.
3. **Record the answer** in the conductor's tracking file when one exists, under the phase's `decisions` list (question header, chosen option or free-text answer). A resumed run reads it and doesn't ask again.
4. **Resume the work.** When the runtime can continue the same sub-agent session (for example Claude Code's `SendMessage` to the agent id), send the answers there. Otherwise spawn a fresh agent of the same type and model with the original prompt plus an `## Answers from the human` section that quotes each question, the answer, and the previous agent's `done_so_far`.
5. **Escalate plan-level answers.** When an answer changes the plan itself (a **Guiding Decisions** row, a phase's scope or acceptance line), ask before resuming: `Amend the plan first (Recommended)` (stop and hand over to [amend-plan](../amend-plan/SKILL.md)), `Apply to this phase only` (record the deviation in tracking and resume).

## Output

Return to the conductor `PASS` — the reviewer explicitly approved — with the approval line, the iteration commits, the `--shortstat` at the start and the end, the rejected findings and the settled decisions; or `STOPPED`, with the blockers that remain and why the loop stopped. The conductor owns branch / push / PR — this skill hands back a working tree in `WORKROOT` whose fixes are committed.
