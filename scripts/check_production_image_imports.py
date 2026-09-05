#!/usr/bin/env python
"""Fail if a module the production image ships imports a dev-only dependency.

`di_core.apps.DICoreConfig.ready()` calls
`container.wire(packages=INTERNAL_INSTALLED_APPS)`, which imports EVERY module
under those packages. The production image installs no dev dependencies
(`uv sync --no-dev`), so one shipped test module importing `model_bakery` stops
web, worker, beat and the release task alike:

    File "/home/user/app/common/tests/test_org_retrievers.py", line 21
      from model_bakery import baker
    ModuleNotFoundError: No module named 'model_bakery'

Nothing in CI runs that image -- tests run on the runner via `uv run` -- which
is why this is a static check rather than a smoke test.

Deliberately free of Django and third-party imports: it reads settings,
pyproject and .dockerignore as text. That keeps it runnable from a pre-commit
hook with no `.env`, no database and no `django.setup()`, which also makes it
fast enough to run on every commit.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from typing import TypeGuard


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SETTINGS = _REPO_ROOT / "vinta_schedule_api" / "settings" / "base.py"

# Import names the production image lacks that no `dev` entry spells out.
# `django_stubs_ext` is real at runtime and reaches the environment only as a
# dependency of the dev-only `django-stubs`, so importing it outside a
# TYPE_CHECKING block would break the containers.
_TRANSITIVE_DEV_IMPORTS = {"django_stubs_ext"}


class CheckError(Exception):
    """The check could not run, as distinct from finding an offender."""


def _wired_packages() -> list[str]:
    """`INTERNAL_INSTALLED_APPS`, read out of the settings module as source.

    Importing the settings would drag in Django and every credential they
    expect, so the literal is parsed instead.
    """
    tree = ast.parse(_SETTINGS.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "INTERNAL_INSTALLED_APPS" not in targets:
            continue
        if not isinstance(node.value, ast.List):
            raise CheckError("INTERNAL_INSTALLED_APPS is no longer a list literal")
        return [
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    raise CheckError(f"could not find INTERNAL_INSTALLED_APPS in {_SETTINGS}")


def _dev_only_import_names() -> set[str]:
    """Import names provided by `[dependency-groups] dev`, absent from the image."""
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r"^dev = \[(.*?)^\]", pyproject, re.S | re.M)
    if match is None:
        raise CheckError("could not find the `dev` dependency group in pyproject.toml")

    names = set(_TRANSITIVE_DEV_IMPORTS)
    for spec in re.findall(r'"([A-Za-z0-9_.\[\]-]+)', match.group(1)):
        names.add(spec.split("[")[0].replace("-", "_").lower())
    return names


def _excluded_from_image() -> tuple[set[str], set[str], set[str]]:
    """The `.dockerignore` entries that keep test support out of the image.

    Read rather than restated so this check cannot drift from what actually
    ships. Only the `**/name` and root-level `name.py` forms are interpreted --
    the rest of the file names directories this walk never reaches anyway.
    """
    lines = [
        line.strip()
        for line in (_REPO_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    components = {p[3:] for p in lines if p.startswith("**/") and not p.endswith(".py")}
    filenames = {p[3:] for p in lines if p.startswith("**/") and p.endswith(".py")}
    root_files = {p for p in lines if not p.startswith("**/") and p.endswith(".py")}
    return components, filenames, root_files


def _is_type_checking_guard(node: ast.AST) -> TypeGuard[ast.If]:
    """`if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` -- never runs.

    A TypeGuard rather than a bool so the caller can reach `.orelse` on the
    narrowed node; `ast.iter_child_nodes` only promises `AST`.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imports_executed_at_import_time(path: pathlib.Path) -> set[str]:
    """Import names bound when the module is first imported.

    Skips two things that do not run then: the body of a TYPE_CHECKING guard
    (annotations only), and function bodies, where an import is deferred to the
    call. Class bodies do execute, so they are walked.
    """
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                names.update(alias.name.split(".")[0] for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:
                    names.add(child.module.split(".")[0])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            elif _is_type_checking_guard(child):
                for fallback in child.orelse:  # the `else:` branch does run
                    visit(fallback)
            else:
                visit(child)

    visit(tree)
    return names


def main() -> int:
    packages = _wired_packages()
    dev_imports = _dev_only_import_names()
    components, filenames, root_files = _excluded_from_image()

    def shipped(path: pathlib.Path) -> bool:
        relative = path.relative_to(_REPO_ROOT)
        if components & set(relative.parts):
            return False
        if path.name in filenames:
            return False
        return str(relative) not in root_files

    offenders: list[str] = []
    checked = 0
    for package in packages:
        for path in sorted((_REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts or not shipped(path):
                continue
            checked += 1
            forbidden = _imports_executed_at_import_time(path) & dev_imports
            if forbidden:
                offenders.append(
                    f"  {path.relative_to(_REPO_ROOT)} imports {', '.join(sorted(forbidden))}"
                )

    if offenders:
        print(
            f"{len(offenders)} module(s) ship in the production image and import a "
            f"dev-only dependency:\n" + "\n".join(offenders),
            file=sys.stderr,
        )
        print(
            "\nEvery container calls container.wire() over INTERNAL_INSTALLED_APPS at "
            "startup and imports these, so the task dies with ModuleNotFoundError.\n"
            "Either move the module behind a .dockerignore entry (test support belongs "
            "in tests/, testing/, factories.py or fixtures.py), or drop the dev-only "
            "import.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {checked} shipped module(s) across {len(packages)} package(s), "
        "none importing a dev-only dependency."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckError as exc:
        print(f"check could not run: {exc}", file=sys.stderr)
        sys.exit(2)
