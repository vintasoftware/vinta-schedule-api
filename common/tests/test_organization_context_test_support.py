"""Unit tests for ``common.organization_context_test_support``.

Exercises the tripwire directly (bypassing pytest's fixture protocol) so its
own catching/silent behavior is pinned independently of
``conftest.assert_no_unbound_scoped_queries``, which is a thin wrapper around
it.
"""

from __future__ import annotations

import pytest

from calendar_integration.models import Calendar
from common.organization_context import organization_context
from common.organization_context_test_support import (
    assert_all_scoped_queries_are_bound,
    raise_if_unbound_scoped_queries_occurred,
)
from organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Test Organization")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
    )


def test_records_nothing_when_every_scoped_query_runs_bound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        with organization_context(organization):
            list(Calendar.objects.filter_by_organization(organization.id))
            Calendar.objects.filter_by_organization(organization.id).count()
            Calendar.objects.filter_by_organization(organization.id).get(pk=calendar.pk)

    assert violations == []
    raise_if_unbound_scoped_queries_occurred(violations)  # must not raise


def test_records_a_violation_when_iter_runs_unbound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        # No `organization_context(...)` bound here on purpose.
        list(Calendar.objects.filter_by_organization(organization.id))

    assert violations == ["Calendar.objects.__iter__()"]
    with pytest.raises(AssertionError, match="Unbound organization-scoped queries"):
        raise_if_unbound_scoped_queries_occurred(violations)


def test_records_a_violation_when_get_runs_unbound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        Calendar.objects.filter_by_organization(organization.id).get(pk=calendar.pk)

    assert violations == ["Calendar.objects.get()"]


def test_records_a_violation_when_count_runs_unbound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        Calendar.objects.filter_by_organization(organization.id).count()

    assert violations == ["Calendar.objects.count()"]


def test_records_one_violation_per_unbound_call(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        list(Calendar.objects.filter_by_organization(organization.id))
        list(Calendar.objects.filter_by_organization(organization.id))

    assert violations == [
        "Calendar.objects.__iter__()",
        "Calendar.objects.__iter__()",
    ]


def test_restores_the_original_methods_on_exit(organization, calendar):
    from organizations.querysets import BaseOrganizationModelQuerySet

    originals = {
        name: getattr(BaseOrganizationModelQuerySet, name) for name in ("__iter__", "get", "count")
    }

    with assert_all_scoped_queries_are_bound():
        list(Calendar.objects.filter_by_organization(organization.id))

    for name, original in originals.items():
        assert getattr(BaseOrganizationModelQuerySet, name) is original


def test_restores_the_original_methods_even_when_the_block_raises(organization):
    from organizations.querysets import BaseOrganizationModelQuerySet

    originals = {
        name: getattr(BaseOrganizationModelQuerySet, name) for name in ("__iter__", "get", "count")
    }

    with pytest.raises(ValueError, match="boom"):
        with assert_all_scoped_queries_are_bound():
            raise ValueError("boom")

    for name, original in originals.items():
        assert getattr(BaseOrganizationModelQuerySet, name) is original


def test_explicit_organization_filter_is_orthogonal_to_the_binding_check(organization, calendar):
    """The tripwire checks the *context binding*, not the explicit filter the
    manager itself already requires. A query with the explicit filter but no
    binding is exactly the Phase 0 state -- passes today's manager contract,
    and is precisely what this tripwire exists to still flag.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        # Carries the explicit organization filter `BaseOrganizationModelQuerySet`
        # itself requires -- would not raise `ImproperlyConfigured` -- but still
        # has no `organization_context` bound.
        list(Calendar.objects.filter_by_organization(organization.id))

    assert violations


def test_pytest_fixture_is_wired_and_passes_for_properly_bound_queries(
    assert_no_unbound_scoped_queries, organization, calendar
):
    """Smoke test for the actual pytest fixture (``conftest
    .assert_no_unbound_scoped_queries``), requested the way a Phase 0 task test
    would use it: every scoped query in this test runs bound, so requesting
    the fixture must not fail the test.
    """
    with organization_context(organization):
        list(Calendar.objects.filter_by_organization(organization.id))

    assert assert_no_unbound_scoped_queries == []
