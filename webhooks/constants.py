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


class WebhookStatus(TextChoices):
    PENDING = "pending", "Pending"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
