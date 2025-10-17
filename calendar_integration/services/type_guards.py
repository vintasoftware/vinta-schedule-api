from typing import TypeGuard

from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
    ServiceNotAuthenticatedError,
    ServiceNotInitializedError,
)
from calendar_integration.services.protocols.authenticated_calendar_service import (
    AuthenticatedCalendarService,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.initialized_calendar_service import (
    InitializedCalendarService,
)
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)


def is_authenticated_calendar_service(
    calendar_service: BaseCalendarService | InitializedOrAuthenticatedCalendarService,
    raise_error: bool = True,
) -> TypeGuard[AuthenticatedCalendarService]:
    """
    Check if the calendar service is authenticated.
    An authenticated calendar service has an associated account and calendar adapter.

    Args:
        calendar_service: The calendar service instance to check.
        raise_error: Whether to raise an error if the check fails.

    Returns:
        True if the calendar service is authenticated, False otherwise.

    Raises:
        ServiceNotAuthenticatedError: If the calendar service is not authenticated and raise_error is True.
    """

    if (
        hasattr(calendar_service, "organization")
        and calendar_service.organization is not None
        and hasattr(calendar_service, "account")
        and calendar_service.account is not None
        and hasattr(calendar_service, "calendar_adapter")
        and calendar_service.calendar_adapter is not None
    ):
        return True

    if not raise_error:
        return False

    raise ServiceNotAuthenticatedError("Calendar service is not authenticated")


def is_initialized_calendar_service(
    calendar_service: BaseCalendarService, raise_error: bool = True
) -> TypeGuard[InitializedCalendarService]:
    """
    Check if the calendar service is initialized.
    An initialized calendar service has an associated organization but no account or calendar adapter.

    Args:
        calendar_service: The calendar service instance to check.
        raise_error: Whether to raise an error if the check fails.

    Returns:
        True if the calendar service is initialized, False otherwise.

    Raises:
        ServiceNotInitializedError: If the calendar service is not initialized and raise_error is True.
    """

    if (
        hasattr(calendar_service, "organization")
        and calendar_service.organization is not None
        and hasattr(calendar_service, "account")
        and calendar_service.account is None
        and hasattr(calendar_service, "calendar_adapter")
        and calendar_service.calendar_adapter is None
    ):
        return True

    if not raise_error:
        return False

    raise ServiceNotInitializedError("Calendar service is not initialized without provider")


def is_initialized_or_authenticated_calendar_service(
    calendar_service: BaseCalendarService, raise_error: bool = True
) -> TypeGuard[InitializedOrAuthenticatedCalendarService]:
    """
    Check if the calendar service is initialized or authenticated.
    An initialized or authenticated calendar service has an associated organization.

    Args:
        calendar_service: The calendar service instance to check.
        raise_error: Whether to raise an error if the check fails.

    Returns:
        True if the calendar service is initialized or authenticated, False otherwise.

    Raises:
        CalendarServiceOrganizationNotSet: If the calendar service is not initialized or authenticated and raise_error is True.
    """

    if hasattr(calendar_service, "organization") and calendar_service.organization is not None:
        return True

    if not raise_error:
        return False

    raise CalendarServiceOrganizationNotSetError(
        "Calendar service is not initialized or authenticated"
    )
