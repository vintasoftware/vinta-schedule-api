from django.core.exceptions import ImproperlyConfigured, PermissionDenied

from calendar_integration.constants import GroupScopedRuleType


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


# Webhook Exceptions
class WebhookValidationError(CalendarIntegrationError):
    """Raised when webhook payload validation fails"""

    default_message = "Invalid webhook payload received"


class WebhookAuthenticationError(CalendarIntegrationError):
    """Raised when webhook authentication/verification fails"""

    default_message = "Webhook authentication failed"


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


class TokenExpiredError(CalendarPermissionError):
    default_message = "The token has expired."


class TokenAlreadyUsedError(CalendarPermissionError):
    default_message = "The token has already been used."


class TokenRevokedError(CalendarPermissionError):
    default_message = "The token has been revoked."


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


class WebhookProcessingError(CalendarIntegrationError):
    """Errors during webhook processing"""

    pass


class WebhookIgnoredError(WebhookProcessingError):
    default_message = "Webhook event ignored as per processing rules"


class WebhookProcessingFailedError(WebhookProcessingError):
    default_message = "Webhook event processing failed due to an internal error"


# Change Request Errors


class ChangeRequestError(CalendarIntegrationError):
    """Base class for ExternalEventChangeRequest lifecycle errors."""

    pass


class ChangeRequestNotPendingError(ChangeRequestError):
    """Raised when an action requires a PENDING request but the request is not PENDING.

    The REST/GraphQL layer maps this to HTTP 409 Conflict.
    """

    default_message = "This change request is no longer pending and cannot be resolved."


class ChangeRequestIneligibleError(ChangeRequestError, PermissionDenied):
    """Raised when a membership is not eligible to resolve a change request.

    The REST/GraphQL layer maps this to HTTP 403 Forbidden.
    """

    default_message = "You are not eligible to resolve this change request."


# Calendar Group errors
class CalendarGroupError(CalendarIntegrationError):
    """Base class for CalendarGroup-related errors."""

    pass


class CalendarGroupValidationError(CalendarGroupError):
    """Raised when CalendarGroup input data is invalid."""

    pass


class CalendarGroupSlotInUseError(CalendarGroupError):
    """Raised when a `CalendarGroupSlot` cannot be removed outright because it is
    referenced by a future-booked event.

    Removing one calendar from a slot's roster while the slot itself survives
    never raises this -- that removal is unconditionally lenient (it deletes
    only the `CalendarGroupSlotMembership` row; see
    `CalendarGroupService._reconcile_slot`). This error is reserved for
    deleting the whole slot, which would also drop every remaining calendar's
    group-scoped windows, blocked time, and quota rules for it.
    """

    default_message = "Cannot remove slot because it is referenced by future group bookings."


class CalendarGroupHasFutureEventsError(CalendarGroupError):
    """Raised when a group cannot be deleted because it has future bookings."""

    default_message = "Cannot delete CalendarGroup because it has future bookings."


class CalendarGroupSlotConfigNotFoundError(CalendarGroupError):
    """Raised when a (calendar, group slot) target for group-scoped availability
    configuration cannot be resolved.

    Deliberately the SAME exception -- same type, same message -- whether the
    membership genuinely does not exist or the acting user is simply not
    authorized to manage it. A member must not be able to learn that a group
    or roster entry exists by comparing error shapes: a plain 404-shaped
    error here is indistinguishable from a 403 in disguise.
    """

    default_message = "No group-scoped availability configuration found for this calendar and slot."


class CalendarGroupScopedRuleViolationError(CalendarGroupError):
    """Raised when a directly-named calendar violates a group-scoped
    configuration rule for the requested booking/reschedule time.

    Carries ``calendar_id`` and ``rule_type`` (see ``GroupScopedRuleType``) so
    callers can build a structured error response -- never the configured
    rule values themselves: enough for an admin to act on, without leaking
    roster detail to external bookers on public links. Naming only the
    quota rule that was violated, never its configured cap or the calendar's
    current count.
    """

    def __init__(
        self,
        calendar_id: int,
        rule_type: str = GroupScopedRuleType.OUTSIDE_WINDOW,
        message: str | None = None,
    ) -> None:
        self.calendar_id = calendar_id
        self.rule_type = rule_type
        if message is None:
            message = (
                f"Calendar {calendar_id} is not bookable for the requested time in "
                f"this group ({rule_type})."
            )
        super().__init__(message)


# Bookable Slots errors
class BookableSlotsValidationError(CalendarIntegrationError):
    """Raised when single-calendar / bundle bookable-slot input data is invalid."""

    pass


# Booking Policy Errors
class DuplicateBookingPolicyError(CalendarIntegrationError):
    """Raised when a second BookingPolicy is created for the same target/org.

    Callers (REST serializers, GraphQL mutations) should map this to a 400 /
    validation error with the message surfaced to the client.
    """

    pass


class BookingPolicyViolationError(CalendarIntegrationError):
    """Raised when a booking request violates the resolved EffectivePolicy.

    The violation may be due to lead-time (too soon), max-horizon (too far
    ahead), or a buffer envelope (the requested window overlaps the dead zone
    of an existing event).  Callers (GraphQL mutations) should map this to a
    user-facing error explaining that the slot is not available under the
    current policy.
    """

    default_message = "The requested time slot is not available under the current booking policy."


# External Client Identifier Errors
class ExternalClientIdentifierError(CalendarIntegrationError):
    """Base class for ``ExternalClientIdentifierService`` write rejections."""

    pass


class ExternalClientIdentifierInvalidTargetError(ExternalClientIdentifierError):
    """Raised when the write target's model is outside ``IDENTIFIABLE_MODELS``.

    The table is generic (any ``ContentType``), but the write surface is
    allowlisted -- see ``calendar_integration.external_client_identifiers``.
    """

    def __init__(self, model_label: str):
        self.model_label = model_label
        super().__init__(f"External client identifiers cannot be attached to '{model_label}'.")


class ExternalClientIdentifierCrossOrganizationError(ExternalClientIdentifierError):
    """Raised when the write target's organization differs from the service's bound
    organization.

    A ``GenericForeignKey`` cannot be an ``OrganizationSafeForeignKey``, so nothing
    at the schema level stops an identifier row from pointing at a record in
    another organization. This is the code-enforced half of that guarantee.
    """

    default_message = "Target does not belong to the current organization."


class ExternalClientIdentifierInvalidSystemError(ExternalClientIdentifierError):
    """Raised when an incoming ``system`` does not parse as a valid absolute URL.

    ``system`` is stored in a ``URLField`` and is the leading match column (after
    ``organization``/``content_type``) of ``extclientid_uniq_system_ident``, so an
    unparseable value cannot be a stable lookup key. The model's own ``URLField``
    validators only run through ``Model.full_clean()``, which
    ``ExternalClientIdentifierService.replace_for_target`` never calls (it writes via
    ``bulk_create``), so this check has to run explicitly here -- the one place every
    write path (GraphQL, REST) funnels through.
    """

    default_message = "system must be a valid URL."


class ExternalClientIdentifierBlankIdentifierError(ExternalClientIdentifierError):
    """Raised when an incoming ``identifier`` is blank or whitespace-only."""

    default_message = "identifier must not be blank."


class ExternalClientIdentifierTooLongError(ExternalClientIdentifierError):
    """Raised when an incoming ``identifier`` exceeds the 255-character column limit."""

    default_message = "identifier must be at most 255 characters."


class ExternalClientIdentifierDuplicateSystemError(ExternalClientIdentifierError):
    """Raised when one incoming list has two pairs that normalize to the same ``system``.

    We reject this instead of picking the last one. Picking the last one would be a
    silent trap: a caller who sends ``[{crm, "A"}, {crm, "B"}]`` would get ``B``
    stored, while believing ``A`` was set. A later lookup for ``A`` would find
    nothing, with no error to explain why. Phase 3 passes this list straight through
    from an external API caller, so the ambiguity must surface as an error here
    instead of being resolved by list order.
    """

    default_message = "Duplicate system in identifiers list; each system must appear at most once."
