"""The cost guards on every aggregate field, exercised through the schema.

* ``limit`` outside 1-100 raises the exact wording
  ``public_api.queries._slice_qs`` uses for every other paginated field.
* a filter range wider than ``MAX_AGGREGATE_RANGE`` raises (Phase 1's guard,
  reused rather than rebuilt).
* a query that omits the mandatory bounds fails GraphQL validation before a
  resolver ever runs, because ``startDatetime`` / ``endDatetime`` are
  non-null on the filter input type.
* the per-query Postgres statement timeout, the fourth guard, both fires on a
  genuinely slow query and leaves the connection usable afterward.
"""

import datetime
import uuid

from django.db.models.expressions import RawSQL
from django.db.utils import OperationalError

import pytest
from model_bakery import baker
from psycopg.errors import QueryCanceled
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations import fields as fields_module
from public_api.aggregations.errors import AggregateTimeoutError
from public_api.constants import MAX_AGGREGATE_RANGE, PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


CALENDAR_EVENT_AGGREGATE_QUERY = """
query CalendarEventAggregate(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
    $limit: Int = 100
    $offset: Int = 0
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: $groupBy
        timezone: "UTC"
        limit: $limit
        offset: $offset
    ) {
        count
    }
}
"""

# No `limit` / `offset` variables at all, so the field falls back to its
# schema defaults (100 / 0) -- used by the unbounded-range test, which is
# about the filter, not the slice.
CALENDAR_EVENT_AGGREGATE_QUERY_NO_SLICE = """
query CalendarEventAggregate(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
) {
    calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
        count
    }
}
"""

CALENDAR_EVENT_AGGREGATE_QUERY_NO_BOUNDS = """
query CalendarEventAggregate($groupBy: [CalendarEventGroupByInput!]!) {
    calendarEventAggregate(
        filter: { calendarId: 1 }
        groupBy: $groupBy
        timezone: "UTC"
    ) {
        count
    }
}
"""


@pytest.mark.django_db
class TestAggregateCostGuards:
    def setup_method(self):
        self.client = APIClient()

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _make_calendar(self, org: Organization) -> Calendar:
        unique = uuid.uuid4().hex[:8]
        return Calendar.objects.create(
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

    def _token(self, org: Organization):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
        )
        return system_user, token, auth_service

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _variables(self, *, limit=None, offset=None, days: int = 5) -> dict:
        """Variables for ``CALENDAR_EVENT_AGGREGATE_QUERY``.

        ``limit`` / ``offset`` are omitted entirely (not sent as explicit
        ``null``) when not overridden, so the operation's own default value
        applies -- the field's GraphQL argument type is non-null (``Int!``
        with a server-side default), and an explicit ``null`` would fail
        GraphQL argument validation before ever reaching the resolver.
        """
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(days=days)
        variables: dict = {
            "filter": {
                "startDatetime": start.isoformat(),
                "endDatetime": end.isoformat(),
                "calendarId": None,
            },
            "groupBy": [{"field": "CALENDAR_ID"}],
        }
        if limit is not None:
            variables["limit"] = limit
        if offset is not None:
            variables["offset"] = offset
        return variables

    @pytest.mark.parametrize("limit", [0, 101])
    def test_limit_outside_the_band_is_rejected(self, limit):
        org = self._org()
        self._make_calendar(org)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY,
            system_user,
            token,
            auth,
            self._variables(limit=limit),
        )

        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, f"expected a limit error for limit={limit}"
        assert any(
            "Limit must be between 1 and 100" in (err.get("message") or "") for err in errors
        )

    def test_negative_offset_is_rejected(self):
        org = self._org()
        self._make_calendar(org)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY,
            system_user,
            token,
            auth,
            self._variables(offset=-1),
        )

        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, "expected an offset error for offset=-1"
        assert any("Offset must be non-negative" in (err.get("message") or "") for err in errors)

    def test_an_over_long_range_is_rejected(self):
        org = self._org()
        self._make_calendar(org)
        system_user, token, auth = self._token(org)

        too_many_days = MAX_AGGREGATE_RANGE.days + 1
        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY_NO_SLICE,
            system_user,
            token,
            auth,
            self._variables(days=too_many_days),
        )

        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, "expected a range error for an over-long range"
        assert any("too large" in (err.get("message") or "").lower() for err in errors)

    def test_a_query_without_bounds_fails_validation(self):
        """`startDatetime` / `endDatetime` are non-null on the filter input,
        so an aggregate query that omits them never reaches a resolver --
        GraphQL itself refuses it."""
        org = self._org()
        self._make_calendar(org)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_AGGREGATE_QUERY_NO_BOUNDS,
            system_user,
            token,
            auth,
            {"groupBy": [{"field": "CALENDAR_ID"}]},
        )

        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, "expected a validation error for a filter missing its mandatory bounds"
        assert data.get("data") in (None, {})


class TestIsStatementTimeout:
    """``_is_statement_timeout`` tells a real timeout cancellation apart from
    any other ``OperationalError`` -- it is the switch between raising the
    caller-visible ``AggregateTimeoutError`` and re-raising verbatim."""

    def test_true_when_the_cause_is_query_canceled(self):
        exc = OperationalError("canceling statement due to statement timeout")
        exc.__cause__ = QueryCanceled("canceling statement due to statement timeout")
        assert fields_module._is_statement_timeout(exc) is True

    def test_false_for_an_unrelated_operational_error(self):
        exc = OperationalError("connection already closed")
        exc.__cause__ = ValueError("not a cancellation")
        assert fields_module._is_statement_timeout(exc) is False

    def test_false_with_no_cause_at_all(self):
        exc = OperationalError("some other failure")
        assert fields_module._is_statement_timeout(exc) is False


@pytest.mark.django_db
class TestExecuteWithStatementTimeout:
    """The fourth cost guard, exercised against a real Postgres connection:
    a query that runs longer than the budget is cancelled and surfaces as
    ``AggregateTimeoutError`` rather than the raw driver exception, and the
    connection is left usable for whatever the request does next."""

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def test_a_slow_query_raises_aggregate_timeout_error(self, monkeypatch):
        monkeypatch.setattr(fields_module, "AGGREGATE_STATEMENT_TIMEOUT_MS", 50)
        org = self._org()
        # `pg_sleep` is a per-row function call: with no matching row to
        # evaluate it against, the query would return instantly without ever
        # sleeping, regardless of the timeout.
        calendar = Calendar.objects.create(
            organization=org,
            name="Slow Calendar",
            external_id=f"cal-{uuid.uuid4().hex[:8]}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

        with organization_context(org):
            # `_delay` must be named in `values()` too -- an annotation added
            # after `.annotate()` but not selected by a later `.values()` is
            # dropped from the query entirely and would never actually sleep.
            slow_queryset = Calendar.objects.annotate(_delay=RawSQL("pg_sleep(1)", [])).values(
                "id", "_delay"
            )

            with pytest.raises(AggregateTimeoutError):
                fields_module._execute_with_statement_timeout(slow_queryset)

            # The savepoint rollback on the timeout path undid `SET LOCAL`,
            # so the same connection runs a normal query right after.
            assert list(Calendar.objects.values("id")) == [{"id": calendar.id}]

    def test_a_fast_query_returns_rows_and_resets_the_timeout(self, monkeypatch):
        # Deliberately tight: small enough that a leaked `SET LOCAL` would
        # cancel the slower query run directly afterward, below.
        monkeypatch.setattr(fields_module, "AGGREGATE_STATEMENT_TIMEOUT_MS", 50)
        org = self._org()
        calendar = Calendar.objects.create(
            organization=org,
            name="Fast Calendar",
            external_id=f"cal-{uuid.uuid4().hex[:8]}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

        with organization_context(org):
            rows = fields_module._execute_with_statement_timeout(Calendar.objects.values("id"))
            assert rows == [{"id": calendar.id}]

            # Run outside the helper, well past the 50ms budget just used.
            # If the explicit reset on the success path had not undone the
            # `SET LOCAL`, this would raise instead of completing.
            slow_queryset = Calendar.objects.annotate(_delay=RawSQL("pg_sleep(0.2)", [])).values(
                "id", "_delay"
            )
            assert [row["id"] for row in slow_queryset] == [calendar.id]
