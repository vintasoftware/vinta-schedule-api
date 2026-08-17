"""Membership resolution stays on the package's request/direct-call seams.

No production or test module may import or call
``get_active_organization_membership``, and no code may write
``_active_membership`` onto a user. Both were the repo-owned resolver
that ``vinta_orgs`` ``0.3.0`` replaced -- the package resolves through
``request.organization_membership`` on a request and ``resolve_membership_for_user``
off one, and, unlike the deleted helper, refuses to silently pick the oldest active
membership when a caller belongs to several.

Reintroducing either is silent: a stashed ``_active_membership`` shadows the package's
resolution for the rest of the request, and the caller sees a plausible membership for
the wrong organization rather than an error. Grep would find the obvious spelling; a
``getattr(user, "_active_membership")`` it would not. So the check is an AST scan, and
it lives here rather than in a test because it walks ~700 modules and the suite's
per-test budget is 10 seconds (``pytest.ini``). Wired into ``.pre-commit-config.yaml``
and ``.github/workflows/main.yml`` beside the other two relocated scans.
"""

from __future__ import annotations

import ast
import os
import pathlib
import tempfile

from django.core.management.base import BaseCommand, CommandError


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Pruned *during* the walk rather than filtered afterwards. Only a speed change: a
#: path under any of these carries the directory in ``parts``, so filtering after the
#: walk selected the identical set.
PRUNED_DIRS = frozenset({".git", ".venv", "migrations", "node_modules"})

# Spelled in halves so this module's own source carries neither name as a literal --
# it is inside the scanned tree, and a reader grepping for the resolver should not be
# sent here first.
REMOVED_RESOLVER = "get_active_" + "organization_membership"
REMOVED_USER_ATTRIBUTE = "_" + "active_membership"

DYNAMIC_ACCESSORS = frozenset({"getattr", "setattr", "delattr"})

#: The anti-vacuity floor. Three stable modules, one per reason the walk could go
#: blind: a first-party production module, a shared utility outside the app, and this
#: command itself. An empty or truncated scan satisfies "no violations" by finding
#: nothing, which is the exact silent pass this guard exists to prevent.
SCAN_SENTINELS = frozenset(
    {
        "organizations/models.py",
        "common/utils/view_utils.py",
        "organizations/management/commands/check_legacy_membership_resolution.py",
    }
)


def _python_modules(repo_root: pathlib.Path = REPO_ROOT) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [name for name in dirnames if name not in PRUNED_DIRS]

        directory = pathlib.Path(dirpath)

        for filename in filenames:
            if filename.endswith(".py"):
                paths.append(directory / filename)

    return sorted(paths)


def _assert_scan_reached_repo(scanned: set[str]) -> None:
    missing = sorted(SCAN_SENTINELS - scanned)

    if missing:
        raise AssertionError(
            "The legacy-membership scan did not reach modules that are known to "
            "exist, so it would report success without checking anything. Suspect "
            f"REPO_ROOT ({REPO_ROOT}) or PRUNED_DIRS.\n\n"
            f"Scanned {len(scanned)} modules; missing: {missing}"
        )


def _names_a_removed_symbol(func: ast.expr, names: frozenset[str]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in names
    if isinstance(func, ast.Attribute):
        return func.attr in names
    return False


def _violations(
    path: pathlib.Path,
    repo_root: pathlib.Path = REPO_ROOT,
    source: str | None = None,
) -> list[str]:
    if source is None:
        source = path.read_text()

    relative = path.relative_to(repo_root)
    tree = ast.parse(source, str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == REMOVED_RESOLVER
        ):
            violations.append(f"{relative}:{node.lineno}: definition")

        if isinstance(node, ast.ImportFrom) and any(
            alias.name == REMOVED_RESOLVER for alias in node.names
        ):
            violations.append(f"{relative}:{node.lineno}: import")

        if isinstance(node, ast.Call):
            if _names_a_removed_symbol(node.func, frozenset({REMOVED_RESOLVER})):
                violations.append(f"{relative}:{node.lineno}: call")

            # ``getattr(user, "_active_membership")`` and friends: invisible to a
            # plain attribute scan, and the likeliest way the stash comes back.
            if (
                _names_a_removed_symbol(node.func, DYNAMIC_ACCESSORS)
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in {REMOVED_RESOLVER, REMOVED_USER_ATTRIBUTE}
            ):
                violations.append(f"{relative}:{node.lineno}: dynamic attribute access")

        if isinstance(node, ast.Attribute) and node.attr in {
            REMOVED_RESOLVER,
            REMOVED_USER_ATTRIBUTE,
        }:
            violations.append(f"{relative}:{node.lineno}: attribute access")

    return violations


def _run_scan() -> list[str]:
    errors: list[str] = []
    scanned: set[str] = set()
    violations: list[str] = []

    for path in _python_modules():
        scanned.add(str(path.relative_to(REPO_ROOT)))
        violations.extend(_violations(path))

    _assert_scan_reached_repo(scanned)

    if violations:
        errors.append(
            "The removed membership resolver or the user stash is back:\n\n"
            + "\n".join(f"  - {violation}" for violation in violations)
            + "\n\nResolve a membership through request.organization_membership on a "
            "request, or vinta_orgs.helpers.organizations.resolve_membership_for_user "
            "off one."
        )

    return errors


#: One entry per shape the scan must catch, so a failure names the shape that stopped
#: being caught rather than a count that moved.
OFFENDING_SHAPES = {
    "definition": f"def {REMOVED_RESOLVER}(user):\n    return None\n",
    "import": f"from organizations.services import {REMOVED_RESOLVER}\n",
    "call": f"def f(user):\n    return {REMOVED_RESOLVER}(user)\n",
    "attribute call": f"def f(resolver, user):\n    return resolver.{REMOVED_RESOLVER}(user)\n",
    "user stash write": f"def f(user, membership):\n    user.{REMOVED_USER_ATTRIBUTE} = membership\n",
    "dynamic attribute access": (
        f'def f(user):\n    return getattr(user, "{REMOVED_USER_ATTRIBUTE}", None)\n'
    ),
}


def _run_mutant_tests() -> list[str]:
    """The guard's own regression test.

    Without it, a scan that silently stopped matching -- a renamed AST node, a typo,
    an over-eager prune -- would report success forever, which is the same failure
    mode this command exists to prevent, one level up. The floor is asserted too: a
    walk rooted somewhere with none of the sentinels must *raise*, not pass.
    """
    errors: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        for name, body in OFFENDING_SHAPES.items():
            path = root / name.replace(" ", "_") / "module.py"
            path.parent.mkdir(parents=True)
            path.write_text(body)

            if not _violations(path, repo_root=path.parent):
                errors.append(f"AST regression: {name!r} is no longer detected.")

        clean = root / "clean" / "module.py"
        clean.parent.mkdir(parents=True)
        clean.write_text(
            "def f(request):\n"
            "    return request.organization_membership\n"
            "def g(user):\n"
            "    return resolve_membership_for_user(user)\n"
        )

        if _violations(clean, repo_root=clean.parent):
            errors.append("AST regression: a sanctioned resolution spelling was reported.")

        # The floor: a walk that reached nothing must fail rather than pass.
        blind = {str(path.relative_to(root)) for path in _python_modules(root)}
        try:
            _assert_scan_reached_repo(blind)
        except AssertionError:
            pass
        else:
            errors.append(
                "Anti-vacuity regression: the sentinel floor accepted a scan that "
                "reached none of the modules it names."
            )

    return errors


class Command(BaseCommand):
    help = (
        "Statically verify that no module reintroduces the removed membership "
        "resolver or the _active_membership user stash."
    )

    def handle(self, *args, **options):
        errors = _run_scan()
        errors.extend(_run_mutant_tests())

        if errors:
            self.stdout.write(self.style.ERROR("\n\n".join(errors)))
            self.stdout.write("")
            raise CommandError("Legacy membership resolution static analysis FAILED.")

        self.stdout.write(
            self.style.SUCCESS("Legacy membership resolution static analysis passed.")
        )
        self.stdout.write(f"  Modules scanned: {len(_python_modules())}")
        self.stdout.write(f"  AST regression shapes: {len(OFFENDING_SHAPES)}")
