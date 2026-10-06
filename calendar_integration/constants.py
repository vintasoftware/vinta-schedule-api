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


class AppointmentTypeScopedRuleType(TextChoices):
    """Which appointment-type-scoped rule a booking or reschedule violated.

    Named exactly as required to be surfaced to a caller: outside
    window, inside block, quota consumed -- never the configured values
    themselves (e.g. the cap or current count).
    """

    OUTSIDE_WINDOW = "outside_window", "Outside window"
    INSIDE_BLOCK = "inside_block", "Inside block"
    QUOTA_CONSUMED = "quota_consumed", "Quota consumed"


class QuotaPeriod(TextChoices):
    """Fixed calendar period an ``AppointmentTypeSlotQuotaRule`` cap applies to.

    Values match exactly what the ``calculate_appointment_type_quota_period_counts``
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
    mutations). Selected by ``CalendarManagementTokenQuerySet.booking_codes``,
    which is what makes it eligible for revocation via ``revoke_token`` /
    ``DELETE /booking-codes/<id>/`` -- ``kind`` alone is necessary but not
    sufficient for the REST surface: ``BookingCodeViewSet.destroy`` also
    requires the caller to be the owner-or-org-admin of the token's target
    before it actually revokes anything.

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


class ResourceSyncStatus(TextChoices):
    """Where a provider-backed room is in its sync lifecycle.

    Stored on ``ResourceCalendarProviderLink.sync_status``. Rooms with no link
    (manual ``INTERNAL`` rooms, and every room in an organization with the
    ``resource_calendar_provider_sync`` flag off) have no status at all. See the
    state diagram in ``ai-plans/2026-10-04-RESOURCE_CALENDAR_PROVIDER_SYNC_SPEC.md``.
    """

    PENDING_CREATION = "pending_creation", "Pending Creation"
    SYNCED = "synced", "Synced"
    PENDING_UPDATE = "pending_update", "Pending Update"
    PENDING_DELETION = "pending_deletion", "Pending Deletion"
    SYNC_FAILED = "sync_failed", "Sync Failed"
    ARCHIVED = "archived", "Archived"


class ResourceSyncOperation(TextChoices):
    """The provider write a room link is waiting on, or failed on."""

    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"


# The room fields Vinta Schedule and the provider both write. These are the keys of
# ``ResourceCalendarProviderLink.provider_snapshot`` and ``.pending_fields``.
# ``location_ref`` is a ``{"external_building_id": ..., "external_floor_id": ...}``
# dict, or ``None`` for a room with no location.
RESOURCE_SYNCED_FIELDS: tuple[str, ...] = ("name", "description", "capacity", "location_ref")


# The ``(sync_status, failed_operation)`` states in which a provider-backed room
# exists on the provider and is not on its way out. ``failed_operation`` is only
# read for ``SYNC_FAILED``; for every other status it is ``""`` here. This is the
# one definition of the rule: ``ResourceCalendarProviderLink.is_bookable`` checks
# an instance against it and ``ResourceCalendarProviderLinkQuerySet.on_provider``
# builds its filter from it.
ROOM_ON_PROVIDER_STATES: frozenset[tuple[str, str]] = frozenset(
    {
        (ResourceSyncStatus.SYNCED, ""),
        (ResourceSyncStatus.PENDING_UPDATE, ""),
        (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.UPDATE),
    }
)


class BookingCancelMode(TextChoices):
    """How a booking of a room that is being deleted is cancelled."""

    REMOVE_ROOM = "remove_room", "Remove only the room"
    CANCEL_EVENT = "cancel_event", "Cancel the whole event"


class BookingResolutionKind(TextChoices):
    """How a booking of a room that is being deleted is resolved, as callers name it.

    ``MOVE`` needs a target room. See ``booking_resolution_from``.
    """

    ABORT = "abort", "Cancel the room deletion"
    MOVE = "move", "Move to another room"
    REMOVE_ROOM = "remove_room", "Remove only the room"
    CANCEL_EVENT = "cancel_event", "Cancel the whole event"


class RoomDeletionOutcome(TextChoices):
    """What a synced-room delete did. The label is the message to show the caller."""

    DELETED = "deleted", "The room was deleted."
    ABORTED = "aborted", "The room has bookings and the resolution cancelled the deletion."
    REJECTED = "rejected", "Some bookings cannot be resolved as asked."
    INCOMPLETE = (
        "incomplete",
        "Resolving the bookings stopped part way; preview again and retry.",
    )


class BookingRoomChange(TextChoices):
    """What happened to a booking's room when a room was deleted. Sent to the organizer."""

    MOVED = "moved", "Moved to another room"
    ROOM_REMOVED = "room_removed", "Room removed from the booking"
    EVENT_CANCELLED = "event_cancelled", "Booking cancelled"


class BookingRejectionReason(TextChoices):
    """Why a booking's resolution was rejected. The label is the message shown to callers."""

    NOT_IN_PREVIEW = "not_in_preview", "booking is not in the deletion preview"
    TARGET_NOT_FOUND = "target_not_found", "target room not found"
    TARGET_IS_SAME_ROOM = "target_is_same_room", "target room is the room being deleted"
    TARGET_NOT_BOOKABLE = "target_not_bookable", "target room is not bookable"
    TARGET_ON_DIFFERENT_PROVIDER = (
        "target_on_different_provider",
        "target room is on a different provider",
    )
    TARGET_TOO_SMALL = "target_too_small", "target room is too small"
    TARGET_BUSY = "target_busy", "target room is busy"
    ON_ROOM_CALENDAR = (
        "on_room_calendar",
        "booking is on the room's own calendar and can only be cancelled",
    )
