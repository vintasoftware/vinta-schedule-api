"""Unit tests for the aggregate query audit payload."""

import json
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from public_api.aggregations.audit import record_aggregate_query
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    FilterBounds,
    MetricSpec,
    TemporalGranularity,
)


def test_audit_payload_contains_metrics_and_dimensions_but_no_values():
    """The audit record captures operation names and field paths, but no values."""
    plan = AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=(
            DimensionSpec(
                alias="event_date",
                field_path="start_time",
                granularity=TemporalGranularity.DAY,
                tzinfo=ZoneInfo("America/New_York"),
            ),
        ),
        metrics=(
            MetricSpec(
                alias="event_count",
                field_path="id",
                op=AggregateOp.COUNT,
            ),
            MetricSpec(
                alias="concatenated_titles",
                field_path="title",
                op=AggregateOp.CONCAT,
                options={"separator": "; "},
            ),
        ),
        filter_bounds=FilterBounds(
            predicates={"calendar_ids": (1, 2, 3)},
        ),
        limit=10,
        offset=0,
    )

    mock_audit_service = MagicMock()
    mock_system_user = MagicMock()
    organization_id = 42

    with patch.object(
        mock_audit_service,
        "actor_from_system_user",
        return_value={"actor": "test"},
    ):
        with patch.object(
            mock_audit_service,
            "scope_from_organization_id",
            return_value={"scope": "test"},
        ):
            record_aggregate_query(
                plan=plan,
                organization_id=organization_id,
                system_user=mock_system_user,
                row_count=3,
                audit_service=mock_audit_service,
            )

    # Assert that record was called
    mock_audit_service.record.assert_called_once()
    call_kwargs = mock_audit_service.record.call_args[1]

    # Get the diff payload which contains the metadata
    diff = call_kwargs["diff"]
    subject = call_kwargs["subject"]

    # Verify subject is correctly formed
    assert subject.subject_id == "calendar_event"
    assert subject.subject_type == "public_api.aggregations.aggregate_query"

    # Verify row count, limit, offset in diff
    assert diff["row_count"] == 3
    assert diff["limit"] == 10
    assert diff["offset"] == 0

    # Verify dimensions are recorded
    assert len(diff["dimensions"]) == 1
    assert diff["dimensions"][0]["alias"] == "event_date"
    assert diff["dimensions"][0]["field_path"] == "start_time"
    assert diff["dimensions"][0]["granularity"] == "DAY"
    assert diff["dimensions"][0]["timezone"] == "America/New_York"

    # Verify metrics are recorded
    assert len(diff["metrics"]) == 2
    assert diff["metrics"][0]["alias"] == "event_count"
    assert diff["metrics"][0]["field_path"] == "id"
    assert diff["metrics"][0]["operation"] == "COUNT"
    assert diff["metrics"][1]["alias"] == "concatenated_titles"
    assert diff["metrics"][1]["field_path"] == "title"
    assert diff["metrics"][1]["operation"] == "CONCAT"

    # Verify filter bounds are recorded
    assert diff["filter_bounds"]["predicates"]["calendar_ids"] == [1, 2, 3]

    # Most important: verify that no field values are recorded
    # Convert to JSON to ensure it's serializable and contains no non-opaque data
    json_str = json.dumps(diff, default=str)

    # Assert that no title values appear
    assert "title" not in json_str or json_str.count("title") == 1  # Only field_path

    # Assert that no concatenated string appears
    # (The separator would not appear in the payload)
    assert ";" not in json_str


def test_audit_payload_with_date_bounds():
    """Filter bounds with date ranges are recorded as ISO strings."""
    import datetime

    plan = AggregateQueryPlan(
        entity=AggregatableEntity.AVAILABLE_TIME,
        dimensions=(
            DimensionSpec(
                alias="by_calendar",
                field_path="calendar_id",
            ),
        ),
        metrics=(
            MetricSpec(
                alias="duration_sum",
                field_path="duration_minutes",
                op=AggregateOp.SUM,
            ),
        ),
        filter_bounds=FilterBounds(
            start=datetime.datetime(2024, 1, 1, 0, 0, 0),
            end=datetime.datetime(2024, 12, 31, 23, 59, 59),
        ),
        limit=50,
        offset=5,
    )

    mock_audit_service = MagicMock()
    mock_system_user = MagicMock()

    with patch.object(
        mock_audit_service,
        "actor_from_system_user",
        return_value={"actor": "test"},
    ):
        with patch.object(
            mock_audit_service,
            "scope_from_organization_id",
            return_value={"scope": "test"},
        ):
            record_aggregate_query(
                plan=plan,
                organization_id=99,
                system_user=mock_system_user,
                row_count=0,
                audit_service=mock_audit_service,
            )

    call_kwargs = mock_audit_service.record.call_args[1]
    diff = call_kwargs["diff"]

    assert diff["filter_bounds"]["start"] == "2024-01-01T00:00:00"
    assert diff["filter_bounds"]["end"] == "2024-12-31T23:59:59"
    assert diff["row_count"] == 0
