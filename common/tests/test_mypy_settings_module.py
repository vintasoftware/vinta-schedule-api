"""The settings module mypy's django-stubs plugin loads must ship in the repo.

``mypy`` cannot start without this. The django-stubs plugin builds a ``DjangoContext``
while mypy is still *loading plugins*, and that calls ``settings._setup()`` on whatever
``[tool.django-stubs] django_settings_module`` names. If the import fails, mypy does not
report a type error -- it dies with ``INTERNAL ERROR -- Error constructing plugin
instance of NewSemanalDjangoPlugin`` before analysing a single file.

That is what a stale value here costs: the setting pointed at
``vinta_schedule_api.settings.local``, which `.gitignore` excludes and which only exists
on a machine that copied ``local.py.example`` by hand. So `uv run mypy .` -- one of the
six commands in AGENTS.md's outer gate -- aborted on every fresh clone and for every
agent, while passing for the developers who happened to have the file.

Importability alone would not catch a regression: on a developer machine
``settings.local`` imports fine. The tracked-by-git assertion is the one that holds
everywhere, and it is the actual invariant -- the module has to be one every checkout
gets, not one the local-setup step creates.

mypy runs in neither CI nor pre-commit, so this test is the only automated gate on that.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def configured_settings_module() -> str:
    with PYPROJECT.open("rb") as fh:
        pyproject = tomllib.load(fh)
    module = pyproject["tool"]["django-stubs"]["django_settings_module"]
    assert isinstance(module, str) and module, "django_settings_module must be a non-empty string"
    return module


def test_configured_settings_module_is_importable(configured_settings_module):
    """What mypy does at plugin-load time, without paying for a full mypy run."""
    assert importlib.util.find_spec(configured_settings_module) is not None, (
        f"[tool.django-stubs] django_settings_module = {configured_settings_module!r}, "
        "but that module does not exist in this checkout, so `uv run mypy .` aborts "
        "with INTERNAL ERROR before analysing anything."
    )


def test_configured_settings_module_is_tracked_by_git(configured_settings_module):
    """The invariant a developer machine's own ``local.py`` would otherwise hide."""
    if (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=REPO_ROOT,
            capture_output=True,
        ).returncode
        != 0
    ):
        pytest.skip("not a git work tree; nothing to ask about tracked files")

    spec = importlib.util.find_spec(configured_settings_module)
    assert spec is not None and spec.origin is not None
    relative = Path(spec.origin).resolve().relative_to(REPO_ROOT)

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(relative)],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert tracked.returncode == 0, (
        f"{relative} is not tracked by git, so it is absent from every fresh clone and "
        "from CI. Point [tool.django-stubs] django_settings_module at a committed "
        "settings module (the test settings, as pytest.ini does)."
    )
