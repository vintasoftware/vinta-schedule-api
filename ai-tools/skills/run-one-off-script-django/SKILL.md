---
name: run-one-off-script-django
description: Author the Django-specific runner for a one-off operational script in the Vinta Schedule API — either a `BaseCommand` under `<app>/management/commands/<name>.py` (headless / cron / CI invocation) or a Jupyter notebook under `notebooks/<name>/runner.ipynb` (interactive review on production data). Ships the matching Runtime adapter at `scripts/one_off/_runtime_django.py` (`DjangoMgmtRuntime` for management commands, `JupyterRuntime` for notebooks). Sister skill to [add-one-off-script](../add-one-off-script/SKILL.md) — that skill authors the script body (`BaseOneOffScript` subclass), this skill wires the Django-specific runner around it.
---

# Run One-Off Script (Django Runner)

`add-one-off-script` produces a `BaseOneOffScript` subclass: a stateless engine implementing `query`, `process`, `commit`, with the safety contract (dry-run by default, idempotent re-run, batched DB ops, segmented CSV backups, interruption-safe handlers, console + filesystem + S3 logs). This skill wires the **runner** — the artifact you actually invoke — for Django.

Two runner shapes; pick one per script (or both, when ops + interactive review both apply):

- **Django management command** (`<app>/management/commands/<name>.py`) — headless. Invoked via `python manage.py <name> --apply` / `--resume` / `--status` / `--restore`. Required for cron / CI / production-host runs.
- **Jupyter notebook** (`notebooks/<name>/runner.ipynb`) — interactive. Loads Django settings via `django.setup()` in the first cell; runs cell-by-cell. The right fit when an operator wants to inspect intermediate state before committing.

The Runtime adapter (`scripts/one_off/_runtime_django.py`) is **shared across scripts** — write it once per project, reuse for every one-off.

## Decision questions

1. **Which surface?** Cron / CI / production-host one-shot → management command. Interactive review / operator-driven on production data → notebook. Both → ship both, document which is canonical.
2. **Does it need DB writes?** Yes → wire transactions per the safety contract (one transaction per batch, never one transaction for the whole run). No → simpler — reads + reports only.
3. **Does it cross tenant boundaries?** If yes — pause and re-confirm with the user. Tenant-spanning one-offs are rare in this project; usually iterate per-organization explicitly.
4. **Does the operator need to abort mid-run?** Yes (default for any destructive script) → `BaseOneOffScript`'s signal handlers must work in the chosen runner. Notebooks intercept kernel-interrupt; mgmt commands intercept SIGTERM / SIGINT.

## Checklist

### 0. Stage the Runtime adapter once per project

If `scripts/one_off/_runtime_django.py` doesn't exist yet, create it. It exposes one or both of:

- `DjangoMgmtRuntime(BaseCommand)` — hooks `BaseCommand.handle()` to `BaseOneOffScript.execute()`. Respects `--apply` / `--resume` / `--status` / `--restore` flags. Loads Django settings + DB connection before iteration starts.
- `JupyterRuntime` — swaps signal handlers for kernel-interrupt; skips the PID-file lease (notebook = single instance by construction); calls `django.setup()` on first import.

Reference structure (adapt to project layout):

```python
# scripts/one_off/_runtime_django.py
from __future__ import annotations

import signal
import sys
from typing import TYPE_CHECKING

import django
from django.core.management.base import BaseCommand

if TYPE_CHECKING:
    from scripts.one_off._base import BaseOneOffScript


class DjangoMgmtRuntime(BaseCommand):
    """
    Adapter from Django's BaseCommand surface to BaseOneOffScript.

    Subclass per script:
        class Command(DjangoMgmtRuntime):
            script_cls = BackfillTenantFlag
    """

    script_cls: type["BaseOneOffScript"]

    help = "One-off operational script. See script_cls for what it does."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run).")
        parser.add_argument("--resume", action="store_true", help="Resume from last processed item.")
        parser.add_argument("--status", action="store_true", help="Print last run status without executing.")
        parser.add_argument("--restore", type=str, default=None, help="Restore from CSV backup at the given path.")
        parser.add_argument("--batch-size", type=int, default=None, help="Override batch size for this run.")

    def handle(self, *args, **options):
        script = self.script_cls(
            apply=options["apply"],
            resume=options["resume"],
            batch_size=options.get("batch_size"),
        )

        # Signal handlers — graceful shutdown on Ctrl+C / SIGTERM
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: script.request_stop())

        if options["status"]:
            script.print_status()
            return
        if options["restore"]:
            script.restore_from_csv(options["restore"])
            return

        script.execute()


class JupyterRuntime:
    """
    Notebook-side adapter. Use:

        from scripts.one_off._runtime_django import JupyterRuntime
        runner = JupyterRuntime(BackfillTenantFlag, apply=False)
        runner.execute()
    """

    def __init__(self, script_cls: type["BaseOneOffScript"], *, apply: bool = False, **kwargs):
        if not django.apps.apps.ready:
            django.setup()
        self.script = script_cls(apply=apply, **kwargs)
        # Notebooks rely on kernel-interrupt rather than POSIX signals;
        # BaseOneOffScript's stop flag is exposed via .request_stop() and the engine polls it.

    def execute(self):
        self.script.execute()

    def status(self):
        self.script.print_status()
```

The above is a sketch — match the `BaseOneOffScript` API actually shipped at `scripts/one_off/_base.py`. Read it first.

Add a `__init__.py` next to the adapter so it's importable as a module: `scripts/one_off/__init__.py` (empty), `scripts/one_off/_runtime_django.py` (the adapter above).

### 1. Author the script folder via `add-one-off-script`

Run `add-one-off-script` first — that's where the actual `BaseOneOffScript` subclass and the per-script README live. This skill assumes the folder structure already exists at `scripts/one_off/<YYYY-MM-DD>-<name>/`.

### 2. Wire the management command (when ops / CI / cron is in scope)

For script `2026-06-03-backfill-tenant-flag`:

1. Pick the owning app for the management command. Usually the app that owns the target model.
2. Create `<app>/management/__init__.py` + `<app>/management/commands/__init__.py` if they don't exist.
3. Create `<app>/management/commands/backfill_tenant_flag.py`:
   ```python
   from scripts.one_off._runtime_django import DjangoMgmtRuntime
   from scripts.one_off.backfill_tenant_flag_20260603.script import BackfillTenantFlag


   class Command(DjangoMgmtRuntime):
       script_cls = BackfillTenantFlag
   ```
4. Test:
   ```bash
   uv run python manage.py backfill_tenant_flag --status
   uv run python manage.py backfill_tenant_flag             # dry-run
   uv run python manage.py backfill_tenant_flag --apply     # commit (use carefully)
   ```

### 3. Wire the Jupyter notebook (when interactive review is in scope)

1. Create `notebooks/<name>/runner.ipynb` (one folder per script — keeps notebooks + outputs grouped).
2. First cell:
   ```python
   import os, sys

   sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "..", "..")))
   os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vinta_schedule_api.settings.local")

   import django; django.setup()

   from scripts.one_off._runtime_django import JupyterRuntime
   from scripts.one_off.<script_module> import <ScriptClass>

   runner = JupyterRuntime(<ScriptClass>, apply=False)
   ```
3. Subsequent cells exercise `runner.script.query(...)`, `runner.script.process(...)`, `runner.execute()`, `runner.status()`.
4. Pin notebook outputs **only after a dry-run**. Real execution = clear outputs before commit so the notebook stays diff-readable.

### 4. Update the per-script README

`add-one-off-script` writes `scripts/one_off/<name>/README.md`. Append a **Run** section listing the three legitimate entry points so the next operator doesn't have to guess:

```markdown
## Run

- Dry-run via mgmt command (recommended first pass):
  `uv run python manage.py backfill_tenant_flag`
- Apply via mgmt command:
  `uv run python manage.py backfill_tenant_flag --apply`
- Status only:
  `uv run python manage.py backfill_tenant_flag --status`
- Restore from a CSV backup:
  `uv run python manage.py backfill_tenant_flag --restore <path>`
- Interactive review (notebook):
  `jupyter lab notebooks/2026-06-03-backfill-tenant-flag/runner.ipynb`
- Headless shell-stdin variant (last resort, no resume support):
  `uv run python manage.py shell < scripts/one_off/2026-06-03-backfill-tenant-flag/script.py`
```

## Pitfalls

- **Defining a `Command` that wraps `BaseOneOffScript.execute()` directly instead of going through `DjangoMgmtRuntime`.** Bypasses signal handler wiring + flag parsing + status / restore subcommands. Use the adapter.
- **Notebook that doesn't call `django.setup()` before importing models.** First model import raises `AppRegistryNotReady`. Always include the bootstrap cell.
- **Committed notebook outputs from a `--apply` run.** Production data leaks into git. Clear outputs before commit (`jupyter nbconvert --clear-output --inplace` or VS Code's "Clear All Outputs" + commit).
- **`--apply` path that triggers without `--apply` because flag parsing is buggy.** Default must be dry-run; `--apply` is the only way to commit. The adapter shows the canonical wiring — don't reinvent.
- **Skipping the `--status` / `--restore` paths.** Operators need them when a run dies mid-way. The adapter wires them for free.
- **Adding the mgmt command to an app that doesn't own the data.** Code review confusion later. Match the command to the model's owning app.
- **Running `--apply` against `vinta_schedule_api.settings.production` from the host.** Production DB credentials live behind the deploy environment. Run inside the production execution environment, never from a developer laptop.

## Verification

Run the [outer gate](../../AGENTS.md#outer-gate) — must pass. Skill-specific extras:

```bash
# Adapter loads
uv run python -c "from scripts.one_off._runtime_django import DjangoMgmtRuntime, JupyterRuntime; print('ok')"

# Mgmt command registers + responds to --help
uv run python manage.py <command_name> --help

# Dry-run executes
uv run python manage.py <command_name>

# Status + restore subcommands defined
uv run python manage.py <command_name> --status
```

Spot-checks:
- [ ] `scripts/one_off/_runtime_django.py` exists, matches the `BaseOneOffScript` API.
- [ ] Mgmt command file is a 3-line subclass of `DjangoMgmtRuntime` (no duplicated CLI parsing).
- [ ] Notebook bootstrap cell calls `django.setup()` before model imports.
- [ ] Notebook outputs cleared.
- [ ] Per-script README's **Run** section lists the four entry points (dry-run / apply / status / restore / notebook).
- [ ] Default is dry-run; `--apply` is required to commit.
- [ ] Tested against a non-production DB first.
