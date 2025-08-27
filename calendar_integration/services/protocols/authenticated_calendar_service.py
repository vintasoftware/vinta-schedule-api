from allauth.socialaccount.models import SocialAccount

from calendar_integration.models import GoogleCalendarServiceAccount, Organization
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter


class AuthenticatedCalendarService(BaseCalendarService):
    organization: Organization
    account: SocialAccount | GoogleCalendarServiceAccount
    calendar_adapter: CalendarAdapter
