from django.db.models import TextChoices


class WebhookEventType(TextChoices):
    CALENDAR_EVENT_CREATED = "calendar_event_created", "Calendar Event Created"
    CALENDAR_EVENT_UPDATED = "calendar_event_updated", "Calendar Event Updated"
    CALENDAR_EVENT_DELETED = "calendar_event_deleted", "Calendar Event Deleted"
    CALENDAR_EVENT_ATTENDEE_ADDED = "calendar_event_attendee_added", "Calendar Event Attendee Added"
    CALENDAR_EVENT_ATTENDEE_REMOVED = (
        "calendar_event_attendee_removed",
        "Calendar Event Attendee Removed",
    )
    CALENDAR_EVENT_ATTENDEE_UPDATED = (
        "calendar_event_attendee_updated",
        "Calendar Event Attendee Updated",
    )
    ORGANIZATION_MEMBER_CREATED = (
        "organization_member_created",
        "Organization member created",
    )


WEBHOOK_EVENT_DESCRIPTIONS: dict[WebhookEventType, str] = {
    WebhookEventType.CALENDAR_EVENT_CREATED: (
        "Fires after a new calendar event is created via CalendarEventService.create_event, "
        "once the creating transaction commits. The payload carries the created event's id, "
        "calendar_id, recurrence flags (is_recurring, recurring_event_id), "
        "start_time/end_time/timezone, title, and description."
    ),
    WebhookEventType.CALENDAR_EVENT_UPDATED: (
        "Fires after an existing calendar event's fields (title, description, start/end time, "
        "timezone, or recurrence rule) are changed via CalendarEventService.update_event, once "
        "the transaction commits. The payload carries the updated event's id, calendar_id, "
        "recurrence flags, start_time/end_time/timezone, title, and description — the same "
        "shape as calendar_event_created."
    ),
    WebhookEventType.CALENDAR_EVENT_DELETED: (
        "Fires after a calendar event (or a single occurrence of a recurring series, which "
        "becomes a cancellation exception rather than a hard delete) is removed via "
        "CalendarEventService.delete_event, once the transaction commits. The payload is a "
        "snapshot of the event captured immediately before deletion, in the same shape as "
        "calendar_event_created."
    ),
    WebhookEventType.CALENDAR_EVENT_ATTENDEE_ADDED: (
        "Fires once per newly-added attendee (internal member or external invitee) when "
        "CalendarEventService.update_event adds them to an existing event's attendee list. The "
        "payload carries the attendee's email, name, status, user_id (null for external "
        "attendees), and the event they were added to."
    ),
    WebhookEventType.CALENDAR_EVENT_ATTENDEE_REMOVED: (
        "Fires once per attendee (internal member or external invitee) when "
        "CalendarEventService.update_event drops them from an existing event's attendee list "
        "during reconciliation. The payload carries the removed attendee's email, name, status, "
        "user_id (null for external attendees), and the event they were removed from."
    ),
    WebhookEventType.CALENDAR_EVENT_ATTENDEE_UPDATED: (
        "Fires when CalendarEventService.update_event finds an external attendee already on the "
        "event and changes their contact details (email or name) while reconciling the attendee "
        "list. The payload carries the updated attendee's email, name, status, user_id (null, "
        "as this only applies to external attendees), and the event they belong to."
    ),
    WebhookEventType.ORGANIZATION_MEMBER_CREATED: (
        "Fires after a new, active organization membership is created — covering an "
        "organization's first admin membership on org creation, a user accepting an "
        "OrganizationInvitation, and the signup-time tenant-provisioning flow that auto-joins a "
        "user via a pending invitation. Emitted post-commit; skipped entirely for inactive "
        "memberships. The payload carries the member's user_id and email, the organization's id "
        "and name, and the membership's role."
    ),
}


class WebhookStatus(TextChoices):
    PENDING = "pending", "Pending"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
