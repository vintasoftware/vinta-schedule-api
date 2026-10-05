"""Connect an organization to its Microsoft 365 tenant through admin consent.

The round trip:

1. An org admin asks for a consent URL. ``build_consent_url`` stores a fresh random
   nonce on the organization's ``MicrosoftOrganizationConnection`` and signs
   ``{organization id, nonce}`` into the OAuth ``state``.
2. A Global Administrator of the customer's tenant opens the URL and grants admin
   consent to Vinta's multi-tenant Entra app. Microsoft redirects the browser to the
   callback with ``tenant``, ``admin_consent`` and the same ``state``.
3. ``complete_consent`` checks the signature and the age of the state, then clears
   the stored nonce in the same row lock that saves the tenant id. A state is
   therefore good for one callback only, and only for the organization it was
   issued to.
4. ``verify`` mints an app-only token for the tenant, checks its ``roles`` claim and
   reads one building from Microsoft Places. Only then is ``write_enabled`` set.
"""

import dataclasses
import datetime
import logging
import secrets
import urllib.parse
from typing import Any

from django.core import signing
from django.db import transaction
from django.utils import timezone

import requests

from calendar_integration.exceptions import (
    MicrosoftAppOnlyTokenError,
    MicrosoftConnectionNotConfiguredError,
    MicrosoftConsentDeniedError,
    MicrosoftConsentStateError,
)
from calendar_integration.models import MicrosoftOrganizationConnection
from calendar_integration.services.calendar_clients.ms_app_only_token import (
    GRAPH_DEFAULT_SCOPE,
    MicrosoftAppOnlyTokenProvider,
    is_tenant_id,
)
from organizations.models import Organization


logger = logging.getLogger(__name__)

ADMIN_CONSENT_URL = "https://login.microsoftonline.com/organizations/v2.0/adminconsent"
GRAPH_BUILDINGS_URL = "https://graph.microsoft.com/v1.0/places/microsoft.graph.building"
GRAPH_REQUEST_TIMEOUT_SECONDS = 15

CONSENT_STATE_SALT = "calendar_integration.microsoft_connection.consent_state"
CONSENT_STATE_MAX_AGE = datetime.timedelta(hours=1)

REQUIRED_ROLES = frozenset({"Place.ReadWrite.All", "Calendars.Read"})

EXCHANGE_RBAC_REMINDER = (
    "Room writes also need an Exchange administrator to assign the "
    "TenantPlacesManagement and MailRecipient roles to the Vinta Schedule app "
    "(see the Microsoft room sync setup guide)."
)
NOT_CONSENTED_MESSAGE = (
    "No Microsoft 365 tenant is connected yet. Ask a Global Administrator of your "
    "tenant to open the admin consent link and accept it."
)


@dataclasses.dataclass(frozen=True)
class MicrosoftConnectionVerification:
    """The outcome of ``MicrosoftConnectionService.verify``."""

    write_enabled: bool
    verified_at: datetime.datetime | None
    error: str


class MicrosoftConnectionService:
    """Admin consent and write-access verification for an organization's Microsoft tenant."""

    def __init__(self, token_provider: MicrosoftAppOnlyTokenProvider):
        self.token_provider = token_provider

    def build_consent_url(self, organization: Organization, redirect_uri: str) -> str:
        """Return the admin-consent URL for ``organization``.

        Each call issues a new nonce, so only the newest link works.

        Raises:
            MicrosoftConnectionNotConfiguredError: Vinta's Entra app is not configured.
        """
        if not self.token_provider.is_configured:
            raise MicrosoftConnectionNotConfiguredError()
        nonce = secrets.token_urlsafe(32)
        with transaction.atomic():
            connection, _ = MicrosoftOrganizationConnection.objects.filter_by_organization(
                organization
            ).get_or_create(organization=organization)
            connection.consent_state = nonce
            connection.save(update_fields=["consent_state", "modified"])
        state = signing.dumps({"org": organization.pk, "nonce": nonce}, salt=CONSENT_STATE_SALT)
        query = urllib.parse.urlencode(
            {
                "client_id": self.token_provider.client_id,
                "scope": GRAPH_DEFAULT_SCOPE,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{ADMIN_CONSENT_URL}?{query}"

    def organization_id_from_state(self, state: str) -> int:
        """The organization id a consent ``state`` was issued to, once its signature checks out.

        This proves only that Vinta issued the state within the last hour. Whether it
        is still unused is decided by ``complete_consent``.

        Raises:
            MicrosoftConsentStateError: the state is tampered, expired or malformed.
        """
        return self._read_state(state)[0]

    def complete_consent(
        self, state: str, tenant: str, admin_consent: str
    ) -> MicrosoftOrganizationConnection:
        """Store the consenting tenant on the organization the ``state`` was issued to.

        The nonce is used up whether or not consent was granted.

        Raises:
            MicrosoftConsentStateError: the state is tampered, expired, already used, or
                was not issued to the organization it names.
            MicrosoftConsentDeniedError: the admin declined, or Microsoft sent no valid
                tenant id. The nonce is still used up.
        """
        organization_id, nonce = self._read_state(state)
        granted = admin_consent.lower() == "true" and is_tenant_id(tenant)
        with transaction.atomic():
            connection = (
                MicrosoftOrganizationConnection.objects.filter_by_organization(organization_id)
                .select_for_update()
                .filter(consent_state=nonce)
                .first()
            )
            if connection is None:
                raise MicrosoftConsentStateError()
            connection.consent_state = ""
            update_fields = ["consent_state", "modified"]
            if granted:
                connection.tenant_id = tenant.lower()
                connection.consented_at = timezone.now()
                # A new consent has not been verified yet, whatever the old one was.
                connection.write_enabled = False
                connection.verified_at = None
                connection.last_verification_error = ""
                update_fields += [
                    "tenant_id",
                    "consented_at",
                    "write_enabled",
                    "verified_at",
                    "last_verification_error",
                ]
            connection.save(update_fields=update_fields)
        if not granted:
            logger.info("Microsoft admin consent not granted for organization %s", organization_id)
            raise MicrosoftConsentDeniedError()
        logger.info("Microsoft admin consent stored for organization %s", organization_id)
        return connection

    def verify(self, organization: Organization) -> MicrosoftConnectionVerification:
        """Check that the connected tenant lets Vinta write rooms, and record the outcome.

        Raises:
            MicrosoftConnectionNotConfiguredError: Vinta's Entra app is not configured.
        """
        connection = MicrosoftOrganizationConnection.objects.filter_by_organization(
            organization
        ).first()
        if connection is None or not connection.tenant_id:
            if connection is not None:
                self._record_failure(connection, NOT_CONSENTED_MESSAGE)
            return MicrosoftConnectionVerification(
                write_enabled=False,
                verified_at=connection.verified_at if connection is not None else None,
                error=NOT_CONSENTED_MESSAGE,
            )

        error = self._check_write_access(connection.tenant_id)
        if error is not None:
            self._record_failure(connection, f"{error} {EXCHANGE_RBAC_REMINDER}")
        else:
            connection.write_enabled = True
            connection.verified_at = timezone.now()
            connection.last_verification_error = ""
            connection.save(
                update_fields=[
                    "write_enabled",
                    "verified_at",
                    "last_verification_error",
                    "modified",
                ]
            )
        return MicrosoftConnectionVerification(
            write_enabled=connection.write_enabled,
            verified_at=connection.verified_at,
            error=connection.last_verification_error,
        )

    def _check_write_access(self, tenant_id: str) -> str | None:
        """Return a remediation message, or ``None`` when the tenant passes every check."""
        try:
            token = self.token_provider.get_token(tenant_id)
        except MicrosoftAppOnlyTokenError:
            return (
                "Vinta Schedule could not get an access token for your Microsoft 365 "
                "tenant. Admin consent may have been revoked: ask a Global "
                "Administrator to open the admin consent link again."
            )

        missing = sorted(REQUIRED_ROLES - token.roles)
        if missing:
            return (
                "The Vinta Schedule app is missing these Microsoft Graph application "
                f"permissions: {', '.join(missing)}. Ask a Global Administrator to open "
                "the admin consent link again and accept every permission."
            )

        try:
            response = requests.get(
                GRAPH_BUILDINGS_URL,
                params={"$top": "1"},
                headers={"Authorization": f"Bearer {token.access_token}"},
                timeout=GRAPH_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Microsoft Places check failed for tenant %s: %s", tenant_id, type(exc).__name__
            )
            return "Vinta Schedule could not reach Microsoft Graph. Try again in a few minutes."
        if response.status_code != 200:
            logger.warning(
                "Microsoft Places check for tenant %s returned HTTP %s",
                tenant_id,
                response.status_code,
            )
            return (
                f"Microsoft Graph refused to list your buildings (HTTP {response.status_code}). "
                "Check that the Vinta Schedule app still has Place.ReadWrite.All."
            )
        return None

    def _record_failure(self, connection: MicrosoftOrganizationConnection, message: str) -> None:
        connection.write_enabled = False
        connection.last_verification_error = message
        connection.save(update_fields=["write_enabled", "last_verification_error", "modified"])

    @staticmethod
    def _read_state(state: str) -> tuple[int, str]:
        try:
            payload: Any = signing.loads(
                state, salt=CONSENT_STATE_SALT, max_age=CONSENT_STATE_MAX_AGE
            )
        except signing.BadSignature:
            # SignatureExpired is a BadSignature too.
            raise MicrosoftConsentStateError() from None
        if not isinstance(payload, dict):
            raise MicrosoftConsentStateError()
        organization_id = payload.get("org")
        nonce = payload.get("nonce")
        # bool is an int subclass; a state never carries one, so refuse it outright.
        if (
            not isinstance(organization_id, int)
            or isinstance(organization_id, bool)
            or not isinstance(nonce, str)
            or not nonce
        ):
            raise MicrosoftConsentStateError()
        return organization_id, nonce
