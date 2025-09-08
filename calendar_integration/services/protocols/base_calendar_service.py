from typing import Protocol

from allauth.socialaccount.models import SocialAccount

from calendar_integration.models import (
    CalendarEventUpdateToken,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from organizations.models import Organization
from public_api.models import SystemUser
from users.models import User


class BaseCalendarService(Protocol):
    calendar_side_effects_service: CalendarSideEffectsService | None

    @staticmethod
    def get_calendar_adapter_for_account(
        account: User | GoogleCalendarServiceAccount,
    ) -> tuple[CalendarAdapter, SocialAccount | GoogleCalendarServiceAccount]:
        ...

    def authenticate(
        self,
        account: SocialAccount | GoogleCalendarServiceAccount,
        organization: Organization,
    ) -> None:
        ...

    def initialize_without_provider(
        self,
        user: User | CalendarEventUpdateToken | SystemUser | None = None,
        organization: Organization | None = None,
    ) -> None:
        ...
