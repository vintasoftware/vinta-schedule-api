"""REST tests for creating synced rooms and listing resource locations.

``POST /calendar/resource/`` with ``provider`` / ``location_id`` /
``idempotency_key`` and ``GET /calendar/resource-locations/``. Both are for org
admins only. The room directory is ``FakeRoomDirectory``, installed by overriding
the container's ``resource_directory_adapter_resolver``.
"""

from collections.abc import Iterator
from typing import Any

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider
from calendar_integration.factories import create_resource_location
from calendar_integration.models import Calendar, ResourceLocation
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from organizations.tests.helpers import make_admin_membership, make_membership
from users.models import User


NOT_ENABLED = "Resource calendar provider sync is not enabled for this organization."


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
    return Organization.objects.create(name="Room Admin Org")


@pytest.fixture
def admin_client(organization: Organization) -> APIClient:
    admin = baker.make(User)
    make_admin_membership(user=admin, organization=organization)
    client = APIClient()
    client.force_authenticate(user=admin)
    return client


@pytest.fixture
def member_client(organization: Organization) -> APIClient:
    member = baker.make(User)
    make_membership(user=member, organization=organization)
    client = APIClient()
    client.force_authenticate(user=member)
    return client


@pytest.fixture
def directory(di_container: Any) -> Iterator[FakeRoomDirectory]:
    directory = FakeRoomDirectory(CalendarProvider.GOOGLE)
    di_container.resource_directory_adapter_resolver.override(FakeRoomDirectoryResolver(directory))
    try:
        yield directory
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()


@pytest.mark.django_db
class TestResourceLocationsEndpoint:
    url = "api:Calendars-resource-locations"

    def test_admin_lists_active_locations(
        self, admin_client: APIClient, organization: Organization
    ) -> None:
        _enable_flag(organization)
        hq = _location(organization, external_building_id="hq", building_name="HQ")
        tower = _location(organization, provider=CalendarProvider.MICROSOFT, building_name="Tower")
        _location(organization, external_building_id="old", is_active=False)

        response = admin_client.get(reverse(self.url))
        google_only = admin_client.get(reverse(self.url), {"provider": "google"})

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 2
        assert [dict(row) for row in response.data["results"]] == [
            {"id": hq.id, "provider": "google", "building_name": "HQ", "floor_name": "1"},
            {"id": tower.id, "provider": "microsoft", "building_name": "Tower", "floor_name": "1"},
        ]
        assert [row["id"] for row in google_only.data["results"]] == [hq.id]

    def test_flag_off_is_404(self, admin_client: APIClient, organization: Organization) -> None:
        _location(organization)

        response = admin_client.get(reverse(self.url))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_non_admin_is_forbidden(
        self, member_client: APIClient, organization: Organization
    ) -> None:
        _enable_flag(organization)

        response = member_client.get(reverse(self.url))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unknown_provider_is_400(
        self, admin_client: APIClient, organization: Organization
    ) -> None:
        _enable_flag(organization)

        response = admin_client.get(reverse(self.url), {"provider": "dropbox"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestCreateSyncedRoomEndpoint:
    url = "api:Calendars-resource"

    def test_admin_creates_a_google_room_pending_creation(
        self,
        admin_client: APIClient,
        organization: Organization,
        directory: FakeRoomDirectory,
    ) -> None:
        _enable_flag(organization)
        location = _location(organization, building_name="HQ")
        payload = {
            "name": "Conf Room 4B",
            "capacity": 8,
            "provider": "google",
            "location_id": location.id,
            "idempotency_key": "K1",
        }

        response = admin_client.post(reverse(self.url), payload, format="json")
        replay = admin_client.post(reverse(self.url), payload, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["provider"] == CalendarProvider.GOOGLE
        assert response.data["provider_sync"] == {
            "status": "pending_creation",
            "failed_operation": "",
            "last_error": "",
            "last_synced_at": None,
            "location": {
                "id": location.id,
                "provider": "google",
                "building_name": "HQ",
                "floor_name": "1",
            },
            "flagged_bookings_at": None,
        }
        assert replay.status_code == status.HTTP_201_CREATED
        assert replay.data["id"] == response.data["id"]
        assert Calendar.objects.filter_by_organization(organization.id).count() == 1

    def test_replay_of_a_disabled_room_returns_it(
        self,
        admin_client: APIClient,
        organization: Organization,
        directory: FakeRoomDirectory,
    ) -> None:
        _enable_flag(organization)
        location = _location(organization)
        payload = {
            "name": "Conf Room 4B",
            "provider": "google",
            "location_id": location.id,
            "idempotency_key": "K1",
        }
        created = admin_client.post(reverse(self.url), payload, format="json")
        disabled = admin_client.delete(
            reverse("api:Calendars-detail", kwargs={"pk": created.data["id"]})
        )

        replay = admin_client.post(reverse(self.url), payload, format="json")

        assert disabled.status_code == status.HTTP_204_NO_CONTENT
        assert replay.status_code == status.HTTP_201_CREATED
        assert replay.data["id"] == created.data["id"]
        assert replay.data["visibility"] == "inactive"
        assert replay.data["provider_sync"]["status"] == "pending_creation"

    def test_provider_room_with_the_flag_off_is_400(
        self, admin_client: APIClient, organization: Organization
    ) -> None:
        location = _location(organization)

        response = admin_client.post(
            reverse(self.url),
            {"name": "Conf Room", "provider": "google", "location_id": location.id},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["non_field_errors"] == [NOT_ENABLED]
        assert not Calendar.objects.filter_by_organization(organization.id).exists()

    def test_manual_room_response_is_unchanged(
        self, admin_client: APIClient, organization: Organization
    ) -> None:
        response = admin_client.post(
            reverse(self.url), {"name": "Manual Room", "capacity": 4}, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["provider"] == CalendarProvider.INTERNAL
        assert "provider_sync" not in response.data

    def test_manual_room_rejects_provider_only_fields(
        self, admin_client: APIClient, organization: Organization
    ) -> None:
        response = admin_client.post(
            reverse(self.url), {"name": "Manual Room", "idempotency_key": "K1"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not Calendar.objects.filter_by_organization(organization.id).exists()

    def test_non_admin_is_forbidden(
        self, member_client: APIClient, organization: Organization
    ) -> None:
        _enable_flag(organization)
        location = _location(organization)

        response = member_client.post(
            reverse(self.url),
            {"name": "Conf Room", "provider": "google", "location_id": location.id},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not Calendar.objects.filter_by_organization(organization.id).exists()
