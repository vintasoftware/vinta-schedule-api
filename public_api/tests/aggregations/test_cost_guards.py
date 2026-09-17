"""The four guards that bound what one aggregate may cost.

An aggregate is the most expensive thing this API can be asked to do, and each
guard closes a hole the others leave open: a date range does not bound group
cardinality, a limit does not bound scan cost, and a timeout only converts a
slow query into a failed one. Each is asserted here on the condition it exists
for, and the pagination ones are asserted on the *message* -- they share it with
every other list field on this API, which is the point of sharing the check.
"""

import datetime

from django.db import OperationalError, connection, transaction

import pytest
from graphql import GraphQLError

import public_api.aggregations.fields as fields_module
from public_api.aggregations.errors import (
    QUERY_TIMEOUT_MESSAGE,
    AggregateQueryTimeoutError,
)
from public_api.aggregations.fields import aggregate_statement_timeout
from public_api.constants import MAX_AGGREGATE_RANGE, PublicAPIResources
from public_api.pagination import (
    LIMIT_OUT_OF_RANGE_MESSAGE,
    OFFSET_NEGATIVE_MESSAGE,
    validate_pagination,
)
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


PAGINATED_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!, $limit: Int!, $offset: Int!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
    limit: $limit
    offset: $offset
  ) {
    key { calendarId }
    count
  }
}
"""

RANGE_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
  ) {
    count
  }
}
"""

UNBOUNDED_QUERY = """
query EventAggregate {
  calendarEventAggregate(
    filter: {}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
  ) {
    count
  }
}
"""

UNKNOWN_TIMEZONE_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!, $tz: String!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
    timezone: $tz
  ) {
    count
  }
}
"""


@pytest.fixture
def event_token(organization):
    calendar = make_calendar(organization)
    make_event(
        organization,
        calendar,
        title="Anything",
        start=datetime.datetime(2026, 3, 10, 9, 0),
        minutes=30,
    )
    return org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])


def _errors(response) -> list[str]:
    return [error["message"] for error in response.json().get("errors", [])]


@pytest.mark.django_db
class TestLimitGuard:
    """Guard 1 -- `limit` is 1 to 100, the bound every list field applies."""

    @pytest.mark.parametrize("limit", [0, 101, 1000])
    def test_limit_outside_the_range_is_refused(self, event_token, limit):
        system_user, token, auth = event_token
        response = post_graphql(
            PAGINATED_QUERY,
            system_user,
            token,
            auth,
            {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
                "limit": limit,
                "offset": 0,
            },
        )

        assert response.status_code == 200
        assert _errors(response) == [LIMIT_OUT_OF_RANGE_MESSAGE]
        assert LIMIT_OUT_OF_RANGE_MESSAGE == "Limit must be between 1 and 100"

    def test_negative_offset_is_refused(self, event_token):
        system_user, token, auth = event_token
        response = post_graphql(
            PAGINATED_QUERY,
            system_user,
            token,
            auth,
            {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
                "limit": 10,
                "offset": -1,
            },
        )

        assert response.status_code == 200
        assert _errors(response) == [OFFSET_NEGATIVE_MESSAGE]

    @pytest.mark.parametrize("limit", [1, 100])
    def test_the_boundary_values_are_accepted(self, event_token, limit):
        system_user, token, auth = event_token
        response = post_graphql(
            PAGINATED_QUERY,
            system_user,
            token,
            auth,
            {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
                "limit": limit,
                "offset": 0,
            },
        )

        assert response.status_code == 200
        assert _errors(response) == []
        assert len(response.json()["data"]["calendarEventAggregate"]) == 1

    def test_the_bound_is_the_shared_one(self):
        """The aggregate fields and the list fields check the same function."""
        with pytest.raises(GraphQLError, match=LIMIT_OUT_OF_RANGE_MESSAGE):
            validate_pagination(0, 101)
        with pytest.raises(GraphQLError, match=OFFSET_NEGATIVE_MESSAGE):
            validate_pagination(-1, 10)


@pytest.mark.django_db
class TestDateRangeGuard:
    """Guard 2 -- the mandatory bounded range the filter inputs carry."""

    def test_an_over_long_range_is_refused(self, event_token):
        system_user, token, auth = event_token
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = start + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1)

        response = post_graphql(
            RANGE_QUERY,
            system_user,
            token,
            auth,
            {"start": start.isoformat(), "end": end.isoformat()},
        )

        assert response.status_code == 200
        errors = _errors(response)
        assert len(errors) == 1
        assert "exceeds maximum" in errors[0]
        assert str(MAX_AGGREGATE_RANGE.days) in errors[0]

    def test_a_backwards_range_is_refused(self, event_token):
        system_user, token, auth = event_token
        response = post_graphql(
            RANGE_QUERY,
            system_user,
            token,
            auth,
            {"start": WINDOW_END.isoformat(), "end": WINDOW_START.isoformat()},
        )

        assert response.status_code == 200
        assert _errors(response) == ["Invalid time range."]

    def test_a_query_without_bounds_fails_validation(self, event_token):
        """`startDatetime` / `endDatetime` are non-null on the input type.

        So an unbounded range never reaches a resolver: GraphQL refuses the
        document, which is a guard the server cannot forget to apply.
        """
        system_user, token, auth = event_token
        response = post_graphql(UNBOUNDED_QUERY, system_user, token, auth, {})

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("data") is None, "an unbounded aggregate was executed"
        errors = _errors(response)
        assert errors
        assert any("startDatetime" in message for message in errors), errors
        assert any("endDatetime" in message for message in errors), errors

    def test_an_unknown_timezone_is_refused_without_echoing_it(self, event_token):
        """The bucketing clock has to be a real one, and the name is not repeated."""
        system_user, token, auth = event_token
        response = post_graphql(
            UNKNOWN_TIMEZONE_QUERY,
            system_user,
            token,
            auth,
            {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
                "tz": "Mars/Olympus_Mons",
            },
        )

        assert response.status_code == 200
        errors = _errors(response)
        assert errors == ["Unknown timezone"]
        assert "Mars" not in response.content.decode()


@pytest.mark.django_db
class TestDeterministicOrderGuard:
    """Guard 3 -- paging a grouped result returns each group exactly once."""

    def test_paging_partitions_the_groups(self, organization):
        calendars = [make_calendar(organization, name=f"C{index}") for index in range(5)]
        base = datetime.datetime(2026, 3, 10, 9, 0)
        for calendar in calendars:
            make_event(organization, calendar, title="E", start=base, minutes=30)

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        seen: list[int] = []
        for offset in (0, 2, 4):
            response = post_graphql(
                PAGINATED_QUERY,
                system_user,
                token,
                auth,
                {
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                    "limit": 2,
                    "offset": offset,
                },
            )
            assert _errors(response) == []
            seen.extend(
                row["key"]["calendarId"]
                for row in response.json()["data"]["calendarEventAggregate"]
            )

        # Every group once, none twice: only possible with a stable ORDER BY.
        assert sorted(seen) == sorted(calendar.id for calendar in calendars)
        assert len(seen) == len(set(seen))


class _QueryCanceledError(Exception):
    """Stands in for psycopg's `QueryCanceled`, which carries this SQLSTATE."""

    sqlstate = "57014"


class _UnrelatedDatabaseError(Exception):
    """A database failure that is not the timeout firing."""

    sqlstate = "08006"


@pytest.mark.django_db
class TestStatementTimeoutGuard:
    """Guard 4 -- a per-query statement timeout, around aggregate execution only."""

    def test_the_timeout_cancels_a_query_that_overruns_it(self):
        """A statement over the budget really is cancelled, with SQLSTATE 57014.

        Its own savepoint, because a cancelled statement aborts its
        transaction and the context manager still has a `SET` to run on the
        way out.
        """
        with pytest.raises(OperationalError) as exc_info:
            with aggregate_statement_timeout(milliseconds=50):
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SELECT pg_sleep(2)")

        assert getattr(exc_info.value.__cause__, "sqlstate", None) == "57014"

    def test_the_previous_timeout_is_restored_on_the_way_out(self):
        """The budget applies to the aggregate, not to the rest of the request."""

        def current_timeout() -> str:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                return cursor.fetchone()[0]

        before = current_timeout()
        with aggregate_statement_timeout(milliseconds=1234):
            inside = current_timeout()
        after = current_timeout()

        assert inside == "1234ms"
        assert inside != before
        assert after == before

    def test_a_cancelled_aggregate_reports_the_documented_message(self, monkeypatch):
        """SQLSTATE 57014 becomes the partner-facing timeout error.

        The cancel is injected rather than provoked: a grouped scan over a
        handful of test rows finishes inside any timeout Postgres will accept,
        so provoking one for real would be flaky. What is under test is the
        mapping, not Postgres' ability to cancel -- which the case above
        already asserts against a real statement.
        """

        def raise_canceled(*_args, **_kwargs):
            raise OperationalError(
                "canceling statement due to statement timeout"
            ) from _QueryCanceledError()

        monkeypatch.setattr(fields_module, "execute_plan", raise_canceled)

        with pytest.raises(AggregateQueryTimeoutError) as exc_info:
            fields_module._execute_within_budget(None, None)  # type: ignore[arg-type]

        assert exc_info.value.message == QUERY_TIMEOUT_MESSAGE

    def test_a_database_error_that_is_not_a_cancel_is_not_disguised(self, monkeypatch):
        """Only a cancel maps to the timeout message; everything else propagates."""

        def raise_other(*_args, **_kwargs):
            raise OperationalError("connection reset") from _UnrelatedDatabaseError()

        monkeypatch.setattr(fields_module, "execute_plan", raise_other)

        with pytest.raises(OperationalError, match="connection reset"):
            fields_module._execute_within_budget(None, None)  # type: ignore[arg-type]
