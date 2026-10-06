"""REST tests for editing a synced room and retrying its sync.

``PATCH /calendar/{id}/resource/`` and ``POST /calendar/{id}/resource/retry-sync/``
are for org admins only, and 404 while the resource calendar provider sync flag is
off. With the flag on, the generic ``PUT`` / ``PATCH`` / ``DELETE /calendar/{id}/``
reject a synced room, which they would change without telling the provider; with
it off they behave as before.
"""

from collections.abc import Iterator
from typing import Any

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.factories import create_resource_location, create_resource_provider_link
from calendar_integration.models import Calendar, ResourceCalendarProviderLink, ResourceLocation
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from organizations.tests.helpers import make_admin_membership, make_membership
from users.models import User


def _set_flag(organization: Organization, enabled: bool) -> None:
    with organization_context(organization):
        OrganizationFeatureFlag.objects.update_or_create(
            organization=organization,
            key=RESOURCE_CALENDAR_PROVIDER_SYNC,
            defaults={"enabled": enabled},
        )


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Admin Org")


@pytest.fixture
def location(organization: Organization) -> ResourceLocation:
    with organization_context(organization):
        return create_resource_location(organization=organization)


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


@pytest.fixture(autouse=True)
def directory(di_container: Any) -> Iterator[FakeRoomDirectory]:
    directory = FakeRoomDirectory(CalendarProvider.GOOGLE)
    di_container.resource_directory_adapter_resolver.override(FakeRoomDirectoryResolver(directory))
    try:
        yield directory
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()


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


def _reload(organization: Organization, room: Calendar) -> tuple[Calendar, Any]:
    with organization_context(organization):
        room.refresh_from_db()
        return room, ResourceCalendarProviderLink.objects.get(calendar=room)


def _detail(room: Calendar) -> str:
    return reverse("api:Calendars-detail", kwargs={"pk": room.id})


def _resource(room: Calendar) -> str:
    return reverse("api:Calendars-resource-update", kwargs={"pk": room.id})


def _retry(room: Calendar) -> str:
    return reverse("api:Calendars-resource-retry-sync", kwargs={"pk": room.id})


@pytest.mark.django_db
class TestUpdateResourceAction:
    def test_admin_edits_a_synced_room(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        response = admin_client.patch(_resource(room), {"capacity": 10}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["capacity"] == 10
        assert response.data["provider_sync"]["status"] == ResourceSyncStatus.PENDING_UPDATE
        room, link = _reload(organization, room)
        assert room.capacity == 10
        assert link.pending_fields == {"capacity": 10}

    def test_null_capacity_clears_it_and_omitted_leaves_it(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        renamed = admin_client.patch(_resource(room), {"name": "4C"}, format="json")
        cleared = admin_client.patch(_resource(room), {"capacity": None}, format="json")

        assert renamed.data["capacity"] == 8
        assert cleared.data["capacity"] is None

    def test_rejected_edit_is_400(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        response = admin_client.patch(
            _resource(room), {"visibility": CalendarVisibility.INACTIVE}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "use deleteResourceCalendar" in response.data["non_field_errors"][0]

    def test_non_admin_is_forbidden(
        self, member_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        response = member_client.patch(_resource(room), {"capacity": 10}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_flag_off_is_404(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)

        response = admin_client.patch(_resource(room), {"capacity": 10}, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestRetrySyncAction:
    def test_admin_retries_a_failed_room(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(
            organization,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )

        response = admin_client.post(_retry(room))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["provider_sync"]["status"] == ResourceSyncStatus.PENDING_UPDATE

    def test_room_that_did_not_fail_is_400(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        response = admin_client.post(_retry(room))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["non_field_errors"] == [
            "Only a room whose sync failed can be retried."
        ]

    def test_non_admin_is_forbidden(
        self, member_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(
            organization,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )

        response = member_client.post(_retry(room))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_flag_off_is_404(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(
            organization,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
        )

        response = admin_client.post(_retry(room))

        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestGenericActionsOnASyncedRoom:
    def test_flag_on_patch_is_rejected(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        response = admin_client.patch(_detail(room), {"name": "Renamed"}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "PATCH /calendar/{id}/resource/" in response.data["non_field_errors"][0]
        room, link = _reload(organization, room)
        assert room.name == "Conf Room 4B"
        assert link.sync_status == ResourceSyncStatus.SYNCED

    def test_flag_on_delete_is_rejected(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        _set_flag(organization, True)
        room = _google_room(organization, location)

        response = admin_client.delete(_detail(room))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        room, _link = _reload(organization, room)
        assert room.visibility == CalendarVisibility.ACTIVE

    def test_flag_off_patch_and_delete_are_unchanged(
        self, admin_client: APIClient, organization: Organization, location: ResourceLocation
    ) -> None:
        room = _google_room(organization, location)

        patched = admin_client.patch(_detail(room), {"name": "Renamed"}, format="json")
        deleted = admin_client.delete(_detail(room))

        assert patched.status_code == status.HTTP_200_OK
        assert deleted.status_code == status.HTTP_204_NO_CONTENT
        room, _link = _reload(organization, room)
        assert (room.name, room.visibility) == ("Renamed", CalendarVisibility.INACTIVE)

    def test_flag_on_manual_room_is_unchanged(
        self, admin_client: APIClient, organization: Organization
    ) -> None:
        _set_flag(organization, True)
        with organization_context(organization):
            room = Calendar.objects.create(
                organization=organization,
                name="Manual room",
                calendar_type=CalendarType.RESOURCE,
            )

        response = admin_client.patch(_detail(room), {"name": "Renamed"}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["name"] == "Renamed"
