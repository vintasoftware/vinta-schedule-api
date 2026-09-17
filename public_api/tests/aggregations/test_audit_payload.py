"""Test that audit payloads record shape, not values."""

import datetime

import pytest

from audit_integration.constants import AuditAction
from organizations.models import Organization
from public_api.aggregations.audit import record_aggregate_query
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    FilterBounds,
    MetricSpec,
)
from public_api.models import SystemUser


@pytest.mark.django_db
def test_audit_payload_omits_field_values_and_predicates(
    db, mocker, user, organization: Organization, system_user: SystemUser
):
    """Audit payload records metrics and operations, not values or group keys."""
    # Mock the audit service to capture what we're recording.
    audit_service = mocker.MagicMock()
    audit_service.actor_from_system_user.return_value = {"type": "system_user", "id": "token-123"}
    audit_service.scope_from_organization_id.return_value = {"organization_id": organization.id}

    # Build a plan with a concat metric (the most sensitive case — it generates string values).
    now = datetime.datetime(2025, 9, 17, 12, 0, 0, tzinfo=datetime.UTC)
    plan = AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=(),
        metrics=(
            MetricSpec.row_count(alias="count"),
            MetricSpec(alias="title_concat", field_path="title", op=AggregateOp.CONCAT, options={"separator": ", ", "distinct": True}),
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
        system_user=system_user,
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

    # Check the payload structure.
    diff = call_kwargs["diff"]
    assert diff["entity"] == "calendar_event"
    assert diff["limit"] == 10
    assert diff["offset"] == 0
    assert diff["row_count"] == 5

    # Verify the metrics are recorded BY NAME AND OPERATION, not by value.
    metrics = diff["metrics"]
    assert len(metrics) == 3
    assert metrics[0]["alias"] == "count"
    assert metrics[0]["op"] is None

    assert metrics[1]["alias"] == "title_concat"
    assert metrics[1]["field_path"] == "title"
    assert metrics[1]["op"] == "concat"

    assert metrics[2]["alias"] == "duration_sum"
    assert metrics[2]["field_path"] == "duration_minutes"
    assert metrics[2]["op"] == "sum"

    # Verify filter bounds are recorded as opaque ids and datetime ranges, not values.
    bounds = diff["filter_bounds"]
    assert bounds["start"] == now.isoformat()
    assert bounds["end"] == (now + datetime.timedelta(days=1)).isoformat()
    assert bounds["predicates"]["calendar_id"] == 123
    # Free-text fields are not present.
    assert "calendar_name" not in bounds
    assert "calendar_title" not in bounds


@pytest.mark.django_db
def test_audit_payload_with_no_metrics(mocker, organization: Organization, system_user: SystemUser):
    """Audit payload handles a plan with minimal metrics (only row count)."""
    audit_service = mocker.MagicMock()
    audit_service.actor_from_system_user.return_value = {"type": "system_user"}
    audit_service.scope_from_organization_id.return_value = {"organization_id": organization.id}

    now = datetime.datetime(2025, 9, 17, 12, 0, 0, tzinfo=datetime.UTC)
    plan = AggregateQueryPlan(
        entity=AggregatableEntity.AVAILABLE_TIME,
        dimensions=(),
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
        system_user=system_user,
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
