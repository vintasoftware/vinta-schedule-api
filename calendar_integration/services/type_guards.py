from typing import TypeGuard

from calendar_integration.services.protocols.authenticated_calendar_service import (
    AuthenticatedCalendarService,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.initialized_calendar_service import (
    NoProviderCalendarService,
)


def is_calendar_service_authenticated(
    calendar_service: BaseCalendarService,
) -> TypeGuard[AuthenticatedCalendarService]:
    return (
        hasattr(calendar_service, "organization")
        and calendar_service.organization is not None
        and hasattr(calendar_service, "account")
        and calendar_service.account is not None
        and hasattr(calendar_service, "calendar_adapter")
        and calendar_service.calendar_adapter is not None
    )


def is_calendar_service_initialized_without_provider(
    calendar_service: BaseCalendarService,
) -> TypeGuard[NoProviderCalendarService]:
    return (
        hasattr(calendar_service, "organization")
        and calendar_service.organization is not None
        and hasattr(calendar_service, "account")
        and calendar_service.account is None
        and hasattr(calendar_service, "calendar_adapter")
        and calendar_service.calendar_adapter is None
    )


def is_calendar_service_initialized_or_authenticated(
    calendar_service: BaseCalendarService,
) -> TypeGuard[NoProviderCalendarService]:
    return hasattr(calendar_service, "organization") and calendar_service.organization is not None
