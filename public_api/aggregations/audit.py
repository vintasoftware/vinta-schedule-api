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

    Records the full query plan plus row count. The plan's as_audit_dict()
    captures the shape (entity, dimensions, metrics, filter bounds, order_by,
    window, pagination) without any aggregated values or field values.

    Args:
        audit_service: The audit service from DI.
        actor_from_system_user: The result of audit_service.actor_from_system_user.
        system_user: The SystemUser (token) making the request.
        organization_id: The organization ID.
        plan: The AggregateQueryPlan.
        row_count: The number of rows returned.
    """
    payload = {**plan.as_audit_dict(), "row_count": row_count}

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
