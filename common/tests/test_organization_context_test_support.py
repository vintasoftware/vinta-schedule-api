"""Unit tests for ``common.organization_context_test_support``.

Exercises the tripwire directly (bypassing pytest's fixture protocol) so its own
catching/silent behavior is pinned independently of
``conftest.assert_no_unbound_scoped_queries``, which is a thin wrapper around it.

The contract these pin changed -- see that module's docstring. In short: a
query that *names* its organization is
fine unbound (``filter_by_organization(...)`` is the sanctioned way to reach
outside the ambient context), and what is reported is a query on a scoped table
that neither binds nor names one.
"""

from __future__ import annotations

from django.db.models import Count
from django.db.models.sql.compiler import SQLCompiler
from django.utils.functional import SimpleLazyObject

import pytest

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarSync, CalendarWebhookEvent
from common.organization_context import organization_context
from common.organization_context_test_support import (
    _clauses_of,
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
    """A ``SimpleLazyObject`` that resolves to ``None`` -- exactly how
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
    """A blind spot in the previous implementation, closed.

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
        CalendarWebhookEvent.original_manager.all().delete()

    # Django's delete collector fast-paths a single-table, no-cascade delete into
    # one statement, so exactly one violation is reported.
    assert violations == ["CalendarWebhookEvent (DELETE)"]


def test_records_a_violation_for_a_read_addressed_by_primary_key(organization, calendar):
    """A primary key does **not** excuse a ``SELECT``.

    ``Calendar.objects.unscoped().get(pk=<id from the URL>)`` is the shape of an
    IDOR, and "a primary key identifies one row in the whole table" is not a
    defence: the scoping decision is precisely *whose* row it is, and the read is
    what makes it. The queryset-method guard reported this shape, so
    exempting it would have been a coverage regression against the one defect
    class the tripwire exists for.

    ``refresh_from_db()`` is reported for the same reason and by design -- it
    re-reads a row without saying which organization may see it.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        Calendar.original_manager.filter(pk=calendar.pk).exists()

    assert violations == ["Calendar (SELECT)"]

    with assert_all_scoped_queries_are_bound() as refresh_violations:
        calendar.refresh_from_db()

    assert refresh_violations == ["Calendar (SELECT)"]


def test_records_nothing_for_the_update_and_delete_behind_an_instance(
    organization, calendar, calendar_webhook_event
):
    """The two statements Django itself emits for a row the caller already holds:
    the ``UPDATE`` behind ``save()`` and the ``DELETE`` the collector issues once
    it has gathered the rows. Neither has a scoping decision left in it -- the row
    was fetched earlier, and the statement only writes back what the instance
    already says. Reporting these would flag ``instance.save()`` in every test
    that requests the fixture.
    """
    with assert_all_scoped_queries_are_bound() as violations:
        calendar.name = "Renamed"
        calendar.save(update_fields=["name"])

    assert violations == []

    with assert_all_scoped_queries_are_bound() as delete_violations:
        # ``CalendarWebhookEvent`` rather than ``Calendar``: it cascades to
        # nothing, so Django takes the fast path and the whole delete is the one
        # statement under test. A cascading delete *reads* its children first,
        # and those reads are reported -- correctly, they are unscoped ``SELECT``s.
        CalendarWebhookEvent.original_manager.filter(pk=calendar_webhook_event.pk).delete()

    assert delete_violations == []


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
    with assert_all_scoped_queries_are_bound() as violations:
        list(CalendarSync.objects.unscoped().filter(calendar=calendar))

    assert violations == []


def test_restores_the_original_method_on_exit(organization, calendar):
    original = SQLCompiler.execute_sql

    with assert_all_scoped_queries_are_bound():
        list(Calendar.original_manager.all())

    assert SQLCompiler.execute_sql is original


def test_restores_the_original_method_even_when_the_block_raises(organization):
    original = SQLCompiler.execute_sql

    with pytest.raises(ValueError, match="boom"):
        with assert_all_scoped_queries_are_bound():
            raise ValueError("boom")

    assert SQLCompiler.execute_sql is original


class TestTheSelectListIsDiscardedAtTheOutermostFrom:
    """``_clauses_of`` is what stops the select list answering the question.

    Every scoped model names ``organization_id`` in its select list, so the
    search has to start at the query's *own* ``FROM``. Both naive splits get one
    of these two shapes wrong, which is why it counts brackets: ``partition``
    stops at a subquery in the select list and hands the outer select list back;
    ``rpartition`` stops at a subquery in the ``WHERE`` and throws the outer
    ``WHERE`` away.

    Unit-tested on strings rather than through the ORM because which of these
    two shapes a given queryset compiles to is Django's choice about select-list
    ordering, not a property of this project.
    """

    def test_a_subquery_in_the_select_list_does_not_drag_the_select_list_back_in(self):
        sql = (
            'SELECT "t"."id", (SELECT U0."x" FROM "u" U0) AS "a", "t"."organization_id" '
            'FROM "t" WHERE "t"."name" = \'x\''
        )

        assert _clauses_of(sql) == ' FROM "t" WHERE "t"."name" = \'x\''
        assert "organization_id" not in _clauses_of(sql)

    def test_a_subquery_in_the_where_clause_does_not_take_the_where_clause_with_it(self):
        sql = (
            'SELECT "t"."id" FROM "t" '
            'WHERE ("t"."organization_id" = 1 AND "t"."x" IN (SELECT U0."id" FROM "u" U0))'
        )

        assert "organization_id" in _clauses_of(sql)

    def test_a_statement_with_no_from_is_returned_whole(self):
        sql = 'UPDATE "t" SET "name" = \'x\' WHERE "t"."organization_id" = 1'

        assert _clauses_of(sql) == sql


def test_ignores_models_that_are_not_organization_scoped(organization):
    with assert_all_scoped_queries_are_bound() as violations:
        list(Organization.objects.all())

    assert violations == []


def test_pytest_fixture_is_wired_and_passes_for_properly_bound_queries(
    assert_no_unbound_scoped_queries, organization, calendar
):
    """Smoke test for the actual pytest fixture (``conftest
    .assert_no_unbound_scoped_queries``), requested the way a task test
    would use it: every scoped query in this test runs bound, so requesting the
    fixture must not fail the test.
    """
    with organization_context(organization):
        list(Calendar.objects.all())

    assert assert_no_unbound_scoped_queries == []
