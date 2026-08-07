"""Django runner surface for one-off scripts.

Two pieces, shared by every one-off script in the project:

- :class:`DjangoMgmtRuntime` -- the ``Runtime`` adapter. A management command runs
  in an ordinary process, so the lease, the stop handlers, and the on-disk run dir
  all work exactly as in ``LocalRuntime``; the only thing that changes is that the
  command owns stdout, so log lines route through it instead of a second handler.
- :class:`DjangoMgmtCommand` -- the ``BaseCommand`` base. Parses the flags the
  safety contract requires (``--apply`` / ``--resume`` / ``--status`` /
  ``--restore``) and drives the engine, so a per-script command is three lines.

Per-script command::

    from scripts.one_off._runtime_django import DjangoMgmtCommand
    from <loaded script module> import MyScript, build_config


    class Command(DjangoMgmtCommand):
        script_cls = MyScript
        config_builder = staticmethod(build_config)
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError, CommandParser

from scripts.one_off._base import LocalRuntime, ScriptConfig, _print_status


if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from scripts.one_off._base import BaseOneOffScript


def load_script_module(folder: str) -> ModuleType:
    """Load ``script.py`` out of a dated one-off folder.

    One-off folders are named ``<YYYY-MM-DD>-<kebab>``, which is not a valid Python
    identifier, so the module cannot be reached by a normal import and has to be
    loaded by path.
    """
    module_name = "one_off_" + folder.replace("-", "_")
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached

    path = Path(__file__).resolve().parent / folder / "script.py"
    if not path.is_file():
        # `spec_from_file_location` happily builds a spec for a path that does not
        # exist and only fails on exec, several frames deep. Name the folder here.
        raise ImportError(f"no one-off script at {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load one-off script at {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses in the script resolve their annotations
    # through ``sys.modules[cls.__module__]``, which is None for an unregistered
    # module.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DjangoMgmtRuntime(LocalRuntime):
    """``LocalRuntime`` with Django's storage backend and command-owned output.

    Two things change; everything else is inherited deliberately. A management
    command is a plain process, so the PID-file lease still guards against a
    concurrent run and SIGINT / SIGTERM still reach the engine's stop handler --
    re-implementing either would only create a second thing to keep correct.

    - **Artifacts go to Django's default storage**, not the template's own boto3
      client. The project already configures media storage per environment
      (``MediaStorage`` over S3, Floci in dev, ``FileSystemStorage`` under test),
      so uploads need no ``ONE_OFF_S3_*`` env vars and behave correctly wherever
      the command runs.
    - **Log lines route through the command**, so styling, ``--no-color``,
      verbosity, and test capture work.
    """

    #: Key prefix inside the default storage backend.
    storage_prefix: ClassVar[str] = "one-off-runs"

    def __init__(self, config: ScriptConfig, command: BaseCommand, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self._command = command
        # Remote artifacts are keyed by run so a second run never overwrites the
        # evidence of the first. The local run dir does reuse names -- the CSV
        # chunk writer opens with "w" -- so the uploaded copy is the one that
        # keeps per-run history.
        self._run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        # ``LocalRuntime`` attaches its own stdout StreamHandler. The command owns
        # stdout here -- styling, ``--no-color``, verbosity, and test capture all
        # go through ``self.stdout`` -- so drop the duplicate and keep the file
        # handler, which is the durable copy.
        for handler in list(self._logger.handlers):
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                self._logger.removeHandler(handler)

    def log(self, level: str, message: str) -> None:
        super().log(level, message)
        normalized = level.upper()
        if normalized == "ERROR":
            self._command.stderr.write(self._command.style.ERROR(message))
            return
        if normalized in ("WARN", "WARNING"):
            self._command.stdout.write(self._command.style.WARNING(message))
            return
        self._command.stdout.write(message)

    @property
    def storage_run_prefix(self) -> str:
        return f"{self.storage_prefix}/{self.config.name}/{self._run_id}"

    def upload_run_artifacts(self) -> None:
        """Copy the run dir into Django's default storage. Best-effort by contract.

        A failure here is logged and swallowed: the filesystem copy under
        ``run_dir`` is authoritative, and losing the run because the bucket was
        unreachable would be a worse outcome than losing the remote copy.
        """
        prefix = self.storage_run_prefix
        uploaded = 0
        try:
            for path in self.list_run_artifacts():
                with path.open("rb") as handle:
                    default_storage.save(f"{prefix}/{path.name}", File(handle))
                uploaded += 1
        except Exception as exc:  # noqa: BLE001 — never let the sink take down the run
            self.log(
                "ERROR",
                f"storage: upload FAILED after {uploaded} file(s) — the copy at "
                f"{self.run_dir} is authoritative: {exc!r}",
            )
            return
        # `default_storage` is a lazy proxy; `.__class__` is proxied through to the
        # configured backend, while `type()` would only ever report the wrapper.
        backend = default_storage.__class__.__name__
        self.log("INFO", f"storage: uploaded {uploaded} file(s) to {backend}:{prefix}/")


class DjangoMgmtCommand(BaseCommand):
    """Base command that drives a ``BaseOneOffScript`` subclass.

    Subclasses set ``script_cls`` and ``config_builder`` and nothing else. Keeping
    the flag parsing here is what guarantees every one-off shares the same safety
    surface: dry-run unless ``--apply``, plus the ``--status`` and ``--restore``
    paths an operator needs when a run dies mid-way.
    """

    script_cls: ClassVar[type[BaseOneOffScript]]
    config_builder: ClassVar[Callable[[], ScriptConfig]]

    help = "Run a one-off operational script. Dry-run unless --apply is given."  # noqa: A003

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this the run is a dry-run and touches nothing.",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Skip items recorded in the processed log by a previous run.",
        )
        parser.add_argument(
            "--status",
            action="store_true",
            help="Print lease, processed count, and log tail without executing.",
        )
        parser.add_argument(
            "--restore",
            metavar="BACKUP_DIR",
            default=None,
            help="Restore rows from the CSV backups in BACKUP_DIR.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Override the script's default batch size for this run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        config = type(self).config_builder()
        if options.get("batch_size"):
            config = replace(config, batch_size=options["batch_size"])

        if options["status"]:
            # Read-only: deliberately built before any runtime, so `--status` never
            # takes the lease of the run it is reporting on.
            _print_status(config)
            return

        runtime = DjangoMgmtRuntime(config, command=self)
        script = self.script_cls(
            config=config,
            runtime=runtime,
            dry_run=not options["apply"],
            resume=options["resume"],
        )

        if options["restore"]:
            script.restore_from_backup(Path(options["restore"]))
            return

        exit_code = script.execute()
        if exit_code != 0:
            raise CommandError(
                f"{config.name}: one or more items failed -- see {runtime.run_dir / 'run.log'}. "
                "Re-run with --resume once the cause is fixed."
            )
