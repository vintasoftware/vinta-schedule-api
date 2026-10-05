import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from calendar_integration.exceptions import (
    ResourceDirectoryError,
    ResourceDirectoryPermissionError,
)
from calendar_integration.models import GoogleCalendarServiceAccount
from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
    GoogleCalendarAdapter,
)
from calendar_integration.services.dataclasses import GoogleWriteAccessResult


if TYPE_CHECKING:
    from organizations.models import Organization


logger = logging.getLogger(__name__)


class GoogleWriteAccessService:
    """Checks whether an organization's Google service account may write rooms.

    Room writes go only through the organization-level service account (the one
    with no calendar). ``write_enabled`` on it is set only after a client built
    with the write scope makes a Directory call successfully.
    """

    NO_SERVICE_ACCOUNT_MESSAGE = (
        "No organization-level Google service account is configured for this organization."
    )
    INVALID_KEY_MESSAGE = (
        "The Google service account key could not be loaded. Upload the service "
        "account's key again, then verify write access."
    )
    REMEDIATION_MESSAGE = (
        "Google refused room write access. In the Google Admin Console, grant the "
        "https://www.googleapis.com/auth/admin.directory.resource.calendar scope to the "
        "service account under Security > API controls > Domain-wide delegation, then "
        "verify write access again."
    )
    UNAVAILABLE_MESSAGE = (
        "Google could not be reached to verify write access. Try again in a few minutes."
    )

    def verify(self, organization: "Organization") -> GoogleWriteAccessResult:
        """Verify room write access and record the outcome on the service account.

        - Success sets ``write_enabled`` and ``write_verified_at``.
        - A refused permission, a rejected request or an unloadable key clears
          ``write_enabled`` and returns a remediation message.
        - A transient failure (Google unreachable, 5xx, 429) changes nothing, so a
          Google outage does not switch room writes off.
        """
        service_account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(organization.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        if service_account is None:
            return GoogleWriteAccessResult(
                write_enabled=False,
                write_verified_at=None,
                error=self.NO_SERVICE_ACCOUNT_MESSAGE,
            )

        try:
            adapter = GoogleCalendarAdapter.from_service_account(
                {
                    "account_id": str(service_account.id),
                    "email": service_account.email,
                    "private_key_id": service_account.private_key_id,
                    "private_key": service_account.private_key,
                    "admin_email": service_account.admin_email,
                },
                write=True,
            )
        except ValueError:
            logger.warning(
                "Google service account %s key could not be loaded (organization %s)",
                service_account.id,
                organization.id,
            )
            return self._record_failure(service_account, self.INVALID_KEY_MESSAGE)

        try:
            adapter.verify_room_write_access()
        except ResourceDirectoryError as error:
            logger.warning(
                "Google room write access check failed for service account %s "
                "(organization %s): %s",
                service_account.id,
                organization.id,
                error,
            )
            if error.is_transient and not isinstance(error, ResourceDirectoryPermissionError):
                return GoogleWriteAccessResult(
                    write_enabled=service_account.write_enabled,
                    write_verified_at=service_account.write_verified_at,
                    error=self.UNAVAILABLE_MESSAGE,
                )
            return self._record_failure(service_account, self.REMEDIATION_MESSAGE)

        service_account.write_enabled = True
        service_account.write_verified_at = timezone.now()
        service_account.save(update_fields=["write_enabled", "write_verified_at", "modified"])
        return GoogleWriteAccessResult(
            write_enabled=True,
            write_verified_at=service_account.write_verified_at,
            error="",
        )

    @staticmethod
    def _record_failure(
        service_account: GoogleCalendarServiceAccount, message: str
    ) -> GoogleWriteAccessResult:
        service_account.write_enabled = False
        service_account.save(update_fields=["write_enabled", "modified"])
        return GoogleWriteAccessResult(
            write_enabled=False,
            write_verified_at=service_account.write_verified_at,
            error=message,
        )
