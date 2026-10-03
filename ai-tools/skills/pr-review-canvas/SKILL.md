---
name: pr-review-canvas
description: Generate a review canvas for a GitHub pull request or GitLab merge
  request, for the work in this clone before a pull request exists, or for two
  refs, with the pr-review tool. Runs `pr-review prepare`, writes the layered
  model.json the prompt asks for, and runs `pr-review publish` to validate it
  and, unless the config turns sharing off, share it as a compressed PR/MR
  comment. Use when the user runs `/pr-review-canvas <pr-number>`,
  `/pr-review-canvas branch`, `/pr-review-canvas uncommitted`,
  `/pr-review-canvas --base <ref> --head <ref>`, or asks for a review canvas for
  a PR or MR, for their branch, or for what they have not committed.
metadata:
  body-sha256: 44e896ff27323b345111da5cb2d4932363c05bf695adf8787a50c14aa3e3bff4
---

# pr-review-canvas

You produce one JSON file that groups a pull request's diff into semantic layers with attention
points, and hand it to the `pr-review` CLI. The CLI does the deterministic work (fetching, diffs,
validation, storage); you do the reading and the writing of `model.json`. Nothing here checks out
a branch or writes outside the canvas directory.

When the target already has a canvas for a commit this head was built on, prepare renders a
different prompt: it names that canvas, lists which files the new commits left untouched, and tells
you what to carry from it word for word and what to decide again. Follow that prompt as written;
the two lists are computed, not suggestions.

Arguments: `<pr-number> [--force]`, `branch [--base <ref>] [--force]`,
`uncommitted [--base <ref>] [--force]`, or `--base <ref> --head <ref> [--force]`. `--force`
regenerates a canvas that already exists for the head commit: prepare removes the old `model.json` and any other leftovers from the canvas
directory, keeping `derived/`, `publish.log` (the attempts history), and the published
`review.json` + `manifest.json` (the page keeps showing the old canvas until your publish replaces
it), so you start a fresh `model.json`. Run every `pr-review` command from the repository root.

## Flow

### Model choice

The project picks the default model. The AI Chat settings (`chatAgent`, `chatModel`, and
`serve --chat-agent/--chat-model`) are for the chat pane and never pick your model. Prepare prints
the project's `generation.models` as
`models`, keyed by agent id (the `--agent` you publish with): `{ "claude": "opus" }`. That entry is
the default, so Claude generates with Opus unless the project names another model. Generate with
the model under your own agent id. When `models` has no entry for your agent, keep the model you
run on. If the prepared diff changes authentication, access policy, or protected health
information (PHI) handling, use a more capable model, such as Opus, for the generation and
validation steps when available, unless the project's model is already that capable. Honor an
explicit user model choice over all of these.

When the model to use is not the one you run on, delegate generation and validation to a subagent
on that model: pass it the prepared prompt and context paths; it writes the same model file. If the
host cannot select models, or does not offer the named one, keep its selected model and tell the
user. Record the model that actually generated the canvas when publishing.

### 1. Prepare

```bash
pr-review prepare --pr <n> [--force]
# or, before a pull request exists, one of the two reviews of this clone:
pr-review prepare --branch [--base <ref>] [--force]
pr-review prepare --uncommitted [--base <ref>] [--force]
# or, for any two refs:
pr-review prepare --base <ref> --head <ref> [--force]
```

The skill word maps to the flag: `/pr-review-canvas branch` runs `prepare --branch`, and
`/pr-review-canvas uncommitted` runs `prepare --uncommitted`.

Both compare against the default branch, which prepare reads from `origin/HEAD` unless `--base`
names another one. They differ in what the head holds:

- `--branch` is the tip of the current branch. Whatever is in the working tree is left out.
- `--uncommitted` is the working tree itself: prepare snapshots the edits and the untracked files
  into a commit of its own, without touching anything the user has staged. With a clean tree it is
  the branch tip, and the canvas is the same one `--branch` would build.

They are separate reviews with separate pages, progress and chat threads, so preparing one leaves
the other alone. Pick the one the user asked for; when they only say "review my work", ask which,
unless the words already decide it ("before I commit" is `uncommitted`, "before I open the PR" with
everything committed is `branch`).

The JSON carries an extra `local` object with the review's name, the base that was resolved, the
branch the head is on, and whether uncommitted work is in it. Tell the user all four: a review of
the wrong base, or of a tree that has moved, is worth catching early.

Progress goes to stderr. The last stdout line is JSON:

```json
{
    "canvasDir": "...",
    "headSha": "...",
    "mergeBaseSha": "...",
    "promptPath": "...",
    "contextPath": "...",
    "models": { "claude": "opus" },
    "sharing": "shared",
    "status": "prepared"
}
```

`sharing` is `"shared"` when publish will attempt a PR/MR canvas comment, or `"off"` otherwise
(including refs and local runs). Tell the user before publishing when sharing is enabled.
`--data-dir` and `PR_REVIEW_DATA_DIR` change storage only: test runs still post to the live PR
unless project `sharing.canvasComment: false` or personal `canvasComment: false` turns sharing off.

- `status: "exists"` means a canvas already exists for this head. Stop and tell the user:
  "canvas already exists for <headSha>; run with --force to regenerate".
- A line of the form `{ "error": { "code", "message", "hint" } }` means prepare failed. Report the
  code, message, and hint verbatim and stop. `pr-review doctor` names which of git, origin,
  `gh` or `glab`, the data dir, and the skill install is missing.

### 2. Read the task

Read `promptPath` in full: it holds the pull request, the manifest with every hunk id, the diffs
(inline or by file path), the layering and length rules, the rulebook, and the JSON schema. On an
incremental run it also names the basis canvas and a `basis-model.json` containing only model fields: read that file for the exact
wording of everything it tells you to carry. Read
`contextPath` when you need the paths of the head files, the base files, or the patches. Read any
untouched file with `git show <headSha>:<path>` from the repository root, using the SHA returned
by prepare. The working tree may be on another branch. Do not check anything out.

### 3. Write model.json

Write `<canvasDir>/model.json` matching the schema in the prompt. Write JSON only; no prose in the
file, no comments, no markdown fence.

Use the host's file-writing tool (such as Write) for `model.json` in the canvas directory. This
instruction takes precedence over auto-mode harness advice to write through Bash: shell heredocs
into a data directory may be refused by write guards.

If your file-writing tool cannot write to `canvasDir`, for example because you are isolated to a
worktree, do not copy the file in with the shell. Run prepare again with
`--data-dir <a directory you can write>/.pr-review` and use the `canvasDir` it prints: validate
and publish find that data dir from it. Tell the user that `pr-review serve` shows this canvas
only when started with that same `--data-dir`.

### 4. Check before publishing

```bash
pr-review validate <canvasDir>/model.json --canvas <canvasDir> --human --fix
```

Same checks the publish step runs. It prints `ok: model.json passes against <n> files`, or one
line per problem in the same form publish uses. Fix what it names and run it again until it says ok.

`--fix` first shortens the titles that are over their cap, by dropping the explainer after the
first `:` or `—`, and writes the file back. Each one is reported as
`fixed <where>: "<before>" -> "<after>"`; read them, since the shortened title is what publishes.
A title with nothing to drop is left alone for you to rewrite. Prose is never cut for you: an
over-cap rationale, note, or body reports where the cap falls in your own words
(`what fits ends at "..."`), and the rewrite is yours.

`--fix` also repairs three fold errors that have one right answer, and reports each as
`fixed <where>: fold "<title>" <before> -> <after>, <why>` or
`fixed <where>: dropped fold "<title>" at <range>: <why>`:

- a fold that runs past its chunk is cut back to the assigned chunk its first line is in; one that
  starts in no chunk assigned to that file in that layer is dropped, never moved;
- a fold that repeats an earlier fold's exact range is dropped;
- a fold over an attention point is shrunk so the point stays outside it, or dropped when no single
  range of at least three lines around the point is left. The report names generated missing-test
  points by their behavior.

Read these lines: a dropped fold may have been hiding a range you still want hidden, and the
coverage errors (`FOLD_MISSING`) that follow are yours to answer. Folds that partly overlap, a
reversed range, and every rule about how much to hide are left for you to fix.

Run this before every publish, including after a repair. A publish round-trip costs more than this
command, and length caps are the usual reason a publish is rejected: they are measured on the text
a reader sees, which you cannot count reliably while writing.

### 5. Publish

```bash
pr-review publish <canvasDir> --agent <your agent id> --model <model id if you know it> --harness <claude-code|codex|other>
```

- `--agent`: a free-text id of the agent product you are: `claude`, `codex`, `gemini`, ...
- `--model`: the model id when you know it (`claude-opus-5-5`, `gpt-6-astra`, ...); omit it otherwise.
- `--harness`: `claude-code` when you run inside Claude Code, `codex` inside Codex, `other`
  anywhere else.

On success the last line is `{ "status": "published", "sharing", "headSha", "reviewJsonPath",
"attempts", "reviewUrl" }` (`reviewUrl` is absent only for a `--base/--head` run; a local run
points at `/review/branch` or `/review/uncommitted`).
For PR/MR runs, publish creates or updates your canvas comment using the host CLI login, unless
`sharing.canvasComment` is off in `pr-review.config.yml` or `canvasComment: false` is set in
`.pr-review/settings.yml` (the personal file wins). Publish reads the config itself; do not post
the canvas any other way. Always inspect `sharing.status`: local validation success does not mean
remote sharing succeeded.

For a local run there is nothing to share: `sharing.status` is `"local"`. Report `reviewUrl` and
tell the user to start `pr-review serve` to read the canvas.

On failure the command prints one line per problem, then an error line, and exits 5:

```
HUNK_UNASSIGNED packages_x_ts#3 in packages/x.ts (@@ -40,7 +41,9 @@) is in no layer
TEXT_TOO_LONG layers.0.rationale: 412 visible chars, cap 300
{"error":{"code":"MODEL_INVALID","message":"model.json has 2 problems","hint":"fix model.json and run publish again"}}
```

Fix exactly the named problems in `model.json` and run publish again. Give up after the number of
failed rounds the prompt states (`maxRepairRounds`, 3 by default) and report the last output
verbatim. Do not weaken the content to pass: shorten text, move hunks, fix links.

If publish prints `CANVAS_STALE`, the PR head, branch, or working tree moved while you worked. Tell the user and offer to run
prepare again; pass `--allow-stale` only when the user asks for the canvas of the old commit.

### 6. Report the sharing result

For a PR/MR run, report the local `reviewUrl` (start it with `pr-review serve`) and inspect `sharing`:

- `status: "shared"`: link to `sharing.url` and say the canvas was shared automatically.
- `status: "off"`: the config keeps canvases local, so nothing was posted. Say so, give the local
  `reviewUrl`, and do not suggest uploading the canvas to the PR/MR or offer to share it.
- `status: "failed"`: clearly warn that automatic sharing failed, quote `sharing.warning`, and
  give the absolute `sharing.zipPath`. Tell the user to open the PR/MR, edit its description,
  drag the ZIP into the editor, wait for upload to finish, and save. Replace any older canvas
  attachment link. Include these instructions in your final response; the local canvas is ready,
  but reviewers still need the upload. Do not regenerate the model to repair a sharing failure.

For a local run, `sharing.status` is `"local"` and there is nothing to share. Give the user
`reviewUrl` (`http://localhost:<port>/review/branch` or `.../review/uncommitted`) and tell them to
start `pr-review serve` if it is not running. Say which base was compared and whether uncommitted
work was included, both from the `local` object prepare printed.

For a `--base/--head` run, `sharing.status` is `"local"` too, but the canvas has no page of its
own. Report the stored commit and export it:

```bash
pr-review export --head <headSha> --json
```

Export prints `{ "status": "exported", "path", "name", "headSha", "prNumber" }`; `prNumber`
is absent for a refs-only export. Give the returned absolute `path`. Re-exporting the same canvas
uses the same name and replaces an existing ZIP at that path. Once a PR exists, `pr-review export --head <headSha> --pr <n> --json`
stamps its number for manual upload, or rerun this skill for the PR number with `--force` to share
automatically. A canvas of a working-tree snapshot cannot be carried to a pull request this way:
its commit is on no branch, so generate a fresh one for the PR.

### 7. Hand over the self-review

The canvas is ready for its author before it is ready for reviewers. End your report of a PR/MR or
local run by asking the user to self-review before requesting review: start `pr-review serve`,
open `reviewUrl`, and resolve each attention point marked **yours** with a one-line reason.
Resolving updates the canvas comment for a PR/MR run, so reviewers see only what is left; when
`sharing.status` was `"off"`, the resolutions stay in the local canvas. Say how many points
are for the author and how many for the reviewer, counting generated missing-test points too.
Read the published `review.json` for these counts. For refs runs, report the same counts with the
ZIP path; self-review in the page follows once the canvas belongs to a PR. Leave resolution to the
author: the reason is theirs to give.

## Rules the validator enforces (and models tend to break)

- Every hunk id from the manifest appears in exactly one layer. Check the manifest against your
  layers before you publish; a missed hunk is the most common failure.
- At most one layer with `kind: "other"`, last when present, and omitted when there are no
  mechanical hunks. It carries no risk tag. A test file may sit in Other only when the code it covers
  is in Other too.
- A small change set (the prompt states the hunk limit) gets one real layer unless concerns truly differ; optional Other does not count.
- Test files come after the files they cover, inside the same layer, never in a layer of their own.
  The prompt's layering rules name the path patterns this project counts as tests; they are the
  ones the validator uses.
- Every text is within its cap, measured on the text a reader sees (link targets and backticks do
  not count). Rationales, notes, and annotations are one or two short sentences.
- At most 12 attention points, counting one per `missing` test entry.
- Every attention point names its `audience`: `author` when the author can answer it alone,
  `reviewer` when it needs someone else's judgment. When in doubt, `reviewer`.
- A `missing` test entry may set `audience` (default `reviewer`) and an `anchor` with `path`,
  `line`, and `side` (`new` or `old`) inside one of its layer's chunks. Without an anchor, publish
  chooses the first changed row of the source matched by `testPath`, or the layer's first changed
  row. Optional `title` uses the point-title cap; otherwise the title is shortened at a word
  boundary. Carried entries retain their published title, audience, and anchor. Follow the
  printed carried coordinates when the anchor moves, so author resolutions keep their identity.
- A test of code outside the diff goes at the end of the most relevant real layer.
- Folds on a collapsed file take effect when the reader opens that file. Folding thresholds count
  diff rows in assigned chunks, including context, using the longer side of each chunk.
- `covered` test entries name a `testPath` that exists at the PR head (changed or not).
- Annotations and attention points sit on lines inside a hunk, on the side you name.
- Links use only the four forms `#layer:`, `#file:`, `#hunk:`, `#line:` and must resolve.
- At most one diagram per layer (its `diagram` field plus a ```mermaid fence in its rationale)
  and one in the summary; a fence in any other field stays a code block. Draw only when relations
  beat prose and most canvases need zero to two diagrams in total, keep labels short, and write no
  `click` directives, HTML labels, `%%{init}%%` blocks, or `---` front matter.
- `diagram.links` maps a node id of the source to a canvas link, at most 12 per diagram. Spell the
  node id the way the source spells it (`store`, not the label in its brackets; `App`, not the
  name after `as`), and link only nodes that stand for a layer, a file, or a hunk of this canvas.
- Markdown is allowed; headings are not. No prose outside the JSON file.

## Updating a shared canvas

After new commits, run this skill again for the PR number. The run updates the canvas of the
nearest earlier commit instead of writing one from nothing, and the reviewer's progress on the
untouched files follows it. Add `--force` to regenerate a canvas for the same commit, or to start
over from a blank page. Publish updates your canvas comment, unless sharing is off; follow the
sharing-result instructions above. Reviewers click **refresh** to load it. A canvas for a different
PR head shows **Canvas is outdated**; an older canvas remains readable with posting disabled.
