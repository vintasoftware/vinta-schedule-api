from django.db.models import TextChoices


class CalendarType(TextChoices):
    PERSONAL = "personal", "Personal Calendar"
    RESOURCE = "resource", "Resource Calendar"
    VIRTUAL = "virtual", "Virtual Calendar"
    BUNDLE = "bundle", "Bundle Calendar"


class CalendarProvider(TextChoices):
    INTERNAL = "internal", "Internal Calendar"
    GOOGLE = "google", "Google Calendar"
    MICROSOFT = "microsoft", "Microsoft Outlook Calendar"
    APPLE = "apple", "Apple Calendar"
    ICS = "ics", "ICS"


class RSVPStatus(TextChoices):
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    PENDING = "pending", "Pending"


class CalendarSyncStatus(TextChoices):
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    IN_PROGRESS = "in_progress", "In Progress"
    NOT_STARTED = "not_started", "Not Started"


class CalendarSyncTriggerSource(TextChoices):
    IMPORT = "import", "Import"
    MANUAL = "manual", "Manual"
    WEBHOOK = "webhook", "Webhook"
    ADMIN = "admin", "Admin"


class CalendarOrganizationResourceImportStatus(TextChoices):
    SUCCESS = "success", "Success"
    # The import ran without error but did not import everything it discovered --
    # currently only because the organization ran out of `resource_calendars`
    # headroom mid-import. A distinct terminal status rather than SUCCESS plus an
    # advisory string on `error_message`: a consumer that has to string-match an
    # error column to tell a clean import from a truncated one has no contract.
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"
    IN_PROGRESS = "in_progress", "In Progress"
    NOT_STARTED = "not_started", "Not Started"


class RecurrenceFrequency(TextChoices):
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"
    YEARLY = "YEARLY", "Yearly"


class RecurrenceWeekday(TextChoices):
    MONDAY = "MO", "Monday"
    TUESDAY = "TU", "Tuesday"
    WEDNESDAY = "WE", "Wednesday"
    THURSDAY = "TH", "Thursday"
    FRIDAY = "FR", "Friday"
    SATURDAY = "SA", "Saturday"
    SUNDAY = "SU", "Sunday"


class EventManagementPermissions(TextChoices):
    CREATE = "create", "Create Event"
    UPDATE_ATTENDEES = "update_attendees", "Update Event Attendees"
    UPDATE_SELF_RSVP = "update_self_rsvp", "Update Self RSVP on Event"
    UPDATE_DETAILS = "update_details", "Update Event Details"
    CANCEL = "cancel", "Cancel Event"
    RESCHEDULE = "reschedule", "Reschedule Event"


class CalendarVisibility(TextChoices):
    ACTIVE = "active", "Active"
    UNLISTED = "unlisted", "Unlisted"
    INACTIVE = "inactive", "Inactive"


class IncomingWebhookProcessingStatus(TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    IGNORED = "ignored", "Ignored"


class ExternalEventChangeKind(TextChoices):
    """Kind of inbound external change being requested."""

    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"


class ExternalEventChangeRequestStatus(TextChoices):
    """Lifecycle status of an ExternalEventChangeRequest."""

    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    STALE = "stale", "Stale"
    AUTO_UNDONE = "auto_undone", "Auto-undone"


class GroupScopedRuleType(TextChoices):
    """Which group-scoped rule a booking or reschedule violated
    (CALENDAR_GROUP_SCOPED_AVAILABILITY spec, Decisions -> Errors).

    Named exactly as the spec requires them surfaced to a caller: outside
    window, inside block, quota consumed -- never the configured values
    themselves. ``OUTSIDE_WINDOW`` is enforced as of Phase 1b and
    ``INSIDE_BLOCK`` as of Phase 2a; ``QUOTA_CONSUMED`` is reserved for
    Phase 3b.
    """

    OUTSIDE_WINDOW = "outside_window", "Outside window"
    INSIDE_BLOCK = "inside_block", "Inside block"
