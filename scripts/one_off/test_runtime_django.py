"""Tests for the shared Django runner surface used by every one-off script."""

import datetime
from pathlib import Path

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.test import override_settings

import pytest

from scripts.one_off._base import ScriptConfig
from scripts.one_off._runtime_django import DjangoMgmtRuntime, load_script_module


@pytest.fixture
def command() -> BaseCommand:
    return BaseCommand()


@pytest.fixture
def runtime(tmp_path: Path, command: BaseCommand) -> DjangoMgmtRuntime:
    config = ScriptConfig(name="test-runtime-django", log_dir=tmp_path / "runs")
    return DjangoMgmtRuntime(config, command=command)


def test_stdout_is_not_double_written(runtime: DjangoMgmtRuntime):
    """The command owns stdout, so LocalRuntime's own stream handler must be gone."""
    import logging

    stream_handlers = [
        handler
        for handler in runtime._logger.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]
    assert stream_handlers == []


def test_log_lines_still_reach_the_run_log(runtime: DjangoMgmtRuntime):
    runtime.log("INFO", "hello from the runner")
    runtime.fsync_log()

    assert "hello from the runner" in (runtime.run_dir / "run.log").read_text()


@pytest.mark.django_db
def test_artifacts_upload_to_django_default_storage(runtime: DjangoMgmtRuntime, tmp_path: Path):
    """Artifacts land in the configured storage backend, not a bespoke S3 client."""
    (runtime.run_dir / "run.log").write_text("log body")
    (runtime.run_dir / "some_table.001.csv").write_text("id\n1\n")

    with override_settings(MEDIA_ROOT=str(tmp_path / "media")):
        runtime.upload_run_artifacts()

        prefix = runtime.storage_run_prefix
        assert default_storage.exists(f"{prefix}/run.log")
        assert default_storage.exists(f"{prefix}/some_table.001.csv")
        with default_storage.open(f"{prefix}/run.log") as handle:
            assert handle.read() == b"log body"


@pytest.mark.django_db
def test_two_runs_do_not_overwrite_each_others_artifacts(tmp_path: Path, command: BaseCommand):
    """Remote keys are per-run, so a re-run cannot destroy the first run's evidence."""
    config = ScriptConfig(name="test-runtime-django", log_dir=tmp_path / "runs")
    first = DjangoMgmtRuntime(config, command=command)
    second = DjangoMgmtRuntime(config, command=command)

    assert first.storage_run_prefix != second.storage_run_prefix


def test_upload_failure_is_swallowed(runtime: DjangoMgmtRuntime, monkeypatch):
    """A dead sink must never take the run down -- the on-disk copy is authoritative."""

    def boom(*_args, **_kwargs):
        raise OSError("bucket unreachable")

    monkeypatch.setattr(default_storage, "save", boom)
    (runtime.run_dir / "run.log").write_text("log body")

    runtime.upload_run_artifacts()  # must not raise

    assert "upload FAILED" in (runtime.run_dir / "run.log").read_text()


def test_load_script_module_reads_a_dated_folder():
    module = load_script_module("2026-08-05-repair-untruncated-recurring-parents")

    assert hasattr(module, "RepairUntruncatedRecurringParents")
    assert isinstance(module.build_config(), ScriptConfig)
    # Cached: a second call must not re-execute the module.
    assert load_script_module("2026-08-05-repair-untruncated-recurring-parents") is module


def test_load_script_module_rejects_a_missing_folder():
    with pytest.raises(ImportError):
        load_script_module(f"{datetime.date.today().isoformat()}-does-not-exist")
