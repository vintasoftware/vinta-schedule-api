import datetime
from typing import Annotated

from dependency_injector.wiring import Provide, inject

from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization, OrganizationMembership
from users.models import User


class OrganizationService:
    @inject
    def __init__(self, calendar_service: Annotated[CalendarService, Provide["calendar_service"]]):
        self.calendar_service = calendar_service

    def create_organization(
        self, creator: User, name: str, should_sync_rooms: bool = False
    ) -> Organization:
        """
        Create a new calendar organization.
        :param name: Name of the calendar organization.
        :param should_sync_rooms: Whether to sync rooms for this organization.
        :return: Created Organization instance.
        """
        self.organization = Organization.objects.create(
            name=name,
            should_sync_rooms=should_sync_rooms,
        )
        OrganizationMembership.objects.create(user=creator, organization=self.organization)

        if should_sync_rooms:
            self.calendar_service.initialize_without_provider(
                user_or_token=creator, organization=self.organization
            )
            now = datetime.datetime.now(tz=datetime.UTC)
            self.calendar_service.request_organization_calendar_resources_import(
                start_time=now,
                end_time=now + datetime.timedelta(days=365),
            )
        return self.organization
