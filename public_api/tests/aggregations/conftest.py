"""Shared fixtures for the aggregate root-field tests.

The three integration modules here all need the same three things: an
organization, rows to aggregate, and a token that can reach them through the
real GraphQL endpoint. Posting through `/graphql/` rather than calling the
resolver is deliberate -- the middleware that binds the organization and the
`permission_classes` that gate the field both live on that path, and a
resolver-level call exercises neither.
"""

import datetime
import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import User


# A window every temporal fixture below sits inside, and short enough that
# `MAX_AGGREGATE_RANGE` is never the thing under test by accident.
WINDOW_START = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
WINDOW_END = datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC)


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")


def make_calendar(organization: Organization, *, name: str | None = None) -> Calendar:
    unique = uuid.uuid4().hex[:8]
    return Calendar.objects.create(
        organization=organization,
        name=name or f"Calendar {unique}",
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
    start: datetime.datetime,
    minutes: int,
) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar_fk=calendar,
        external_id=f"event-{uuid.uuid4().hex[:8]}",
        title=title,
        description=f"{title} description",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
    )


def make_available_time(
    organization: Organization,
    calendar: Calendar,
    *,
    start: datetime.datetime,
    minutes: int,
) -> AvailableTime:
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar_fk=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
    )


def make_blocked_time(
    organization: Organization,
    calendar: Calendar,
    *,
    reason: str,
    start: datetime.datetime,
    minutes: int,
) -> BlockedTime:
    return baker.make(
        BlockedTime,
        organization=organization,
        calendar_fk=calendar,
        # Unique together with the calendar, so it cannot be left at its default.
        external_id=f"blocked-{uuid.uuid4().hex[:8]}",
        reason=reason,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
    )


def make_member(
    organization: Organization,
) -> tuple[User, OrganizationMembership]:
    unique = uuid.uuid4().hex[:8]
    user = baker.make(User, email=f"user_{unique}@example.com")
    membership = make_membership(
        user=user,
        organization=organization,
        groups=(GROUP_ORGANIZATION_MEMBER,),
        is_active=True,
    )
    return user, membership


def own_calendar(organization: Organization, user: User, calendar: Calendar) -> None:
    CalendarOwnership.objects.create(
        organization=organization, calendar=calendar, membership_user_id=user.id
    )


def org_wide_token(organization: Organization, resources: list[str]):
    """An unrestricted token carrying exactly `resources`."""
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
    """A token acting as one membership, so owner scoping applies."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
        organization=organization,
        scoped_to_membership=membership,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


def post_graphql(query: str, system_user, token, auth_service, variables=None):
    """POST a document through the real endpoint, middleware and guards included."""
    from di_core.containers import container

    # Declared `AppContainer | None`; wired at app startup, so it is never None
    # by the time a test runs.
    assert container is not None

    client = APIClient()
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": variables or {}},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )
