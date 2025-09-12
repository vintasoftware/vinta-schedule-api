from typing import Protocol

from calendar_integration.models import Organization
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)
from public_api.models import SystemUser
from users.models import User


class InitializedCalendarService(InitializedOrAuthenticatedCalendarService, Protocol):
    organization: Organization
    account: None
    calendar_adapter: None
    user_or_token: User | str | SystemUser | None
