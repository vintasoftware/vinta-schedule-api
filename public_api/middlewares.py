from collections.abc import Callable
from typing import Annotated

from django.http import (
    HttpRequest,
    HttpResponse,
)

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from public_api.models import SystemUser
from public_api.services import PublicAPIAuthService


class PublicApiSystemUserMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        # One-time configuration and initialization.

    def _get_credentials_from_request(self, request: HttpRequest) -> tuple[str, str]:
        """
        Extracts the system user ID and token from the request headers.
        The expected format is:
        Authorization: Bearer <system_user_id>:<token>
        Returns a tuple of (system_user_id, token).
        """
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise ValueError("Invalid Authorization header format")

        try:
            system_user_id, token = auth_header.split("Bearer ")[-1].split(":", 1)
        except ValueError as e:
            raise ValueError("Invalid Authorization header format") from e

        return system_user_id, token

    @inject
    def _get_system_user_from_request(
        self,
        request: HttpRequest,
        public_api_auth_service: Annotated[
            PublicAPIAuthService, Provide["public_api_auth_service"]
        ],
    ) -> SystemUser | None:  # type: ignore
        try:
            system_user_id, token = self._get_credentials_from_request(request)
        except ValueError:
            return None

        try:
            system_user, authenticated = public_api_auth_service.check_system_user_token(
                system_user_id, token
            )
        except (SystemUser.DoesNotExist, ValueError):
            return None

        return None if not authenticated or not system_user.is_active else system_user

    def _get_organization_from_request(self, request: HttpRequest) -> Organization | None:
        organization_id = request.headers.get("X-Public-Api-Organization-Id")
        if not organization_id:
            return None
        return Organization.objects.filter(id=organization_id).first()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # ignore middleware if request is not the graphql endpoint
        request.public_api_system_user = None
        request.public_api_organization = None

        if request.get_full_path() == "/graphql/":
            request.public_api_system_user = self._get_system_user_from_request(request)

        if request.public_api_system_user:
            request.public_api_organization = request.public_api_system_user.organization

            if not request.public_api_organization:
                request.public_api_organization = self._get_organization_from_request(request)

        response = self.get_response(request)

        # Code to be executed for each request/response after
        # the view is called.

        return response
