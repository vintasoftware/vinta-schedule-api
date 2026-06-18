from typing import ClassVar

from django.http import HttpRequest

from strawberry import Info
from strawberry.permission import BasePermission

from public_api.constants import PublicAPIResources


class IsAuthenticated(BasePermission):
    message = "You must be authenticated to access this resource."

    def has_permission(self, source, info: Info, **kwargs) -> bool:  # type: ignore
        request: HttpRequest = info.context.request
        # request.public_api_system_user is set by public_api.middlewares.PublicApiSystemUserMiddleware
        system_user = getattr(request, "public_api_system_user", None)
        if system_user is None:
            return False

        # Check if the system user is active
        return system_user.is_active


class OrganizationResourceAccess(BasePermission):
    message = "You don't have access to query this resource."

    # Mapping from GraphQL field names to resource names
    FIELD_TO_RESOURCE_MAPPING: ClassVar[dict[str, str]] = {
        "calendars": PublicAPIResources.CALENDAR,
        "calendarEvents": PublicAPIResources.CALENDAR_EVENT,
        "blockedTimes": PublicAPIResources.BLOCKED_TIME,
        "availableTimes": PublicAPIResources.AVAILABLE_TIME,
        "availabilityWindows": PublicAPIResources.AVAILABILITY_WINDOWS,
        "unavailableWindows": PublicAPIResources.UNAVAILABLE_WINDOWS,
        "users": PublicAPIResources.USER,
        "calendarGroup": PublicAPIResources.CALENDAR_GROUP,
        "calendarGroups": PublicAPIResources.CALENDAR_GROUP,
        "calendarGroupAvailability": PublicAPIResources.CALENDAR_GROUP,
        "calendarGroupBookableSlots": PublicAPIResources.CALENDAR_GROUP,
        "calendarGroupEvents": PublicAPIResources.CALENDAR_GROUP,
        "deleteSystemUser": PublicAPIResources.SYSTEM_USER,
        "createOrganization": PublicAPIResources.ORGANIZATION,
        # createInvitation requires INVITATION scope. MEMBERSHIP is conceptually also implied
        # (the invitation will create a membership on accept), but the permission mechanism
        # supports one resource per field; INVITATION is the primary gating resource.
        "createInvitation": PublicAPIResources.INVITATION,
        "createSystemUserToken": PublicAPIResources.SYSTEM_USER,
        "updateBranding": PublicAPIResources.BRANDING,
        "childOrganizations": PublicAPIResources.CHILD_ORG_ANALYTICS,
        "createResourceCalendar": PublicAPIResources.CREATE_RESOURCE_CALENDAR,
        "disableResourceCalendar": PublicAPIResources.DISABLE_RESOURCE_CALENDAR,
        "importResourceCalendars": PublicAPIResources.IMPORT_RESOURCE_CALENDARS,
        "createAvailabilityWindow": PublicAPIResources.CREATE_AVAILABILITY_WINDOW,
        "updateAvailabilityWindow": PublicAPIResources.UPDATE_AVAILABILITY_WINDOW,
        "deleteAvailabilityWindow": PublicAPIResources.DELETE_AVAILABILITY_WINDOW,
        "batchUpdateAvailabilityWindows": PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS,
    }

    def has_permission(self, source, info: Info, **kwargs) -> bool:  # type: ignore
        request: HttpRequest = info.context.request
        # request.public_api_system_user is set by public_api.middlewares.PublicApiSystemUserMiddleware
        system_user = getattr(request, "public_api_system_user", None)
        if system_user is None:
            return False

        # request.public_api_organization is set by public_api.middlewares.PublicApiSystemUserMiddleware
        if not getattr(request, "public_api_organization", None):
            return False

        # Map GraphQL field name to resource name
        resource_name = self.FIELD_TO_RESOURCE_MAPPING.get(info.field_name, info.field_name)

        # check system_user has access to queried resources
        return system_user.available_resources.filter(resource_name=resource_name).exists()
