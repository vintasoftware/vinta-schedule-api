"""Public GraphQL tests for previewing and deleting a synced room.

Covers ``resourceCalendarDeletionPreview`` and ``deleteResourceCalendar``: their
``preview_resource_calendar_deletion`` / ``delete_resource_calendar`` grants, the
resolution input validation, and the flag-off error. The room directory is
``FakeRoomDirectory`` (every room free), and organizer emails are caught by
overriding ``room_sync_notifier``. The provider delete is queued on commit, which
never happens inside a test transaction, so a deleted room stays pending deletion.
"""

import datetime
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType, ResourceSyncStatus
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
        bookings { eventId calendarId title isSeries seriesFrom }
    }
}
"""

DELETE_MUTATION = """
mutation DeleteResourceCalendar($input: DeleteResourceCalendarInput!) {
    deleteResourceCalendar(input: $input) {
        success
        errorMessage
        calendar { id providerSync { status } }
        abortedBookings { eventId }
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


def _google_room(organization: Organization, name: str) -> Calendar:
    with organization_context(organization):
        room = Calendar.objects.create(
            organization=organization,
            name=name,
            email=f"{name.lower().replace(' ', '-')}@resource.example.com",
            provider=CalendarProvider.GOOGLE,
            external_id=f"ext-{name}",
            calendar_type=CalendarType.RESOURCE,
            capacity=10,
        )
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


def _status(organization: Organization, room: Calendar) -> str:
    with organization_context(organization):
        return ResourceCalendarProviderLink.objects.get(calendar=room).sync_status


@pytest.fixture
def organization(db: Any) -> Organization:
    organization = Organization.objects.create(name="Room Partner Org")
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
            PublicAPIResources.DELETE_RESOURCE_CALENDAR,
        ],
    )


@pytest.fixture
def room_a(organization: Organization) -> Calendar:
    return _google_room(organization, "Room A")


@pytest.fixture
def room_b(organization: Organization) -> Calendar:
    return _google_room(organization, "Room B")


def _preview(api: _Api, room: Calendar) -> dict[str, Any]:
    data = api.post(PREVIEW_QUERY, {"calendarId": room.id})
    assert "errors" not in data, data
    return data["data"]["resourceCalendarDeletionPreview"]


def _delete_input(
    organization: Organization, room: Calendar, fingerprint: str, **fields: Any
) -> dict[str, Any]:
    return {
        "input": {
            "organizationId": organization.id,
            "calendarId": room.id,
            "fingerprint": fingerprint,
            **fields,
        }
    }


@pytest.mark.django_db
class TestDeletionPreview:
    def test_lists_the_bookings(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)

        preview = _preview(api, room_a)

        assert preview["fingerprint"]
        assert preview["bookings"] == [
            {
                "eventId": m1.id,
                "calendarId": m1.calendar_fk_id,
                "title": "M1",
                "isSeries": False,
                "seriesFrom": None,
            }
        ]

    def test_manual_room_is_an_error(self, api: _Api, organization: Organization) -> None:
        with organization_context(organization):
            room = Calendar.objects.create(
                organization=organization, name="Manual", calendar_type=CalendarType.RESOURCE
            )

        data = api.post(PREVIEW_QUERY, {"calendarId": room.id})

        assert data["errors"][0]["message"] == (
            "This room is not synced with a provider: use disableResourceCalendar."
        )

    def test_flag_off_is_not_enabled(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        _set_flag(organization, False)

        data = api.post(PREVIEW_QUERY, {"calendarId": room_a.id})

        assert data["errors"][0]["message"] == NOT_ENABLED

    def test_requires_the_preview_grant(self, organization: Organization, room_a: Calendar) -> None:
        api = _Api(organization, [PublicAPIResources.DELETE_RESOURCE_CALENDAR])

        data = api.post(PREVIEW_QUERY, {"calendarId": room_a.id})

        assert data["data"] is None
        assert "don't have access" in data["errors"][0]["message"]


@pytest.mark.django_db
class TestDeleteResourceCalendar:
    def test_move_to_another_room_deletes(
        self, api: _Api, organization: Organization, room_a: Calendar, room_b: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)
        fingerprint = _preview(api, room_a)["fingerprint"]

        data = api.post(
            DELETE_MUTATION,
            _delete_input(
                organization,
                room_a,
                fingerprint,
                defaultResolution="MOVE",
                targetCalendarId=room_b.id,
            ),
        )

        assert data["data"]["deleteResourceCalendar"] == {
            "success": True,
            "errorMessage": None,
            "calendar": {
                "id": str(room_a.id),
                "providerSync": {"status": "PENDING_DELETION"},
            },
            "abortedBookings": [],
            "rejectedBookings": [],
            "appliedEventIds": [m1.id],
            "pendingEventIds": [],
            "failedAtEventId": None,
        }
        with organization_context(organization):
            assert set(
                ResourceAllocation.objects.filter(event=m1).values_list("calendar_fk_id", flat=True)
            ) == {room_b.id}

    def test_invalid_move_is_rejected_per_booking(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)
        fingerprint = _preview(api, room_a)["fingerprint"]

        data = api.post(
            DELETE_MUTATION,
            _delete_input(
                organization,
                room_a,
                fingerprint,
                defaultResolution="MOVE",
                targetCalendarId=room_a.id,
            ),
        )

        result = data["data"]["deleteResourceCalendar"]
        assert result["success"] is False
        assert result["rejectedBookings"] == [
            {
                "eventId": m1.id,
                "reason": "TARGET_IS_SAME_ROOM",
                "message": "target room is the room being deleted",
            }
        ]
        assert _status(organization, room_a) == ResourceSyncStatus.SYNCED

    def test_abort_with_a_booking_returns_it(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)
        fingerprint = _preview(api, room_a)["fingerprint"]

        data = api.post(
            DELETE_MUTATION,
            _delete_input(organization, room_a, fingerprint, defaultResolution="ABORT"),
        )

        result = data["data"]["deleteResourceCalendar"]
        assert result["success"] is False
        assert result["abortedBookings"] == [{"eventId": m1.id}]
        assert _status(organization, room_a) == ResourceSyncStatus.SYNCED

    def test_stale_fingerprint_is_an_error(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        data = api.post(
            DELETE_MUTATION,
            _delete_input(organization, room_a, "stale", defaultResolution="ABORT"),
        )

        assert data["data"]["deleteResourceCalendar"]["success"] is False
        assert data["data"]["deleteResourceCalendar"]["errorMessage"]
        assert _status(organization, room_a) == ResourceSyncStatus.SYNCED

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"defaultResolution": "MOVE"}, "A move needs a target room."),
            (
                {"defaultResolution": "ABORT", "targetCalendarId": 1},
                "A target room only applies to a move.",
            ),
            (
                {
                    "defaultResolution": "ABORT",
                    "overrides": [
                        {"eventId": 7, "resolution": "CANCEL_EVENT"},
                        {"eventId": 7, "resolution": "REMOVE_ROOM"},
                    ],
                },
                "Booking 7 has more than one override.",
            ),
        ],
        ids=["move-without-target", "target-without-move", "duplicate-override"],
    )
    def test_invalid_resolution_input_is_rejected(
        self,
        api: _Api,
        organization: Organization,
        room_a: Calendar,
        fields: dict[str, Any],
        message: str,
    ) -> None:
        data = api.post(DELETE_MUTATION, _delete_input(organization, room_a, "fp", **fields))

        result = data["data"]["deleteResourceCalendar"]
        assert (result["success"], result["errorMessage"]) == (False, message)
        assert _status(organization, room_a) == ResourceSyncStatus.SYNCED

    def test_flag_off_is_not_enabled(
        self, api: _Api, organization: Organization, room_a: Calendar
    ) -> None:
        _set_flag(organization, False)

        data = api.post(
            DELETE_MUTATION,
            _delete_input(organization, room_a, "fp", defaultResolution="ABORT"),
        )

        result = data["data"]["deleteResourceCalendar"]
        assert (result["success"], result["errorMessage"]) == (False, NOT_ENABLED)

    def test_requires_the_delete_grant(self, organization: Organization, room_a: Calendar) -> None:
        api = _Api(organization, [PublicAPIResources.PREVIEW_RESOURCE_CALENDAR_DELETION])

        data = api.post(
            DELETE_MUTATION,
            _delete_input(organization, room_a, "fp", defaultResolution="ABORT"),
        )

        assert data["data"] is None
        assert "don't have access" in data["errors"][0]["message"]
        assert _status(organization, room_a) == ResourceSyncStatus.SYNCED
