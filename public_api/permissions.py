from typing import ClassVar

from django.http import HttpRequest

from strawberry import Info
from strawberry.permission import BasePermission

from public_api.constants import PublicAPIResources


class IsAuthenticated(BasePermission):
    message = "You must be authenticated to access this resource."

    def has_permission(self, source, info: Info, **kwargs) -> bool:  # type: ignore
        request: HttpRequest = info.context.request
        # request.public_api_system_user is set by core.public_api.middlewares.PublicApiSystemUserMiddleware
        system_user = getattr(request, "public_api_system_user", None)
        if system_user is None:
            return False

        # Check if the system user is active
        return system_user


class OrganizationResourceAccess(BasePermission):
    message = "You don't have access to query this resource."

    # Mapping from GraphQL field names to resource names
    FIELD_TO_RESOURCE_MAPPING: ClassVar[dict[str, str]] = {
        "calendars": PublicAPIResources.CALENDAR,
        "calendarEvents": PublicAPIResources.CALENDAR_EVENT,
        "blockedTimes": PublicAPIResources.BLOCKED_TIME,
        "availableTimes": PublicAPIResources.AVAILABLE_TIME,
        "availabilityWindows": PublicAPIResources.AVAILABILITY_WINDOWS,
        "users": PublicAPIResources.USER,
    }

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

    def has_permission(self, source, info: Info, **kwargs) -> bool:  # type: ignore
        request: HttpRequest = info.context.request
        # request.public_api_system_user is set by core.public_api.middlewares.PublicApiSystemUserMiddleware
        system_user = getattr(request, "public_api_system_user", None)
        if system_user is None:
            return False

        # request.public_api_organization is set by core.public_api.middlewares.PublicApiSystemUserMiddleware
        if not getattr(request, "public_api_organization", None):
            return False

        # Map GraphQL field name to resource name
        resource_name = self.FIELD_TO_RESOURCE_MAPPING.get(info.field_name, info.field_name)

        # check system_user has access to queried resources
        if not system_user.available_resources.filter(resource_name=resource_name).exists():
            return False

        return True
