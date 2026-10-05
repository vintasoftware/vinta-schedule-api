"""Endpoints that connect an organization to its Microsoft 365 tenant.

* ``POST calendar/microsoft-connection/consent-url/`` and
  ``POST calendar/microsoft-connection/verify/`` are org-admin REST endpoints. They
  bind the organization through ``TenantScopedViewMixin`` like every other
  tenant-scoped view.
* ``GET calendar/microsoft-connection/callback/`` is where Microsoft sends the
  browser after admin consent. It is unauthenticated and binds no organization: the
  one it acts on comes from the signed ``state``, and it always redirects to
  ``FRONTEND_BASE_URL``.

All three answer 404 while ``resource_calendar_provider_sync`` is off for the
organization, so the surface is not advertised.
"""

import logging
import urllib.parse
from typing import Annotated, Any, cast

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponseRedirect
from django.urls import reverse
from django.views import View

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from calendar_integration.exceptions import (
    MicrosoftConnectionNotConfiguredError,
    MicrosoftConsentDeniedError,
    MicrosoftConsentStateError,
)
from calendar_integration.serializers import (
    MicrosoftConnectionVerificationSerializer,
    MicrosoftConsentUrlSerializer,
)
from calendar_integration.services.microsoft_connection_service import (
    MicrosoftConnectionService,
)
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC, is_enabled
from common.utils.view_utils import TenantScopedViewMixin
from organizations.models import Organization
from organizations.permissions import IsOrganizationAdmin


logger = logging.getLogger(__name__)

FLAG_OFF_MESSAGE = "Resource calendar provider sync is not enabled for this organization."
NOT_CONFIGURED_MESSAGE = "Microsoft room sync is not configured on this environment."

# Where the web app shows the outcome of admin consent. The query string carries only
# the values below, never anything from the incoming request.
CONSENT_RESULT_PATH = "/settings/integrations/microsoft"
CONSENT_STATUS_CONNECTED = "connected"
CONSENT_STATUS_ERROR = "error"
CONSENT_REASON_INVALID_STATE = "invalid_state"
CONSENT_REASON_DENIED = "consent_denied"


def _acting_organization(request: Request) -> Organization:
    # IsOrganizationAdmin has already required an active membership.
    return cast("Any", request).organization_membership.organization


def _require_flag(organization: Organization) -> None:
    if not is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.pk):
        raise NotFound(FLAG_OFF_MESSAGE)


def _not_configured_response() -> Response:
    return Response({"detail": NOT_CONFIGURED_MESSAGE}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class MicrosoftConsentUrlView(TenantScopedViewMixin, APIView):
    """Return the admin-consent link for the acting organization's Microsoft tenant."""

    permission_classes = (IsOrganizationAdmin,)

    @extend_schema(request=None, responses={200: MicrosoftConsentUrlSerializer})
    @inject
    def post(
        self,
        request: Request,
        microsoft_connection_service: Annotated[
            MicrosoftConnectionService, Provide["microsoft_connection_service"]
        ],
    ) -> Response:
        """Issue a new single-use consent link. Older links stop working."""
        organization = _acting_organization(request)
        _require_flag(organization)
        redirect_uri = request.build_absolute_uri(reverse("microsoft-connection-callback"))
        try:
            consent_url = microsoft_connection_service.build_consent_url(
                organization, redirect_uri=redirect_uri
            )
        except MicrosoftConnectionNotConfiguredError:
            return _not_configured_response()
        return Response(MicrosoftConsentUrlSerializer({"consent_url": consent_url}).data)


class MicrosoftConnectionVerifyView(TenantScopedViewMixin, APIView):
    """Check that the connected tenant allows room writes, and record the outcome."""

    permission_classes = (IsOrganizationAdmin,)

    @extend_schema(request=None, responses={200: MicrosoftConnectionVerificationSerializer})
    @inject
    def post(
        self,
        request: Request,
        microsoft_connection_service: Annotated[
            MicrosoftConnectionService, Provide["microsoft_connection_service"]
        ],
    ) -> Response:
        """Verify write access. A failed check answers 200 with ``error`` set."""
        organization = _acting_organization(request)
        _require_flag(organization)
        try:
            result = microsoft_connection_service.verify(organization)
        except MicrosoftConnectionNotConfiguredError:
            return _not_configured_response()
        return Response(
            MicrosoftConnectionVerificationSerializer(
                {
                    "write_enabled": result.write_enabled,
                    "verified_at": result.verified_at,
                    "error": result.error,
                }
            ).data
        )


class MicrosoftConsentCallbackView(View):
    """Where Microsoft redirects the browser after a tenant admin answers admin consent.

    A plain Django view: the browser arrives from Microsoft with no Vinta session or
    token, so nothing binds an organization. The only organization it touches is the
    one named inside the signed ``state``, reached explicitly through
    ``filter_by_organization`` by the feature-flag check and by the service.
    """

    @inject
    def get(
        self,
        request: HttpRequest,
        microsoft_connection_service: Annotated[
            MicrosoftConnectionService, Provide["microsoft_connection_service"]
        ],
    ) -> HttpResponseRedirect:
        """Store the consenting tenant, then send the browser back to the web app."""
        state = request.GET.get("state", "")
        try:
            organization_id = microsoft_connection_service.organization_id_from_state(state)
        except MicrosoftConsentStateError:
            return _consent_result_redirect(CONSENT_STATUS_ERROR, CONSENT_REASON_INVALID_STATE)

        if not is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization_id):
            raise Http404(FLAG_OFF_MESSAGE)

        try:
            microsoft_connection_service.complete_consent(
                state,
                tenant=request.GET.get("tenant", ""),
                admin_consent=request.GET.get("admin_consent", ""),
            )
        except MicrosoftConsentStateError:
            logger.info("Rejected Microsoft consent callback for organization %s", organization_id)
            return _consent_result_redirect(CONSENT_STATUS_ERROR, CONSENT_REASON_INVALID_STATE)
        except MicrosoftConsentDeniedError:
            return _consent_result_redirect(CONSENT_STATUS_ERROR, CONSENT_REASON_DENIED)
        return _consent_result_redirect(CONSENT_STATUS_CONNECTED)


def _consent_result_redirect(outcome: str, reason: str | None = None) -> HttpResponseRedirect:
    query = {"status": outcome} if reason is None else {"status": outcome, "reason": reason}
    return HttpResponseRedirect(
        f"{settings.FRONTEND_BASE_URL}{CONSENT_RESULT_PATH}?{urllib.parse.urlencode(query)}"
    )
