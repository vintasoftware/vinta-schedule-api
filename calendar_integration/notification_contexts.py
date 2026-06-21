"""Notification contexts for calendar_integration in-app notifications.

Contexts are registered via the ``@register_context`` decorator, which registers
on import. Import this module from the ``CalendarIntegrationConfig.ready()``
method to ensure the contexts are registered at startup.
"""

from typing import Any

from vintasend.exceptions import NotificationContextGenerationError
from vintasend.services.notification_service import register_context

from calendar_integration.constants import ExternalEventChangeKind


@register_context("external_event_change_request_approver_context")
def external_event_change_request_approver_context(
    change_request_id: int,
    event_title: str,
    change_kind: str,
    organization_id: int,
) -> dict[str, Any]:
    """Context for in-app notifications sent to eligible approvers on PENDING request creation.

    Args:
        change_request_id: PK of the ``ExternalEventChangeRequest`` row.
        event_title: Title of the event the change targets (captured at notification
            time so the notification remains meaningful even if the event is later
            deleted).
        change_kind: ``ExternalEventChangeKind.UPDATE`` or
            ``ExternalEventChangeKind.DELETE`` — displayed in the notification body.
        organization_id: ID of the organization the request belongs to.

    Returns:
        A dict with ``change_request_id``, ``event_title``, ``change_kind``, and
        ``organization_id`` available in the body template.
    """
    if change_kind not in (ExternalEventChangeKind.UPDATE, ExternalEventChangeKind.DELETE):
        raise NotificationContextGenerationError(
            f"Invalid change_kind for change request notification: {change_kind!r}"
        )
    return {
        "change_request_id": change_request_id,
        "event_title": event_title,
        "change_kind": change_kind,
        "organization_id": organization_id,
    }
