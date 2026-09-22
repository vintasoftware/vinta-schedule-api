from typing import ClassVar

from django.http import HttpRequest

from strawberry import Info
from strawberry.permission import BasePermission

from public_api.constants import PublicAPIResources


#: Where :class:`OrganizationResourceAccess` memoizes its resource lookups for
#: the life of one request. See ``_has_resource`` for why that is safe.
_RESOURCE_CACHE_ATTRIBUTE = "_public_api_resource_access_cache"


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
        "eventIcs": PublicAPIResources.CALENDAR_EVENT,
        "blockedTimes": PublicAPIResources.BLOCKED_TIME,
        "availableTimes": PublicAPIResources.AVAILABLE_TIME,
        "availabilityWindows": PublicAPIResources.AVAILABILITY_WINDOWS,
        "unavailableWindows": PublicAPIResources.UNAVAILABLE_WINDOWS,
        "users": PublicAPIResources.USER,
        "appointmentType": PublicAPIResources.APPOINTMENT_TYPE,
        "appointmentTypes": PublicAPIResources.APPOINTMENT_TYPE,
        "appointmentTypeAvailability": PublicAPIResources.APPOINTMENT_TYPE,
        "appointmentTypeBookableSlots": PublicAPIResources.APPOINTMENT_TYPE,
        "appointmentTypeEvents": PublicAPIResources.APPOINTMENT_TYPE,
        "createAppointmentType": PublicAPIResources.APPOINTMENT_TYPE,
        "updateAppointmentType": PublicAPIResources.APPOINTMENT_TYPE,
        "deleteAppointmentType": PublicAPIResources.APPOINTMENT_TYPE,
        "createAppointmentTypeEvent": PublicAPIResources.APPOINTMENT_TYPE,
        "appointmentTypeStaleSelections": PublicAPIResources.APPOINTMENT_TYPE,
        "calendarPool": PublicAPIResources.CALENDAR_POOL,
        "calendarPools": PublicAPIResources.CALENDAR_POOL,
        "createCalendarPool": PublicAPIResources.CALENDAR_POOL,
        "updateCalendarPool": PublicAPIResources.CALENDAR_POOL,
        "deleteCalendarPool": PublicAPIResources.CALENDAR_POOL,
        "deleteSystemUser": PublicAPIResources.SYSTEM_USER,
        "createOrganization": PublicAPIResources.ORGANIZATION,
        # createInvitation requires INVITATION scope. MEMBERSHIP is conceptually also implied
        # (the invitation will create a membership on accept), but the permission mechanism
        # supports one resource per field; INVITATION is the primary gating resource.
        "createInvitation": PublicAPIResources.INVITATION,
        "createSystemUserToken": PublicAPIResources.SYSTEM_USER,
        "createScopedSystemUser": PublicAPIResources.SYSTEM_USER,
        "updateBranding": PublicAPIResources.BRANDING,
        "createBrandingLogoUpload": PublicAPIResources.BRANDING,
        "childOrganizations": PublicAPIResources.CHILD_ORG_ANALYTICS,
        # Single-use booking-code create / revoke mutations
        "createCalendarBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "createAppointmentTypeBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "createCalendarRescheduleBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "createAppointmentTypeRescheduleBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "createCalendarCancellationBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "createAppointmentTypeCancellationBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "revokeBookingCode": PublicAPIResources.CALENDAR_BOOKING_CODE,
        "createCalendar": PublicAPIResources.CREATE_CALENDAR,
        "updateCalendar": PublicAPIResources.UPDATE_CALENDAR,
        "createResourceCalendar": PublicAPIResources.CREATE_RESOURCE_CALENDAR,
        "disableResourceCalendar": PublicAPIResources.DISABLE_RESOURCE_CALENDAR,
        "updateResourceCalendar": PublicAPIResources.UPDATE_RESOURCE_CALENDAR,
        "importResourceCalendars": PublicAPIResources.IMPORT_RESOURCE_CALENDARS,
        "createAvailabilityWindow": PublicAPIResources.CREATE_AVAILABILITY_WINDOW,
        "updateAvailabilityWindow": PublicAPIResources.UPDATE_AVAILABILITY_WINDOW,
        "deleteAvailabilityWindow": PublicAPIResources.DELETE_AVAILABILITY_WINDOW,
        "batchUpdateAvailabilityWindows": PublicAPIResources.BATCH_UPDATE_AVAILABILITY_WINDOWS,
        "appointmentTypeScopedAvailabilityWindows": PublicAPIResources.APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS,
        "batchUpsertAppointmentTypeScopedAvailabilityWindows": (
            PublicAPIResources.BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS
        ),
        "appointmentTypeScopedBlockedTimes": PublicAPIResources.APPOINTMENT_TYPE_SCOPED_BLOCKED_TIMES,
        "batchUpsertAppointmentTypeScopedBlockedTimes": (
            PublicAPIResources.BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_BLOCKED_TIMES
        ),
        "appointmentTypeScopedQuotaRules": PublicAPIResources.APPOINTMENT_TYPE_SCOPED_QUOTA_RULES,
        "batchUpsertAppointmentTypeScopedQuotaRules": (
            PublicAPIResources.BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_QUOTA_RULES
        ),
        "createBlockedTime": PublicAPIResources.CREATE_BLOCKED_TIME,
        "updateBlockedTime": PublicAPIResources.UPDATE_BLOCKED_TIME,
        "deleteBlockedTime": PublicAPIResources.DELETE_BLOCKED_TIME,
        "scheduleEvent": PublicAPIResources.CALENDAR_EVENT,
        "updateCalendarEvent": PublicAPIResources.CALENDAR_EVENT,
        "rescheduleCalendarEvent": PublicAPIResources.CALENDAR_EVENT,
        "rescheduleAppointmentTypeEvent": PublicAPIResources.CALENDAR_EVENT,
        "cancelEvent": PublicAPIResources.CALENDAR_EVENT,
        "calendarBundles": PublicAPIResources.CALENDAR_BUNDLE,
        "createCalendarBundle": PublicAPIResources.CREATE_CALENDAR_BUNDLE,
        "updateCalendarBundle": PublicAPIResources.UPDATE_CALENDAR_BUNDLE,
        "disableCalendarBundle": PublicAPIResources.DISABLE_CALENDAR_BUNDLE,
        "webhookConfigurations": PublicAPIResources.WEBHOOK_CONFIGURATION,
        "createWebhookConfiguration": PublicAPIResources.WEBHOOK_CONFIGURATION,
        "updateWebhookConfiguration": PublicAPIResources.WEBHOOK_CONFIGURATION,
        "deleteWebhookConfiguration": PublicAPIResources.WEBHOOK_CONFIGURATION,
        "webhookDeliveryEvents": PublicAPIResources.WEBHOOK_CONFIGURATION,
        "externalEventChangeRequests": PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST,
        "approveExternalEventChangeRequest": PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST,
        "rejectExternalEventChangeRequest": PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST,
        "calendarBookableSlots": PublicAPIResources.BOOKABLE_SLOTS,
        "bookingPolicies": PublicAPIResources.BOOKING_POLICY,
        "createBookingPolicy": PublicAPIResources.BOOKING_POLICY,
        "updateBookingPolicy": PublicAPIResources.BOOKING_POLICY,
        "deleteBookingPolicy": PublicAPIResources.BOOKING_POLICY,
        # Aggregate root fields (public_api/aggregations/fields.py). Each one
        # requires the same resource as the entity's existing list field --
        # an aggregate discloses nothing a caller could not already read one
        # row at a time. See "Aggregate fields require the same resource
        # scope as the entity's list field" in the plan's Guiding Decisions.
        "calendarEventAggregate": PublicAPIResources.CALENDAR_EVENT,
        "availableTimeAggregate": PublicAPIResources.AVAILABLE_TIME,
        "blockedTimeAggregate": PublicAPIResources.BLOCKED_TIME,
        "appointmentTypeAggregate": PublicAPIResources.APPOINTMENT_TYPE,
        "calendarAggregate": PublicAPIResources.CALENDAR,
        "calendarPoolAggregate": PublicAPIResources.CALENDAR_POOL,
        # Nested aggregates (calendar_integration/graphql.py). The resource is
        # the AGGREGATED entity's, never the parent's: reaching an event
        # rollup through a calendar has to cost the same grant as reading the
        # events, or the nesting would be a way around the event scope.
        # `blockedTimeAggregate` needs no second entry -- the nested field and
        # the root field share a name, and therefore this mapping.
        "eventAggregate": PublicAPIResources.CALENDAR_EVENT,
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
        return self._has_resource(request, system_user, resource_name)

    @staticmethod
    def _has_resource(request: HttpRequest, system_user, resource_name: str) -> bool:  # type: ignore[no-untyped-def]
        """Whether this token holds ``resource_name``, asked once per request.

        A permission class runs per *field resolution*, so a permissioned
        field on a type inside a list is checked once per item -- twenty-five
        identical `EXISTS` queries for twenty-five calendars, all asking
        whether the same token holds the same resource. Nothing in the request
        can change that answer: the middleware binds the system user and the
        organization once, and a grant is not edited mid-query. So the answer
        is memoized on the request, keyed by the token and the resource.

        This matters most for the nested aggregate fields
        (``calendar_integration/graphql.py``), which are the first permissioned
        fields this API mounts under a list -- without it their query count
        grows with the list's length even though the aggregate itself is a
        single batched query.
        """
        cache = getattr(request, _RESOURCE_CACHE_ATTRIBUTE, None)
        if cache is None:
            cache = {}
            setattr(request, _RESOURCE_CACHE_ATTRIBUTE, cache)

        key = (system_user.id, resource_name)
        if key not in cache:
            cache[key] = system_user.available_resources.filter(
                resource_name=resource_name
            ).exists()
        return cache[key]
