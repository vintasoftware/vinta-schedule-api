"""Audit logging for aggregate queries.

Records who ran which aggregate over what: entity, dimensions, metrics, filter
bounds, and row count — but never field values, group keys, or free-text predicates.
"""

from typing import Any

from vinta_audit_logs.types import SubjectRef

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from public_api.models import SystemUser

from .plan import AggregateQueryPlan


def record_aggregate_query(
    audit_service: OrganizationAuditService,
    actor_from_system_user: Any,
    system_user: SystemUser | None,
    organization_id: int,
    plan: AggregateQueryPlan,
    row_count: int,
) -> None:
    """Record an aggregate query to the audit trail.

    Records actor, organization, entity, dimensions, metrics, filter bounds,
    limit/offset, and row count — never field values, group keys, or free-text
    predicate values.

    Args:
        audit_service: The audit service from DI.
        actor_from_system_user: The result of audit_service.actor_from_system_user.
        system_user: The SystemUser (token) making the request.
        organization_id: The organization ID.
        plan: The AggregateQueryPlan.
        row_count: The number of rows returned.
    """
    payload = _serialize_plan(plan, row_count)

    audit_service.record(
        action=AuditAction.AGGREGATE_QUERY,
        actor=actor_from_system_user,
        subject=SubjectRef(
            subject_type=f"aggregate.{plan.entity.value}",
            subject_id="",
        ),
        scope=audit_service.scope_from_organization_id(organization_id),
        diff=payload,
    )


def _serialize_plan(plan: AggregateQueryPlan, row_count: int) -> dict[str, Any]:
    """Serialize an AggregateQueryPlan to a payload for the audit record.

    Records entity, dimension field paths and granularities, metric aliases and
    operations, filter bounds (datetime range and scalar id predicates), pagination,
    and row count. Explicitly omits any field value.
    """
    return {
        "entity": plan.entity.value,
        "dimensions": [
            {
                "field_path": dim.field_path,
                "granularity": dim.granularity.value if dim.granularity else None,
            }
            for dim in plan.dimensions
        ],
        "metrics": [
            {
                "alias": metric.alias,
                "field_path": metric.field_path,
                "op": metric.op.value if metric.op else None,
            }
            for metric in plan.metrics
        ],
        "filter_bounds": plan.filter_bounds.as_audit_dict(),
        "limit": plan.limit,
        "offset": plan.offset,
        "row_count": row_count,
    }
