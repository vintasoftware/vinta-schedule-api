from typing import Protocol

from calendar_integration.models import Organization
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)


class InitializedCalendarService(InitializedOrAuthenticatedCalendarService, Protocol):
    organization: Organization
    account: None
    calendar_adapter: None
