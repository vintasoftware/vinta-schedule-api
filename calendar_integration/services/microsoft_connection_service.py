"""Connect an organization to its Microsoft 365 tenant through admin consent.

The round trip:

1. An org admin asks for a consent URL. ``build_consent_url`` stores a fresh random
   nonce on the organization's ``MicrosoftOrganizationConnection`` and signs
   ``{organization id, nonce}`` into the OAuth ``state``.
2. A Global Administrator of the customer's tenant opens the URL, signs in, and
   grants admin consent to Vinta's multi-tenant Entra app (an OpenID Connect
   authorization-code request with ``prompt=admin_consent``). Microsoft redirects the
   browser to the callback with an authorization ``code`` and the same ``state``.
3. ``complete_consent`` checks the signature and the age of the state, then clears
   the stored nonce under a row lock, so a state is good for one callback only, and
   only for the organization it was issued to. It then redeems the code with Vinta's
   client secret and stores the ``tid`` of the returned ``id_token``.

   The tenant never comes from the callback's query string. Anyone holding a state
   could write that, and would then connect a tenant they never signed in to.
4. ``verify`` mints a new app-only token for the tenant (never a cached one), checks
   its ``roles`` claim and
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
    SIGN_IN_SCOPE,
    MicrosoftAppOnlyTokenProvider,
)
from organizations.models import Organization


logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
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
                "response_type": "code",
                "response_mode": "query",
                "redirect_uri": redirect_uri,
                "scope": SIGN_IN_SCOPE,
                # Shows the tenant-wide consent screen, which only an admin can accept.
                "prompt": "admin_consent",
                "state": state,
                # Echoed back inside the id_token, tying it to this attempt.
                "nonce": nonce,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def organization_id_from_state(self, state: str) -> int:
        """The organization id a consent ``state`` was issued to, once its signature checks out.

        This proves only that Vinta issued the state within the last hour. Whether it
        is still unused is decided by ``complete_consent``.

        Raises:
            MicrosoftConsentStateError: the state is tampered, expired or malformed.
        """
        return self._read_state(state)[0]

    def complete_consent(
        self, state: str, code: str, redirect_uri: str
    ) -> MicrosoftOrganizationConnection:
        """Store the tenant the admin signed in to on the organization ``state`` was issued to.

        ``code`` is the authorization code from the callback, and ``redirect_uri`` must
        be the one the consent URL carried. The nonce is used up before the code is
        redeemed, so a state works once whatever happens next.

        Raises:
            MicrosoftConsentStateError: the state is tampered, expired, already used, or
                was not issued to the organization it names.
            MicrosoftConsentDeniedError: the admin declined (no code came back).
            MicrosoftSignInError: Microsoft did not confirm the sign-in for this attempt.
        """
        organization_id, nonce = self._read_state(state)
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
            connection.save(update_fields=["consent_state", "modified"])
        if not code:
            logger.info("Microsoft admin consent not granted for organization %s", organization_id)
            raise MicrosoftConsentDeniedError()

        tenant_id = self.token_provider.tenant_from_sign_in(
            code, redirect_uri=redirect_uri, nonce=nonce
        )
        connection.tenant_id = tenant_id
        connection.consented_at = timezone.now()
        # A new consent has not been verified yet, whatever the old one was.
        connection.write_enabled = False
        connection.verified_at = None
        connection.last_verification_error = ""
        connection.save(
            update_fields=[
                "tenant_id",
                "consented_at",
                "write_enabled",
                "verified_at",
                "last_verification_error",
                "modified",
            ]
        )
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
            # Always a new token: a cached one keeps the roles it was minted with, so a
            # permission granted or revoked since would go unseen until it expires.
            token = self.token_provider.get_token(tenant_id, force_refresh=True)
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
