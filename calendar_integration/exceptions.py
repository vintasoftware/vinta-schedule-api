from django.core.exceptions import ImproperlyConfigured


# API Validation Errors
class CalendarServiceNotInjectedError(ImproperlyConfigured):
    pass


# Service Layer/Internal Errors
class CalendarIntegrationError(Exception):
    """Base exception for calendar integration errors"""

    pass


class CalendarAuthenticationError(CalendarIntegrationError):
    """Raised when calendar authentication fails"""

    pass


class InvalidCalendarTokenError(CalendarAuthenticationError):
    def __init__(self, message="User doesn't have a valid calendar token. Please reauthenticate"):
        super().__init__(message)


class BundleCalendarError(CalendarIntegrationError):
    """Base class for bundle calendar related errors"""

    pass


class InvalidPrimaryCalendarError(BundleCalendarError):
    def __init__(self, message="Primary calendar must be one of the child calendars"):
        super().__init__(message)


class BundleCalendarNotFoundError(BundleCalendarError):
    def __init__(self, message="Calendar must be a bundle calendar"):
        super().__init__(message)


class EmptyBundleCalendarError(BundleCalendarError):
    def __init__(self, message="Bundle calendar has no child calendars"):
        super().__init__(message)


class NoPrimaryCalendarError(BundleCalendarError):
    def __init__(self, message="Bundle calendar has no designated primary child calendar"):
        super().__init__(message)


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
    def __init__(self, message="No available time windows for the event."):
        super().__init__(message)


class InvalidEventTypeError(EventManagementError):
    def __init__(self, message="Event must be a bundle primary event"):
        super().__init__(message)


class MissingOrganizationError(EventManagementError):
    def __init__(self, message="Organization is required for bundle operations"):
        super().__init__(message)


class ExceptionToNonRecurringEventError(EventManagementError):
    def __init__(self, object_type_name: str):
        super().__init__(f"Cannot create exception for non-recurring {object_type_name}")


class InvalidCalendarOperationError(EventManagementError):
    def __init__(self, message="This calendar does not manage available windows."):
        super().__init__(message)


class MissingCallbackError(EventManagementError):
    def __init__(self, message="create_continuation_callback is required when not cancelling"):
        super().__init__(message)


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
    def __init__(self, message="Invalid or expired Google service account credentials provided."):
        super().__init__(message)


class MSGraphCredentialsError(InvalidCredentialsError, MSOutlookAdapterError):
    def __init__(self, message="Invalid or expired Microsoft Graph credentials provided."):
        super().__init__(message)


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
    def __init__(self, message="notification_url is required for webhook subscriptions"):
        super().__init__(message)


class RoomEmailRequiredError(RequiredParameterError):
    def __init__(self, message="room_email is required for room event subscriptions"):
        super().__init__(message)


# Calendar Permission Service Errors
class CalendarPermissionError(CalendarIntegrationError):
    """Base class for calendar permission errors"""

    pass


class InvalidTokenError(CalendarPermissionError):
    def __init__(self, message="Invalid token string provided."):
        super().__init__(message)


class InvalidParameterCombinationError(CalendarPermissionError):
    def __init__(self, message="Specify either calendar_id or event_id, not both."):
        super().__init__(message)


class MissingRequiredParameterError(CalendarPermissionError):
    def __init__(self, message="Either calendar_id or event_id must be specified."):
        super().__init__(message)


class PermissionServiceInitializationError(CalendarPermissionError):
    def __init__(self, error: str):
        super().__init__(f"Error initializing CalendarPermissionCheckService: {error}")


class NoPermissionsSpecifiedError(CalendarPermissionError):
    def __init__(self, message="At least one permission must be specified to create a token."):
        super().__init__(message)


# Model Level Errors
class CalendarModelError(CalendarIntegrationError):
    """Base class for calendar model errors"""

    pass


class RecurrenceExceptionError(CalendarModelError):
    def __init__(self, message="Cannot create exception for non-recurring event"):
        super().__init__(message)


class MissingOrganizationForExceptionError(CalendarModelError):
    def __init__(self, message="CalendarEvent is missing organization (cannot create exception)"):
        super().__init__(message)


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


class CalenderServiceOrganizationNotSetError(CalendarServiceStateError):
    def __init__(self, message="Calendar service is not initialized or authenticated"):
        super().__init__(message)


# Recurrence Utils Errors
class RecurrenceError(CalendarIntegrationError):
    """Errors related to recurrence processing"""

    pass


class NoRecurrenceRuleError(RecurrenceError):
    def __init__(self, message="No recurrence rule provided"):
        super().__init__(message)
