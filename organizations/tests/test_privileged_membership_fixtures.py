"""No test may build a privileged membership without the groups that carry it.

Since Phase 4 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
every **permission class** reads an organization-named permission check
(``organizations.authorization.has_organization_permission``, not
``user.has_perm``), which resolves through ``OrganizationMembership.groups``.
Four ``membership.is_admin`` readers survive outside the permission classes
until Phase 6 drops the columns -- enumerated in
``ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md``. They still read the
*column*, so the fixture shape this module bans is precisely the one where they
and a permission class answer differently about the same caller in the same
request. Production keeps groups in step with ``role``
/ ``is_billing_owner`` through
``organizations.services.sync_membership_groups_from_role``; ``baker.make`` and
``objects.create`` do not, so a test written the old way produces a membership
that **cannot exist in production**: privileged by column, unprivileged by
permission.

**That failure is silent, which is why this test exists.** A test whose admin
membership carries no groups sees a denial. If it asserted an allow it turns red
and someone fixes it; if it asserted a denial for some *other* reason -- a
missing entitlement, the wrong organization, an unowned calendar -- it keeps
passing and stops proving anything. Grep cannot find those, and neither can a
test run. A static scan can.

So this is the guard, not the sweep's memory: it re-derives the answer from the
source tree on every run, so a new test written the old way fails here rather
than years later in an audit.

**The escape hatch** is a ``# groups-deliberately-absent`` comment on the
construction. Every user of it is enumerated in ``OPT_OUT_MODULES`` at the
bottom of this module, which both guard tests read -- that list, not this
paragraph, is the count. Each earns it because the group-less
state is the subject rather than an accident: the Phase 3 backfill migration
test (which builds pre-backfill rows through historical models), the
auth-backend test (which assigns groups by name to reach states the role
mapping cannot produce), the ones that pin "a role with no group buys nothing",
and this module, whose fixtures are source text rather than rows.

Sanctioned spellings, all in ``organizations/tests/helpers.py``:
``make_membership`` / ``make_admin_membership`` / ``make_billing_owner_membership``
to build one, ``grant_membership_groups`` to bring an existing one back in step
after a role change.
"""

import ast
import functools
import os
import pathlib

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Directories the scan never descends into, pruned *during* the walk.
#:
#: Pruning rather than filtering-after is what keeps this module inside the 10s
#: per-test budget in ``pytest.ini``: the repo holds ~10k ``.py`` files, all but
#: ~300 of them under ``.venv``, so walking the whole tree and discarding the
#: rest cost ~5s per call -- and both guard tests below called it. The pruned
#: walk selects the identical set in ~0.05s.
PRUNED_DIRS = frozenset({".venv", "migrations", "node_modules"})

#: What the scan accepts as "this membership's groups were seen to".
SYNC_CALLS = frozenset({"grant_membership_groups", "sync_membership_groups_from_role"})

#: Opt-out marker, for the modules where a group-less membership is the subject.
DELIBERATE = "groups-deliberately-absent"

#: Constructors that write ``role`` / ``is_billing_owner`` without the dual-write.
CONSTRUCTOR_METHODS = frozenset(
    {"create", "get_or_create", "update_or_create", "bulk_create", "update", "bulk_update"}
)

PRIVILEGE_KWARGS = frozenset({"role", "is_billing_owner"})

#: Stable files proving that every inclusion route in ``_test_modules`` still works.
SCAN_ROUTE_SENTINELS = frozenset(
    {
        "organizations/tests/test_privileged_membership_fixtures.py",
        "conftest.py",
        "audit/factories.py",
    }
)

#: Every top-level project app with modules currently selected by ``_test_modules``.
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


def _test_modules(repo_root: pathlib.Path = REPO_ROOT) -> list[pathlib.Path]:
    """Every module a test fixture can be built in.

    ``factories.py`` is in scope alongside ``tests/`` and ``conftest.py``: the
    per-app factory modules (``calendar_integration/factories.py``,
    ``audit/factories.py``) build memberships for tests exactly the way a
    ``conftest`` fixture does, and live outside any ``tests`` package, so the
    original path filter could not reach them.

    ``PRUNED_DIRS`` is applied to ``dirnames`` in place, so the walk never
    descends into them at all. That is only a speed change: a path under any of
    those directories carries the directory in ``parts``, so the previous
    filter-after-walking discarded exactly the same files.
    """
    paths = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [name for name in dirnames if name not in PRUNED_DIRS]
        directory = pathlib.Path(dirpath)
        in_tests = "tests" in directory.relative_to(repo_root).parts
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            if in_tests or filename in {"conftest.py", "factories.py"}:
                paths.append(directory / filename)
    return sorted(paths)


@functools.cache
def _scanned_sources() -> tuple[tuple[pathlib.Path, str], ...]:
    """Every in-scope module, read from disk **once** for the whole session.

    Both repo-wide guards below need the same ~300 files -- one to parse them,
    one to grep them for the opt-out marker -- and each used to walk and read
    the tree independently, which is what pushed the pair past the per-test
    timeout on CI.

    The cache is only safe because it cannot come back empty unnoticed: a wrong
    root, a broken glob or an over-eager prune would otherwise turn *both*
    guards into unconditional passes, which is the exact silent-success failure
    this module exists to prevent. ``_assert_the_scan_reached_the_repo`` is that
    floor, and it runs before the value is returned. ``functools.cache`` does
    not memoize exceptions, so a failing floor keeps failing.
    """
    sources = tuple((path, path.read_text()) for path in _test_modules())
    _assert_the_scan_reached_the_repo(sources)
    return sources


def _assert_the_scan_reached_the_repo(sources: tuple[tuple[pathlib.Path, str], ...]) -> None:
    """The anti-vacuity floor: the walk must have found the files we know exist.

    ``OPT_OUT_MODULES`` and ``SCAN_ROUTE_SENTINELS`` are the floor rather than a
    bare count. The sentinels pin the three independent inclusion routes -- a
    module under ``tests/``, the root ``conftest.py``, and an app
    ``factories.py`` -- while the opt-outs pin known test modules across apps.
    A count could be met by unrelated files after one route was accidentally
    removed.
    """
    found = {str(path.relative_to(REPO_ROOT)) for path, _ in sources}
    missing = sorted((set(OPT_OUT_MODULES) | SCAN_ROUTE_SENTINELS) - found)

    assert not missing, (
        "The module scan did not reach modules that are known to exist, so the "
        "guards in this file would pass without checking anything. Suspect "
        f"REPO_ROOT ({REPO_ROOT}), PRUNED_DIRS or the path filter in "
        f"_test_modules.\n\nFound {len(found)} modules; missing: {missing}"
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
    """Only a *literal* unprivileged value is accepted.

    A variable, a conditional expression, or anything else the scan cannot read
    counts as privileged. Over-flagging costs one helper call; under-flagging
    costs a test that proves nothing.
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
            for key, value in zip(keyword.value.keys, keyword.value.values, strict=False):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    check(key.value, value)
            continue
        if keyword.arg is None:
            continue
        check(keyword.arg, keyword.value)
    return found


def _privilege_fields(call: ast.Call) -> list[str]:
    """The privileged field *names* in a ``bulk_update`` field list.

    ``bulk_update(rows, ["role"])`` writes the column straight past ``save()``
    and names it positionally, so ``_privilege_kwargs`` -- which reads keywords
    -- sees nothing at all. The new value lives on the model instances rather
    than in the call, so there is no literal to judge: naming the field is taken
    as privileged, in keeping with the over-flag bias stated above.
    """
    found: list[str] = []
    candidates = [*call.args, *(kw.value for kw in call.keywords if kw.arg == "fields")]
    for candidate in candidates:
        if not isinstance(candidate, ast.List | ast.Tuple):
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
    """``(OrganizationMembership, ...)`` / ``(_model=OrganizationMembership)``."""
    if call.args and _names_membership(call.args[0]):
        return True
    return any(kw.arg == "_model" and _names_membership(kw.value) for kw in call.keywords)


def _constructor_kind(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        # A bare name, two shapes:
        #  * ``from model_bakery.baker import make`` -- the same constructor the
        #    ``baker.make`` branch below matches, one import away from invisible
        #    to an attribute-only scan. Any bare call naming the membership model
        #    positionally qualifies; a sanctioned helper never does (they take
        #    ``user=`` / ``organization=``).
        #  * ``OrganizationMembership(role=...)`` -- an unsaved instance, which is
        #    how the objects handed to ``bulk_create`` are built.
        if func.id == "OrganizationMembership":
            return "OrganizationMembership(...)"
        if _names_the_membership_model(call):
            return f"{func.id}(OrganizationMembership, ...)"
        return None
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in {"make", "prepare"} and isinstance(func.value, ast.Name):
        if func.value.id != "baker":
            return None
        return "baker.make" if _names_the_membership_model(call) else None
    if func.attr in {"update", "bulk_update"}:
        # ``queryset.update(role=...)`` writes past ``save()`` and so past the
        # dual-write. Flagged whatever the receiver is: the kwarg names are
        # specific enough that a false positive is cheap.
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
    """Each node's nearest enclosing function, or ``None`` for module level.

    The scan's "this membership's groups were seen to" exemption is keyed on a
    *variable name*, and ``membership`` is the commonest name in these files. A
    module-wide exemption therefore let one function's synced ``membership``
    silently vouch for a different, never-synced ``membership`` in a function
    fifty lines away -- the exemption is only meaningful where the two
    statements can actually be about the same object.

    A nested function is its own scope, so a membership built in an enclosing
    function and synced only inside a closure is flagged. That is the
    over-flagging direction, which costs one helper call.
    """
    scopes: dict[int, ast.AST | None] = {id(tree): None}

    def descend(node: ast.AST, scope: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = (
                child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            )
            scopes[id(child)] = child_scope
            descend(child, child_scope)

    descend(tree, None)
    return scopes


def _sync_targets(tree: ast.Module, scopes: dict[int, ast.AST | None]) -> dict[int, set[str]]:
    """Names handed to a group-sync call, keyed by the scope it happened in."""
    names: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in SYNC_CALLS:
                continue
            scope = names.setdefault(id(scopes.get(id(node))), set())
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    scope.add(arg.id)
                elif isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                    scope.add(arg.value.id)
    return names


def _wrapped_call_ids(tree: ast.Module) -> set[int]:
    """Calls passed directly to a sync call: ``grant_membership_groups(X.create(...))``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in SYNC_CALLS:
                ids.update(id(arg) for arg in node.args)
    return ids


def _bound_names(statement: ast.AST) -> set[str]:
    names: set[str] = set()
    targets: list[ast.AST]
    if isinstance(statement, ast.Assign):
        targets = list(statement.targets)
    elif isinstance(statement, ast.AugAssign):
        targets = [statement.target]
    else:
        return names
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                names.add(node.value.id)
    return names


def _offenders(
    path: pathlib.Path, repo_root: pathlib.Path = REPO_ROOT, source: str | None = None
) -> list[str]:
    """``source`` lets the repo-wide guard hand in the text ``_scanned_sources``
    already read, instead of this function reading the same file a second time.
    Left optional so the mutant self-tests below, which point at a ``tmp_path``
    module that is deliberately not in the cache, still exercise this same
    analysis by path.
    """
    if source is None:
        source = path.read_text()
    # Every way this function can emit an offender ends at a name in
    # ``PRIVILEGE_KWARGS``: a keyword argument, a ``defaults`` dict key, a
    # ``bulk_update`` field string, or an assigned attribute. All four require
    # the word itself in the source, so a module without one cannot offend and
    # need not be parsed. This replaces a filter keyed on ``OrganizationMembership
    # or role``, which parsed 40 more modules for nothing and, in the other
    # direction, could skip a module that named ``is_billing_owner`` alone.
    if not any(kwarg in source for kwarg in PRIVILEGE_KWARGS):
        return []
    tree = ast.parse(source, str(path))
    lines = source.splitlines()
    wrapped = _wrapped_call_ids(tree)
    scopes = _scope_of(tree)
    synced = _sync_targets(tree, scopes)

    accounted: set[int] = set()
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.Assign | ast.AugAssign):
            continue
        in_scope = synced.get(id(scopes.get(id(statement))), set())
        if _bound_names(statement) & in_scope:
            accounted.update(id(node) for node in ast.walk(statement))

    # The marker lives in seven modules repo-wide, but ``marked`` below runs a
    # slice-and-join for every node of every module. Asking the whole source
    # once first is the same answer -- no line range can hold a marker the file
    # does not -- and skips ~450k joins across a full scan.
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
        elif isinstance(node, ast.Assign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Attribute) or target.attr not in PRIVILEGE_KWARGS:
                    continue
                if _is_unprivileged(target.attr, node.value):
                    continue
                offenders.append(f"{relative}:{node.lineno}: <membership>.{target.attr} = ...")
    return offenders


def test_no_test_builds_a_privileged_membership_without_its_groups():
    offenders: list[str] = []
    for path, source in _scanned_sources():
        offenders.extend(_offenders(path, source=source))

    assert not offenders, (
        "These test modules build a membership that is privileged by column and "
        "unprivileged by permission -- a shape production cannot produce, and one "
        "that makes an authorization assertion pass for the wrong reason. Build it "
        "with organizations.tests.helpers.make_membership (or call "
        "grant_membership_groups on it after a role change); if the missing groups "
        "are the point, say so with a "
        f"'# {DELIBERATE}' comment.\n\n" + "\n".join(offenders)
    )


#: One line per shape the scan must catch, so a failure names the shape that
#: stopped being caught rather than a count that moved.
OFFENDING_SHAPES = {
    "baker.make": (
        "    baker.make(\n        OrganizationMembership, user=None, role=OrganizationRole.ADMIN\n    )"
    ),
    "bare baker make import": "    make(OrganizationMembership, user=None, role='admin')",
    "objects.create": "    OrganizationMembership.objects.create(user=None, is_billing_owner=True)",
    "attribute assignment": "    m.role = OrganizationRole.ADMIN",
    "queryset.update": "    OrganizationMembership.objects.filter(pk=1).update(role='admin')",
    "update_or_create defaults": (
        "    OrganizationMembership.objects.update_or_create(\n"
        "        user=None, defaults={'role': OrganizationRole.ADMIN}\n"
        "    )"
    ),
    "bulk_update field list": "    OrganizationMembership.objects.bulk_update(rows, ['role'])",
    "bare constructor": "    OrganizationMembership(user=None, role=OrganizationRole.ADMIN)",
    "sync in a different function": (
        "    membership = OrganizationMembership.objects.create(user=None, role='admin')"
    ),
}


def test_the_scan_would_catch_a_new_offender(tmp_path):
    """The guard's own regression test: a mutant module must be flagged.

    Without this, a scan that silently stopped matching -- a renamed helper, a
    changed AST shape, a typo in a method name -- would report success forever,
    which is the same failure mode the guard exists to prevent, one level up.

    Every shape in ``OFFENDING_SHAPES`` is asserted **individually**, because a
    single module asserted by count lets one shape stop being caught while
    another starts being caught twice. The last shape is the module-scope hole:
    a ``membership`` synced inside ``test_other`` must not vouch for the
    ``membership`` built in ``test_x``.
    """
    for name, body in OFFENDING_SHAPES.items():
        offending = tmp_path / name.replace(" ", "_") / "tests" / "test_mutant.py"
        offending.parent.mkdir(parents=True)
        offending.write_text(
            "from model_bakery import baker\n"
            "from model_bakery.baker import make\n"
            "from organizations.models import OrganizationMembership, OrganizationRole\n"
            "from organizations.tests.helpers import grant_membership_groups\n"
            "def test_x():\n"
            f"{body}\n"
            "def test_other():\n"
            "    membership = make_membership(user=None, role='admin')\n"
            "    grant_membership_groups(membership)\n"
        )

        assert _offenders_in(offending), f"{name} is no longer caught"


def test_the_scan_accepts_every_sanctioned_spelling(tmp_path):
    accepted = tmp_path / "tests" / "test_clean.py"
    accepted.parent.mkdir(parents=True)
    accepted.write_text(
        "from organizations.tests.helpers import grant_membership_groups, make_membership\n"
        "def test_x():\n"
        "    make_membership(user=None, role='admin')\n"
        "    grant_membership_groups(\n"
        "        OrganizationMembership.objects.create(user=None, role='admin')\n"
        "    )\n"
        "    m = OrganizationMembership.objects.create(user=None, role='admin')\n"
        "    grant_membership_groups(m)\n"
        "    baker.make(  # groups-deliberately-absent\n"
        "        OrganizationMembership, user=None, role='admin'\n"
        "    )\n"
        "    baker.make(OrganizationMembership, user=None, role=OrganizationRole.MEMBER)\n"
        "def test_y():\n"
        "    membership = OrganizationMembership.objects.create(user=None, role='admin')\n"
        "    grant_membership_groups(membership)\n"
    )

    assert _offenders_in(accepted) == []


def _offenders_in(path: pathlib.Path) -> list[str]:
    """``_offenders`` against a path outside the repo, for the two tests above.

    Takes the root as an argument rather than rebinding the module-level
    ``REPO_ROOT``: the rebinding made these two tests order-sensitive against
    anything else reading it, for nothing more than a relative path in a message.
    """
    return _offenders(path, repo_root=path.parents[1])


#: Every module allowed to carry the opt-out marker, sorted.
#:
#: **One literal, read by both tests below**, because the guard used to keep two
#: and they disagreed: a four-entry ``parametrize`` list beside a seven-entry
#: exact-match assertion, with ``test_branding_gate_parity.py`` using the marker
#: while appearing in neither. A guard that can contradict itself is not one.
#: Adding an eighth user of the marker is a deliberate edit *here* -- which is
#: where a reviewer will ask why -- and it turns both tests red until it is made.
OPT_OUT_MODULES = [
    "organizations/tests/test_branding_gate_parity.py",
    "organizations/tests/test_group_backfill_migration.py",
    "organizations/tests/test_membership_manager.py",
    "organizations/tests/test_permission_backend.py",
    "organizations/tests/test_permissions_parity.py",
    "organizations/tests/test_privileged_membership_fixtures.py",
    "payments/tests/test_dunning_recipients.py",
]


@pytest.mark.parametrize("module", OPT_OUT_MODULES)
def test_the_opt_out_is_confined_to_the_modules_that_earned_it(module):
    """The other direction from ``test_no_other_module_uses_the_opt_out``: a
    module listed above must still *need* the marker.

    One row per module, so a stale entry names itself rather than moving a
    count.
    """
    sources = {str(path.relative_to(REPO_ROOT)): source for path, source in _scanned_sources()}
    source = sources[module]

    assert DELIBERATE in source, f"{module} no longer needs the opt-out; drop it from this list"


def test_no_other_module_uses_the_opt_out():
    users = sorted(
        str(path.relative_to(REPO_ROOT))
        for path, source in _scanned_sources()
        if DELIBERATE in source
    )

    assert users == sorted(OPT_OUT_MODULES), users


def test_a_blind_scan_fails_the_floor_instead_of_passing(tmp_path):
    """The cached scan must not be able to report success on nothing.

    ``_scanned_sources`` is read by both repo-wide guards, and both of them
    phrase success as an absence -- no offenders, no unexpected opt-out. An
    empty or truncated scan therefore satisfies them *by finding nothing*,
    which is precisely the silent pass this module exists to catch. So the
    floor is asserted here directly, on the two ways the scan can go blind.
    """
    wrong_root_sources = tuple((path, path.read_text()) for path in _test_modules(tmp_path))
    with pytest.raises(AssertionError, match="did not reach modules"):
        _assert_the_scan_reached_the_repo(wrong_root_sources)

    partial = tuple(
        (path, source) for path, source in _scanned_sources() if "payments" not in str(path)
    )
    with pytest.raises(AssertionError, match=r"payments/tests/test_dunning_recipients\.py"):
        _assert_the_scan_reached_the_repo(partial)


def test_the_pruned_walk_still_reaches_every_scanned_app():
    """``PRUNED_DIRS`` prunes during the walk, so an over-broad entry there
    would quietly shrink the scan. This pins the complete top-level app surface
    the walk is supposed to span.
    """
    scanned = {
        relative.parts[0]
        for path, _ in _scanned_sources()
        if len((relative := path.relative_to(REPO_ROOT)).parts) > 1
    }
    missing = sorted(EXPECTED_SCANNED_APPS - scanned)
    unexpected = sorted(scanned - EXPECTED_SCANNED_APPS)

    assert scanned == EXPECTED_SCANNED_APPS, (
        "the module walk no longer reaches the complete expected app surface; "
        f"missing: {missing}; unexpected: {unexpected}; reached: {sorted(scanned)}"
    )
