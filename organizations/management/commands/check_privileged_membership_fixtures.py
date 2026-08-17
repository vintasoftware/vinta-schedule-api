from __future__ import annotations

import ast
import os
import pathlib
import tempfile

from django.core.management.base import BaseCommand, CommandError


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

PRUNED_DIRS = frozenset({".venv", "migrations", "node_modules"})

SYNC_CALLS = frozenset(
    {
        "grant_membership_groups",
        "sync_membership_groups_from_role",
    }
)

DELIBERATE = "groups-deliberately-absent"

CONSTRUCTOR_METHODS = frozenset(
    {
        "create",
        "get_or_create",
        "update_or_create",
        "bulk_create",
        "update",
        "bulk_update",
    }
)

PRIVILEGE_KWARGS = frozenset(
    {
        "role",
        "is_billing_owner",
    }
)

SCAN_ROUTE_SENTINELS = frozenset(
    {
        "conftest.py",
        "audit/factories.py",
    }
)

EXPECTED_SCANNED_APPS = frozenset(
    {
        "accounts",
        "audit",
        "calendar_integration",
        "common",
        "legal",
        "notifications",
        "organizations",
        "payments",
        "public_api",
        "s3direct_overrides",
        "users",
        "webhooks",
    }
)

OPT_OUT_MODULES = [
    "organizations/tests/test_branding_gate_parity.py",
    "organizations/tests/test_group_backfill_migration.py",
    "organizations/tests/test_membership_manager.py",
    "organizations/tests/test_permission_backend.py",
    "organizations/tests/test_permissions_parity.py",
    "payments/tests/test_dunning_recipients.py",
]


def _test_modules(repo_root: pathlib.Path = REPO_ROOT) -> list[pathlib.Path]:
    """Return every Python module in scope for the membership-fixture scan."""
    paths = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [name for name in dirnames if name not in PRUNED_DIRS]

        directory = pathlib.Path(dirpath)
        relative_parts = directory.relative_to(repo_root).parts
        in_tests = "tests" in relative_parts

        for filename in filenames:
            if not filename.endswith(".py"):
                continue

            if in_tests or filename in {"conftest.py", "factories.py"}:
                paths.append(directory / filename)

    return sorted(paths)


def _scanned_sources() -> tuple[tuple[pathlib.Path, str], ...]:
    sources = tuple((path, path.read_text()) for path in _test_modules())

    _assert_scan_reached_repo(sources)

    return sources


def _assert_scan_reached_repo(
    sources: tuple[tuple[pathlib.Path, str], ...],
) -> None:
    found = {str(path.relative_to(REPO_ROOT)) for path, _ in sources}

    missing = sorted((set(OPT_OUT_MODULES) | SCAN_ROUTE_SENTINELS) - found)

    if missing:
        raise AssertionError(
            "The module scan did not reach modules that are known to exist. "
            "The static analysis may be passing vacuously.\n\n"
            f"Found {len(found)} modules; missing: {missing}"
        )


def _names_membership(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "OrganizationMembership"

    if isinstance(node, ast.Attribute):
        return node.attr == "OrganizationMembership"

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split(".")[-1] == "OrganizationMembership"

    return False


def _is_unprivileged(name: str, value: ast.AST) -> bool:
    """
    Only literal unprivileged values are accepted.

    Anything the AST scanner cannot prove to be unprivileged is treated as
    privileged. This intentionally biases toward false positives rather than
    silently allowing an invalid fixture shape.
    """
    if name == "role":
        if isinstance(value, ast.Attribute):
            return value.attr == "MEMBER"

        return isinstance(value, ast.Constant) and value.value == "member"

    if name == "is_billing_owner":
        return isinstance(value, ast.Constant) and value.value is False

    return True


def _privilege_kwargs(call: ast.Call) -> list[str]:
    found: list[str] = []

    def check(name: str | None, value: ast.AST) -> None:
        if name in PRIVILEGE_KWARGS and not _is_unprivileged(name, value):
            found.append(name)  # type: ignore[arg-type]

    for keyword in call.keywords:
        if keyword.arg in {"defaults", "create_defaults"} and isinstance(keyword.value, ast.Dict):
            for key, value in zip(
                keyword.value.keys,
                keyword.value.values,
                strict=False,
            ):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    check(key.value, value)

            continue

        if keyword.arg is None:
            continue

        check(keyword.arg, keyword.value)

    return found


def _privilege_fields(call: ast.Call) -> list[str]:
    found: list[str] = []

    candidates = [
        *call.args,
        *(keyword.value for keyword in call.keywords if keyword.arg == "fields"),
    ]

    for candidate in candidates:
        if not isinstance(candidate, (ast.List, ast.Tuple)):
            continue

        for element in candidate.elts:
            if (
                isinstance(element, ast.Constant)
                and isinstance(element.value, str)
                and element.value in PRIVILEGE_KWARGS
            ):
                found.append(element.value)

    return found


def _names_the_membership_model(call: ast.Call) -> bool:
    if call.args and _names_membership(call.args[0]):
        return True

    return any(
        keyword.arg == "_model" and _names_membership(keyword.value) for keyword in call.keywords
    )


def _constructor_kind(call: ast.Call) -> str | None:
    func = call.func

    if isinstance(func, ast.Name):
        if func.id == "OrganizationMembership":
            return "OrganizationMembership(...)"

        if _names_the_membership_model(call):
            return f"{func.id}(OrganizationMembership, ...)"

        return None

    if not isinstance(func, ast.Attribute):
        return None

    if func.attr in {"make", "prepare"} and isinstance(
        func.value,
        ast.Name,
    ):
        if func.value.id != "baker":
            return None

        return "baker.make" if _names_the_membership_model(call) else None

    if func.attr in {"update", "bulk_update"}:
        return f"queryset.{func.attr}"

    if func.attr not in CONSTRUCTOR_METHODS:
        return None

    owner = func.value

    if isinstance(owner, ast.Attribute):
        if owner.attr in {"objects", "original_manager"} and _names_membership(owner.value):
            return f"objects.{func.attr}"

        if owner.attr == "memberships":
            return f"memberships.{func.attr}"

    if isinstance(owner, ast.Name) and owner.id == "OrganizationMembership":
        return f"OrganizationMembership.{func.attr}"

    return None


def _scope_of(tree: ast.Module) -> dict[int, ast.AST | None]:
    scopes: dict[int, ast.AST | None] = {
        id(tree): None,
    }

    def descend(
        node: ast.AST,
        scope: ast.AST | None,
    ) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = (
                child
                if isinstance(
                    child,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                )
                else scope
            )

            scopes[id(child)] = child_scope
            descend(child, child_scope)

    descend(tree, None)

    return scopes


def _sync_targets(
    tree: ast.Module,
    scopes: dict[int, ast.AST | None],
) -> dict[int, set[str]]:
    names: dict[int, set[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if not isinstance(node.func, ast.Name):
            continue

        if node.func.id not in SYNC_CALLS:
            continue

        scope = names.setdefault(
            id(scopes.get(id(node))),
            set(),
        )

        for arg in node.args:
            if isinstance(arg, ast.Name):
                scope.add(arg.id)

            elif isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                scope.add(arg.value.id)

    return names


def _wrapped_call_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if not isinstance(node.func, ast.Name):
            continue

        if node.func.id not in SYNC_CALLS:
            continue

        ids.update(id(arg) for arg in node.args)

    return ids


def _bound_names(statement: ast.AST) -> set[str]:
    names: set[str] = set()

    if isinstance(statement, ast.Assign):
        targets = list(statement.targets)
    elif isinstance(statement, ast.AugAssign):
        targets = [statement.target]
    else:
        return set()

    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                names.add(node.id)

            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                names.add(node.value.id)

    return set(names)


def _offenders(
    path: pathlib.Path,
    repo_root: pathlib.Path = REPO_ROOT,
    source: str | None = None,
) -> list[str]:
    if source is None:
        source = path.read_text()

    # Fast pre-filter. If the source doesn't contain either privileged field,
    # it cannot contain one of the patterns we care about.
    if not any(kwarg in source for kwarg in PRIVILEGE_KWARGS):
        return []

    tree = ast.parse(source, str(path))
    lines = source.splitlines()

    wrapped = _wrapped_call_ids(tree)
    scopes = _scope_of(tree)
    synced = _sync_targets(tree, scopes)

    accounted: set[int] = set()

    for statement in ast.walk(tree):
        if not isinstance(
            statement,
            (ast.Assign, ast.AugAssign),
        ):
            continue

        in_scope = synced.get(
            id(scopes.get(id(statement))),
            set(),
        )

        if _bound_names(statement) & in_scope:
            accounted.update(id(node) for node in ast.walk(statement))

    has_marker = DELIBERATE in source

    def marked(node: ast.AST) -> bool:
        if not has_marker:
            return False

        start = getattr(node, "lineno", None)

        if start is None:
            return False

        end = getattr(node, "end_lineno", start)

        return DELIBERATE in "\n".join(lines[start - 1 : end])

    relative = path.relative_to(repo_root)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if id(node) in wrapped or id(node) in accounted or marked(node):
            continue

        if isinstance(node, ast.Call):
            kind = _constructor_kind(node)

            if kind is None:
                continue

            hits = _privilege_kwargs(node)

            if kind.endswith("bulk_update"):
                hits += _privilege_fields(node)

            if hits:
                offenders.append(f"{relative}:{node.lineno}: {kind}({', '.join(hits)}=...)")

        elif isinstance(
            node,
            (ast.Assign, ast.AugAssign),
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]

            for target in targets:
                if not isinstance(
                    target,
                    ast.Attribute,
                ):
                    continue

                if target.attr not in PRIVILEGE_KWARGS:
                    continue

                if _is_unprivileged(
                    target.attr,
                    node.value,
                ):
                    continue

                offenders.append(f"{relative}:{node.lineno}: <membership>.{target.attr} = ...")

    return offenders


OFFENDING_SHAPES = {
    "baker.make": (
        "    baker.make(\n"
        "        OrganizationMembership, "
        "user=None, role=OrganizationRole.ADMIN\n"
        "    )"
    ),
    "bare baker make import": ("    make(OrganizationMembership, user=None, role='admin')"),
    "objects.create": (
        "    OrganizationMembership.objects.create(user=None, is_billing_owner=True)"
    ),
    "attribute assignment": ("    m.role = OrganizationRole.ADMIN"),
    "queryset.update": ("    OrganizationMembership.objects.filter(pk=1).update(role='admin')"),
    "update_or_create defaults": (
        "    OrganizationMembership.objects.update_or_create(\n"
        "        user=None, "
        "defaults={'role': OrganizationRole.ADMIN}\n"
        "    )"
    ),
    "bulk_update field list": ("    OrganizationMembership.objects.bulk_update(rows, ['role'])"),
    "bare constructor": ("    OrganizationMembership(user=None, role=OrganizationRole.ADMIN)"),
    "sync in a different function": (
        "    membership = OrganizationMembership.objects.create(user=None, role='admin')"
    ),
}


def _offenders_in(path: pathlib.Path) -> list[str]:
    return _offenders(
        path,
        repo_root=path.parents[1],
    )


def _run_mutant_tests() -> list[str]:
    errors = []

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        for name, body in OFFENDING_SHAPES.items():
            path = root / name.replace(" ", "_") / "tests" / "test_mutant.py"

            path.parent.mkdir(parents=True)

            path.write_text(
                "from model_bakery import baker\n"
                "from model_bakery.baker import make\n"
                "from organizations.models import "
                "OrganizationMembership, OrganizationRole\n"
                "from organizations.tests.helpers import "
                "grant_membership_groups\n"
                "def test_x():\n"
                f"{body}\n"
                "def test_other():\n"
                "    membership = make_membership("
                "user=None, role='admin')\n"
                "    grant_membership_groups(membership)\n"
            )

            if not _offenders_in(path):
                errors.append(f"AST regression: {name!r} is no longer detected.")

        accepted = root / "accepted" / "tests" / "test_clean.py"
        accepted.parent.mkdir(parents=True)

        accepted.write_text(
            "from organizations.tests.helpers import "
            "grant_membership_groups, make_membership\n"
            "def test_x():\n"
            "    make_membership(user=None, role='admin')\n"
            "    grant_membership_groups(\n"
            "        OrganizationMembership.objects.create("
            "user=None, role='admin')\n"
            "    )\n"
            "    m = OrganizationMembership.objects.create("
            "user=None, role='admin')\n"
            "    grant_membership_groups(m)\n"
            "    baker.make(  # groups-deliberately-absent\n"
            "        OrganizationMembership, user=None, "
            "role='admin'\n"
            "    )\n"
            "    baker.make(OrganizationMembership, "
            "role=OrganizationRole.MEMBER)\n"
            "def test_y():\n"
            "    membership = OrganizationMembership.objects.create("
            "user=None, role='admin')\n"
            "    grant_membership_groups(membership)\n"
        )

        if _offenders_in(accepted):
            errors.append(
                "AST regression: a sanctioned fixture spelling "
                "was incorrectly reported as an offender."
            )

        errors.extend(_run_floor_mutant_tests())

    return errors


def _run_floor_mutant_tests() -> list[str]:
    """The floor's own regression test: prove it raises rather than passing.

    ``_assert_scan_reached_repo`` is what stops this command reporting success on
    nothing -- every check below phrases success as an *absence* (no offenders, no
    unexpected opt-out), so an empty or truncated scan satisfies all of them by
    finding nothing. That makes the floor the single point where a wrong
    ``REPO_ROOT``, a broken path filter or an over-eager ``PRUNED_DIRS`` entry turns
    into a failure instead of a green run, and an unexercised floor is exactly the
    silent pass it exists to prevent.

    Both ways the scan can go blind are asserted: a walk rooted somewhere that
    contains none of the known modules, and a real scan with one app's modules
    dropped out of it.
    """
    errors: list[str] = []

    with tempfile.TemporaryDirectory() as empty:
        wrong_root = tuple((path, path.read_text()) for path in _test_modules(pathlib.Path(empty)))

    try:
        _assert_scan_reached_repo(wrong_root)
    except AssertionError:
        pass
    else:
        errors.append(
            "Anti-vacuity regression: the floor accepted a scan rooted outside the "
            "repository, so a wrong REPO_ROOT would report success on nothing."
        )

    partial = tuple(
        (path, source) for path, source in _scanned_sources() if "payments" not in str(path)
    )

    try:
        _assert_scan_reached_repo(partial)
    except AssertionError:
        pass
    else:
        errors.append(
            "Anti-vacuity regression: the floor accepted a scan that had lost every "
            "payments module, so a truncated walk would report success."
        )

    return errors


def _run_scan() -> list[str]:
    errors = []

    sources = _scanned_sources()

    offenders = []

    for path, source in sources:
        offenders.extend(
            _offenders(
                path,
                source=source,
            )
        )

    if offenders:
        errors.append(
            "Privileged membership fixtures without synchronized "
            "groups were found:\n\n"
            + "\n".join(f"  - {offender}" for offender in offenders)
            + "\n\n"
            "Use organizations.tests.helpers.make_membership "
            "or grant_membership_groups(), or add "
            f"#{DELIBERATE} when the group-less state is intentional."
        )

    users = sorted(
        str(path.relative_to(REPO_ROOT)) for path, source in sources if DELIBERATE in source
    )

    if users != sorted(OPT_OUT_MODULES):
        errors.append(
            "Opt-out registry is out of date.\n"
            f"Expected: {sorted(OPT_OUT_MODULES)}\n"
            f"Found:    {users}"
        )

    scanned_apps = {
        path.relative_to(REPO_ROOT).parts[0]
        for path, _ in sources
        if len(path.relative_to(REPO_ROOT).parts) > 1
    }

    missing = sorted(EXPECTED_SCANNED_APPS - scanned_apps)
    unexpected = sorted(scanned_apps - EXPECTED_SCANNED_APPS)

    if missing or unexpected:
        errors.append(
            "The module walk no longer reaches the expected "
            "application surface.\n"
            f"Missing:   {missing}\n"
            f"Unexpected: {unexpected}\n"
            f"Reached:   {sorted(scanned_apps)}"
        )

    return errors


class Command(BaseCommand):
    help = (
        "Statically verify that tests do not construct privileged "
        "OrganizationMembership objects without synchronized groups."
    )

    def handle(self, *args, **options):
        errors = []

        # Production scan.
        errors.extend(_run_scan())

        # Scanner self-tests. These protect against the analyzer itself
        # silently becoming weaker or accepting an invalid fixture shape.
        errors.extend(_run_mutant_tests())

        if errors:
            self.stdout.write(self.style.ERROR("\n\n".join(errors)))
            self.stdout.write("")
            raise CommandError("Privileged membership fixture static analysis FAILED.")

        sources = _scanned_sources()

        self.stdout.write(
            self.style.SUCCESS("Privileged membership fixture static analysis passed.")
        )
        self.stdout.write(f"  Modules scanned: {len(sources)}")
        self.stdout.write(f"  Allowed opt-outs: {len(OPT_OUT_MODULES)}")
        self.stdout.write(f"  AST regression shapes: {len(OFFENDING_SHAPES)}")
