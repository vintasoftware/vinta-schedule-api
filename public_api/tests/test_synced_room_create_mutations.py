"""Public GraphQL tests for creating synced rooms and listing their locations.

Covers ``createResourceCalendar`` with ``provider`` / ``locationId`` /
``idempotencyKey``, ``CalendarGraphQLType.providerSync``, and the paginated
``resourceLocations`` query with its ``list_resource_locations`` grant. The room
directory is ``FakeRoomDirectory``, installed by overriding the container's
``resource_directory_adapter_resolver``. The push is queued on commit, which never
happens inside a test transaction, so created rooms stay pending creation.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider
from calendar_integration.factories import create_resource_location
from calendar_integration.models import Calendar, ResourceCalendarProviderLink, ResourceLocation
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService


NOT_ENABLED = "Resource calendar provider sync is not enabled for this organization."

CREATE_MUTATION = """
mutation CreateResourceCalendar($input: CreateResourceCalendarInput!) {
    createResourceCalendar(input: $input) {
        success
        errorMessage
        calendar {
            id
            name
            provider
            externalId
            providerSync {
                status
                failedOperation
                lastError
                lastSyncedAt
                flaggedBookingsAt
                location { id provider buildingName floorName }
            }
        }
    }
}
"""

LOCATIONS_QUERY = """
query ResourceLocations($provider: CalendarProvider, $offset: Int, $limit: Int) {
    resourceLocations(provider: $provider, offset: $offset, limit: $limit) {
        id
        provider
        buildingName
        floorName
    }
}
"""

CALENDARS_QUERY = """
query Calendars($calendarId: Int) {
    calendars(calendarId: $calendarId) {
        id
        providerSync { status }
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


def _enable_flag(organization: Organization) -> None:
    with organization_context(organization):
        OrganizationFeatureFlag.objects.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )


def _location(organization: Organization, **kwargs: Any) -> ResourceLocation:
    with organization_context(organization):
        return create_resource_location(organization=organization, **kwargs)


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Partner Org")


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
            PublicAPIResources.CREATE_RESOURCE_CALENDAR,
            PublicAPIResources.LIST_RESOURCE_LOCATIONS,
            PublicAPIResources.CALENDAR,
        ],
    )


def _create_input(organization: Organization, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"organizationId": organization.id, "name": "Conf Room 4B"}
    payload.update(overrides)
    return {"input": payload}


# ---------------------------------------------------------------------------
# createResourceCalendar
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateSyncedRoom:
    def test_google_room_returns_pending_creation_and_replays_by_key(
        self, api: _Api, organization: Organization, directory: FakeRoomDirectory
    ) -> None:
        _enable_flag(organization)
        location = _location(
            organization,
            external_building_id="hq",
            building_name="HQ",
            external_floor_id="4",
            floor_name="4",
        )
        variables = _create_input(
            organization,
            provider="GOOGLE",
            locationId=location.id,
            idempotencyKey="K1",
            description="Fourth floor",
            capacity=8,
        )

        first = api.post(CREATE_MUTATION, variables)
        second = api.post(CREATE_MUTATION, variables)

        assert "errors" not in first
        result = first["data"]["createResourceCalendar"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        calendar = result["calendar"]
        assert calendar["name"] == "Conf Room 4B"
        assert calendar["provider"] == CalendarProvider.GOOGLE
        assert calendar["providerSync"] == {
            "status": "PENDING_CREATION",
            "failedOperation": None,
            "lastError": "",
            "lastSyncedAt": None,
            "flaggedBookingsAt": None,
            "location": {
                "id": str(location.id),
                "provider": "GOOGLE",
                "buildingName": "HQ",
                "floorName": "4",
            },
        }
        assert second["data"]["createResourceCalendar"]["success"] is True
        assert second["data"]["createResourceCalendar"]["calendar"]["id"] == calendar["id"]
        with organization_context(organization):
            assert ResourceCalendarProviderLink.objects.count() == 1
        # The push waits for the commit, so the provider has not been called yet.
        assert directory.calls == []

    def test_provider_room_is_rejected_when_the_flag_is_off(
        self, api: _Api, organization: Organization, directory: FakeRoomDirectory
    ) -> None:
        location = _location(organization)

        data = api.post(
            CREATE_MUTATION,
            _create_input(organization, provider="GOOGLE", locationId=location.id),
        )

        assert data["data"]["createResourceCalendar"] == {
            "success": False,
            "errorMessage": NOT_ENABLED,
            "calendar": None,
        }
        assert not Calendar.objects.filter_by_organization(organization.id).exists()

    def test_provider_omitted_with_the_flag_off_creates_a_manual_room(
        self, api: _Api, organization: Organization
    ) -> None:
        data = api.post(CREATE_MUTATION, _create_input(organization, capacity=4))

        result = data["data"]["createResourceCalendar"]
        assert result["success"] is True
        assert result["calendar"]["provider"] == CalendarProvider.INTERNAL
        assert result["calendar"]["externalId"] == ""
        assert result["calendar"]["providerSync"] is None
        room = Calendar.objects.filter_by_organization(organization.id).get(
            id=result["calendar"]["id"]
        )
        assert (room.provider, room.capacity) == (CalendarProvider.INTERNAL, 4)

    def test_internal_room_rejects_provider_only_fields(
        self, api: _Api, organization: Organization
    ) -> None:
        data = api.post(CREATE_MUTATION, _create_input(organization, idempotencyKey="K1"))

        assert data["data"]["createResourceCalendar"] == {
            "success": False,
            "errorMessage": (
                "locationId and idempotencyKey only apply to rooms created on Google or Microsoft."
            ),
            "calendar": None,
        }
        assert not Calendar.objects.filter_by_organization(organization.id).exists()

    def test_not_write_enabled_is_rejected(
        self, api: _Api, organization: Organization, directory: FakeRoomDirectory
    ) -> None:
        _enable_flag(organization)
        location = _location(organization, provider=CalendarProvider.MICROSOFT)

        data = api.post(
            CREATE_MUTATION,
            _create_input(organization, provider="MICROSOFT", locationId=location.id),
        )

        assert data["data"]["createResourceCalendar"] == {
            "success": False,
            "errorMessage": "Microsoft 365 write access is not enabled for this organization.",
            "calendar": None,
        }

    def test_requires_the_create_grant(self, organization: Organization) -> None:
        _enable_flag(organization)
        location = _location(organization)
        api = _Api(organization, [PublicAPIResources.LIST_RESOURCE_LOCATIONS])

        data = api.post(
            CREATE_MUTATION,
            _create_input(organization, provider="GOOGLE", locationId=location.id),
        )

        assert data["data"] is None
        assert "don't have access" in data["errors"][0]["message"]


# ---------------------------------------------------------------------------
# providerSync on other calendar reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_provider_sync_is_null_for_a_calendar_without_a_link(
    api: _Api, organization: Organization
) -> None:
    room = baker.make(Calendar, organization=organization, external_id="manual-room")

    data = api.post(CALENDARS_QUERY, {"calendarId": room.id})

    assert data["data"]["calendars"] == [{"id": str(room.id), "providerSync": None}]


# ---------------------------------------------------------------------------
# resourceLocations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResourceLocations:
    def test_lists_active_locations_paginated_and_filtered(
        self, api: _Api, organization: Organization
    ) -> None:
        _enable_flag(organization)
        hq_1 = _location(
            organization,
            external_building_id="hq",
            building_name="HQ",
            external_floor_id="1",
            floor_name="1",
        )
        hq_2 = _location(
            organization,
            external_building_id="hq",
            building_name="HQ",
            external_floor_id="2",
            floor_name="2",
        )
        annex = _location(
            organization,
            external_building_id="annex",
            building_name="Annex",
            external_floor_id="",
            floor_name="",
        )
        tower = _location(
            organization,
            provider=CalendarProvider.MICROSOFT,
            external_building_id="tower",
            building_name="Tower",
        )
        _location(organization, external_building_id="old", building_name="Old", is_active=False)
        other = Organization.objects.create(name="Other Org")
        _location(other, external_building_id="elsewhere", building_name="Elsewhere")

        first_page = api.post(LOCATIONS_QUERY, {"offset": 0, "limit": 2})
        second_page = api.post(LOCATIONS_QUERY, {"offset": 2, "limit": 2})
        google_only = api.post(LOCATIONS_QUERY, {"provider": "GOOGLE"})

        def ids(data: dict[str, Any]) -> list[str]:
            return [row["id"] for row in data["data"]["resourceLocations"]]

        # Ordered by provider, building, floor.
        assert ids(first_page) == [str(annex.id), str(hq_1.id)]
        assert ids(second_page) == [str(hq_2.id), str(tower.id)]
        assert ids(google_only) == [str(annex.id), str(hq_1.id), str(hq_2.id)]
        assert second_page["data"]["resourceLocations"][1] == {
            "id": str(tower.id),
            "provider": "MICROSOFT",
            "buildingName": "Tower",
            "floorName": "1",
        }

    def test_rejects_a_limit_over_100(self, api: _Api, organization: Organization) -> None:
        _enable_flag(organization)

        data = api.post(LOCATIONS_QUERY, {"limit": 101})

        assert data["errors"][0]["message"] == "Limit must be between 1 and 100"

    def test_flag_off_returns_the_not_enabled_error(
        self, api: _Api, organization: Organization
    ) -> None:
        _location(organization)

        data = api.post(LOCATIONS_QUERY, {})

        assert data["data"] is None
        assert data["errors"][0]["message"] == NOT_ENABLED

    def test_requires_the_list_resource_locations_grant(self, organization: Organization) -> None:
        _enable_flag(organization)
        _location(organization)
        api = _Api(organization, [PublicAPIResources.CREATE_RESOURCE_CALENDAR])

        data = api.post(LOCATIONS_QUERY, {})

        assert data["data"] is None
        assert "don't have access" in data["errors"][0]["message"]
