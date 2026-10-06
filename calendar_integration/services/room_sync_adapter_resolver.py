"""Finds the real Google or Microsoft room directory adapter for an organization."""

from typing import TYPE_CHECKING

from calendar_integration.constants import CalendarProvider
from calendar_integration.exceptions import ResourceDirectoryNotWriteEnabledError
from calendar_integration.models import (
    GoogleCalendarServiceAccount,
    MicrosoftOrganizationConnection,
)
from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
    GoogleCalendarAdapter,
)
from calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter import (
    MSOutlookCalendarAdapter,
)
from calendar_integration.services.calendar_clients.ms_app_only_token import (
    MicrosoftAppOnlyTokenProvider,
)
from calendar_integration.services.protocols.resource_directory_adapter import (
    ResourceDirectoryAdapter,
)
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC, is_enabled


if TYPE_CHECKING:
    from organizations.models import Organization


class RoomSyncAdapterResolver:
    """``ResourceDirectoryAdapterResolver`` backed by the organization-level connections.

    Google uses the organization's service account (the one with no calendar) with the
    write scope. Microsoft uses the organization's ``MicrosoftOrganizationConnection``
    through an app-only token. Reads tenant-scoped rows, so the organization must be
    bound by the caller.
    """

    def __init__(self, microsoft_token_provider: MicrosoftAppOnlyTokenProvider) -> None:
        self.microsoft_token_provider = microsoft_token_provider

    def adapter_for(self, organization: "Organization", provider: str) -> ResourceDirectoryAdapter:
        """The adapter for ``organization`` on ``provider``.

        Raises:
            ResourceDirectoryNotWriteEnabledError: the flag is off, or the provider's
                connection is missing or not write-enabled.
        """
        if not self.is_write_enabled(organization, provider):
            raise ResourceDirectoryNotWriteEnabledError()

        if provider == CalendarProvider.GOOGLE:
            service_account = self._google_service_account(organization)
            if service_account is None:
                raise ResourceDirectoryNotWriteEnabledError()
            return GoogleCalendarAdapter.from_service_account_model(service_account, write=True)

        connection = self._microsoft_connection(organization)
        if connection is None:
            raise ResourceDirectoryNotWriteEnabledError()
        return MSOutlookCalendarAdapter.from_app_only(connection, self.microsoft_token_provider)

    def is_write_enabled(self, organization: "Organization", provider: str) -> bool:
        """True when the flag is on and the provider's connection is write-enabled."""
        if not is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id):
            return False
        if provider == CalendarProvider.GOOGLE:
            account = self._google_service_account(organization)
            return account is not None and account.write_enabled
        if provider == CalendarProvider.MICROSOFT:
            connection = self._microsoft_connection(organization)
            return connection is not None and connection.write_enabled
        return False

    @staticmethod
    def _google_service_account(
        organization: "Organization",
    ) -> GoogleCalendarServiceAccount | None:
        return (
            GoogleCalendarServiceAccount.objects.filter_by_organization(organization.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )

    @staticmethod
    def _microsoft_connection(
        organization: "Organization",
    ) -> MicrosoftOrganizationConnection | None:
        return MicrosoftOrganizationConnection.objects.filter_by_organization(
            organization.id
        ).first()
