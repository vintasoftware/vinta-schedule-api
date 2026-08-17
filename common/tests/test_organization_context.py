"""Unit tests for ``common.organization_context``.

Covers three properties: binding, nesting, and restoration of the
*previous* binding (not a bare clear). These were the semantics the swap to the
package's state had to preserve, and they still are: package ``0.4.0`` replaced
the module-level functions with ``OrganizationState``, and this module keeps the same
seven names here as shims over one specialized instance of it. Every test below
goes through those shims, which is what makes them the contract test for the
shim layer as well as for the semantics.
"""

from __future__ import annotations

import threading

import pytest

from common.organization_context import (
    ProjectOrganizationState,
    clear_current_organization,
    get_current_organization,
    organization_context,
    organization_state,
    reset_current_organization,
    set_current_organization,
)
from organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def org_a() -> Organization:
    return Organization.objects.create(name="Org A")


@pytest.fixture
def org_b() -> Organization:
    return Organization.objects.create(name="Org B")


def test_get_current_organization_defaults_to_none():
    assert get_current_organization() is None


def test_set_current_organization_binds_the_organization(org_a):
    token = set_current_organization(org_a)
    try:
        assert get_current_organization() == org_a
    finally:
        reset_current_organization(token)


def test_reset_current_organization_restores_previous_value(org_a, org_b):
    outer_token = set_current_organization(org_a)
    inner_token = set_current_organization(org_b)

    assert get_current_organization() == org_b

    reset_current_organization(inner_token)
    assert get_current_organization() == org_a

    reset_current_organization(outer_token)
    assert get_current_organization() is None


def test_clear_current_organization_unbinds_without_error_when_nothing_bound():
    # Must not raise even though nothing was ever bound in this context.
    clear_current_organization()
    assert get_current_organization() is None


def test_clear_current_organization_unbinds_an_active_binding(org_a):
    set_current_organization(org_a)
    assert get_current_organization() == org_a

    clear_current_organization()
    assert get_current_organization() is None


def test_organization_context_as_context_manager_binds_and_restores(org_a):
    assert get_current_organization() is None

    with organization_context(org_a) as bound:
        assert bound == org_a
        assert get_current_organization() == org_a

    assert get_current_organization() is None


def test_organization_context_nesting_restores_previous_organization_not_none(org_a, org_b):
    """Nesting must restore the *previous* organization, never just clear it.

    The property that matters in practice: a fan-out task
    that binds once per organization inside a loop must land back on whatever
    was bound before the loop started (commonly ``None``, but not always --
    exercised here with an outer non-``None`` binding), not on a hardcoded
    clear.
    """
    with organization_context(org_a):
        assert get_current_organization() == org_a

        with organization_context(org_b):
            assert get_current_organization() == org_b

        # Restored to the *outer* organization, not cleared.
        assert get_current_organization() == org_a

    assert get_current_organization() is None


def test_organization_context_sequential_iterations_do_not_leak(org_a, org_b):
    """Per-iteration binding (fan-out over organizations) leaves no residue.

    Mirrors the pattern the task/command bindings use: one
    ``organization_context(...)`` block per organization in a loop, with
    nothing bound before or after the loop runs.
    """
    assert get_current_organization() is None

    for org in (org_a, org_b):
        with organization_context(org):
            assert get_current_organization() == org
        assert get_current_organization() is None

    assert get_current_organization() is None


def test_organization_context_restores_on_exception(org_a):
    assert get_current_organization() is None

    with pytest.raises(ValueError, match="boom"):
        with organization_context(org_a):
            assert get_current_organization() == org_a
            raise ValueError("boom")

    assert get_current_organization() is None


def test_organization_context_as_decorator(org_a):
    @organization_context(org_a)
    def read_bound_organization() -> Organization | None:
        return get_current_organization()

    assert get_current_organization() is None
    assert read_bound_organization() == org_a
    assert get_current_organization() is None


def test_organization_context_recursive_decorator_calls_do_not_share_token_stack(org_a, org_b):
    """Each invocation of a decorated function gets its own token stack.

    Guards ``organization_context._recreate_cm``: without it, a recursive call
    would push/pop against a stack shared with the outer call, and popping
    twice on unwind would restore the wrong organization.
    """

    @organization_context(org_b)
    def inner() -> Organization | None:
        return get_current_organization()

    @organization_context(org_a)
    def outer() -> tuple[Organization | None, Organization | None, Organization | None]:
        before = get_current_organization()
        during = inner()
        after = get_current_organization()
        return before, during, after

    before, during, after = outer()
    assert before == org_a
    assert during == org_b
    assert after == org_a
    assert get_current_organization() is None


def test_organization_context_is_isolated_per_thread(org_a):
    """A binding in one thread must not leak into another.

    ``contextvars.ContextVar`` is isolated per-context by construction, and a
    plain ``threading.Thread`` target runs in a fresh context copied at thread
    start -- this pins that behavior for this module specifically, since it is
    exactly what makes the binding safe to use from a Celery worker thread
    without crossing tenants.
    """
    observed: dict[str, Organization | None] = {}

    def other_thread_body() -> None:
        observed["before"] = get_current_organization()
        with organization_context(org_a):
            observed["during"] = get_current_organization()
        observed["after"] = get_current_organization()

    with organization_context(org_a):
        assert get_current_organization() == org_a

        thread = threading.Thread(target=other_thread_body)
        thread.start()
        thread.join()

        # The main thread's binding is unaffected by the other thread's work.
        assert get_current_organization() == org_a

    assert observed["before"] is None
    assert observed["during"] == org_a
    assert observed["after"] is None


class TestTheStateSpecialization:
    """The shims and the state object must be one binding, on *our* model.

    Package ``0.4.0`` types its state generically against
    ``AbstractOrganization``; a project binds its swapped model by declaring
    ``model_class`` once. Getting that declaration wrong is silent -- the
    package would resolve ``ORGANIZATION_MODEL`` at runtime either way and only
    the static type would be wrong -- so it is asserted rather than assumed.
    """

    def test_the_state_is_bound_to_this_projects_organization_model(self):
        assert ProjectOrganizationState.model_class is Organization
        assert organization_state.model is Organization

    def test_the_shims_and_the_state_object_share_one_binding(self, org_a):
        """No second contextvar hiding behind the function names.

        The shims exist so ~30 modules did not have to change when ``0.4.0``
        deleted the function API. That is only true while they delegate: a shim
        that grew its own storage would bind something the package's managers,
        ``SingleOrganizationModelMixin.save()`` and
        ``scope_queryset_to_current_organization`` never read.
        """
        with organization_context(org_a):
            assert organization_state.get() == org_a

        token = organization_state.set(org_a)
        try:
            assert get_current_organization() == org_a
        finally:
            reset_current_organization(token)

        assert get_current_organization() is None
