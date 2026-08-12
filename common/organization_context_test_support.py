"""Test-only tripwire: catch an ``OrganizationModel``-scoped query that runs unbound.

Backs the ``assert_no_unbound_scoped_queries`` pytest fixture (root
``conftest.py``). Split out as a plain context manager, independent of
pytest's fixture protocol, so its own behavior -- catching an unbound query,
staying silent when everything is bound -- can be unit-tested directly rather
than only indirectly through pytest's fixture teardown machinery. See
``common/tests/test_organization_context_test_support.py``.

Context: Phase 0 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
threads an explicit ``organization_context(...)`` binding through every Celery
task and management command that touches a scoped model, while
``tenancy.managers.BaseOrganizationModelManager`` /
``tenancy.querysets.BaseOrganizationModelQuerySet`` keep enforcing only
their own, unrelated contract (an explicit ``organization`` filter on the
query itself, independent of any binding). Phase 2 flips that: the manager
swaps to implicit, context-bound scoping and (``STRICT_ORGANIZATION_FILTER =
True``) an unbound query raises instead of silently returning nothing. This
module lets a test assert that *future* contract now, ahead of the flip.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from common.organization_context import get_current_organization


#: ``__iter__``, ``get``, and ``count`` are the three query-execution entry
#: points ``BaseOrganizationModelQuerySet._check_required_tenant_filter``
#: already instruments for the (unrelated) explicit-filter check -- reused
#: here so both guards fire from the same, already-audited set of entry
#: points rather than a second, possibly incomplete one. ``exists``,
#: ``update``, ``delete``, and ``aggregate`` are added on top of that shared
#: set: none of the three above run for them, so an unbound
#: ``Model.objects.exists()`` / ``.update()`` / ``.delete()`` /
#: ``.aggregate()`` would otherwise pass this tripwire silently even though
#: Phase 2's implicit scoping applies to them too. Known blind spot left for
#: Phase 2a to close: any *other* queryset-execution method (e.g. a custom
#: manager method that calls ``.values_list(...)`` and iterates it without
#: going through one of the eight names here) is still unguarded -- this
#: tuple only covers ``QuerySet``'s own execution entry points, not every
#: possible path to one.
_GUARDED_METHOD_NAMES = ("__iter__", "get", "count", "exists", "update", "delete", "aggregate")


@contextlib.contextmanager
def assert_all_scoped_queries_are_bound() -> Iterator[list[str]]:
    """Monkeypatch ``BaseOrganizationModelQuerySet`` for the duration of the block.

    Yields the (initially empty) list of violations found; append happens as
    they occur, so the caller can inspect it either during or after the
    ``with`` block. Restores the original methods unconditionally on exit,
    including when the block raises.

    Does **not** itself assert anything -- see
    :func:`raise_if_unbound_scoped_queries_occurred` for that, kept separate
    so a caller can choose to collect violations without failing (e.g. to
    assert on the exact list contents).
    """
    # Deferred: not to avoid a circular import (verified there is none -- this
    # module and ``tenancy.querysets`` do not import each other), but
    # because this module can itself be imported before ``django.setup()``
    # completes (``conftest.py`` imports it from inside a fixture body, but
    # pytest collects ``conftest.py`` itself earlier than that), and
    # importing anything that touches Django's ORM at that point risks
    # ``AppRegistryNotReady`` -- mirrors the same deferral in
    # ``common.organization_context._get_organization_by_slug``.
    from tenancy.querysets import BaseOrganizationModelQuerySet

    unbound_calls: list[str] = []

    def _guard(method_name: str, original: Callable) -> Callable:
        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            bound = get_current_organization()
            if not bound:
                # ``not bound`` (rather than ``bound is None``) so a
                # ``SimpleLazyObject`` that *resolves* to ``None`` -- how
                # Phase 0's Celery task bindings bind a stale/deleted
                # organization id -- is still reported as unbound.
                # ``LazyObject.__bool__`` proxies through to the wrapped
                # value (forcing resolution), so this is correct here even
                # though it would be too eager a check outside a test-only
                # guard.
                unbound_calls.append(f"{self.model.__name__}.objects.{method_name}()")
            return original(self, *args, **kwargs)

        return wrapper

    originals = {
        name: getattr(BaseOrganizationModelQuerySet, name) for name in _GUARDED_METHOD_NAMES
    }
    for name, original in originals.items():
        setattr(BaseOrganizationModelQuerySet, name, _guard(name, original))

    try:
        yield unbound_calls
    finally:
        for name, original in originals.items():
            setattr(BaseOrganizationModelQuerySet, name, original)


def raise_if_unbound_scoped_queries_occurred(unbound_calls: list[str]) -> None:
    """Raise ``AssertionError`` when ``unbound_calls`` (from the context manager
    above) is non-empty, naming every violation found.
    """
    assert not unbound_calls, (
        "Unbound organization-scoped queries executed (no organization was bound "
        f"via common.organization_context at the time): {unbound_calls}. Phase 2's "
        "implicit, strict scoping will raise here -- bind the organization before "
        "the call, mirroring the Phase 0 task/command bindings."
    )
