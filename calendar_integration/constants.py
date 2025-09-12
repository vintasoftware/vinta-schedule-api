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


class CalendarOrganizationResourceImportStatus(TextChoices):
    SUCCESS = "success", "Success"
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
