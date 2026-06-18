from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from calendar_integration.models import Calendar
from public_api.scoping import scoped_calendar_ids
from public_api.types import PublicApiHttpRequest


if TYPE_CHECKING:
    from calendar_integration.services.calendar_group_service import CalendarGroupService
    from calendar_integration.services.calendar_service import CalendarService


@dataclass
class QueryDependencies:
    calendar_service: "CalendarService"
    calendar_group_service: "CalendarGroupService"


@inject
def get_query_dependencies(
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
    calendar_group_service: Annotated[
        "CalendarGroupService | None", Provide["calendar_group_service"]
    ] = None,
) -> QueryDependencies:
    required_dependencies = [calendar_service, calendar_group_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return QueryDependencies(
        calendar_service=cast("CalendarService", calendar_service),
        calendar_group_service=cast("CalendarGroupService", calendar_group_service),
    )


def get_org(info: strawberry.Info):
    org = info.context.request.public_api_organization
    if not org:
        raise GraphQLError("Organization not found in request context")
    return org


def prepare_service_and_calendar(
    info: strawberry.Info,
    calendar_id: int,
    _deps: "QueryDependencies | None" = None,
) -> tuple["CalendarService", Calendar]:
    """Resolve org, initialize calendar_service, apply owner-scope guard, and fetch calendar.

    Args:
        info: Strawberry resolver info carrying the request context.
        calendar_id: ID of the calendar to fetch.
        _deps: Optional pre-resolved QueryDependencies (used by callers that already hold
            a deps instance, e.g. to support test mocking via the caller's DI accessor).
            When omitted, get_query_dependencies() is called internally.

    Returns:
        Tuple of (CalendarService, Calendar).

    Raises:
        Calendar.DoesNotExist: When the calendar is not found or is outside the owner's scope.
        GraphQLError: When organization context is missing or DI dependencies are unavailable.
    """
    org = get_org(info)
    deps = _deps if _deps is not None else get_query_dependencies()
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
