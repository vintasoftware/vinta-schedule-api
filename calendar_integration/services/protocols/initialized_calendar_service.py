from calendar_integration.models import Organization
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService


class NoProviderCalendarService(BaseCalendarService):
    organization: Organization
    account: None
    calendar_adapter: None
