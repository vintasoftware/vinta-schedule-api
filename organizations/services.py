from organizations.models import Organization


class OrganizationService:
    def create_organization(self, name: str, should_sync_rooms: bool = False) -> Organization:
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
        return self.organization
