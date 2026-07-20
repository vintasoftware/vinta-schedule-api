from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, cast

from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
)

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from payments.billing_constants import Entitlement
from payments.exceptions import OverLimitError
from public_api.exceptions import InvalidAuthorizationHeaderError, PublicAPIServiceUnavailableError
from public_api.models import SystemUser
from public_api.services import PublicAPIAuthService
from public_api.types import PublicApiHttpRequest


if TYPE_CHECKING:
    from payments.services.entitlement_service import EntitlementService


class PublicApiSystemUserMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        # One-time configuration and initialization.

    def _get_credentials_from_request(self, request: HttpRequest) -> tuple[int, str]:
        """
        Extracts the system user ID and token from the request headers.
        The expected format is:
        Authorization: Bearer <system_user_id>:<token>
        Returns a tuple of (system_user_id, token).
        """
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise InvalidAuthorizationHeaderError()

        try:
            system_user_id_str, token = auth_header.split("Bearer ")[-1].split(":", 1)
        except ValueError as e:
            raise InvalidAuthorizationHeaderError() from e

        try:
            system_user_id = int(system_user_id_str)
        except ValueError as e:
            raise InvalidAuthorizationHeaderError() from e

        return system_user_id, token

    @inject
    def _get_system_user_from_request(
        self,
        request: PublicApiHttpRequest,
        public_api_auth_service: Annotated[
            PublicAPIAuthService | None, Provide["public_api_auth_service"]
        ] = None,
    ) -> SystemUser | None:
        if public_api_auth_service is None:
            raise PublicAPIServiceUnavailableError()

        try:
            system_user_id, token = self._get_credentials_from_request(request)
        except Exception as e:
            if isinstance(e, InvalidAuthorizationHeaderError):
                return None
            raise

        try:
            system_user, authenticated = public_api_auth_service.check_system_user_token(
                system_user_id, token
            )
        except (SystemUser.DoesNotExist, Exception) as e:
            if isinstance(e, InvalidAuthorizationHeaderError):
                return None
            raise

        return None if not authenticated or not system_user.is_active else system_user

    def _get_organization_from_request(self, request: PublicApiHttpRequest) -> Organization | None:
        if not (organization_id := request.headers.get("X-Public-Api-Organization-Id")):
            return None
        return Organization.objects.filter(id=organization_id).first()

    @inject
    def _has_partner_api_entitlement(
        self,
        organization: Organization,
        entitlement_service: Annotated[
            "EntitlementService | None", Provide["entitlement_service"]
        ] = None,
    ) -> bool:
        """Is ``organization`` (or its billing root) entitled to use the partner API?

        Fails closed defensively when DI wiring itself is broken (``entitlement_service``
        is ``None``): denying an authenticated request beats silently granting every
        organization unrestricted API access because a container failed to wire.

        Under normal operation this cannot lock out a legitimate organization:
        ``EntitlementService.has_entitlement`` resolves at the billing root, and every
        billing root always holds exactly one ``Subscription`` (the "no plan-less
        state" invariant from Phase 4) whose entitlement rows are synced from its plan
        on creation -- including the seeded ``unlimited`` default, which grants every
        ``Entitlement`` member. An org only loses this gate by being placed on a plan
        that explicitly omits or disables ``partner_api``, which is the intended
        enforcement, not an accident of a missing row.
        """
        if entitlement_service is None:
            return False
        return entitlement_service.has_entitlement(organization, Entitlement.PARTNER_API)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # ignore middleware if request is not the graphql endpoint

        extended_request = cast(PublicApiHttpRequest, request)
        extended_request.public_api_system_user = None
        extended_request.public_api_organization = None

        if extended_request.get_full_path().startswith("/graphql/"):
            extended_request.public_api_system_user = self._get_system_user_from_request(
                extended_request
            )

        if extended_request.public_api_system_user:
            extended_request.public_api_organization = (
                extended_request.public_api_system_user.organization
                or self._get_organization_from_request(extended_request)
            )

        # Entitlement gate: an organization without `partner_api` cannot use the
        # GraphQL API at all, not just individual mutations. Scoped to requests that
        # actually resolved an authenticated organization above (anonymous / public
        # GraphQL queries -- e.g. brandingForTenant -- never reach here, since
        # `public_api_organization` stays None for them) so this can never reject an
        # unauthenticated request the normal `IsAuthenticated` permission class would
        # otherwise handle. Bypasses GraphQL execution entirely and returns a real
        # HTTP 402 (rather than the graphql-core-swallowed 200 + `errors` shape a
        # resolver-level rejection would produce), matching the plan's contract for
        # this specific chokepoint.
        if extended_request.public_api_organization is not None and not (
            self._has_partner_api_entitlement(extended_request.public_api_organization)
        ):
            error = OverLimitError.from_missing_entitlement(Entitlement.PARTNER_API)
            return JsonResponse(error.as_error_body(), status=402)

        return self.get_response(extended_request)
