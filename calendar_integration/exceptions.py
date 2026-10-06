from django.core.exceptions import ImproperlyConfigured, PermissionDenied

from calendar_integration.constants import AppointmentTypeScopedRuleType


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


class RoomNotBookableError(EventManagementError):
    """An event write allocates a room whose provider link is not bookable.

    Raised for a room that is pending creation, failed on create, pending deletion,
    failed on delete, or archived. Rooms with no provider link are never rejected.
    """

    default_message = "Room is not bookable."


class StaleBookingPreviewError(CalendarIntegrationError):
    """The room's bookings changed since the deletion preview the caller sent.

    Raised by ``BookingResolutionService.validate`` when the fingerprint does not
    match. The caller must request a new preview.
    """

    default_message = "The room's bookings changed since the preview. Request a new preview."


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


# Appointment Type errors
class AppointmentTypeError(CalendarIntegrationError):
    """Base class for AppointmentType-related errors."""

    pass


class AppointmentTypeValidationError(AppointmentTypeError):
    """Raised when AppointmentType input data is invalid."""

    pass


class AppointmentTypeSlotInUseError(AppointmentTypeError):
    """Raised when an `AppointmentTypeSlot` cannot be removed outright because it is
    referenced by a future-booked event.

    Removing one calendar from a slot's roster while the slot itself survives
    never raises this -- that removal is unconditionally lenient (it deletes
    only the `AppointmentTypeSlotMembership` row; see
    `AppointmentTypeService._reconcile_slot`). This error is reserved for
    deleting the whole slot, which would also drop every remaining calendar's
    appointment-type-scoped windows, blocked time, and quota rules for it.
    """

    default_message = (
        "Cannot remove slot because it is referenced by future appointment type bookings."
    )


class AppointmentTypeHasFutureEventsError(AppointmentTypeError):
    """Raised when an appointment type cannot be deleted because it has future bookings."""

    default_message = "Cannot delete AppointmentType because it has future bookings."


class AppointmentTypeSlotConfigNotFoundError(AppointmentTypeError):
    """Raised when a (calendar, appointment type slot) target for appointment-type-scoped availability
    configuration cannot be resolved.

    Deliberately the SAME exception -- same type, same message -- whether the
    membership genuinely does not exist or the acting user is simply not
    authorized to manage it. A member must not be able to learn that an appointment type
    or roster entry exists by comparing error shapes: a plain 404-shaped
    error here is indistinguishable from a 403 in disguise.
    """

    default_message = (
        "No appointment-type-scoped availability configuration found for this calendar and slot."
    )


class AppointmentTypeScopedRuleViolationError(AppointmentTypeError):
    """Raised when a directly-named calendar violates an appointment-type-scoped
    configuration rule for the requested booking/reschedule time.

    Carries ``calendar_id`` and ``rule_type`` (see ``AppointmentTypeScopedRuleType``) so
    callers can build a structured error response -- never the configured
    rule values themselves: enough for an admin to act on, without leaking
    roster detail to external bookers on public links. Naming only the
    quota rule that was violated, never its configured cap or the calendar's
    current count.
    """

    def __init__(
        self,
        calendar_id: int,
        rule_type: str = AppointmentTypeScopedRuleType.OUTSIDE_WINDOW,
        message: str | None = None,
    ) -> None:
        self.calendar_id = calendar_id
        self.rule_type = rule_type
        if message is None:
            message = (
                f"Calendar {calendar_id} is not bookable for the requested time in "
                f"this appointment type ({rule_type})."
            )
        super().__init__(message)


# Calendar Pool errors
class CalendarPoolError(CalendarIntegrationError):
    """Base class for CalendarPool-related errors."""

    pass


class CalendarPoolValidationError(CalendarPoolError):
    """Raised when CalendarPool input data is invalid (e.g. a roster calendar
    id that does not belong to this organization)."""

    pass


class CalendarPoolInUseError(CalendarPoolError):
    """Raised when a `CalendarPool` cannot be deleted because it is still
    attached to at least one `AppointmentTypeSlot`.

    Mirrors `AppointmentTypeHasFutureEventsError`'s refuse-when-referenced
    posture (see the plan's Pool deletion decision), but carries the distinct
    names of every referencing appointment type so the REST layer can name them in a 409
    without a second query.
    """

    def __init__(self, appointment_type_names: list[str]) -> None:
        self.appointment_type_names = appointment_type_names
        names = ", ".join(sorted(appointment_type_names))
        super().__init__(
            f"Cannot delete CalendarPool because it is still attached to slots "
            f"in these appointment_types: {names}."
        )


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


class ResourceDirectoryError(CalendarIntegrationError):
    """Raised when a provider's room directory (Google Directory, Microsoft Places) fails.

    ``is_transient`` tells the push engine whether to retry. A transient error is
    retried with backoff until the link's retry deadline; a non-transient one moves
    the link to sync failed right away. The base class defaults to transient, so an
    unclassified provider failure (5xx, 429, a timeout) is retried rather than
    dropped.
    """

    default_message = "The room directory provider returned an error."
    default_is_transient = True

    def __init__(self, message: str | None = None, *, is_transient: bool | None = None):
        super().__init__(message)
        self.is_transient = self.default_is_transient if is_transient is None else is_transient


class ResourceDirectoryInvalidInputError(ResourceDirectoryError):
    """The provider rejected the request itself (HTTP 400 / 409 / 412 / 422).

    Never transient: sending the same payload again fails the same way.
    """

    default_message = "The room directory provider rejected the request as invalid."
    default_is_transient = False


class ResourceDirectoryPermissionError(ResourceDirectoryError):
    """The provider refused the credentials or the scope (HTTP 401 / 403).

    Transient by design: an IT admin can restore the permission while the push is
    still inside its retry window.
    """

    default_message = "The room directory provider denied access."
    default_is_transient = True


class ResourceDirectoryNotFoundError(ResourceDirectoryError):
    """The room or location does not exist on the provider (HTTP 404).

    Not transient. Callers decide what a missing room means: a delete treats it as
    already done, an update treats it as a sync failure.
    """

    default_message = "The room or location was not found on the provider."
    default_is_transient = False


class ResourceDirectoryNotWriteEnabledError(ResourceDirectoryPermissionError):
    """The organization has no write-enabled connection for the provider.

    Raised by ``ResourceDirectoryAdapterResolver.adapter_for``. It is a permission
    error, and so transient, because an org admin can verify write access again
    while a push is still being retried.
    """

    default_message = "Room writes are not enabled for this organization and provider."


class MicrosoftConnectionNotConfiguredError(CalendarIntegrationError):
    """``MS_CLIENT_ID`` or ``MS_CLIENT_SECRET`` is empty, so no Microsoft call can be made."""

    default_message = "Microsoft room sync is not configured on this environment."


class MicrosoftAppOnlyTokenError(CalendarIntegrationError):
    """The Microsoft identity platform refused, or never answered, an app-only token request.

    ``status_code`` and ``error_code`` (the OAuth ``error`` field, such as
    ``invalid_client`` or ``unauthorized_client``) are kept for callers. The
    provider's ``error_description`` is not, because it is free text.
    """

    default_message = "Could not get an app-only token from Microsoft."

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class MicrosoftConsentStateError(CalendarIntegrationError):
    """The admin-consent ``state`` is tampered, expired, already used or not this org's."""

    default_message = "The Microsoft admin-consent link is invalid or was already used."


class MicrosoftConsentDeniedError(CalendarIntegrationError):
    """The tenant admin did not grant admin consent: Microsoft sent no authorization code."""

    default_message = "Microsoft admin consent was not granted."


class MicrosoftSignInError(CalendarIntegrationError):
    """The admin's sign-in could not be confirmed with Microsoft.

    The authorization code was refused, or the returned ``id_token`` was not issued to
    Vinta's app for this consent attempt, or it named no tenant.
    """

    default_message = "Could not confirm the Microsoft sign-in."


class MicrosoftSignInNotAdminError(MicrosoftSignInError):
    """The user who signed in is not an administrator who can grant admin consent.

    Their ``id_token`` carries neither the Global Administrator nor the Privileged Role
    Administrator role, so the sign-in does not prove control of the tenant.
    """

    default_message = "The Microsoft sign-in was not made by a tenant administrator."
