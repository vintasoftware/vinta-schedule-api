from django.core.exceptions import ImproperlyConfigured, PermissionDenied


# API Validation Errors
class CalendarServiceNotInjectedError(ImproperlyConfigured):
    pass


# Service Layer/Internal Errors
class CalendarIntegrationError(Exception):
    """Base exception for calendar integration errors"""

    default_message = ""

    def __init__(self, message: str | None = None):
        if message is None:
            message = self.default_message
        super().__init__(message)


class CalendarAuthenticationError(CalendarIntegrationError):
    """Raised when calendar authentication fails"""

    pass


class InvalidCalendarTokenError(CalendarAuthenticationError):
    default_message = "User doesn't have a valid calendar token. Please reauthenticate"


class BundleCalendarError(CalendarIntegrationError):
    """Base class for bundle calendar related errors"""

    pass


class InvalidPrimaryCalendarError(BundleCalendarError):
    default_message = "Primary calendar must be one of the child calendars"


class BundleCalendarNotFoundError(BundleCalendarError):
    default_message = "Calendar must be a bundle calendar"


class EmptyBundleCalendarError(BundleCalendarError):
    default_message = "Bundle calendar has no child calendars"


class NoPrimaryCalendarError(BundleCalendarError):
    default_message = "Bundle calendar has no designated primary child calendar"


class CalendarUnavailableError(BundleCalendarError):
    def __init__(self, calendar_name: str):
        super().__init__(f"No availability in child calendar {calendar_name}")


class EventManagementError(CalendarIntegrationError):
    """Base class for event management errors"""

    pass


class InvalidTimezoneError(EventManagementError):
    def __init__(self, iana_tz: str):
        super().__init__(f"Invalid IANA timezone: {iana_tz}")


class NoAvailableTimeWindowsError(EventManagementError):
    default_message = "No available time windows for the event."


class InvalidEventTypeError(EventManagementError):
    default_message = "Event must be a bundle primary event"


class MissingOrganizationError(EventManagementError):
    default_message = "Organization is required for bundle operations"


class ExceptionToNonRecurringEventError(EventManagementError):
    def __init__(self, object_type_name: str):
        super().__init__(f"Cannot create exception for non-recurring {object_type_name}")


class InvalidCalendarOperationError(EventManagementError):
    default_message = "This calendar does not manage available windows."


class MissingCallbackError(EventManagementError):
    default_message = "create_continuation_callback is required when not cancelling"


# Calendar Adapters - External API Errors
class CalendarAdapterError(CalendarIntegrationError):
    """Base class for calendar adapter errors"""

    pass


class GoogleCalendarAdapterError(CalendarAdapterError):
    """Google Calendar specific errors"""

    pass


class MSOutlookAdapterError(CalendarAdapterError):
    """Microsoft Outlook specific errors"""

    pass


class InvalidCredentialsError(CalendarAdapterError):
    """Raised when calendar credentials are invalid or expired"""

    pass


class GoogleCredentialsError(InvalidCredentialsError, GoogleCalendarAdapterError):
    def __init__(self, message="Invalid or expired Google credentials provided."):
        super().__init__(message)


class GoogleServiceAccountError(InvalidCredentialsError, GoogleCalendarAdapterError):
    default_message = "Invalid or expired Google service account credentials provided."


class MSGraphCredentialsError(InvalidCredentialsError, MSOutlookAdapterError):
    default_message = "Invalid or expired Microsoft Graph credentials provided."


class UnsupportedRRuleError(MSOutlookAdapterError):
    def __init__(self, component_key: str):
        super().__init__(f"Unsupported RRULE component: {component_key}")


class CalendarAPIError(CalendarAdapterError):
    """Base class for external calendar API operation errors"""

    pass


class EventOperationError(CalendarAPIError):
    """Errors during event CRUD operations"""

    pass


class WebhookOperationError(CalendarAPIError):
    """Errors during webhook subscription operations"""

    pass


class RequiredParameterError(CalendarAPIError):
    """Raised when required parameters are missing"""

    pass


class NotificationURLRequiredError(RequiredParameterError):
    default_message = "notification_url is required for webhook subscriptions"


class RoomEmailRequiredError(RequiredParameterError):
    default_message = "room_email is required for room event subscriptions"


# Calendar Permission Service Errors
class CalendarPermissionError(CalendarIntegrationError, PermissionDenied):
    """Base class for calendar permission errors"""

    pass


class InvalidTokenError(CalendarPermissionError):
    default_message = "Invalid token string provided."


class InvalidParameterCombinationError(CalendarPermissionError):
    default_message = "Specify either calendar_id or event_id, not both."


class MissingRequiredParameterError(CalendarPermissionError):
    default_message = "Either calendar_id or event_id must be specified."


class PermissionServiceInitializationError(CalendarPermissionError):
    default_message = "Error initializing CalendarPermissionCheckService."


class NoPermissionsSpecifiedError(CalendarPermissionError):
    default_message = "At least one permission must be specified to create a token."


# Model Level Errors
class CalendarModelError(CalendarIntegrationError):
    """Base class for calendar model errors"""

    pass


class RecurrenceExceptionError(CalendarModelError):
    default_message = "Cannot create exception for non-recurring event"


class MissingOrganizationForExceptionError(CalendarModelError):
    default_message = "CalendarEvent is missing organization (cannot create exception)"


# Other Service Errors
class CalendarServiceStateError(CalendarIntegrationError):
    """Errors related to calendar service state"""

    pass


class ServiceNotAuthenticatedError(CalendarServiceStateError):
    def __init__(self, message="Calendar service is not authenticated"):
        super().__init__(message)


class ServiceNotInitializedError(CalendarServiceStateError):
    def __init__(self, message="Calendar service is not initialized without provider"):
        super().__init__(message)


class CalendarServiceOrganizationNotSetError(CalendarServiceStateError):
    def __init__(self, message="Calendar service is not initialized or authenticated"):
        super().__init__(message)


# Recurrence Utils Errors
class RecurrenceError(CalendarIntegrationError):
    """Errors related to recurrence processing"""

    pass


class NoRecurrenceRuleError(RecurrenceError):
    default_message = "No recurrence rule provided"
