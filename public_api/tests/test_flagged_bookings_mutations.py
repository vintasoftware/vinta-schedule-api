"""Public GraphQL tests for ``resolveFlaggedResourceBookings``.

Covers the ``resolve_flagged_resource_bookings`` grant, the flag-off error, the
resolution input validation, and the acceptance case: an archived, flagged room whose
future booking is moved clears ``providerSync.flaggedBookingsAt``. The room directory
is ``FakeRoomDirectory`` (every room free), and organizer emails are caught by
overriding ``room_sync_notifier``.
"""

import datetime
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    ResourceSyncStatus,
)
from calendar_integration.factories import create_resource_provider_link
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    ResourceAllocation,
    ResourceCalendarProviderLink,
)
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


NOT_ENABLED = "Resource calendar provider sync is not enabled for this organization."

PREVIEW_QUERY = """
query Preview($calendarId: Int!) {
    resourceCalendarDeletionPreview(calendarId: $calendarId) {
        fingerprint
        bookings { eventId }
    }
}
"""

RESOLVE_MUTATION = """
mutation Resolve($input: ResolveFlaggedResourceBookingsInput!) {
    resolveFlaggedResourceBookings(input: $input) {
        success
        outcome
        errorMessage
        calendar { id providerSync { status flaggedBookingsAt } }
        rejectedBookings { eventId reason message }
        appliedEventIds
        pendingEventIds
        failedAtEventId
    }
}
"""


class _Api:
    """A public-API client for one organization's system-user token."""

    def __init__(self, organization: Organization, resources: list[str]) -> None:
        self.auth_service = PublicAPIAuthService()
        system_user, self.token = self.auth_service.create_system_user(
            integration_name="room_partner", organization=organization
        )
        self.system_user: SystemUser = system_user
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        self.client = APIClient()

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        from di_core.containers import container

        assert container is not None
        with container.public_api_auth_service.override(self.auth_service):
            response = self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {self.system_user.id}:{self.token}"},
            )
        assert response.status_code == 200
        return response.json()


def _set_flag(organization: Organization, enabled: bool) -> None:
    with organization_context(organization):
        OrganizationFeatureFlag.objects.update_or_create(
            organization=organization,
            key=RESOURCE_CALENDAR_PROVIDER_SYNC,
            defaults={"enabled": enabled},
        )


def _room(organization: Organization, name: str, *, flagged: bool = False) -> Calendar:
    """A Google room: synced, or archived with flagged bookings when ``flagged``."""
    with organization_context(organization):
        room = Calendar.objects.create(
            organization=organization,
            name=name,
            email=f"{name.lower().replace(' ', '-')}@resource.example.com",
            provider=CalendarProvider.GOOGLE,
            external_id=f"ext-{name}",
            calendar_type=CalendarType.RESOURCE,
            capacity=10,
            visibility=CalendarVisibility.INACTIVE if flagged else CalendarVisibility.ACTIVE,
        )
        if flagged:
            now = timezone.now()
            create_resource_provider_link(
                calendar=room,
                sync_status=ResourceSyncStatus.ARCHIVED,
                archived_at=now,
                flagged_bookings_at=now,
            )
        else:
            create_resource_provider_link(calendar=room, sync_status=ResourceSyncStatus.SYNCED)
    return room


def _booking(organization: Organization, room: Calendar, title: str = "M1") -> CalendarEvent:
    """A one-off event tomorrow, on an internal calendar, that books ``room``."""
    start = timezone.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
    start += datetime.timedelta(days=1)
    with organization_context(organization):
        calendar = Calendar.objects.create(
            organization=organization,
            name=f"{title} calendar",
            external_id=f"organizer-{title}",
            provider=CalendarProvider.INTERNAL,
        )
        event = CalendarEvent.objects.create(
            organization=organization,
            calendar=calendar,
            title=title,
            start_time_tz_unaware=start,
            end_time_tz_unaware=start + datetime.timedelta(hours=1),
            timezone="UTC",
        )
        ResourceAllocation.objects.create(organization=organization, event=event, calendar=room)
    return event


def _room_ids(organization: Organization, event: CalendarEvent) -> set[int]:
    with organization_context(organization):
        return set(
            ResourceAllocation.objects.filter(event=event).values_list("calendar_fk_id", flat=True)
        )


def _flagged_at(organization: Organization, room: Calendar) -> datetime.datetime | None:
    with organization_context(organization):
        return ResourceCalendarProviderLink.objects.get(calendar=room).flagged_bookings_at


@pytest.fixture
def organization(db: Any) -> Organization:
    organization = Organization.objects.create(name="Flagged Partner Org")
    _set_flag(organization, True)
    return organization


@pytest.fixture(autouse=True)
def directory(di_container: Any) -> Iterator[FakeRoomDirectory]:
    directory = FakeRoomDirectory(CalendarProvider.GOOGLE)
    di_container.resource_directory_adapter_resolver.override(FakeRoomDirectoryResolver(directory))
    di_container.room_sync_notifier.override(MagicMock())
    try:
        yield directory
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()
        di_container.room_sync_notifier.reset_override()


@pytest.fixture
def api(organization: Organization) -> _Api:
    return _Api(
        organization,
        [
            PublicAPIResources.PREVIEW_RESOURCE_CALENDAR_DELETION,
            PublicAPIResources.RESOLVE_FLAGGED_RESOURCE_BOOKINGS,
        ],
    )


@pytest.fixture
def room_a(organization: Organization) -> Calendar:
    return _room(organization, "Room A", flagged=True)


@pytest.fixture
def room_b(organization: Organization) -> Calendar:
    return _room(organization, "Room B")


def _fingerprint(api: _Api, room: Calendar) -> str:
    data = api.post(PREVIEW_QUERY, {"calendarId": room.id})
    assert "errors" not in data, data
    return data["data"]["resourceCalendarDeletionPreview"]["fingerprint"]


def _resolve(
    api: _Api, organization: Organization, room: Calendar, fingerprint: str, **fields: Any
) -> dict[str, Any]:
    data = api.post(
        RESOLVE_MUTATION,
        {
            "input": {
                "organizationId": organization.id,
                "calendarId": room.id,
                "fingerprint": fingerprint,
                **fields,
            }
        },
    )
    assert "errors" not in data, data
    return data["data"]["resolveFlaggedResourceBookings"]


@pytest.mark.django_db
class TestResolveFlaggedResourceBookings:
    def test_move_moves_the_booking_and_clears_the_flag(
        self, api: _Api, organization: Organization, room_a: Calendar, room_b: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)
        preview = api.post(PREVIEW_QUERY, {"calendarId": room_a.id})
        assert [
            b["eventId"] for b in preview["data"]["resourceCalendarDeletionPreview"]["bookings"]
        ] == [m1.id]

        result = _resolve(
            api,
            organization,
            room_a,
            preview["data"]["resourceCalendarDeletionPreview"]["fingerprint"],
            defaultResolution="MOVE",
            targetCalendarId=room_b.id,
        )

        assert result["success"] is True
        assert result["outcome"] == "RESOLVED"
        assert result["errorMessage"] is None
        assert result["appliedEventIds"] == [m1.id]
        assert result["calendar"]["providerSync"] == {
            "status": "ARCHIVED",
            "flaggedBookingsAt": None,
        }
        assert _room_ids(organization, m1) == {room_b.id}
        assert _flagged_at(organization, room_a) is None

    def test_abort_is_not_accepted(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)

        result = _resolve(
            api, organization, room_a, _fingerprint(api, room_a), defaultResolution="ABORT"
        )

        assert result["success"] is False
        assert result["outcome"] is None
        assert "cannot be cancelled" in result["errorMessage"]
        assert _room_ids(organization, m1) == {room_a.id}
        assert _flagged_at(organization, room_a) is not None

    def test_rejected_resolution_changes_nothing(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)
        fingerprint = _fingerprint(api, room_a)

        result = _resolve(
            api,
            organization,
            room_a,
            fingerprint,
            defaultResolution="MOVE",
            targetCalendarId=room_a.id,
        )

        assert result["success"] is False
        assert result["outcome"] == "REJECTED"
        assert [r["eventId"] for r in result["rejectedBookings"]] == [m1.id]
        assert _flagged_at(organization, room_a) is not None

    def test_a_room_that_is_not_flagged_is_an_error(
        self, api: _Api, organization: Organization, room_b: Calendar
    ) -> None:
        result = _resolve(api, organization, room_b, "x", defaultResolution="REMOVE_ROOM")

        assert result["success"] is False
        assert result["errorMessage"] == "This room has no flagged bookings to resolve."

    def test_target_is_required_for_move(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        result = _resolve(api, organization, room_a, "x", defaultResolution="MOVE")

        assert result["success"] is False
        assert result["outcome"] is None
        assert _flagged_at(organization, room_a) is not None

    def test_flag_off_is_not_enabled(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        _set_flag(organization, False)

        result = _resolve(api, organization, room_a, "x", defaultResolution="REMOVE_ROOM")

        assert result == {
            "success": False,
            "outcome": None,
            "errorMessage": NOT_ENABLED,
            "calendar": None,
            "rejectedBookings": [],
            "appliedEventIds": [],
            "pendingEventIds": [],
            "failedAtEventId": None,
        }

    def test_requires_the_resolve_grant(self, organization: Organization, room_a: Calendar) -> None:
        api = _Api(organization, [PublicAPIResources.DELETE_RESOURCE_CALENDAR])

        data = api.post(
            RESOLVE_MUTATION,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": room_a.id,
                    "fingerprint": "x",
                    "defaultResolution": "REMOVE_ROOM",
                }
            },
        )

        assert data["data"] is None
        assert "don't have access" in data["errors"][0]["message"]
