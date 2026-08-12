"""Unit tests for ``common.organization_context_test_support``.

Exercises the tripwire directly (bypassing pytest's fixture protocol) so its own
catching/silent behavior is pinned independently of
``conftest.assert_no_unbound_scoped_queries``, which is a thin wrapper around it.

The contract these pin changed in Phase 2a of the vinta-django-orgs migration --
see that module's docstring. In short: a query that *names* its organization is
fine unbound (``filter_by_organization(...)`` is the sanctioned way to reach
outside the ambient context), and what is reported is a query on a scoped table
that neither binds nor names one.
"""

from __future__ import annotations

from django.db.models import Count
from django.utils.functional import SimpleLazyObject

import pytest

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarWebhookEvent
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


@pytest.fixture
def calendar_webhook_event(organization: Organization) -> CalendarWebhookEvent:
    return CalendarWebhookEvent.objects.create(
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        event_type="created",
        external_calendar_id="cal-1",
        raw_payload={},
    )


def test_records_nothing_when_every_scoped_query_runs_bound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        with organization_context(organization):
            list(Calendar.objects.all())
            Calendar.objects.count()
            Calendar.objects.get(pk=calendar.pk)

    assert violations == []
    raise_if_unbound_scoped_queries_occurred(violations)  # must not raise


def test_records_nothing_when_the_query_names_its_organization_unbound(organization, calendar):
    """``filter_by_organization(...)`` is the sanctioned way to reach outside the
    ambient context: it starts from the unscoped queryset and says which
    organization it means, so it is not a violation even with nothing bound.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        list(Calendar.objects.filter_by_organization(organization.id))

    assert violations == []


def test_records_a_violation_when_an_unscoped_read_runs_unbound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        # No `organization_context(...)`, and `original_manager` does not scope.
        list(Calendar.original_manager.all())

    assert violations == ["Calendar (SELECT)"]
    with pytest.raises(AssertionError, match="Organization-scoped queries ran"):
        raise_if_unbound_scoped_queries_occurred(violations)


def test_records_a_violation_when_bound_to_a_lazy_object_that_resolves_to_none(
    organization, calendar
):
    """A ``SimpleLazyObject`` that resolves to ``None`` -- exactly how Phase 0's
    Celery task bindings bind a stale/deleted organization id (see
    ``webhooks/tasks.py`` / ``audit/tasks.py``) -- must still be reported as
    unbound. It is *not* ``is None`` (it is a ``SimpleLazyObject`` instance), so a
    naive ``get_current_organization() is None`` check would miss it entirely.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        with organization_context(SimpleLazyObject(lambda: None)):
            list(Calendar.original_manager.all())

    assert violations == ["Calendar (SELECT)"]


def test_records_a_violation_for_a_read_that_no_queryset_method_wraps(organization, calendar):
    """The Phase 0 blind spot, closed.

    ``iterator()`` reaches the database without going through ``__iter__``,
    ``get``, ``count``, ``exists``, ``update``, ``delete`` or ``aggregate`` -- the
    seven methods the previous implementation wrapped -- so it used to slip past
    entirely. Guarding ``SQLCompiler.execute_sql`` leaves no such route.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        list(Calendar.original_manager.all().iterator())

    assert violations == ["Calendar (SELECT)"]


def test_records_a_violation_for_a_custom_manager_method_that_iterates_itself(
    organization, calendar
):
    """The other half of the same blind spot: a manager method that builds a
    queryset and consumes it through something other than the wrapped names.
    ``values_list(...).first()`` slices and reads ``_result_cache`` directly.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        Calendar.original_manager.all().values_list("id", flat=True).first()

    assert violations == ["Calendar (SELECT)"]


def test_records_a_violation_when_update_runs_unbound(organization, calendar_webhook_event):
    with assert_all_scoped_queries_are_bound() as violations:
        CalendarWebhookEvent.original_manager.all().update(event_type="updated")

    assert violations == ["CalendarWebhookEvent (UPDATE)"]


def test_records_a_violation_when_delete_runs_unbound(organization, calendar_webhook_event):
    with assert_all_scoped_queries_are_bound() as violations:
        CalendarWebhookEvent.original_manager.filter(pk=calendar_webhook_event.pk).delete()

    # Django's delete collector fast-paths a single-table, no-cascade delete into
    # one statement, so exactly one violation is reported.
    assert violations == ["CalendarWebhookEvent (DELETE)"]


def test_records_a_violation_when_aggregate_runs_unbound(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        Calendar.original_manager.all().aggregate(total=Count("id"))

    assert violations == ["Calendar (SELECT)"]


def test_records_one_violation_per_unbound_call(organization, calendar):
    with assert_all_scoped_queries_are_bound() as violations:
        list(Calendar.original_manager.all())
        list(Calendar.original_manager.all())

    assert violations == ["Calendar (SELECT)", "Calendar (SELECT)"]


def test_does_not_report_a_query_organization_matched_through_a_safe_relation(
    organization, calendar
):
    """An organization-safe relation carries its organization condition in the
    join's ``ON`` clause, not in ``WHERE``. Filtering by a target *instance* is
    therefore already organization-matched, and reporting it would push callers
    towards a redundant second filter.
    """
    from calendar_integration.models import CalendarSync

    with assert_all_scoped_queries_are_bound() as violations:
        list(CalendarSync.objects.unscoped().filter(calendar=calendar))

    assert violations == []


def test_restores_the_original_method_on_exit(organization, calendar):
    from django.db.models.sql.compiler import SQLCompiler

    original = SQLCompiler.execute_sql

    with assert_all_scoped_queries_are_bound():
        list(Calendar.original_manager.all())

    assert SQLCompiler.execute_sql is original


def test_restores_the_original_method_even_when_the_block_raises(organization):
    from django.db.models.sql.compiler import SQLCompiler

    original = SQLCompiler.execute_sql

    with pytest.raises(ValueError, match="boom"):
        with assert_all_scoped_queries_are_bound():
            raise ValueError("boom")

    assert SQLCompiler.execute_sql is original


def test_ignores_models_that_are_not_organization_scoped(organization):
    with assert_all_scoped_queries_are_bound() as violations:
        list(Organization.objects.all())

    assert violations == []


def test_pytest_fixture_is_wired_and_passes_for_properly_bound_queries(
    assert_no_unbound_scoped_queries, organization, calendar
):
    """Smoke test for the actual pytest fixture (``conftest
    .assert_no_unbound_scoped_queries``), requested the way a Phase 0 task test
    would use it: every scoped query in this test runs bound, so requesting the
    fixture must not fail the test.
    """
    with organization_context(organization):
        list(Calendar.objects.all())

    assert assert_no_unbound_scoped_queries == []
