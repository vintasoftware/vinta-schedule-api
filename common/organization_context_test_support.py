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
``organizations.managers.BaseOrganizationModelManager`` /
``organizations.querysets.BaseOrganizationModelQuerySet`` keep enforcing only
their own, unrelated contract (an explicit ``organization`` filter on the
query itself, independent of any binding). Phase 2 flips that: the manager
swaps to implicit, context-bound scoping and (``STRICT_ORGANIZATION_FILTER =
True``) an unbound query raises instead of silently returning nothing. This
module lets a test assert that *future* contract now, ahead of the flip.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator

from common.organization_context import get_current_organization


#: The three query-execution entry points ``BaseOrganizationModelQuerySet
#: ._check_required_tenant_filter`` already instruments for the (unrelated)
#: explicit-filter check -- reused here so both guards fire from the same,
#: already-audited set of entry points rather than a second, possibly
#: incomplete one.
_GUARDED_METHOD_NAMES = ("__iter__", "get", "count")


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
    from organizations.querysets import BaseOrganizationModelQuerySet

    unbound_calls: list[str] = []

    def _guard(method_name: str, original: Callable) -> Callable:
        def wrapper(self, *args, **kwargs):
            if get_current_organization() is None:
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
