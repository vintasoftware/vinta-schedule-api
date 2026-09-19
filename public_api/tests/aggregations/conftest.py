"""Shared setup for the aggregate GraphQL tests.

These go through the real HTTP entry point — the middleware that binds the
organization, the permission classes, the schema — because that is the only
path where a permission error, a tenant binding and a resolver are all in play
at once. A resolver called directly bypasses every one of them.
"""

import datetime
import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    CalendarPool,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import User


#: Every event fixture below lives inside this window, so a filter that names it
#: selects all of them and a filter that names another month selects none.
WINDOW_START = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
WINDOW_END = datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name=f"Aggregate Org {uuid.uuid4().hex[:6]}")


@pytest.fixture
def other_organization() -> Organization:
    return baker.make(Organization, name=f"Other Org {uuid.uuid4().hex[:6]}")


def make_calendar(organization: Organization, name: str) -> Calendar:
    unique = uuid.uuid4().hex[:8]
    return Calendar.objects.create(
        organization=organization,
        name=name,
        external_id=f"cal-{unique}",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
    )


def make_event(
    organization: Organization,
    calendar: Calendar,
    *,
    title: str,
    day: int,
    minutes: int,
) -> CalendarEvent:
    start = datetime.datetime(2026, 3, day, 9, 0)
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        external_id=f"ev-{uuid.uuid4().hex[:10]}",
        title=title,
        description=f"{title} description",
        timezone="UTC",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
    )


def make_blocked_time(organization: Organization, calendar: Calendar, *, day: int) -> BlockedTime:
    start = datetime.datetime(2026, 3, day, 14, 0)
    return baker.make(
        BlockedTime,
        organization=organization,
        calendar=calendar,
        external_id=f"bt-{uuid.uuid4().hex[:10]}",
        reason="Lunch",
        timezone="UTC",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=60),
    )


def make_available_time(
    organization: Organization, calendar: Calendar, *, day: int
) -> AvailableTime:
    start = datetime.datetime(2026, 3, day, 8, 0)
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        timezone="UTC",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(hours=8),
    )


def make_appointment_type(organization: Organization, *, name: str) -> AppointmentType:
    return baker.make(
        AppointmentType,
        organization=organization,
        name=name,
        description=f"{name} description",
        accepts_public_scheduling=False,
        duration=datetime.timedelta(minutes=45),
    )


def make_calendar_pool(organization: Organization, *, name: str) -> CalendarPool:
    return baker.make(CalendarPool, organization=organization, name=name, description="")


def make_membership_for(organization: Organization) -> tuple[User, OrganizationMembership]:
    unique = uuid.uuid4().hex[:8]
    user = baker.make(User, email=f"user_{unique}@example.com")
    membership = make_membership(
        user=user,
        organization=organization,
        groups=(GROUP_ORGANIZATION_MEMBER,),
        is_active=True,
    )
    return user, membership


def own(organization: Organization, user: User, calendar: Calendar) -> None:
    CalendarOwnership.objects.create(
        organization=organization, calendar=calendar, membership_user_id=user.id
    )


def org_wide_token(organization: Organization, resources: list[str]):
    """A token with no owner scope: it sees every row in its organization."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=organization
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def scoped_token(
    organization: Organization, membership: OrganizationMembership, resources: list[str]
):
    """A token narrowed to one membership's own calendars."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
        organization=organization,
        scoped_to_membership=membership,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def post_graphql(client: APIClient, query: str, credentials, variables: dict | None = None):
    """POST a document as the given token, through the real middleware."""
    from di_core.containers import container

    assert container is not None, "The DI container is not wired; is Django set up?"
    system_user, token, auth_service = credentials
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": variables or {}},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


def graphql_errors(response) -> list[str]:
    return [error["message"] for error in response.json().get("errors") or []]


def assert_ok(response) -> dict:
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload.get("errors") in (None, []), payload["errors"]
    return payload["data"]


def event_window_variables(**extra) -> dict:
    """Filter variables covering every event the fixtures create."""
    return {
        "filter": {
            "startDatetime": WINDOW_START.isoformat(),
            "endDatetime": WINDOW_END.isoformat(),
            **extra,
        }
    }
