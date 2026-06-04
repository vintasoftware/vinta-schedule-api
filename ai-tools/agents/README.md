# Sub-agents (canonical sources)

Each `<name>.yaml` here is the **single source of truth** for one sub-agent. The setup
script (`ai-tools/scripts/setup-ai-tools.mjs`) reads these and emits per-vendor copies:
Claude markdown under `.claude/agents/`, Cursor markdown under `.cursor/agents/`,
Copilot `.agent.md` files under `.github/agents/`, Codex TOML under `.codex/agents/`.

Do **not** edit the per-vendor files. Edits get overwritten on the next setup run.

## Schema

Full schema: [`vinta-ai-workflows/schemas/sub-agent.v1.schema.json`](../../node_modules/vinta-ai-workflows/schemas/sub-agent.v1.schema.json).

Required fields: `schema_version`, `name`, `description`, `access`, `body`.

```yaml
# yaml-language-server: $schema=./node_modules/vinta-ai-workflows/schemas/sub-agent.v1.schema.json
schema_version: 1
name: <kebab-case>            # matches filename stem; also the slash-command name
description: |                # one-line role + when-to-use
  ...
access: read-only | read-write   # drives vendor defaults (Claude tools:, Cursor readonly:, Codex sandbox_mode, Copilot tools[])
claude-tools: <comma list>    # optional convenience; overrides Claude default derived from access
model: <string>               # optional default model preference
is_background: true           # Cursor-only — agent runs as background task
overrides:                    # optional per-vendor overrides
  claude:   { tools: "<csv>", model: <string> }
  cursor:   { model, readonly, is_background }
  copilot:  { tools: [<id>, ...], model, user-invocable, disable-model-invocation }
  codex:    { sandbox_mode: read-only|workspace-write|workspace-network, model, model_reasoning_effort: low|medium|high }
body: |                       # markdown body — becomes the body of every per-vendor file
  ...
```

## Current agents

- **implementer** — read-write. Default coder for one phase of an `ai-plans/` plan.
- **reviewer** — read-only. Adversarial reviewer; outputs BLOCKER / SHOULD-FIX / NIT.
- **fixer** — read-write. Applies one reviewer finding or fixes one named failure.
- **migration-author** — read-write. Django + raw-SQL migration specialist.

## Adding an agent

1. Create `ai-tools/agents/<new-name>.yaml`.
2. Run `npm run setup:ai-tools` (alias for `node ai-tools/scripts/setup-ai-tools.mjs`).
3. Commit both the YAML and the generated vendor files.
