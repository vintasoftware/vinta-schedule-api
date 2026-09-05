from __future__ import annotations

import ast
import pathlib
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Import names the production image lacks that no `dev` entry spells out.
# `django_stubs_ext` is real at runtime and reaches the environment only as a
# dependency of the dev-only `django-stubs`, so importing it outside a
# TYPE_CHECKING block would break the containers.
_TRANSITIVE_DEV_IMPORTS = {"django_stubs_ext"}


def _dev_only_import_names() -> set[str]:
    """Import names provided by `[dependency-groups] dev`, which the production
    image does not install (`uv sync --no-dev`)."""
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r"^dev = \[(.*?)^\]", pyproject, re.S | re.M)
    if match is None:
        raise CommandError("could not find the `dev` dependency group in pyproject.toml")

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


def _is_type_checking_guard(node: ast.stmt) -> bool:
    """`if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` -- never runs."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imports_executed_at_import_time(path: pathlib.Path) -> set[str]:
    """Import names bound when the module is first imported.

    Skips two things that do not run then: the body of a TYPE_CHECKING guard
    (annotations only, and every module here has `from __future__ import
    annotations`), and function bodies, where an import is deferred to the call.
    Class bodies do execute, so they are walked.
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


class Command(BaseCommand):
    help = (
        "Fail if a module the production image ships imports a dev-only dependency. "
        "di_core wires every module under INTERNAL_INSTALLED_APPS at startup, so one "
        "such import stops every container."
    )

    def handle(self, *args, **options) -> None:
        # `container.wire(packages=INTERNAL_INSTALLED_APPS)` imports every
        # submodule of every package named here -- see di_core/apps.py. The
        # failure is not lazy: it happens in AppConfig.ready(), so web, worker,
        # beat and the release task all die the same way, and only in an image
        # built without dev dependencies. Nothing in CI runs that image, which
        # is why this is a static check.
        packages = getattr(settings, "INTERNAL_INSTALLED_APPS", [])
        if not packages:
            raise CommandError("INTERNAL_INSTALLED_APPS is empty; nothing to check")

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
                        f"{path.relative_to(_REPO_ROOT)} imports {', '.join(sorted(forbidden))}"
                    )

        if offenders:
            listing = "\n".join(f"  {line}" for line in offenders)
            raise CommandError(
                f"{len(offenders)} module(s) ship in the production image and import a "
                f"dev-only dependency:\n{listing}\n\n"
                "Every container calls container.wire() over INTERNAL_INSTALLED_APPS at "
                "startup and imports these, so the task dies with ModuleNotFoundError.\n"
                "Either move the module behind a .dockerignore entry (test support "
                "belongs in tests/, testing/, factories.py or fixtures.py), or drop the "
                "dev-only import."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"OK: {checked} shipped module(s) across {len(packages)} package(s), "
                "none importing a dev-only dependency."
            )
        )
