from django.db.models import TextChoices


class PublicAPIResources(TextChoices):
    """
    Enum for public API resources.
    """

    CALENDAR_EVENT = "calendar_event", "Calendar Event"
    CALENDAR = "calendar", "Calendar"
    RECURRENCE_RULE = "recurrence_rule", "Recurrence Rule"
    EXTERNAL_ATTENDEE = "external_attendee", "External Attendee"
    EXTERNAL_ATTENDANCE = "external_attendance", "External Attendance"
    ATTENDANCE = "attendance", "Attendance"
    USER = "user", "User"
    RESOURCE_ALLOCATION = "resource_allocation", "Resource Allocation"
    EVENT_RECURRING_EXCEPTION = "event_recurring_exception", "Event Recurring Exception"
    BLOCKED_TIME = "blocked_time", "Blocked Time"
    BLOCKED_TIME_RECURRING_EXCEPTION = (
        "blocked_time_recurring_exception",
        "Blocked Time Recurring Exception",
    )
    AVAILABLE_TIME = "available_time", "Available Time"
    AVAILABLE_TIME_RECURRING_EXCEPTION = (
        "available_time_recurring_exception",
        "Available Time Recurring Exception",
    )
    AVAILABILITY_WINDOWS = "availability_windows", "Availability Windows"
    UNAVAILABLE_WINDOWS = "unavailable_windows", "Unavailable Windows"
    ORGANIZATION = "organization", "Organization"
    CALENDAR_GROUP = "calendar_group", "Calendar Group"
    SYSTEM_USER = "system_user", "System User"
    MEMBERSHIP = "membership", "Membership"
    INVITATION = "invitation", "Invitation"
    BRANDING = "branding", "Branding"
    CHILD_ORG_ANALYTICS = "child_org_analytics", "Child Organization Analytics"
    CREATE_RESOURCE_CALENDAR = "create_resource_calendar", "Create Resource Calendar"
    DISABLE_RESOURCE_CALENDAR = "disable_resource_calendar", "Disable Resource Calendar"
    IMPORT_RESOURCE_CALENDARS = "import_resource_calendars", "Import Resource Calendars"
    CREATE_AVAILABILITY_WINDOW = "create_availability_window", "Create Availability Window"
