---
name: review-phase
description: Internal review gate of [implement-plan] / [amend-plan] / [systematic-debugging] — NOT a standalone entry point. Runs the mandatory three-layer review (mechanical checks, plan-compliance walkthrough, independent reviewer subagent) plus the fix loop against one phase's diff in vinta_schedule_api, spawning an independent reviewer, sending findings back to the phase's own implementer to fix, and looping until all three layers are clean. The invoking conductor passes the diff, the phase body to walk against, and the resolved `WORKROOT`; do not invoke directly to "review my code" — use the project's standard code-review path for that.
---

# Review one phase

The single review implementation shared by every plan-execution conductor: [implement-plan](../implement-plan/SKILL.md) (after an implementer runs), [amend-plan](../amend-plan/SKILL.md) (after a rewrite), and [systematic-debugging](../systematic-debugging/SKILL.md) (against the fix diff). Read-only orchestration: this skill **never edits code** — every issue becomes a fix-up subagent task.

## Inputs (passed by the conductor)

- The phase diff (on the current branch inside `WORKROOT`).
- The phase body to walk against (the **new** body when invoked by amend-plan).
- `WORKROOT`, `SANDBOX_TIER` — **this lane's**, resolved by the conductor.
- `main_checkout` — the repo root the run was invoked from (equals `WORKROOT` when no worktree).
- `sibling_workroots` — every other lane's workroot in the pool (empty for a sequential run). The stray-write check covers these too: a write into a lane that is actively implementing another phase is worse than a stray main-checkout write.
- `run_options.full_test_suite` — resolves which outer gate Layer 1 item 3 verifies ran (false = scoped suite; true = full repo suite).
- The project's `reviewer` + `fixer` agent types, plus their `agent_models.reviewer` / `agent_models.fixer` tiers (when set in `.vinta-ai-workflows.yaml`).
- Optional per-phase `reviewer_model_tier` / `fixer_model_tier` overrides — the tiers parsed from this phase's `**Review models**:` line in the plan (null when the phase didn't set one, which is the common case).
- `author_tier` and `crew_reviewers` — the tier of the crew member that implemented this phase, and the plan's **Crew** table rows whose role is `reviewer` (id + tier). Both null for a legacy plan with no roster. Reviewers carry no `WORKROOT` of their own: they use the one above.

## Resolve the reviewer + fixer model

Each of `reviewer` and `fixer` spawns at an **effective tier**, resolved per role with this precedence:

1. The phase's `reviewer_model_tier` / `fixer_model_tier` override, when the conductor passed one (the plan chose a non-default review model for this critical phase).
2. Else, **for `reviewer` only: the cheapest member in `crew_reviewers` whose tier is at or above `author_tier`.** Reviewers are their own members and never write code, so the independence comes from the role rather than from a tier gap — which means a peer-tier review is a genuine second pair of eyes, not an agent grading itself. A reviewer's tier is a floor in the same way an implementer's is: it reads work at or below its own capability.
3. Else the project-wide `agent_models.reviewer` / `agent_models.fixer` tier from `.vinta-ai-workflows.yaml`.
4. Else unset → the runtime default model.

Step 2 is skipped when the roster staffs no qualified reviewer — no reviewers at all, or none at or above this phase's tier, or a legacy plan with no **Crew** table — and the resolution falls through to the project default, which is the behaviour every plan had before rosters existed.

**A claimed reviewer is a running agent, not just a model.** It has one session ledger, so two reviews as the same reviewer would collide over it: if the reviewer this phase needs is mid-review elsewhere, wait for it rather than picking another. The wait is safe — reviewers never take phases, so it is always waiting on a review already in flight.

**It reviews in this phase's `WORKROOT`.** A reviewer has no worktree of its own; it reads the implementer's, with the phase's changes still uncommitted in it. Every Layer 1 command below is therefore a `git -C <WORKROOT>` against a **dirty tree**, and that is the intent — the fix loop corrects the working tree before anything is committed, so a finding never becomes a mistake recorded on the branch plus a correction after it. Continue the reviewer's session when its previous review was in this same lane; otherwise it starts cold, because a session cannot follow an agent into a different directory.

**It does not apply to `fixer`.** The fix goes back to the agent that wrote the code, at its own tier; see the fix loop. A review is a judgement about the code and a fix is a change to it, and the tier that earned the first does not follow the second.

Feed that effective tier into the resolution below (it turns a tier into a concrete spawn model). A phase override applies to that phase only; the next phase falls back to its own author's tier unless it too overrides.

## Resolve an `agent_models` tier to a spawn model

`.vinta-ai-workflows.yaml` may carry an `agent_models` section mapping a role/task (`reviewer`, `fixer`, `worktree_prep`, `integrate`) to a **tier** (1–4) into the same table the per-phase implementer suggestion uses — [`ai-tools/skills/plan-feature/resources/ai-models.yaml`](../plan-feature/resources/ai-models.yaml). `agent_models` is the **project default** for these roles; for `reviewer` / `fixer`, a plan phase's optional `**Review models**:` line may override the tier for that one phase (the caller resolves that precedence and hands this block the effective tier). The mechanical steps (`worktree_prep`, `integrate`) are never plan-named. To turn a tier into the model a spawn actually uses:

1. Determine the **effective tier** for the role. For the mechanical steps it is simply `agent_models.<role>`. For `reviewer` / `fixer` there are now three sources, in this order:
   1. a per-phase `**Review models**:` override the conductor passed;
   2. **the cheapest reviewer on the plan's Crew table at or above the phase's tier** (see [Who reviews](#who-reviews-a-member-not-a-tier) below);
   3. `agent_models.<role>`.
2. **No effective tier (override absent AND key unset, or the whole `agent_models` section absent) → do not force a model.** Spawn with the runtime's default model (today's behavior). Skip the rest.
3. Open [`ai-tools/skills/plan-feature/resources/ai-models.yaml`](../plan-feature/resources/ai-models.yaml), take that tier's `models`, **filter to the vendors the runtime actually exposes**, pick the cheapest/fastest survivor, and translate it to the runner's spawn form — the same resolution [implement-phase](../implement-phase/SKILL.md) runs for the implementer, only keyed by a config tier instead of a plan line.
4. `ai-models.yaml` missing, or the tier has no runtime-available vendor → fall back to the runtime default and surface the fallback once. Never hard-fail a phase over a model-selection miss.

### Who reviews: a member, not a tier

**Reviewers are their own members on the plan's Crew table**, with `role: reviewer`, and they never take phases. That is what makes an agent reviewing its own work impossible rather than merely unlikely — and it replaces an earlier rule that resolved a reviewer *model* one tier above the author, which was a proxy for independence and failed in both directions: a phase covered by the top-tier implementer had nobody above it and fell back to being read at its own tier, and a tier says nothing about *who* once a member is a durable agent rather than a model id.

Resolve it from the roster: the **cheapest reviewer at or above the phase's tier**. A reviewer's tier is a floor in the same way an implementer's is — it reads work at or below its own capability, never above it.

Three consequences worth knowing:

- **A reviewer is claimed, not borrowed.** It has one session ledger, so two reviews running as the same reviewer would collide over it; a phase whose reviewer is busy waits. That cannot deadlock — reviewers never take phases, so the wait is always on a review already running.
- **A reviewer reads the lane it is reviewing**, uncommitted changes and all, so findings are fixed before the commit. It has no worktree of its own, and therefore keeps a session only when consecutive reviews land in the same lane.
- **No reviewer on the roster → fall through to `agent_models.reviewer`**, cold, one session per phase. That is what every plan did before, and the one thing a roster-less plan leaves on the table.

A plan with no **Crew** table skips this step entirely and resolves `agent_models.<role>` as it always did.

### What `fixer` still governs

`fixer` governs fewer rounds than it used to. A finding goes back to the phase's
own implementer — the same agent, in the same session — which fixes at the tier
of the crew member that took the phase
because it *is* that member; `agent_models.fixer` applies to the cold cases
only — a runtime that cannot continue a sub-agent, and the last round before
giving up, which is deliberately handed to an agent that has not seen the work.
See the fix loop in [review-phase](../review-phase/SKILL.md).

Record the **model actually used** in tracking, **and which of the three sources it came from** — a review that quietly fell through to the project default because the roster had nobody above the author is a fact worth being able to read later. For `reviewer` / `fixer`, alongside the review note; for the mechanical steps, in the phase's tracking row next to the branch/PR fields.

## Review

Three layers, all required, in order. The reviewing orchestrator never edits — every issue surfaces as a fix-up subagent task.

## Layer 1 — Mechanical checks

**Against the working tree, before the commit.** The phase's changes are still uncommitted in `<WORKROOT>`, and that is what every command here reads. It is why review sits before integrate rather than after: a finding is fixed in the tree, so the branch never records the mistake and a correction on top of it.

1. `git -C <WORKROOT> status` + `git -C <WORKROOT> diff --stat`: confirm the file list matches the agent's report.
2. **Read the full diff** for every changed file using `git -C <WORKROOT> diff`. Spot-checking is not enough.
3. **Verify the outer gate** ran + green. By default that is `docker compose run --rm api uv run python manage.py check --deploy` (repo-wide) AND the scoped suite `docker compose run --rm api uv run pytest <app>/tests/ -n auto` covering the touched apps. {If run_options.full_test_suite = true:} the outer gate runs `docker compose run --rm api uv run python manage.py check --deploy` AND the full `docker compose run --rm api uv run pytest -n auto` instead — verify that. Look in the report for explicit confirmation the applicable gate was executed + passed. Vague confirmation → **re-run yourself** (in `<WORKROOT>`).
4. **Scope creep**: file touched outside the expected surface area? Unrelated formatting churn? Surface it.
5. **No-secrets scan**: `git -C <WORKROOT> diff` for `password|secret|token|api_key|AKIA|BEGIN [A-Z]+ KEY`.
6. **Stray main-checkout writes — only when `WORKROOT != <main_checkout>` (i.e. a worktree run).** A subagent told to work inside the worktree can resolve an absolute path back to the **main checkout** and silently edit files there; because worktrees have independent working trees, those edits never reach the phase commit — they sit as uncommitted thrash in the main checkout and read as a silent implementer/fixer failure. **When `SANDBOX_TIER = enforced`, the OS sandbox already blocks these writes and this becomes a cheap backstop (a clean `git status` is the expected result). When `SANDBOX_TIER = none`, it is the *only* defense — run it religiously.** After **every** implementer **and** fixer subagent returns, run:

```bash
git -C <main_checkout> status --short | grep -vE '^\?\?'   # tracked modifications only
```

Any output is a BLOCKER for this phase:
- Diff the stray files (`git -C <main_checkout> diff -- <path>`) to recover intent.
- If the edit belongs in the worktree, re-dispatch the fixer/implementer with an explicit instruction to write to `WORKROOT` (the change is missing from the phase commit until it does).
- Once recovered (or confirmed superseded by the correctly-committed worktree version), discard the stray edits with `git -C <main_checkout> restore -- <path>` so the main checkout returns clean. Never leave the main checkout dirty between phases — a later phase can't tell new thrash from old.

`<main_checkout>` is the repo root the skill was invoked from (NOT `WORKROOT`). When `WORKROOT == <main_checkout>` (`use_worktree = false`), skip this check entirely — your work legitimately lives in that tree.

**Sibling-lane writes — only when the pool has more than one lane.** A reviewer is bound by this too, and is the likeliest agent to trip it: it works in somebody else's `WORKROOT` by design, so "your own lane" for a review turn means *the lane under review*, not one it read last phase. The main checkout is not the only tree an agent can wander into: with a pool provisioned, `<lane-2>/app/models.py` is as reachable from lane 1 as the main checkout is, and a write there is worse than a stray main-checkout write — it lands in a tree another agent is actively editing and testing. The same guard covers both: everything outside the lane's own `WORKROOT` is off-limits.

- **Sandbox** (`SANDBOX_TIER = enforced`): the `--deny` / `--allow` set for a lane denies the **worktree root that holds the pool**, not just the main checkout, and allows only that lane's `WORKROOT` (plus `<main_checkout>/.git` and `<main_checkout>/.vinta-ai-workflows`). One `--deny <pool-root>` covers every sibling.
- **Backstop check** (`SANDBOX_TIER = none`, or as the cheap confirmation when enforced): after every implementer and fixer returns, run the stray-write check against the main checkout **and every sibling lane's workroot**:

  ```bash
  for tree in <main_checkout> <every lane workroot except this lane's>; do
    git -C "$tree" status --short | grep -vE '^\?\?'
  done
  ```

  Output from a sibling lane is a BLOCKER, handled exactly like a stray main-checkout write: diff it, recover the intent into the correct lane, then `git -C <tree> restore --` it away. Do this **before** the sibling's own review reads its diff — otherwise the sibling reviews foreign changes as its own.
7. **Dependency license scan**: `git -C <WORKROOT> diff pyproject.toml uv.lock package.json` (project-relevant manifests) — for every added dep look up its SPDX license (`npm view <pkg> license`, PyPI metadata, repo `LICENSE`). A license in `policies.dependency_licenses.forbidden_spdx` (`GPL-2.0-only`, `GPL-3.0-only`, `AGPL-3.0-only`, `SSPL-1.0`) and not in `allowed_overrides` is a BLOCKER — this project's enforcement mode is `block`. A missing / `UNKNOWN` / undeclared license is **always a BLOCKER** regardless of enforcement mode — there is no override to silently bless undisclosed terms.
8. **No AI co-author trailer**: `git -C <WORKROOT> log -<n>..HEAD --format=%B | grep -i 'Co-Authored-By'` — any AI trailer is a BLOCKER.

## Layer 2 — Plan compliance walkthrough

Open the phase body alongside the diff and walk:

1. **Every numbered "Changes" item implemented.**
2. **Every "Tests" entry materialized**, with assertions actually exercising the called-out behavior.
3. **Acceptance line satisfiable** by the diff.
4. **Repo conventions** from AGENTS.md.
5. **Reusable-skill compliance.**

6. **Feature-flag wiring** if the plan's **Guiding Decisions** declared a flag — flag-OFF is byte-for-byte pre-feature behavior, ≥1 test asserts it.
7. **Cross-phase consistency** with prior tracking summaries.
8. **Comment hygiene** — new or changed comments and doc blocks in the diff read as Simple English, one idea per sentence, no AI-slop vocabulary or negative framing, per the `deslop-comments` skill ([ai-tools/skills/deslop-comments/SKILL.md](ai-tools/skills/deslop-comments/SKILL.md)). Flag any that need rewriting; the fix loop dispatches a fixer to run `deslop-comments` on the phase's touched files.

## Layer 3 — Independent reviewer subagent

After Layers 1–2 pass, hand the diff to a **reviewer from the plan's Crew table** — a member whose role is `reviewer`, which is never a member that writes code — using the project's `reviewer` agent type ([ai-tools/agents/reviewer.md](ai-tools/agents/reviewer.md)) at the model resolved by the [Resolve the reviewer + fixer model](#resolve-the-reviewer--fixer-model) step. Where the roster staffs no reviewer, spawn a **separate** subagent at `agent_models.reviewer` with no implementation context, as before. Read-only by design, either way.

Reviewer prompt template — see the reviewer agent's body for the standard form. Triage findings:
- **BLOCKER**: must fix before the phase is pushed (the conductor's integrate step).
- **SHOULD-FIX**: fix in-phase if cheap; else follow-up issue + tracking note.
- **NIT**: ignore unless trivially cheap.

The reviewer also applies a condensed **structural-simplification lens** — one question: is there an obvious "code-judo" reframe that would make whole branches, helpers, modes, or layers disappear, rather than polishing what's there? Routine phases stop at that one question. When the phase touched core architecture, pushed a file past ~1,000 lines, or the lens surfaces a structural smell too big to resolve inline, escalate to the full [thermo-nuclear-code-quality-review](ai-tools/skills/thermo-nuclear-code-quality-review/SKILL.md) skill against the phase diff — an opt-in deep audit, run deliberately, never on every phase.

The reviewer finds nothing on a >300-LoC multi-file phase → suspicious. Read once more.

## Fix loop

**The implementer fixes its own findings.** The agent that wrote the code still
holds the phase brief, the plan's bounds, the dependency context and its own
reasoning; a fresh fixer holds a quoted finding and has to rediscover the rest —
slower, dearer, and more likely to "fix" the symptom by changing something the
phase deliberately chose. Continuing the implementer is the default, and the
message it gets is a **delta**: the findings and what to do about them, never
the brief again.

1. **Continue the phase's implementer sub-agent** with the findings. Quote each
   one verbatim, say nothing else about the phase — it was told all of that when
   it started, and repeating it invites a re-implementation rather than a fix.
   Do not re-send the plan sections, the dependency summaries, or the phase body.
   For comment-hygiene findings (Layer 2 item 8), tell it to run the
   `deslop-comments` skill ([ai-tools/skills/deslop-comments/SKILL.md](ai-tools/skills/deslop-comments/SKILL.md))
   scoped to the phase's touched files — comment-only edits, no behavior change.
   Whether continued or fresh, the fixing agent re-runs the inner loop + outer
   gate in `<WORKROOT>` before reporting.
2. **Where the runtime cannot continue a finished sub-agent**, spawn a fresh one
   — the project's `fixer` agent type ([ai-tools/agents/fixer.md](ai-tools/agents/fixer.md))
   at the model resolved from `agent_models.fixer` — and give it the phase
   context the implementer would have had, because it has none. Note in the
   phase's tracking record that the fix was a cold hand-off, so a slow phase can
   be read later without guessing.
3. **The last round before you would give up goes to a fresh fixer, always.**
   Reusing the implementer means the agent that wrote the bug is fixing it,
   which is usually the point — it knows why the code is that way — and
   occasionally exactly wrong, because that assumption *was* the bug. When a
   finding has survived the implementer's own attempts, hand it to an agent that
   has not seen the work before escalating a tier or stopping.
4. After the fixer returns, redo Layer 1 in full + the affected portion of Layer 2.
5. Loop until Layers 1, 2, 3 are all clean.

**The reviewer is never the implementer.** Continuing the *reviewer* across
rounds — and across phases — is fine and remembers what it flagged, but the
review itself must come from an agent that did not write the code. An
implementer asked to review its own phase grades its own work from inside its
own reasoning, which is the one thing the layers exist to prevent. On a plan
with a **Crew** table this is structural rather than a rule to follow: reviewers
and implementers are disjoint sets, and no phase can be assigned to a reviewer.

**Which model fixes.** A continued implementer fixes at its own crew member's
tier, because it *is* that member. `agent_models.fixer` therefore governs the
cold cases only — the runtime fallback in step 2 and the escalation in step 3. A
project that set `fixer` to a cheaper tier to save money should know it now
applies to fewer rounds than before.

**The fix does not take the reviewer's tier.** The review runs a tier above the
author deliberately, and it would be easy to carry that tier into the fix on the
grounds that the finding was hard enough to need it. Don't: the review is a
judgement about the code and the fix is a change to it, and the agent best
placed to make that change is still the one that knows why the code is that way.
A finding that genuinely needs a more capable hand is what step 3's escalation is
for.

## Output

Return to the conductor: `PASS` (all three layers clean) with a one-line note, or the list of BLOCKER / SHOULD-FIX findings and what the fix loop applied. The conductor owns branch / push / PR — this skill hands back a clean (or annotated) working tree in `WORKROOT`.
