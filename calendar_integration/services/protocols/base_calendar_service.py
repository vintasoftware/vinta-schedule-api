from typing import Protocol

from allauth.socialaccount.models import SocialAccount

from calendar_integration.models import (
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from organizations.models import Organization


class BaseCalendarService(Protocol):
    @staticmethod
    def get_calendar_adapter_for_account(
        account: SocialAccount | GoogleCalendarServiceAccount,
    ) -> CalendarAdapter:
        ...

    def authenticate(
        self,
        account: SocialAccount | GoogleCalendarServiceAccount,
        organization: Organization,
    ) -> None:
        ...

    def initialize_without_provider(
        self,
        organization: Organization | None = None,
    ) -> None:
        ...
