import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast
from urllib.parse import urlsplit

from django.db.models import Count as DjangoCount
from django.db.models import OuterRef, Subquery, Value
from django.db.models.functions import Concat

import strawberry
import strawberry_django
from dependency_injector.wiring import Provide, inject
from django_virtual_models import QuerySet
from graphql import GraphQLError

from calendar_integration.constants import CalendarType
from calendar_integration.exceptions import (
    InvalidTokenError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    AvailableTimeWindowGraphQLType,
    BlockedTimeGraphQLType,
    BookableSlotProposalGraphQLType,
    CalendarBundleGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
    CalendarGroupGraphQLType,
    CalendarGroupRangeAvailabilityGraphQLType,
    CalendarGroupSlotAvailabilityGraphQLType,
    CalendarWebhookEventGraphQLType,
    CalendarWebhookSubscriptionGraphQLType,
    UnavailableTimeWindowGraphQLType,
    WebhookSubscriptionStatusGraphQLType,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarManagementToken,
)
from organizations.models import Organization, OrganizationMembership, resolve_branding
from public_api.capabilities import assert_org_can_invite
from public_api.permissions import (
    IsAuthenticated,
    OrganizationResourceAccess,
)
from public_api.scoping import scoped_calendar_ids
from public_api.types import (
    ChildOrganizationMetrics,
    PublicApiHttpRequest,
    PublicBrandingResult,
    ValidateReturnUrlResult,
)
from users.graphql import UserGraphQLType
from users.models import User
from webhooks.graphql import WebhookConfigurationGraphQLType, WebhookEventGraphQLType
from webhooks.models import WebhookConfiguration, WebhookEvent


if TYPE_CHECKING:
    from calendar_integration.services.calendar_group_service import CalendarGroupService
    from calendar_integration.services.calendar_permission_service import CalendarPermissionService
    from calendar_integration.services.calendar_service import CalendarService

# Uniform error message for all code-gated read failures.  Never disclose whether the
# code exists, is expired, used, revoked, or bound to the wrong scope.
_CODE_GATED_ERROR_MESSAGE = "Invalid or expired code."

# Maximum client-controlled datetime range for unauthenticated (code-gated) reads.
# Prevents amplification / DoS via unbounded recurrence expansion.
MAX_CODE_GATED_RANGE = datetime.timedelta(days=366)


@dataclass
class QueryDependencies:
    calendar_service: "CalendarService"
    calendar_group_service: "CalendarGroupService"
    calendar_permission_service: "CalendarPermissionService | None" = None


@inject
def get_query_dependencies(
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
    calendar_group_service: Annotated[
        "CalendarGroupService | None", Provide["calendar_group_service"]
    ] = None,
    calendar_permission_service: Annotated[
        "CalendarPermissionService | None", Provide["calendar_permission_service"]
    ] = None,
) -> QueryDependencies:
    required_dependencies = [calendar_service, calendar_group_service, calendar_permission_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return QueryDependencies(
        calendar_service=cast("CalendarService", calendar_service),
        calendar_group_service=cast("CalendarGroupService", calendar_group_service),
        calendar_permission_service=cast("CalendarPermissionService", calendar_permission_service),
    )


def _get_org(info: strawberry.Info):
    org = info.context.request.public_api_organization
    if not org:
        raise GraphQLError("Organization not found in request context")
    return org


def _vinta_default_branding() -> PublicBrandingResult:
    """Return the Vinta Schedule default branding sentinel.

    Used for both missing tenants (no enumeration oracle) and unbranded
    organizations, ensuring the response is identical for unknown vs unbranded
    to prevent enumeration attacks.
    """
    return PublicBrandingResult(
        app_name="Vinta Schedule",
        logo_url="",
        primary_color="",
        secondary_color="",
    )


_ALLOWED_RETURN_URL_SCHEMES = ("http", "https")
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _return_url_origin(raw: str) -> tuple[str, str, int] | None:
    """Parse a URL into its (scheme, host, port) origin, or None if not eligible.

    Returns None for anything that can never be an allowed return URL:
    - non-http/https schemes (javascript:, data:, etc.)
    - protocol-relative URLs (//host — no scheme)
    - URLs without a host
    - unparseable input or out-of-range ports

    The port is normalized to the scheme default when omitted so that
    https://app.example.com and https://app.example.com:443 share one origin.
    Host is lowercased; comparison of two origins is then EXACT tuple equality,
    so https://app.example.com never admits https://app.example.com.evil.com.
    """
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in _ALLOWED_RETURN_URL_SCHEMES:
            return None
        host = parts.hostname
        if not host:
            return None
        port = parts.port
    except (ValueError, TypeError):
        # Malformed URL or out-of-range port (parts.port raises ValueError).
        return None
    if port is None:
        port = _DEFAULT_SCHEME_PORTS[scheme]
    return (scheme, host.lower(), port)


def _slice_qs[TQuerySet: QuerySet](qs: TQuerySet, offset: int, limit: int) -> TQuerySet:
    if offset < 0:
        raise GraphQLError("Offset must be non-negative")
    if limit <= 0 or limit > 100:
        raise GraphQLError("Limit must be between 1 and 100")
    return qs[offset : offset + limit]


def _prepare_service_and_calendar(
    info: strawberry.Info, calendar_id: int
) -> tuple["CalendarService", Calendar]:
    org = _get_org(info)
    deps = get_query_dependencies()
    request: PublicApiHttpRequest = info.context.request
    deps.calendar_service.initialize_without_provider(
        user_or_token=request.public_api_system_user, organization=org
    )

    # Owner-scope check: when the token is scoped, reject calendars outside the owner's set.
    # Match the same not-found path used for a genuinely missing calendar (no existence leak).
    system_user = request.public_api_system_user
    if system_user is not None:
        allowed_ids = scoped_calendar_ids(system_user, org)
        if allowed_ids is not None and calendar_id not in allowed_ids:
            raise Calendar.DoesNotExist("Calendar matching query does not exist.")

    cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
    return deps.calendar_service, cal


def _prepare_service_and_calendar_for_org(
    deps: "QueryDependencies", org: Organization, calendar: Calendar
) -> "CalendarService":
    """Initialize CalendarService with the given org and return it.

    Used by code-gated (unauthenticated) reads where the org + calendar are
    derived from the booking code rather than from the request auth context.
    Receives an already-resolved ``deps`` object to avoid a second DI resolution.
    """
    deps.calendar_service.initialize_without_provider(user_or_token=None, organization=org)
    return deps.calendar_service


def _prepare_group_service_for_org(
    deps: "QueryDependencies", org: Organization
) -> "CalendarGroupService":
    """Initialize CalendarGroupService with the given org and return it.

    Used by code-gated (unauthenticated) reads where the org is derived from
    the booking code.
    Receives an already-resolved ``deps`` object to avoid a second DI resolution.
    """
    deps.calendar_group_service.initialize(organization=org)
    return deps.calendar_group_service


def _resolve_code_from_deps(deps: QueryDependencies, code: str) -> "CalendarManagementToken":
    """Decode and validate a booking code, raising GraphQLError on any failure.

    Centralises the None-guard for ``deps.calendar_permission_service`` so the
    five code-gated read fields share a single call site for mypy purposes.
    """
    if deps.calendar_permission_service is None:
        raise GraphQLError("Internal server error.")
    try:
        token: CalendarManagementToken = deps.calendar_permission_service.resolve_code(code)
    except (InvalidTokenError, TokenExpiredError, TokenAlreadyUsedError, TokenRevokedError):
        raise GraphQLError(_CODE_GATED_ERROR_MESSAGE) from None
    return token


def _get_org_from_token(token: "CalendarManagementToken") -> Organization:
    """Fetch the Organization for the given token, mapping DoesNotExist to the uniform error.

    Guards against hard-deleted organizations, which would otherwise raise an
    unhandled ``Organization.DoesNotExist`` (→ 500).
    """
    try:
        return Organization.objects.get(id=token.organization_id)
    except Organization.DoesNotExist:
        raise GraphQLError(_CODE_GATED_ERROR_MESSAGE) from None


def _validate_code_gated_range(start: datetime.datetime, end: datetime.datetime) -> None:
    """Validate a client-supplied datetime range for code-gated reads.

    Raises ``GraphQLError`` if the range is backwards or exceeds
    ``MAX_CODE_GATED_RANGE``.  Called BEFORE any expensive service call.
    """
    if end <= start:
        raise GraphQLError("Invalid time range.")
    if (end - start) > MAX_CODE_GATED_RANGE:
        raise GraphQLError("Requested time range is too large.")


@strawberry.input
class DateTimeRangeInput:
    """A single [start_time, end_time] window used by calendar-group availability queries."""

    start_time: datetime.datetime
    end_time: datetime.datetime


@strawberry.type
class Query:
    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendars(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        user_id: int | None = None,
        calendar_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CalendarGraphQLType]:
        """Get calendars filtered by user's organization."""
        org = _get_org(info)

        # Validate pagination parameters
        if offset < 0:
            raise GraphQLError("Offset must be non-negative")
        if limit <= 0 or limit > 100:
            raise GraphQLError("Limit must be between 1 and 100")

        queryset = Calendar.objects.filter_by_organization(org.id).only_listed()

        # Owner-scope enforcement: scoped tokens may only see their owner's calendars.
        # None => org-wide token (no-op). A set (possibly empty) => constrain to those ids.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None:
                queryset = queryset.filter(id__in=allowed_ids)

        if calendar_id is not None:
            queryset = queryset.filter(id=calendar_id)

        # Optional filter by owner user (via CalendarOwnership)
        if user_id is not None:
            # related_name on CalendarOwnership is `ownerships`
            queryset = queryset.filter(ownerships__user_id=user_id)

        # Optional filter by calendar type
        if calendar_type is not None:
            queryset = queryset.filter(calendar_type=calendar_type)

        # Apply ordering first, then pagination
        queryset = _slice_qs(queryset.order_by("pk"), offset, limit)

        return list(queryset)

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_events(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        event_id: int | None = None,
    ) -> list[CalendarEventGraphQLType]:
        """Get calendar events filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        org = _get_org(info)

        if event_id is not None:
            qs = CalendarEvent.objects.filter_by_organization(org.id).filter(id=event_id)
            # Owner-scope: for scoped tokens, only return the event if its calendar is in the
            # owner's set. Return empty (not an error) to avoid existence leaks.
            request: PublicApiHttpRequest = info.context.request
            system_user = request.public_api_system_user
            if system_user is not None:
                allowed_ids = scoped_calendar_ids(system_user, org)
                if allowed_ids is not None:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            return qs  # type: ignore[return-value]

        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying events require "
                "calendarId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)
        events = calendar_service.get_calendar_events_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )

        request: PublicApiHttpRequest = info.context.request
        allowed_ids = (
            scoped_calendar_ids(request.public_api_system_user, org)
            if request.public_api_system_user is not None
            else None
        )
        if allowed_ids is not None:
            events = [e for e in events if getattr(e, "calendar_fk_id", None) in allowed_ids]

        return cast(
            list[CalendarEventGraphQLType],
            events,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def blocked_times(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        blocked_time_id: int | None = None,
    ) -> list[BlockedTimeGraphQLType]:
        """Get blocked times filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        org = _get_org(info)

        if blocked_time_id is not None:
            qs = BlockedTime.objects.filter_by_organization(org.id).filter(id=blocked_time_id)
            # Owner-scope: for scoped tokens, only return the blocked time if its calendar is in
            # the owner's set. Return empty (not an error) to avoid existence leaks.
            request: PublicApiHttpRequest = info.context.request
            system_user = request.public_api_system_user
            if system_user is not None:
                allowed_ids = scoped_calendar_ids(system_user, org)
                if allowed_ids is not None:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            return qs  # type: ignore[return-value]

        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying blocked times "
                "require calendarId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        blocked_times = calendar_service.get_blocked_times_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )

        request: PublicApiHttpRequest = info.context.request
        allowed_ids = (
            scoped_calendar_ids(request.public_api_system_user, org)
            if request.public_api_system_user is not None
            else None
        )
        if allowed_ids is not None:
            blocked_times = [
                bt for bt in blocked_times if getattr(bt, "calendar_fk_id", None) in allowed_ids
            ]

        return cast(
            list[BlockedTimeGraphQLType],
            blocked_times,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def available_times(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        available_time_id: int | None = None,
    ) -> list[AvailableTimeGraphQLType]:
        """Get available times filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        org = _get_org(info)

        if available_time_id is not None:
            qs = AvailableTime.objects.filter_by_organization(org.id).filter(id=available_time_id)
            # Owner-scope: for scoped tokens, only return the available time if its calendar is
            # in the owner's set. Return empty (not an error) to avoid existence leaks.
            request: PublicApiHttpRequest = info.context.request
            system_user = request.public_api_system_user
            if system_user is not None:
                allowed_ids = scoped_calendar_ids(system_user, org)
                if allowed_ids is not None:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            return qs  # type: ignore[return-value]

        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying available times "
                "require calendarId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        available_times = calendar_service.get_available_times_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )

        request: PublicApiHttpRequest = info.context.request
        allowed_ids = (
            scoped_calendar_ids(request.public_api_system_user, org)
            if request.public_api_system_user is not None
            else None
        )
        if allowed_ids is not None:
            available_times = [
                at for at in available_times if getattr(at, "calendar_fk_id", None) in allowed_ids
            ]

        return cast(
            list[AvailableTimeGraphQLType],
            available_times,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def users(
        self,
        info: strawberry.Info,
        user_id: int | None = None,
        name: str | None = None,
        email: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[UserGraphQLType]:
        """Get users filtered by user's organization."""
        org = _get_org(info)

        queryset = User.objects.filter(
            organization_memberships__organization=org, organization_memberships__is_active=True
        )
        if user_id is not None:
            queryset = queryset.filter(id=user_id)

        # Filter by concatenated profile first + last name (case-insensitive contains)
        if name is not None:
            queryset = queryset.annotate(
                full_name=Concat("profile__first_name", Value(" "), "profile__last_name")
            ).filter(full_name__icontains=name)

        # Filter by email (case-insensitive contains)
        if email is not None:
            queryset = queryset.filter(email__icontains=email)

        # Apply ordering first, then pagination
        queryset = _slice_qs(queryset.order_by("pk"), offset, limit)

        # Return a concrete list and cast to the declared GraphQL return type so
        # mypy recognizes the return value matches the annotation.
        return cast(
            list[UserGraphQLType],
            list(queryset),
        )

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def availability_windows(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[AvailableTimeWindowGraphQLType]:
        """Get availability windows for a calendar within a date range."""
        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        # Get the availability windows
        availability_windows = calendar_service.get_availability_windows_in_range(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )

        # Convert to GraphQL types
        return [
            AvailableTimeWindowGraphQLType(
                start_time=window.start_time,
                end_time=window.end_time,
                id=window.id,
                can_book_partially=window.can_book_partially,
            )
            for window in availability_windows
        ]

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def unavailable_windows(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindowGraphQLType]:
        """Get unavailable (blocked or event) windows for a calendar within a date range."""
        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        unavailable_windows = calendar_service.get_unavailable_time_windows_in_range(
            calendar=calendar, start_datetime=start_datetime, end_datetime=end_datetime
        )

        return [
            UnavailableTimeWindowGraphQLType(
                start_time=w.start_time, end_time=w.end_time, id=w.id, reason=w.reason
            )
            for w in unavailable_windows
        ]

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_subscriptions(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        provider: str | None = None,
    ) -> list[CalendarWebhookSubscriptionGraphQLType]:
        """Get webhook subscriptions filtered by user's organization."""
        org = _get_org(info)
        deps = get_query_dependencies()

        # Set organization context on service
        deps.calendar_service.organization = org

        subscriptions = deps.calendar_service.list_webhook_subscriptions()

        if calendar_id is not None:
            subscriptions = subscriptions.filter(calendar__id=calendar_id)
        if provider is not None:
            subscriptions = subscriptions.filter(provider=provider)

        return list(subscriptions)  # type: ignore

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_events(
        self,
        info: strawberry.Info,
        subscription_id: int | None = None,
        processing_status: str | None = None,
        hours_back: int = 24,
        limit: int = 50,
    ) -> list[CalendarWebhookEventGraphQLType]:
        """Get recent webhook events filtered by user's organization."""
        org = _get_org(info)

        import datetime

        from calendar_integration.models import CalendarWebhookEvent

        start_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_back)

        queryset = (
            CalendarWebhookEvent.objects.filter(organization=org, created__gte=start_time)
            .select_related("subscription")
            .order_by("-created")
        )

        if subscription_id is not None:
            queryset = queryset.filter(subscription__id=subscription_id)
        if processing_status is not None:
            queryset = queryset.filter(processing_status=processing_status)

        return list(queryset[:limit])

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_health(
        self,
        info: strawberry.Info,
    ) -> WebhookSubscriptionStatusGraphQLType:
        """Get webhook system health status for the organization."""
        org = _get_org(info)
        deps = get_query_dependencies()

        # Set organization context on service
        deps.calendar_service.organization = org

        health_data = deps.calendar_service.get_webhook_health_status()

        return WebhookSubscriptionStatusGraphQLType(
            total_subscriptions=health_data["total_subscriptions"],
            active_subscriptions=health_data["active_subscriptions"],
            expired_subscriptions=health_data["expired_subscriptions"],
            expiring_soon_subscriptions=health_data["expiring_soon_subscriptions"],
            recent_events_count=health_data["recent_events_count"],
            failed_events_count=health_data["failed_events_count"],
            success_rate=health_data["success_rate"],
        )

    # ------------------------------------------------------------------
    # CalendarGroup queries
    # ------------------------------------------------------------------
    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_group(
        self, info: strawberry.Info, group_id: int
    ) -> CalendarGroupGraphQLType | None:
        """Fetch a single CalendarGroup scoped to the caller's organization."""
        org = _get_org(info)
        return CalendarGroup.objects.filter_by_organization(org.id).filter(id=group_id).first()

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_groups(
        self,
        info: strawberry.Info,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CalendarGroupGraphQLType]:
        """List CalendarGroups for the caller's organization."""
        org = _get_org(info)
        qs = CalendarGroup.objects.filter_by_organization(org.id).order_by("pk")
        return cast(list[CalendarGroupGraphQLType], list(_slice_qs(qs, offset, limit)))

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_bundles(
        self,
        info: strawberry.Info,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CalendarBundleGraphQLType]:
        """List bundle calendars for the caller's organization.

        Returns only Calendar rows with calendar_type=BUNDLE, paginated.
        Children are prefetched to avoid N+1 queries.
        """
        org = _get_org(info)
        qs = (
            Calendar.objects.filter_by_organization(org.id)
            .only_listed()
            .filter(calendar_type=CalendarType.BUNDLE)
            .prefetch_related("bundle_children")
            .order_by("pk")
        )
        return cast(list[CalendarBundleGraphQLType], list(_slice_qs(qs, offset, limit)))

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_group_availability(
        self,
        info: strawberry.Info,
        group_id: int,
        ranges: list[DateTimeRangeInput],
    ) -> list[CalendarGroupRangeAvailabilityGraphQLType]:
        """For each range, list which calendars in each slot's pool are available."""
        org = _get_org(info)
        deps = get_query_dependencies()
        deps.calendar_group_service.initialize(organization=org)

        result = deps.calendar_group_service.check_group_availability(
            group_id=group_id,
            ranges=[(r.start_time, r.end_time) for r in ranges],
        )
        return [
            CalendarGroupRangeAvailabilityGraphQLType(
                start_time=r.start_time,
                end_time=r.end_time,
                slots=[
                    CalendarGroupSlotAvailabilityGraphQLType(
                        slot_id=s.slot_id,
                        available_calendar_ids=s.available_calendar_ids,
                        required_count=s.required_count,
                    )
                    for s in r.slots
                ],
            )
            for r in result
        ]

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_group_bookable_slots(
        self,
        info: strawberry.Info,
        group_id: int,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration_seconds: int,
        slot_step_seconds: int = 15 * 60,
    ) -> list[BookableSlotProposalGraphQLType]:
        """Return time windows within the search range where every slot in the
        group has enough available calendars to satisfy its required_count."""
        org = _get_org(info)
        deps = get_query_dependencies()
        deps.calendar_group_service.initialize(organization=org)

        proposals = deps.calendar_group_service.find_bookable_slots(
            group_id=group_id,
            search_window_start=search_window_start,
            search_window_end=search_window_end,
            duration=datetime.timedelta(seconds=duration_seconds),
            slot_step=datetime.timedelta(seconds=slot_step_seconds),
        )
        return [
            BookableSlotProposalGraphQLType(start_time=p.start_time, end_time=p.end_time)
            for p in proposals
        ]

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_group_events(
        self,
        info: strawberry.Info,
        group_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[CalendarEventGraphQLType]:
        """Return events booked under a CalendarGroup overlapping the window."""
        org = _get_org(info)
        deps = get_query_dependencies()
        deps.calendar_group_service.initialize(organization=org)
        events = deps.calendar_group_service.get_group_events(
            group_id=group_id, start=start_datetime, end=end_datetime
        )
        return cast(list[CalendarEventGraphQLType], list(events))

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def child_organizations(
        self,
        info: strawberry.Info,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ChildOrganizationMetrics]:
        """List the acting reseller's direct child organizations with aggregate counts.

        Counts (memberships, calendars, events, calendar groups) are computed as
        ORM Subquery annotations to avoid join fan-out double-counting that arises
        when multiple Count() calls are combined in a single annotate() call across
        different related models.

        Membership count includes ALL memberships (active + inactive) — the plan
        says "memberships" with no active-only qualifier, so all rows are counted.

        "Children" means DIRECT children (parent = acting_org). The plan says
        "its child organizations"/"its children" — literal parent FK match.

        Gate: acting org must have can_invite_organizations=True (assert_org_can_invite)
        AND the token must carry CHILD_ORG_ANALYTICS scope (OrganizationResourceAccess).
        """
        org = _get_org(info)
        assert_org_can_invite(org)

        # Subquery-based counts to avoid join fan-out when multiple aggregates
        # are applied over different relations in a single queryset.
        membership_sq = (
            OrganizationMembership.objects.filter(organization_id=OuterRef("pk"))
            .values("organization_id")
            .annotate(cnt=DjangoCount("id"))
            .values("cnt")
        )
        calendar_sq = (
            Calendar.original_manager.filter(organization_id=OuterRef("pk"))
            .values("organization_id")
            .annotate(cnt=DjangoCount("id"))
            .values("cnt")
        )
        event_sq = (
            CalendarEvent.original_manager.filter(organization_id=OuterRef("pk"))
            .values("organization_id")
            .annotate(cnt=DjangoCount("id"))
            .values("cnt")
        )
        group_sq = (
            CalendarGroup.original_manager.filter(organization_id=OuterRef("pk"))
            .values("organization_id")
            .annotate(cnt=DjangoCount("id"))
            .values("cnt")
        )

        qs = (
            Organization.objects.filter(parent=org)
            .annotate(
                membership_count=Subquery(membership_sq),
                calendar_count=Subquery(calendar_sq),
                event_count=Subquery(event_sq),
                calendar_group_count=Subquery(group_sq),
            )
            .order_by("pk")
        )
        qs = _slice_qs(qs, offset, limit)

        return [
            ChildOrganizationMetrics(
                id=child.id,
                name=child.name,
                created_at=child.created,
                membership_count=child.membership_count or 0,
                calendar_count=child.calendar_count or 0,
                event_count=child.event_count or 0,
                calendar_group_count=child.calendar_group_count or 0,
            )
            for child in qs
        ]

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_configurations(
        self,
        info: strawberry.Info,
        offset: int = 0,
        limit: int = 100,
    ) -> list[WebhookConfigurationGraphQLType]:
        """List outgoing webhook configurations for the caller's organization."""
        org = _get_org(info)
        qs = (
            WebhookConfiguration.objects.filter_by_organization(org.id)
            .filter(deleted_at__isnull=True)
            .order_by("pk")
        )
        return cast(
            list[WebhookConfigurationGraphQLType],
            list(_slice_qs(qs, offset, limit)),
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_delivery_events(
        self,
        info: strawberry.Info,
        offset: int = 0,
        limit: int = 100,
    ) -> list[WebhookEventGraphQLType]:
        """List outgoing webhook delivery history for the caller's organization (read-only)."""
        org = _get_org(info)
        qs = WebhookEvent.objects.filter_by_organization(org.id).order_by("-pk")
        return cast(
            list[WebhookEventGraphQLType],
            list(_slice_qs(qs, offset, limit)),
        )

    # ------------------------------------------------------------------
    # Code-gated read fields (unauthenticated — authorized by booking code)
    # ------------------------------------------------------------------

    @strawberry.field()
    def available_times_with_code(
        self,
        code: str,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[AvailableTimeGraphQLType]:
        """Return available times for the calendar bound to a booking code.

        No org token required.  The code gates access to its bound calendar only.
        Reads are repeatable: the code is never consumed by this query.
        """
        _validate_code_gated_range(start_datetime, end_datetime)
        deps = get_query_dependencies()
        token = _resolve_code_from_deps(deps, code)

        # Resolve the bound calendar (calendar-scope or event.calendar fallback).
        calendar = token.calendar
        if calendar is None and token.event is not None:
            calendar = token.event.calendar
        if calendar is None:
            raise GraphQLError(_CODE_GATED_ERROR_MESSAGE)

        org = _get_org_from_token(token)
        calendar_service = _prepare_service_and_calendar_for_org(deps, org, calendar)

        available_times = calendar_service.get_available_times_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )
        return cast(list[AvailableTimeGraphQLType], available_times)

    @strawberry.field()
    def availability_windows_with_code(
        self,
        code: str,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[AvailableTimeWindowGraphQLType]:
        """Return availability windows for the calendar bound to a booking code.

        No org token required.  The code gates access to its bound calendar only.
        Reads are repeatable: the code is never consumed by this query.
        """
        _validate_code_gated_range(start_datetime, end_datetime)
        deps = get_query_dependencies()
        token = _resolve_code_from_deps(deps, code)

        calendar = token.calendar
        if calendar is None and token.event is not None:
            calendar = token.event.calendar
        if calendar is None:
            raise GraphQLError(_CODE_GATED_ERROR_MESSAGE)

        org = _get_org_from_token(token)
        calendar_service = _prepare_service_and_calendar_for_org(deps, org, calendar)

        windows = calendar_service.get_availability_windows_in_range(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        return [
            AvailableTimeWindowGraphQLType(
                start_time=w.start_time,
                end_time=w.end_time,
                id=w.id,
                can_book_partially=w.can_book_partially,
            )
            for w in windows
        ]

    @strawberry.field()
    def unavailable_windows_with_code(
        self,
        code: str,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindowGraphQLType]:
        """Return unavailable (blocked/event) windows for the calendar bound to a booking code.

        No org token required.  The code gates access to its bound calendar only.
        Reads are repeatable: the code is never consumed by this query.
        """
        _validate_code_gated_range(start_datetime, end_datetime)
        deps = get_query_dependencies()
        token = _resolve_code_from_deps(deps, code)

        calendar = token.calendar
        if calendar is None and token.event is not None:
            calendar = token.event.calendar
        if calendar is None:
            raise GraphQLError(_CODE_GATED_ERROR_MESSAGE)

        org = _get_org_from_token(token)
        calendar_service = _prepare_service_and_calendar_for_org(deps, org, calendar)

        unavailable = calendar_service.get_unavailable_time_windows_in_range(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        return [
            UnavailableTimeWindowGraphQLType(
                start_time=w.start_time, end_time=w.end_time, id=w.id, reason=w.reason
            )
            for w in unavailable
        ]

    @strawberry.field()
    def calendar_group_bookable_slots_with_code(
        self,
        code: str,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration_seconds: int,
        slot_step_seconds: int = 15 * 60,
    ) -> list[BookableSlotProposalGraphQLType]:
        """Return bookable slot proposals for the group bound to a booking code.

        No org token required.  The code gates access to its bound calendar group only.
        Reads are repeatable: the code is never consumed by this query.
        """
        _validate_code_gated_range(search_window_start, search_window_end)
        deps = get_query_dependencies()
        token = _resolve_code_from_deps(deps, code)

        # Resolve the bound group (group-scope or event.calendar_group fallback).
        group = token.calendar_group
        if group is None and token.event is not None:
            group = token.event.calendar_group
        if group is None:
            raise GraphQLError(_CODE_GATED_ERROR_MESSAGE)

        org = _get_org_from_token(token)
        calendar_group_service = _prepare_group_service_for_org(deps, org)

        proposals = calendar_group_service.find_bookable_slots(
            group_id=group.id,
            search_window_start=search_window_start,
            search_window_end=search_window_end,
            duration=datetime.timedelta(seconds=duration_seconds),
            slot_step=datetime.timedelta(seconds=slot_step_seconds),
        )
        return [
            BookableSlotProposalGraphQLType(start_time=p.start_time, end_time=p.end_time)
            for p in proposals
        ]

    @strawberry.field()
    def calendar_group_availability_with_code(
        self,
        code: str,
        ranges: list[DateTimeRangeInput],
    ) -> list[CalendarGroupRangeAvailabilityGraphQLType]:
        """Return per-range slot availability for the group bound to a booking code.

        No org token required.  The code gates access to its bound calendar group only.
        Reads are repeatable: the code is never consumed by this query.
        """
        for r in ranges:
            _validate_code_gated_range(r.start_time, r.end_time)
        deps = get_query_dependencies()
        token = _resolve_code_from_deps(deps, code)

        group = token.calendar_group
        if group is None and token.event is not None:
            group = token.event.calendar_group
        if group is None:
            raise GraphQLError(_CODE_GATED_ERROR_MESSAGE)

        org = _get_org_from_token(token)
        calendar_group_service = _prepare_group_service_for_org(deps, org)

        result = calendar_group_service.check_group_availability(
            group_id=group.id,
            ranges=[(r.start_time, r.end_time) for r in ranges],
        )
        return [
            CalendarGroupRangeAvailabilityGraphQLType(
                start_time=r.start_time,
                end_time=r.end_time,
                slots=[
                    CalendarGroupSlotAvailabilityGraphQLType(
                        slot_id=s.slot_id,
                        available_calendar_ids=s.available_calendar_ids,
                        required_count=s.required_count,
                    )
                    for s in r.slots
                ],
            )
            for r in result
        ]

    @strawberry.field()
    def branding_for_tenant(self, tenant_id: strawberry.ID) -> PublicBrandingResult:
        """Get resolved branding for a tenant, or vinta default if unbranded.

        This is an unauthenticated, rate-limited public query for frontend interstitials.
        It returns the parent-walked branding for the given tenant ID, or the vinta
        default when none. No enumeration oracle: unknown tenant ID returns the same
        default as an unbranded subtree.

        Args:
            tenant_id: The ID of the organization to get branding for.

        Returns:
            PublicBrandingResult with app name, logo, and colors (no secrets).
        """
        try:
            tenant_id_int = int(tenant_id)
            org = Organization.objects.filter(id=tenant_id_int).first()
        except (ValueError, TypeError):
            org = None

        if org is None:
            # Unknown tenant ID returns the vinta default (no enumeration oracle)
            return _vinta_default_branding()

        # Resolve branding by walking up the parent chain to the nearest reseller
        branding = resolve_branding(org)
        if branding is None:
            # Unbranded subtree returns the vinta default
            return _vinta_default_branding()

        # Return the resolved branding (no secrets exposed)
        return PublicBrandingResult(
            app_name=branding.app_name,
            logo_url=branding.logo_url,
            primary_color=branding.primary_color,
            secondary_color=branding.secondary_color,
        )

    @strawberry.field()
    def validate_return_url(self, tenant_id: strawberry.ID, url: str) -> ValidateReturnUrlResult:
        """Validate an OAuth return ("next") URL against a tenant's branding allowlist.

        Unauthenticated, rate-limited public query for the OAuth interstitial
        callback, which has no session yet and so cannot use the reseller-admin
        REST /branding/ endpoint. Answers a yes/no question WITHOUT ever
        serializing the reseller-internal return_url_allowlist (preserves §4.6).

        The candidate URL's ORIGIN (scheme + host + port) must EXACTLY equal the
        origin of an allowlist entry — never a prefix/substring match — so an
        allowlisted https://app.example.com does NOT admit
        https://app.example.com.evil.com. Only http/https candidates can ever be
        allowed; javascript:, data:, protocol-relative //host, and unparseable
        input are rejected.

        No enumeration oracle: unknown tenant ID, no branding row, empty
        allowlist, and any not-allowed case ALL return the identical shape
        {allowed: False, sanitized_url: None} with no error that distinguishes
        "tenant exists" from "doesn't". Never raises on a bad tenant ID.

        Args:
            tenant_id: The ID of the organization whose reseller allowlist applies.
            url: The candidate return URL to validate.

        Returns:
            ValidateReturnUrlResult with allowed and, when allowed, the echoed url.
        """
        not_allowed = ValidateReturnUrlResult(allowed=False, sanitized_url=None)

        # Scheme/parse guard first — never reveals anything about the tenant.
        candidate_origin = _return_url_origin(url)
        if candidate_origin is None:
            return not_allowed

        try:
            tenant_id_int = int(tenant_id)
            org = Organization.objects.filter(id=tenant_id_int).first()
        except (ValueError, TypeError):
            org = None

        if org is None:
            # Unknown / unparseable tenant — same shape as not-allowed (no oracle).
            return not_allowed

        branding = resolve_branding(org)
        if branding is None:
            # Unbranded subtree — same shape as not-allowed (no oracle).
            return not_allowed

        # Build the set of allowed origins; ineligible entries are simply skipped.
        # The allowlist itself is NEVER serialized into the response (§4.6).
        allowed_origins = {
            origin
            for entry in (branding.return_url_allowlist or [])
            if (origin := _return_url_origin(entry)) is not None
        }

        if candidate_origin in allowed_origins:
            return ValidateReturnUrlResult(allowed=True, sanitized_url=url)

        return not_allowed
