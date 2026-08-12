"""Test-only tripwire: catch a query on a scoped model that names no organization.

Backs the ``assert_no_unbound_scoped_queries`` pytest fixture (root
``conftest.py``). Split out as a plain context manager, independent of pytest's
fixture protocol, so its own behavior -- catching an unscoped query, staying
silent when everything is scoped -- can be unit-tested directly rather than only
indirectly through pytest's fixture teardown machinery. See
``common/tests/test_organization_context_test_support.py``.

What it checks, and why that changed in Phase 2a
------------------------------------------------
Phase 0 of the vinta-django-orgs migration added an
``organization_context(...)`` binding to every Celery task and management
command, while the managers of the day ignored the binding entirely. The
tripwire's question then was "did anything run *unbound*?", because an unbound
call site was the thing Phase 2 would break.

Phase 2a landed that flip for ``calendar_integration``: ``objects`` scopes to the
bound organization and, with ``STRICT_ORGANIZATION_FILTER = True``, raises when
nothing is bound. The manager now enforces the "unbound" half by itself, and
loudly. What it cannot enforce is the deliberate escape hatch: ``unscoped()`` /
``original_manager`` / ``filter_by_organization(...)`` all bypass the ambient
context on purpose, and only the *last* of those names an organization. So the
question worth asking became:

    did a query touch an organization-scoped table with no organization bound
    **and** without naming one itself?

**This also closes the blind spot Phase 0 recorded.** That implementation
wrapped seven ``QuerySet`` methods (``__iter__``, ``get``, ``count``, ``exists``,
``update``, ``delete``, ``aggregate``), so anything reaching the database by
another route -- ``iterator()``, ``first()`` on a sliced queryset, ``len(qs)``,
``bulk_update``, ``in_bulk``, or a custom manager method that iterates something
it built itself -- slipped past. The guard now sits on
``SQLCompiler.execute_sql``, the single point every ``SELECT`` / ``UPDATE`` /
``DELETE`` / aggregate passes through, so there is no "other route" left. (Only
``INSERT`` is excluded: it has its own compiler, and an insert reads nothing.)
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from common.organization_context import get_current_organization


if TYPE_CHECKING:
    from django.db.models import Model
    from django.db.models.sql import Query


#: Compiler class name -> the statement it emits, for the violation message.
_STATEMENT_BY_COMPILER = {
    "SQLCompiler": "SELECT",
    "SQLUpdateCompiler": "UPDATE",
    "SQLDeleteCompiler": "DELETE",
    "SQLAggregateCompiler": "aggregate",
}


def _is_organization_scoped(model: type[Model] | None) -> bool:
    """Is ``model`` one of the models this project scopes per organization?

    Both bases are checked: ``calendar_integration`` moved to the package's
    ``SingleOrganizationModelMixin`` in Phase 2a, and ``audit`` / ``webhooks`` /
    ``public_api`` / ``organizations`` still sit on ``OrganizationModel`` until
    Phase 2b.
    """
    if model is None:
        return False

    from vinta_orgs.mixins import SingleOrganizationModelMixin

    from organizations.models import OrganizationModel

    return issubclass(model, SingleOrganizationModelMixin | OrganizationModel)


def _is_scoped_enough(query: Query, model: type[Model]) -> bool:
    """Is this query narrow enough that no ambient organization would add anything?

    Two ways to qualify, both read off the compiled SQL rather than off
    ``Query.where``. An organization-safe relation puts its organization
    condition in a join's ``ON`` clause, and a ``WHERE``-only check would miss it
    -- ``EventAttendance.objects.unscoped().filter(event=event)`` is
    organization-matched through the relation and must not be reported.

    1. **It names an organization.** ``filter_by_organization(...)`` and the
       relation joins above.
    2. **It addresses identified rows by primary key.** ``refresh_from_db()``,
       the ``UPDATE`` behind ``save()``, the delete collector and
       ``assert ... .filter(pk=x).exists()`` all name rows the caller already
       holds; a primary key identifies one row in the whole table, so there is no
       scoping decision left for an organization to make. Reporting these would
       flag ``instance.refresh_from_db()`` in every test that requests the
       fixture, which is a demand for a redundant filter rather than a finding.

    Everything before ``FROM`` is discarded first: the select list of a scoped
    model always names ``organization_id``, which would make this answer "yes"
    for every query.
    """
    try:
        sql = str(query)
    except Exception:  # noqa: BLE001 -- a query this cannot render is not evidence of a leak
        return True

    _, _, after_from = sql.partition(" FROM ")
    clauses = after_from or sql

    if "organization_id" in clauses:
        return True

    primary_key = f'"{model._meta.db_table}"."{model._meta.pk.column}"'
    return f"{primary_key} = " in clauses or f"{primary_key} IN (" in clauses


@contextlib.contextmanager
def assert_all_scoped_queries_are_bound() -> Iterator[list[str]]:
    """Watch every query for the duration of the block.

    Yields the (initially empty) list of violations found; entries are appended
    as they occur, so the caller can inspect it either during or after the
    ``with`` block. Restores the original method unconditionally on exit,
    including when the block raises.

    Does **not** itself assert anything -- see
    :func:`raise_if_unbound_scoped_queries_occurred` for that, kept separate so a
    caller can collect violations without failing (e.g. to assert on the exact
    list contents).
    """
    # Deferred: this module can be imported before ``django.setup()`` completes
    # (``conftest.py`` imports it from inside a fixture body, but pytest collects
    # ``conftest.py`` itself earlier than that), and importing anything that
    # touches Django's ORM at that point risks ``AppRegistryNotReady``.
    from django.db.models.sql.compiler import SQLCompiler

    unbound_calls: list[str] = []

    def _guard(original: Callable) -> Callable:
        def wrapper(self: SQLCompiler, *args: Any, **kwargs: Any) -> Any:
            query = self.query
            model = query.model
            if (
                model is not None
                and _is_organization_scoped(model)
                # ``not bound`` rather than ``bound is None``: a
                # ``SimpleLazyObject`` that *resolves* to ``None`` -- how a task
                # binds a stale or deleted organization id -- is still unbound.
                # ``LazyObject.__bool__`` proxies to the wrapped value, which is
                # correct here even though it would be too eager outside a
                # test-only guard.
                and not get_current_organization()
                and not _is_scoped_enough(query, model)
            ):
                statement = _STATEMENT_BY_COMPILER.get(type(self).__name__, "query")
                unbound_calls.append(f"{model.__name__} ({statement})")
            return original(self, *args, **kwargs)

        return wrapper

    original = SQLCompiler.execute_sql
    # Patched on the base class only: ``SQLUpdateCompiler`` /
    # ``SQLDeleteCompiler`` / ``SQLAggregateCompiler`` all reach it through
    # ``super()``, so one patch covers every statement that reads.
    SQLCompiler.execute_sql = _guard(original)  # type: ignore[method-assign]

    try:
        yield unbound_calls
    finally:
        SQLCompiler.execute_sql = original  # type: ignore[method-assign]


def raise_if_unbound_scoped_queries_occurred(unbound_calls: list[str]) -> None:
    """Raise ``AssertionError`` when ``unbound_calls`` (from the context manager
    above) is non-empty, naming every violation found.
    """
    assert not unbound_calls, (
        "Organization-scoped queries ran with no organization bound (via "
        f"common.organization_context) and without naming one: {unbound_calls}. "
        "Bind the organization before the call, mirroring the Phase 0 task and "
        "command bindings, or scope the query explicitly with "
        "filter_by_organization(...)."
    )
