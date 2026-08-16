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
    """Which group-scoped rule a booking or reschedule violated.

    Named exactly as required to be surfaced to a caller: outside
    window, inside block, quota consumed -- never the configured values
    themselves (e.g. the cap or current count).
    """

    OUTSIDE_WINDOW = "outside_window", "Outside window"
    INSIDE_BLOCK = "inside_block", "Inside block"
    QUOTA_CONSUMED = "quota_consumed", "Quota consumed"


class QuotaPeriod(TextChoices):
    """Fixed calendar period a ``CalendarGroupSlotQuotaRule`` cap applies to.

    Values match exactly what the ``calculate_calendar_group_quota_period_counts``
    Postgres function accepts for its ``p_period_type`` argument -- keep them in
    sync if either side changes.
    """

    DAY = "day", "Day"
    WEEK = "week", "Week"
    MONTH = "month", "Month"
