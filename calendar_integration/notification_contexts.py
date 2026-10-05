"""Notification contexts for calendar_integration in-app and email notifications.

Contexts are registered via the ``@register_context`` decorator, which registers
on import. Import this module from the ``CalendarIntegrationConfig.ready()``
method to ensure the contexts are registered at startup.

The four ``room_*`` / ``booking_room_changed`` contexts back the emails
``RoomSyncNotifier`` sends. They are deliberately plain, like the dunning
contexts in ``payments/notification_contexts.py``: the notifier already has the
room or event in hand and captures its name at send time, so the context does
not re-query a row that may since have been archived or deleted.
"""

from typing import Any

from vintasend.exceptions import NotificationContextGenerationError
from vintasend.services.notification_service import register_context

from calendar_integration.constants import ExternalEventChangeKind
from calendar_integration.services.room_sync_notifier import (
    BookingRoomChange,
    RoomSyncOperation,
)


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


@register_context("room_sync_failed_context")
def room_sync_failed_context(
    room_id: int,
    room_name: str,
    operation: str,
    reason: str,
    organization_id: int,
) -> dict[str, Any]:
    """Context for the email org admins get when a room's provider write gave up.

    Args:
        room_id: PK of the room's ``Calendar``.
        room_name: The room's name, captured when the email was queued.
        operation: A ``RoomSyncOperation`` value: the write that failed.
        reason: A short, generic sentence for admins. Never attendee or event content.
        organization_id: ID of the organization the room belongs to.
    """
    if operation not in RoomSyncOperation.values:
        raise NotificationContextGenerationError(
            f"Invalid operation for room sync failed notification: {operation!r}"
        )
    return {
        "room_id": room_id,
        "room_name": room_name,
        "operation": operation,
        "operation_label": RoomSyncOperation(operation).label,
        "reason": reason,
        "organization_id": organization_id,
    }


@register_context("room_edit_discarded_context")
def room_edit_discarded_context(
    room_id: int,
    room_name: str,
    fields: list[dict[str, str]],
    organization_id: int,
) -> dict[str, Any]:
    """Context for the email org admins get when a queued room edit was dropped.

    Args:
        room_id: PK of the room's ``Calendar``.
        room_name: The room's name, captured when the email was queued.
        fields: One ``{"name": <field name>}`` per field whose queued value the
            provider's won over. Dict-shaped because vintasend only stores
            dict items in a list; the template gets the flat names. Never
            empty: ``RoomSyncNotifier.notify_edit_discarded`` refuses an empty
            list before queuing.
        organization_id: ID of the organization the room belongs to.
    """
    return {
        "room_id": room_id,
        "room_name": room_name,
        "fields": [item["name"] for item in fields],
        "organization_id": organization_id,
    }


@register_context("room_bookings_flagged_context")
def room_bookings_flagged_context(
    room_id: int,
    room_name: str,
    count: int,
    organization_id: int,
) -> dict[str, Any]:
    """Context for the email org admins get when an archived room left flagged bookings.

    Args:
        room_id: PK of the archived room's ``Calendar``.
        room_name: The room's name, captured when the email was queued.
        count: How many future bookings were flagged for resolution.
        organization_id: ID of the organization the room belongs to.
    """
    return {
        "room_id": room_id,
        "room_name": room_name,
        "count": count,
        "organization_id": organization_id,
    }


@register_context("booking_room_changed_context")
def booking_room_changed_context(
    event_id: int,
    event_title: str,
    event_start: str,
    change: str,
    organization_id: int,
) -> dict[str, Any]:
    """Context for the email an organizer gets when their booking's room changed.

    Args:
        event_id: PK of the booking's ``CalendarEvent``.
        event_title: The booking's title, captured when the email was queued.
        event_start: The booking's start, already formatted in its own timezone.
        change: A ``BookingRoomChange`` value.
        organization_id: ID of the organization the booking belongs to.
    """
    if change not in BookingRoomChange.values:
        raise NotificationContextGenerationError(
            f"Invalid change for booking room changed notification: {change!r}"
        )
    return {
        "event_id": event_id,
        "event_title": event_title,
        "event_start": event_start,
        "change": change,
        "organization_id": organization_id,
    }
