"""Test that audit payloads record shape, not values."""

import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from audit_integration.constants import AuditAction
from organizations.models import Organization
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


@pytest.mark.django_db
def test_audit_payload_omits_field_values_and_predicates(
    organization: Organization,
):
    """Audit payload records metrics and operations, not values or group keys."""
    # Mock the audit service to capture what we're recording.
    audit_service = mock.MagicMock()
    audit_service.actor_from_system_user.return_value = {"type": "system_user", "id": "token-123"}
    audit_service.scope_from_organization_id.return_value = {"organization_id": organization.id}

    # Build a plan with at least one dimension (required) and a concat metric.
    now = datetime.datetime(2025, 9, 17, 12, 0, 0, tzinfo=datetime.UTC)
    plan = AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=(
            DimensionSpec(
                alias="by_day",
                field_path="start_date",
                granularity=TemporalGranularity.DAY,
                tzinfo=ZoneInfo("UTC"),
            ),
        ),
        metrics=(
            MetricSpec.row_count(alias="count"),
            MetricSpec(
                alias="title_concat",
                field_path="title",
                op=AggregateOp.CONCAT,
                options={"separator": ", ", "distinct": True},
            ),
            MetricSpec(alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM),
        ),
        filter_bounds=FilterBounds(
            start=now,
            end=now + datetime.timedelta(days=1),
            predicates={"calendar_id": 123},
        ),
        limit=10,
        offset=0,
    )

    # Call the audit function.
    record_aggregate_query(
        audit_service=audit_service,
        actor_from_system_user=audit_service.actor_from_system_user.return_value,
        system_user=None,
        organization_id=organization.id,
        plan=plan,
        row_count=5,
    )

    # Verify the audit record was written with the right shape.
    audit_service.record.assert_called_once()
    call_kwargs = audit_service.record.call_args[1]

    # Check the action and scope are correct.
    assert call_kwargs["action"] == AuditAction.AGGREGATE_QUERY
    assert call_kwargs["scope"] == {"organization_id": organization.id}

    # Verify the entire payload structure and ensure no field values leak.
    diff = call_kwargs["diff"]
    assert diff == {
        "entity": "calendar_event",
        "dimensions": [
            {
                "alias": "by_day",
                "field_path": "start_date",
                "granularity": "DAY",
                "timezone": "UTC",
            }
        ],
        "metrics": [
            {
                "alias": "count",
                "field_path": None,
                "op": "count",
                "options": {},
            },
            {
                "alias": "title_concat",
                "field_path": "title",
                "op": "concat",
                "options": {"distinct": True, "separator": ", "},
            },
            {
                "alias": "duration_sum",
                "field_path": "duration_minutes",
                "op": "sum",
                "options": {},
            },
        ],
        "filter_bounds": {
            "start": now.isoformat(),
            "end": (now + datetime.timedelta(days=1)).isoformat(),
            "predicates": {"calendar_id": 123},
        },
        "has_having": False,
        "order_by": [],
        "window": None,
        "limit": 10,
        "offset": 0,
        "row_count": 5,
    }


@pytest.mark.django_db
def test_audit_payload_with_minimal_metrics(organization: Organization):
    """Audit payload handles a plan with minimal metrics (only row count)."""
    audit_service = mock.MagicMock()
    audit_service.actor_from_system_user.return_value = {"type": "system_user"}
    audit_service.scope_from_organization_id.return_value = {"organization_id": organization.id}

    now = datetime.datetime(2025, 9, 17, 12, 0, 0, tzinfo=datetime.UTC)
    plan = AggregateQueryPlan(
        entity=AggregatableEntity.AVAILABLE_TIME,
        dimensions=(
            DimensionSpec(
                alias="by_week",
                field_path="start_date",
                granularity=TemporalGranularity.WEEK,
                tzinfo=ZoneInfo("UTC"),
            ),
        ),
        metrics=(MetricSpec.row_count(alias="count"),),
        filter_bounds=FilterBounds(
            start=now,
            end=now + datetime.timedelta(days=7),
            predicates={"user_id": 456},
        ),
        limit=100,
        offset=0,
    )

    record_aggregate_query(
        audit_service=audit_service,
        actor_from_system_user=audit_service.actor_from_system_user.return_value,
        system_user=None,
        organization_id=organization.id,
        plan=plan,
        row_count=0,
    )

    call_kwargs = audit_service.record.call_args[1]
    diff = call_kwargs["diff"]

    # Verify minimal payload structure.
    assert diff["entity"] == "available_time"
    assert diff["row_count"] == 0
    assert diff["filter_bounds"]["predicates"]["user_id"] == 456
    assert len(diff["metrics"]) == 1
    assert diff["metrics"][0]["alias"] == "count"
    assert diff["metrics"][0]["op"] == "count"
