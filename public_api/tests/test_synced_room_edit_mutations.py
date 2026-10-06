"""Public GraphQL tests for editing a synced room and retrying its sync.

Covers ``updateResourceCalendar`` on a room with a provider link (including the
``capacity`` omitted / null / int semantics and ``locationId``), and
``retryResourceCalendarSync`` with its ``retry_resource_calendar_sync`` grant. Pushes
are queued on commit, which never happens inside a test transaction, so an edited
synced room stays pending update.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.factories import create_resource_location, create_resource_provider_link
from calendar_integration.models import Calendar, ResourceCalendarProviderLink, ResourceLocation
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


NOT_ENABLED = "Resource calendar provider sync is not enabled for this organization."

UPDATE_MUTATION = """
mutation UpdateResourceCalendar($input: UpdateResourceCalendarInput!) {
    updateResourceCalendar(input: $input) {
        success
        errorMessage
        calendar {
            id
            capacity
            providerSync {
                status
                location { id }
            }
        }
    }
}
"""

RETRY_MUTATION = """
mutation RetryResourceCalendarSync($input: RetryResourceCalendarSyncInput!) {
    retryResourceCalendarSync(input: $input) {
        success
        errorMessage
        calendar {
            id
            providerSync { status failedOperation }
        }
    }
}
"""


class _Api:
    """A public-API client for one organization's system-user token."""

    def __init__(self, organization: Organization, resources: list[str]) -> None:
        self.organization = organization
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


def _location(organization: Organization, **kwargs: Any) -> ResourceLocation:
    with organization_context(organization):
        return create_resource_location(organization=organization, **kwargs)


def _google_room(
    organization: Organization,
    location: ResourceLocation,
    sync_status: str = ResourceSyncStatus.SYNCED,
    **link_kwargs: Any,
) -> Calendar:
    with organization_context(organization):
        room = Calendar.objects.create(
            organization=organization,
            name="Conf Room 4B",
            provider=CalendarProvider.GOOGLE,
            external_id="room-4b",
            calendar_type=CalendarType.RESOURCE,
            capacity=8,
        )
        create_resource_provider_link(
            calendar=room, location=location, sync_status=sync_status, **link_kwargs
        )
    return room


def _link(organization: Organization, room: Calendar) -> ResourceCalendarProviderLink:
    with organization_context(organization):
        return ResourceCalendarProviderLink.objects.get(calendar=room)


@pytest.fixture
def organization(db: Any) -> Organization:
    organization = Organization.objects.create(name="Room Partner Org")
    _set_flag(organization, True)
    return organization


@pytest.fixture
def location(organization: Organization) -> ResourceLocation:
    return _location(organization)


@pytest.fixture
def directory(di_container: Any) -> Iterator[FakeRoomDirectory]:
    directory = FakeRoomDirectory(CalendarProvider.GOOGLE)
    di_container.resource_directory_adapter_resolver.override(FakeRoomDirectoryResolver(directory))
    try:
        yield directory
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()


@pytest.fixture
def api(organization: Organization) -> _Api:
    return _Api(
        organization,
        [
            PublicAPIResources.UPDATE_RESOURCE_CALENDAR,
            PublicAPIResources.RETRY_RESOURCE_CALENDAR_SYNC,
        ],
    )


def _input(organization: Organization, room: Calendar, **fields: Any) -> dict[str, Any]:
    return {"input": {"organizationId": organization.id, "calendarId": room.id, **fields}}


@pytest.mark.django_db
@pytest.mark.usefixtures("directory")
class TestUpdateSyncedRoom:
    def test_capacity_edit_returns_pending_update(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)

        data = api.post(UPDATE_MUTATION, _input(organization, room, capacity=10))

        assert data["data"]["updateResourceCalendar"] == {
            "success": True,
            "errorMessage": None,
            "calendar": {
                "id": str(room.id),
                "capacity": 10,
                "providerSync": {"status": "PENDING_UPDATE", "location": {"id": str(location.id)}},
            },
        }
        assert _link(organization, room).pending_fields == {"capacity": 10}

    def test_omitted_capacity_is_left_unchanged(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)

        data = api.post(UPDATE_MUTATION, _input(organization, room, name="Conf Room 4C"))

        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["capacity"] == 8
        assert _link(organization, room).pending_fields == {"name": "Conf Room 4C"}

    def test_null_capacity_clears_it(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)

        data = api.post(UPDATE_MUTATION, _input(organization, room, capacity=None))

        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["capacity"] is None
        assert _link(organization, room).pending_fields == {"capacity": None}

    def test_location_id_moves_the_room(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)
        annex = _location(organization, external_building_id="annex", external_floor_id="2")

        data = api.post(UPDATE_MUTATION, _input(organization, room, locationId=annex.id))

        result = data["data"]["updateResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["providerSync"]["location"] == {"id": str(annex.id)}

    def test_archived_room_is_rejected(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location, sync_status=ResourceSyncStatus.ARCHIVED)

        data = api.post(UPDATE_MUTATION, _input(organization, room, capacity=10))

        assert data["data"]["updateResourceCalendar"] == {
            "success": False,
            "errorMessage": "An archived room, or one being deleted, cannot be edited.",
            "calendar": None,
        }

    def test_flag_off_keeps_todays_message(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)
        _set_flag(organization, False)

        data = api.post(UPDATE_MUTATION, _input(organization, room, capacity=10))

        assert data["data"]["updateResourceCalendar"] == {
            "success": False,
            "errorMessage": (
                f"Calendar {room.id} is synced from an external provider "
                "(provider=google) and cannot be edited."
            ),
            "calendar": None,
        }


@pytest.mark.django_db
@pytest.mark.usefixtures("directory")
class TestRetryResourceCalendarSync:
    def test_failed_room_goes_back_to_pending(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(
            organization,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
            pending_fields={"capacity": 10},
        )

        data = api.post(RETRY_MUTATION, _input(organization, room))

        assert data["data"]["retryResourceCalendarSync"] == {
            "success": True,
            "errorMessage": None,
            "calendar": {
                "id": str(room.id),
                "providerSync": {"status": "PENDING_UPDATE", "failedOperation": None},
            },
        }

    def test_room_that_did_not_fail_is_rejected(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)

        data = api.post(RETRY_MUTATION, _input(organization, room))

        assert data["data"]["retryResourceCalendarSync"] == {
            "success": False,
            "errorMessage": "Only a room whose sync failed can be retried.",
            "calendar": None,
        }

    def test_flag_off_is_not_enabled(
        self, api: _Api, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(
            organization,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )
        _set_flag(organization, False)

        data = api.post(RETRY_MUTATION, _input(organization, room))

        assert data["data"]["retryResourceCalendarSync"] == {
            "success": False,
            "errorMessage": NOT_ENABLED,
            "calendar": None,
        }

    def test_requires_the_retry_grant(
        self, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(
            organization,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )
        api = _Api(organization, [PublicAPIResources.UPDATE_RESOURCE_CALENDAR])

        data = api.post(RETRY_MUTATION, _input(organization, room))

        assert data["data"] is None
        assert "don't have access" in data["errors"][0]["message"]
        assert _link(organization, room).sync_status == ResourceSyncStatus.SYNC_FAILED

    def test_unknown_calendar_is_not_found(self, api: _Api, organization: Organization) -> None:
        data = api.post(
            RETRY_MUTATION, {"input": {"organizationId": organization.id, "calendarId": 999999}}
        )

        assert data["data"]["retryResourceCalendarSync"] == {
            "success": False,
            "errorMessage": "Calendar not found.",
            "calendar": None,
        }
