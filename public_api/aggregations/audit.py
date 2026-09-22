"""Audit logging hook for aggregate queries.

Records every aggregate query with actor, organization, entity name, dimension
field paths and granularities, metric aliases and operations, filter bounds,
requested limit/offset, and the returned row count. Never records aggregated
values, group key values, or concatenated strings.
"""

from typing import Any

from vinta_audit_logs.types import SubjectRef

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from public_api.aggregations.plan import AggregateQueryPlan


def get_audit_service() -> OrganizationAuditService:
    """Resolve the OrganizationAuditService from the DI container.

    Raises RuntimeError if the container is not initialized or is None.
    """
    from di_core.containers import container

    if container is None:
        raise RuntimeError(
            "DI container is not wired; the aggregate query audit hook cannot resolve "
            "audit_service before di_core.apps.DICoreConfig.ready() runs."
        )

    audit_service = container.audit_service()
    if audit_service is None:
        raise RuntimeError(
            "audit_service provider returned None; check that audit_integration is properly configured."
        )
    return audit_service


def record_aggregate_query(
    plan: AggregateQueryPlan,
    organization_id: int,
    system_user: Any,
    row_count: int,
    audit_service: OrganizationAuditService,
) -> None:
    """Record an aggregate query to the audit trail.

    Captures what was queried — entity, dimensions, metrics, filters — without
    capturing any values. Called by the resolver after the query executes.
    """

    actor = audit_service.actor_from_system_user(system_user)
    scope = audit_service.scope_from_organization_id(organization_id)

    dimensions_metadata = [
        {
            "alias": dim.alias,
            "field_path": dim.field_path,
            "granularity": dim.granularity.value if dim.granularity else None,
            "timezone": str(dim.tzinfo) if dim.tzinfo else None,
        }
        for dim in plan.dimensions
    ]

    metrics_metadata = [
        {
            "alias": metric.alias,
            "field_path": metric.field_path,
            "operation": metric.op.value,
        }
        for metric in plan.metrics
    ]

    filter_bounds_metadata = {
        "start": plan.filter_bounds.start.isoformat() if plan.filter_bounds.start else None,
        "end": plan.filter_bounds.end.isoformat() if plan.filter_bounds.end else None,
        "predicates": {key: list(value) for key, value in plan.filter_bounds.predicates.items()},
    }

    query_metadata = {
        "dimensions": dimensions_metadata,
        "metrics": metrics_metadata,
        "filter_bounds": filter_bounds_metadata,
        "limit": plan.limit,
        "offset": plan.offset,
        "row_count": row_count,
    }

    audit_service.record(
        action=AuditAction.AGGREGATE_QUERY,
        actor=actor,
        subject=SubjectRef(
            subject_type="public_api.aggregations.aggregate_query",
            subject_id=plan.entity.value,
        ),
        scope=scope,
        diff=query_metadata,
    )
