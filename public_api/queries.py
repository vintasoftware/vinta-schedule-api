import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

from django.db.models import Count as DjangoCount
from django.db.models import OuterRef, Subquery, Value
from django.db.models.functions import Concat

import strawberry
import strawberry_django
from dependency_injector.wiring import Provide, inject
from django_virtual_models import QuerySet
from graphql import GraphQLError

from calendar_integration.constants import CalendarType, ExternalEventChangeRequestStatus
from calendar_integration.exceptions import (
    InvalidTokenError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.external_client_identifiers import normalize_system
from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    AvailableTimeWindowGraphQLType,
    BlockedTimeGraphQLType,
    BookableSlotProposalGraphQLType,
    BookingPolicyGraphQLType,
    CalendarBundleGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
    CalendarGroupGraphQLType,
    CalendarGroupRangeAvailabilityGraphQLType,
    CalendarGroupSlotAvailabilityGraphQLType,
    CalendarWebhookEventGraphQLType,
    CalendarWebhookSubscriptionGraphQLType,
    ExternalEventChangeRequestGraphQLType,
    GroupScopedAvailabilityWindowGraphQLType,
    GroupScopedBlockedTimeGraphQLType,
    GroupScopedQuotaRuleGraphQLType,
    UnavailableTimeWindowGraphQLType,
    WebhookSubscriptionStatusGraphQLType,
    group_scoped_availability_window_from_model,
    group_scoped_blocked_time_from_model,
    group_scoped_quota_rule_from_model,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlotQuotaRule,
    CalendarManagementToken,
    CalendarWebhookEvent,
    ExternalEventChangeRequest,
)
from calendar_integration.services.ics_service import CalendarEventICSService
from organizations.branding_logo import build_logo_display_url
from organizations.models import (
    Organization,
    OrganizationMembership,
    resolve_branding_for_display,
)
from public_api.capabilities import assert_org_can_invite
from public_api.permissions import (
    IsAuthenticated,
    OrganizationResourceAccess,
)
from public_api.scoping import scoped_calendar_group_queryset, scoped_calendar_ids
from public_api.types import (
    ChildOrganizationMetrics,
    PublicApiHttpRequest,
    PublicBrandingResult,
)
from users.graphql import UserGraphQLType
from users.models import User
from webhooks.graphql import WebhookConfigurationGraphQLType, WebhookEventGraphQLType
from webhooks.models import WebhookConfiguration, WebhookEvent


if TYPE_CHECKING:
    from calendar_integration.services.bookable_slots_service import BookableSlotsService
    from calendar_integration.services.booking_policy_permission_service import (
        BookingPolicyPermissionService,
    )
    from calendar_integration.services.booking_policy_service import BookingPolicyService
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


@inject
def get_booking_policy_query_dependencies(
    booking_policy_service: Annotated[
        "BookingPolicyService | None", Provide["booking_policy_service"]
    ] = None,
) -> "BookingPolicyService":
    """Resolve the BookingPolicyService from the DI container."""
    if booking_policy_service is None:
        raise GraphQLError("Missing required dependency: booking_policy_service")
    return booking_policy_service


@inject
def get_booking_policy_permission_service(
    booking_policy_permission_service: Annotated[
        "BookingPolicyPermissionService | None",
        Provide["booking_policy_permission_service"],
    ] = None,
) -> "BookingPolicyPermissionService":
    """Resolve the BookingPolicyPermissionService from the DI container."""
    if booking_policy_permission_service is None:
        raise GraphQLError("Missing required dependency: booking_policy_permission_service")
    return booking_policy_permission_service


@inject
def get_bookable_slots_service(
    bookable_slots_service: Annotated[
        "BookableSlotsService | None", Provide["bookable_slots_service"]
    ] = None,
) -> "BookableSlotsService":
    """Resolve the BookableSlotsService from the DI container."""
    if bookable_slots_service is None:
        raise GraphQLError("Missing required dependency: bookable_slots_service")
    return bookable_slots_service


def _get_org(info: strawberry.Info):
    org = info.context.request.public_api_organization
    if not org:
        raise GraphQLError("Organization not found in request context")
    return org


def _vinta_default_branding(request=None) -> PublicBrandingResult:
    """Return the Vinta Schedule default branding sentinel.

    Used for both missing tenants (no enumeration oracle) and unbranded
    organizations, ensuring the response is identical for unknown vs unbranded
    to prevent enumeration attacks. ``logo_url`` is the logo delivery route's
    URL keyed by its reserved "default" sentinel slug (see
    ``organizations.branding_logo.build_logo_display_url``), never empty --
    the route always streams something, even our own default logo.
    """
    return PublicBrandingResult(
        app_name="Vinta Schedule",
        logo_url=build_logo_display_url(None, request=request),
        primary_color="",
        secondary_color="",
    )


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

        # Optional filter by owner user (via CalendarOwnership membership)
        if user_id is not None:
            # related_name on CalendarOwnership is `ownerships`; the denormalized
            # membership_user_id carries the owning user (orphan ownerships excluded).
            queryset = queryset.filter(ownerships__membership_user_id=user_id)

        # Optional filter by calendar type
        if calendar_type is not None:
            queryset = queryset.filter(calendar_type=calendar_type)

        # Prefetch ownership rows + related membership to avoid N+1 when the
        # caller selects `owners { membership { ... } }`.
        queryset = queryset.prefetch_related("ownerships__membership")

        # Apply ordering first, then pagination
        queryset = _slice_qs(queryset.order_by("pk"), offset, limit)

        return list(queryset)

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_events(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        user_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        event_id: int | None = None,
        external_client_identifier_system: str | None = None,
        external_client_identifier_identifier: str | None = None,
    ) -> list[CalendarEventGraphQLType]:
        """Get calendar events filtered by user's organization.

        Supports three lookup modes (in order of precedence):
        1. ``eventId`` — fetch a single event by id (other args ignored).
        2. ``userId`` — fetch all events on calendars owned by that user within a
           date range; optionally intersected with ``calendarId``.
        3. ``calendarId`` — fetch events on a single calendar within a date range.

        ``startDatetime`` and ``endDatetime`` are required for modes 2 and 3.

        ``externalClientIdentifierSystem`` / ``externalClientIdentifierIdentifier`` narrow
        whichever mode above is in play, composing with the owner-scope filtering already
        applied to scoped tokens. They must be supplied together — a bare ``identifier``
        across all systems is not a meaningful query. When neither ``eventId``,
        ``userId`` nor ``calendarId`` is supplied, the identifier pair is itself a
        sufficient lookup mode (``extclientid_uniq_system_ident`` guarantees at most one
        matching event per organization+contentType), so ``startDatetime``/``endDatetime``
        are not required for it. ``system`` is normalized before matching. Recurring
        occurrences generated in-memory by the userId/calendarId branches have no real
        primary key of their own (see ``CalendarEventService.get_calendar_events_expanded``),
        so an identifier tagged directly on an occurrence can never match. An identifier
        tagged on the recurring master, however, DOES reach its occurrences: an
        occurrence is kept when either its own id or its master's id (persisted
        modified-occurrence exceptions) or its master's recurrence rule (plain
        generated occurrences) is one of the matching events.
        """
        # Get the user's organization and request from the GraphQL context.
        org = _get_org(info)
        request: PublicApiHttpRequest = info.context.request

        if (external_client_identifier_system is None) != (
            external_client_identifier_identifier is None
        ):
            raise GraphQLError(
                "externalClientIdentifierSystem and externalClientIdentifierIdentifier must "
                "be supplied together."
            )

        matching_event_ids: set[int] | None = None
        # Recurrence rule ids of the masters in ``matching_event_ids``. Generated
        # occurrences (from the userId/calendarId expansion branches) are in-memory
        # copies with no pk of their own, but they DO carry their master's
        # ``recurrence_rule_fk_id`` (``CalendarEvent.create_instance_from_occurrence``
        # copies it verbatim) -- unlike ``parent_recurring_object_fk_id``, which is only
        # ever set on persisted modified-occurrence exceptions. Matching on it lets a
        # tagged recurring master's identifier reach its occurrences without an extra
        # per-occurrence query.
        matching_recurrence_rule_ids: set[int] = set()
        if external_client_identifier_system is not None:
            normalized_system = normalize_system(external_client_identifier_system)
            matches = list(
                CalendarEvent.objects.filter_by_organization(org.id)
                .filter(
                    external_client_identifiers__system=normalized_system,
                    external_client_identifiers__identifier=external_client_identifier_identifier,
                    external_client_identifiers__organization=org.id,
                )
                .values_list("id", "recurrence_rule_fk_id")
            )
            matching_event_ids = {event_id for event_id, _ in matches}
            matching_recurrence_rule_ids = {
                rule_id for _, rule_id in matches if rule_id is not None
            }

        # --- Branch 1: eventId lookup (unchanged) ---
        if event_id is not None:
            qs = CalendarEvent.objects.filter_by_organization(org.id).filter(id=event_id)
            # Owner-scope: for scoped tokens, only return the event if its calendar is in the
            # owner's set. Return empty (not an error) to avoid existence leaks.
            system_user = request.public_api_system_user
            if system_user is not None:
                allowed_ids = scoped_calendar_ids(system_user, org)
                if allowed_ids is not None:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            if matching_event_ids is not None:
                qs = qs.filter(id__in=matching_event_ids)
            return qs.prefetch_related("external_client_identifiers")  # type: ignore[return-value]

        # --- Branch: identifier-only lookup (new) ---
        # A meaningful standalone mode when eventId/userId/calendarId are all absent:
        # extclientid_uniq_system_ident guarantees at most one matching event.
        if calendar_id is None and user_id is None and matching_event_ids is not None:
            qs = CalendarEvent.objects.filter_by_organization(org.id).filter(
                id__in=matching_event_ids
            )
            system_user = request.public_api_system_user
            if system_user is not None:
                allowed_ids = scoped_calendar_ids(system_user, org)
                if allowed_ids is not None:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            return qs.prefetch_related("external_client_identifiers")  # type: ignore[return-value]

        # --- Branch 2: userId lookup (new) ---
        if user_id is not None:
            if not start_datetime or not end_datetime:
                raise GraphQLError(
                    "Missing required parameters. If not filtered by id, querying events require "
                    "calendarId or userId, startDatetime, and endDatetime. "
                )

            # Resolve calendars owned by the user (via membership), constrained to this org.
            owned_ids: set[int] = set(
                Calendar.objects.filter_by_organization(org.id)
                .filter(ownerships__membership_user_id=user_id)
                .values_list("id", flat=True)
            )

            # Apply scoped-token constraint: intersect with the token's allowed set.
            system_user = request.public_api_system_user
            if system_user is not None:
                token_allowed = scoped_calendar_ids(system_user, org)
                if token_allowed is not None:
                    owned_ids = owned_ids & token_allowed

            # Optional calendarId intersection: calendar must also be owned by userId.
            if calendar_id is not None:
                owned_ids = owned_ids & {calendar_id}

            if not owned_ids:
                return cast(list[CalendarEventGraphQLType], [])

            # Initialize service for the org (no single Calendar to resolve).
            deps = get_query_dependencies()
            deps.calendar_service.initialize_without_provider(
                user_or_token=request.public_api_system_user, organization=org
            )
            events = deps.calendar_service.get_calendar_events_expanded_for_calendars(
                owned_ids,
                start_datetime,
                end_datetime,
                optimize_queryset=lambda qs: qs.prefetch_related("external_client_identifiers"),
            )
            if matching_event_ids is not None:
                events = [
                    e
                    for e in events
                    if e.id in matching_event_ids
                    or e.recurrence_rule_fk_id in matching_recurrence_rule_ids
                    or e.parent_recurring_object_fk_id in matching_event_ids
                ]
            return cast(list[CalendarEventGraphQLType], events)

        # --- Branch 3: calendarId lookup (unchanged) ---
        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying events require "
                "calendarId or userId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)
        events = calendar_service.get_calendar_events_expanded(
            calendar,
            start_datetime,
            end_datetime,
            optimize_queryset=lambda qs: qs.prefetch_related("external_client_identifiers"),
        )

        allowed_ids = (
            scoped_calendar_ids(request.public_api_system_user, org)
            if request.public_api_system_user is not None
            else None
        )
        if allowed_ids is not None:
            events = [e for e in events if getattr(e, "calendar_fk_id", None) in allowed_ids]

        if matching_event_ids is not None:
            events = [
                e
                for e in events
                if e.id in matching_event_ids
                or e.recurrence_rule_fk_id in matching_recurrence_rule_ids
                or e.parent_recurring_object_fk_id in matching_event_ids
            ]

        return cast(
            list[CalendarEventGraphQLType],
            events,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def event_ics(self, info: strawberry.Info, event_id: int) -> str | None:
        """Fetch the ICS string for a single calendar event.

        Returns the RFC 5545 iCalendar document for the requested event as a plain
        string, or ``None`` when the event does not exist, belongs to another
        organization, or is outside the scoped token's allowed calendar set.

        No existence leak: all failure modes return ``None``.
        """
        org = _get_org(info)
        request: PublicApiHttpRequest = info.context.request

        qs = (
            CalendarEvent.objects.filter_by_organization(org.id)
            .filter(id=event_id)
            .select_related("calendar")
            .prefetch_related(
                "calendar__ownerships__membership__user",
                "attendances__membership__user",
                "external_attendances__external_attendee",
                "recurrence_rule",
                "recurrence_exceptions",
            )
        )

        # Owner-scope: for scoped tokens, only return the event if its calendar is
        # in the token owner's set. Return None (not an error) to avoid existence
        # leaks, matching the calendarEvents eventId branch.
        system_user = request.public_api_system_user
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None:
                qs = qs.filter(calendar_fk__in=allowed_ids)

        event = qs.first()
        if event is None:
            return None

        return CalendarEventICSService().build_ics(event).decode("utf-8")

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

        # Already annotated earlier in this scope; re-annotating the same name is a
        # redefinition.
        request = info.context.request
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

        # Already annotated earlier in this scope; re-annotating the same name is a
        # redefinition.
        request = info.context.request
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

        queryset = User.objects.filter(memberships__organization=org, memberships__is_active=True)
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

        start_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_back)

        queryset = (
            CalendarWebhookEvent.objects.filter_by_organization(org)
            .filter(
                created__gte=start_time,
            )
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
        """Fetch a single CalendarGroup scoped to the caller's organization.

        Role-aware scope (calendar-group membership-permissions fix): org-wide
        and scoped-admin tokens may fetch any group in the org; a scoped-member
        token only a group it participates in (owns a calendar in one of the
        group's slots); a scoped token whose membership is missing/inactive
        sees none (fail closed) -- see ``public_api.scoping.system_user_scope``.
        """
        org = _get_org(info)
        request: PublicApiHttpRequest = info.context.request
        qs = scoped_calendar_group_queryset(
            request.public_api_system_user,
            org,
            CalendarGroup.objects.filter_by_organization(org.id),
        )
        return qs.filter(id=group_id).first()

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_groups(
        self,
        info: strawberry.Info,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CalendarGroupGraphQLType]:
        """List CalendarGroups for the caller's organization.

        Role-aware scope: see ``calendar_group`` above -- org-wide/scoped-admin
        see every group, scoped-member sees only groups it participates in,
        missing/inactive scoped membership sees none.
        """
        org = _get_org(info)
        request: PublicApiHttpRequest = info.context.request
        qs = scoped_calendar_group_queryset(
            request.public_api_system_user,
            org,
            CalendarGroup.objects.filter_by_organization(org.id),
        )
        qs = qs.prefetch_related("slots__calendars__ownerships__membership").order_by("pk")
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
            .prefetch_related(
                "bundle_children",
                "ownerships__membership",
                "bundle_children__ownerships__membership",
            )
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

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_bookable_slots(
        self,
        info: strawberry.Info,
        calendar_id: int,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration_seconds: int,
        slot_step_seconds: int = 15 * 60,
    ) -> list[BookableSlotProposalGraphQLType]:
        """Return policy-compliant bookable slot windows for a single calendar.

        Auto-detects personal vs bundle from ``calendar_type``: for a bundle, a
        window is offered only when every child calendar is free.  The resolved
        booking policy (lead-time, max-horizon, buffers) is applied; with no
        policy anywhere the result matches the pre-policy slot engine.
        """
        org = _get_org(info)

        # Owner-scope check: scoped tokens may only target their owner's calendars.
        # Match the same not-found path used for a genuinely missing calendar.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None and calendar_id not in allowed_ids:
                raise Calendar.DoesNotExist("Calendar matching query does not exist.")

        service = get_bookable_slots_service()
        service.initialize(organization=org)

        proposals = service.find_bookable_slots_for_calendar(
            calendar_id=calendar_id,
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

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def group_scoped_availability_windows(
        self,
        info: strawberry.Info,
        group_slot_id: int,
        calendar_id: int | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[GroupScopedAvailabilityWindowGraphQLType]:
        """List group-scoped availability windows for a group slot's roster.

        Returns raw window rows (one per recurring master or one-off window,
        not expanded occurrences) -- mirrors the internal REST surface's list
        shape. Optionally filtered to a single calendar in the slot's roster.
        """
        org = _get_org(info)

        qs = (
            AvailableTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(org.id)
            .select_related("recurrence_rule")
        )

        # Owner-scope: for scoped tokens, only return windows on calendars in
        # the token owner's set.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None:
                qs = qs.filter(calendar_fk__in=allowed_ids)

        if calendar_id is not None:
            qs = qs.filter(calendar_fk_id=calendar_id)

        windows = _slice_qs(qs.order_by("pk"), offset, limit)
        return [group_scoped_availability_window_from_model(w) for w in windows]

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def group_scoped_blocked_times(
        self,
        info: strawberry.Info,
        group_slot_id: int,
        calendar_id: int | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[GroupScopedBlockedTimeGraphQLType]:
        """List group-scoped blocked times for a group slot's roster.

        Returns raw block rows (one per recurring master or one-off block,
        not expanded occurrences) -- mirrors the internal REST surface's
        list shape. Optionally filtered to a single calendar in the slot's
        roster.
        """
        org = _get_org(info)

        qs = (
            BlockedTime.objects.for_group_slot(group_slot_id)
            .filter_by_organization(org.id)
            .select_related("recurrence_rule")
        )

        # Owner-scope: for scoped tokens, only return blocks on calendars in
        # the token owner's set.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None:
                qs = qs.filter(calendar_fk__in=allowed_ids)

        if calendar_id is not None:
            qs = qs.filter(calendar_fk_id=calendar_id)

        blocks = _slice_qs(qs.order_by("pk"), offset, limit)
        return [group_scoped_blocked_time_from_model(b) for b in blocks]

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def group_scoped_quota_rules(
        self,
        info: strawberry.Info,
        group_slot_id: int,
        calendar_id: int | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[GroupScopedQuotaRuleGraphQLType]:
        """List group-scoped quota rules for a group slot's roster.

        Mirrors the internal REST surface's list shape. Optionally filtered
        to a single calendar in the slot's roster.
        """
        org = _get_org(info)

        qs = CalendarGroupSlotQuotaRule.objects.for_group_slot(
            group_slot_id
        ).filter_by_organization(org.id)

        # Owner-scope: for scoped tokens, only return rules on calendars in
        # the token owner's set.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None:
                qs = qs.filter(calendar_fk__in=allowed_ids)

        if calendar_id is not None:
            qs = qs.filter(calendar_fk_id=calendar_id)

        rules = _slice_qs(qs.order_by("pk"), offset, limit)
        return [group_scoped_quota_rule_from_model(r) for r in rules]

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

        Membership count includes ALL memberships (active + inactive): "memberships"
        has no active-only qualifier, so all rows are counted.

        "Children" means DIRECT children (parent = acting_org) — a literal parent FK
        match.

        Access: the acting org must have can_invite_organizations=True
        (assert_org_can_invite) AND the token must carry CHILD_ORG_ANALYTICS scope
        (OrganizationResourceAccess).
        """
        org = _get_org(info)
        assert_org_can_invite(org)

        # Subquery-based counts to avoid join fan-out when multiple aggregates
        # are applied over different relations in a single queryset.
        membership_sq = (
            OrganizationMembership.objects.filter(organization_id=OuterRef("pk"))
            .values("organization_id")
            # OrganizationMembership has a composite PK (user, organization) and no
            # scalar ``id``; count rows via ``user_id`` (a NOT NULL PK column).
            .annotate(cnt=DjangoCount("user_id"))
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

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def external_event_change_requests(
        self,
        info: strawberry.Info,
        status: str | None = None,
        event_id: int | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ExternalEventChangeRequestGraphQLType]:
        """List external event change requests visible to the caller.

        Eligibility scoping:
        - **Scoped token** (``scoped_to_membership`` set): the token's acting
          membership is used to evaluate eligibility — the member sees only
          requests for events they attend, unless that member is an admin.
        - **Org-wide token** (``scoped_to_membership`` is ``None``): the token
          acts as an organization-wide admin and sees all requests in the org.

        Default: returns only ``PENDING`` requests when no ``status`` is passed.
        Pass ``status`` to retrieve historical/resolved requests.

        Pagination is required; maximum 100 results per page.
        """
        if offset < 0:
            raise GraphQLError("Offset must be non-negative")
        if limit < 1 or limit > 100:
            raise GraphQLError("Limit must be between 1 and 100")
        if status is not None and status not in ExternalEventChangeRequestStatus.values:
            raise GraphQLError(
                f"Invalid status '{status}'. "
                f"Valid values: {', '.join(ExternalEventChangeRequestStatus.values)}"
            )

        org = _get_org(info)
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user

        # Resolve the acting membership from the token's scoped_to_membership.
        # Org-wide tokens (scoped_to_membership_user_id=None) are treated as
        # admins — they can see all requests in the organization.
        acting_membership: OrganizationMembership | None = None
        scoped_user_id = getattr(system_user, "scoped_to_membership_user_id", None)
        if system_user is not None and scoped_user_id is not None:
            try:
                acting_membership = OrganizationMembership.objects.get(
                    organization_id=org.id,
                    user_id=scoped_user_id,
                    is_active=True,
                )
            except OrganizationMembership.DoesNotExist:
                # Membership was revoked or deactivated; return empty.
                return cast(list[ExternalEventChangeRequestGraphQLType], [])

        qs = ExternalEventChangeRequest.objects.filter_by_organization(org.id)

        # Eligibility scoping: if acting_membership is set, filter to requests
        # the membership is eligible to resolve. If no membership (org-wide token),
        # the token acts as an organization-wide admin and sees all requests.
        if acting_membership is not None:
            qs = qs.resolvable_by(acting_membership)

        # Default to PENDING when no status filter is provided.
        if status is None:
            qs = qs.filter(status=ExternalEventChangeRequestStatus.PENDING)
        else:
            qs = qs.filter(status=status)

        if event_id is not None:
            qs = qs.filter(event_fk_id=event_id)

        qs = qs.select_related("resolved_by").order_by("-pk")
        return cast(
            list[ExternalEventChangeRequestGraphQLType],
            list(_slice_qs(qs, offset, limit)),
        )

    # ------------------------------------------------------------------
    # BookingPolicy queries
    # ------------------------------------------------------------------

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def booking_policies(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        membership_user_id: int | None = None,
        calendar_group_id: int | None = None,
        is_organization_default: bool | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[BookingPolicyGraphQLType]:
        """List BookingPolicy rows for the caller's organization.

        All four filter args are optional and combinable.  When none are
        supplied, all policies for the org are returned (paginated).
        """
        org = _get_org(info)
        service = get_booking_policy_query_dependencies()
        service.initialize(org)

        qs = service.get_all_policies()

        # Owner-scope: a membership-scoped token sees only the policies it may
        # manage (its own calendars + own membership); org-wide tokens see all.
        permission_service = get_booking_policy_permission_service()
        qs = permission_service.scope_policies_for_system_user(
            qs,
            system_user=info.context.request.public_api_system_user,
            organization_id=org.id,
        )

        if calendar_id is not None:
            qs = qs.filter(calendar_fk_id=calendar_id)
        if membership_user_id is not None:
            qs = qs.filter(membership_user_id=membership_user_id)
        if calendar_group_id is not None:
            qs = qs.filter(calendar_group_fk_id=calendar_group_id)
        if is_organization_default is not None:
            qs = qs.filter(is_organization_default=is_organization_default)

        qs = _slice_qs(qs.order_by("pk"), offset, limit)
        return cast(list[BookingPolicyGraphQLType], list(qs))

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
    def calendar_bookable_slots_with_code(
        self,
        code: str,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration_seconds: int,
        slot_step_seconds: int = 15 * 60,
    ) -> list[BookableSlotProposalGraphQLType]:
        """Return policy-compliant bookable slot windows for a calendar via booking code.

        No org token required.  The code gates access to its bound calendar or
        calendar bundle only. Reads are repeatable: the code is never consumed by
        this query. A group-scoped code is rejected (single/bundle calendars only).

        The response omits policy rule values — slots only.
        """
        _validate_code_gated_range(search_window_start, search_window_end)
        deps = get_query_dependencies()
        token = _resolve_code_from_deps(deps, code)

        # Resolve the bound calendar (calendar-scope or event.calendar fallback).
        # Reject group-scoped codes (single/bundle only).
        calendar = token.calendar
        if calendar is None and token.event is not None:
            calendar = token.event.calendar
        if calendar is None:
            raise GraphQLError(_CODE_GATED_ERROR_MESSAGE)

        org = _get_org_from_token(token)
        service = get_bookable_slots_service()
        service.initialize(organization=org)

        proposals = service.find_bookable_slots_for_calendar(
            calendar_id=calendar.id,
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
    def branding_for_tenant(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        slug: str | None = None,
    ) -> PublicBrandingResult:
        """Get resolved branding for a tenant, or vinta default if unbranded.

        This is an unauthenticated, rate-limited public query for frontend interstitials.
        It returns the parent-walked branding for the given tenant, identified either by
        ``tenant_id`` or by ``slug``, or the vinta default when neither resolves. No
        enumeration oracle: an unknown tenant ID and an unknown slug both return the
        same default as an unbranded subtree, indistinguishably.

        When both arguments are supplied, ``tenant_id`` takes precedence -- callers are
        expected to pass exactly one. When neither is supplied, the organization is
        treated as unknown (same default-on-unknown path).

        Args:
            tenant_id: The ID of the organization to get branding for.
            slug: The organization's public slug, as an alternative to ``tenant_id``.

        Returns:
            PublicBrandingResult with app name, logo, and colors (no secrets).
        """
        request = info.context.request
        org = None
        if tenant_id is not None:
            try:
                tenant_id_int = int(tenant_id)
                org = Organization.objects.filter(id=tenant_id_int).first()
            except (ValueError, TypeError):
                org = None
        elif slug is not None:
            org = Organization.objects.filter(slug=slug).first()

        if org is None:
            # Unknown tenant ID/slug returns the vinta default (no enumeration oracle)
            return _vinta_default_branding(request=request)

        # Resolve branding by walking up the parent chain to the nearest reseller.
        # Presentation caller -> gated on `white_label_branding`; a reseller without it
        # renders the vinta default, same as an unbranded subtree.
        branding = resolve_branding_for_display(org)
        if branding is None:
            # Unbranded subtree returns the vinta default
            return _vinta_default_branding(request=request)

        # Return the resolved branding (no secrets exposed). logo_url is a signed URL
        # for the RESOLVED row's own stored logo (`branding` -- the reseller ancestor's
        # row for a child `org`, or `org`'s own), so a child organization renders its
        # reseller's real logo. A row with no logo falls back to the default-logo
        # delivery URL, identical to the unknown-tenant response above.
        return PublicBrandingResult(
            app_name=branding.app_name,
            logo_url=build_logo_display_url(branding, request=request),
            primary_color=branding.primary_color,
            secondary_color=branding.secondary_color,
        )
