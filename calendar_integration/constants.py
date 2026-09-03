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


class CalendarManagementTokenKind(TextChoices):
    """Explicit discriminator for what a ``CalendarManagementToken`` row is.

    Replaces the pre-Phase-7 heuristic (``minted_by_membership_user_id IS NOT
    NULL OR minted_by_system_user_id IS NOT NULL``), which misclassified a
    booking code minted with no actor at all -- exactly what a codeless
    booking mints -- as NOT a booking code, making it permanently
    un-revokable via ``CalendarPermissionService.revoke_token``.

    ``BOOKING_CODE`` -- a single-use booking code, minted through
    ``CalendarPermissionService.create_booking_token`` (REST
    ``BookingCodeViewSet`` or one of the six GraphQL ``create*BookingCode``
    mutations). Selected by ``CalendarManagementTokenQuerySet.booking_codes``
    and therefore revokable via ``revoke_token`` / ``DELETE
    /booking-codes/<id>/``.

    ``MANAGEMENT_TOKEN`` -- everything else: calendar-owner tokens
    (``create_calendar_owner_token``), attendee tokens
    (``create_attendee_token``), and external-attendee tokens
    (``create_external_attendee_update_token`` /
    ``create_external_attendee_schedule_token``). Never revokable through the
    booking-code surfaces -- that is Phase 6's privilege-escalation fix, and
    this discriminator is what keeps it true regardless of who minted the
    token or whether they set any actor field.
    """

    BOOKING_CODE = "booking_code", "Booking Code"
    MANAGEMENT_TOKEN = "management_token", "Management Token"
