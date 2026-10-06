"""REST tests for ``POST /calendar/{id}/resource/resolve-flagged-bookings/``.

For org admins only, and 404 while the resource calendar provider sync flag is off.
``room_a`` is archived with flagged bookings, as the resync leaves a room the provider
deleted. The room directory is ``FakeRoomDirectory`` (every room free), and organizer
emails are caught by overriding ``room_sync_notifier``.
"""

import datetime
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
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
    organization = Organization.objects.create(name="Flagged Admin Org")
    _set_flag(organization, True)
    return organization


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
    di_container.room_sync_notifier.override(MagicMock())
    try:
        yield directory
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()
        di_container.room_sync_notifier.reset_override()


def _google_room(organization: Organization, name: str, *, flagged: bool = False) -> Calendar:
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


@pytest.fixture
def room_a(organization: Organization) -> Calendar:
    return _google_room(organization, "Room A", flagged=True)


@pytest.fixture
def room_b(organization: Organization) -> Calendar:
    return _google_room(organization, "Room B")


def _booking(organization: Organization, room: Calendar) -> CalendarEvent:
    """A one-off event tomorrow, on an internal calendar, that books ``room``."""
    start = timezone.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
    start += datetime.timedelta(days=1)
    with organization_context(organization):
        calendar = Calendar.objects.create(
            organization=organization,
            name="Organizer",
            external_id="organizer",
            provider=CalendarProvider.INTERNAL,
        )
        event = CalendarEvent.objects.create(
            organization=organization,
            calendar=calendar,
            title="M1",
            start_time_tz_unaware=start,
            end_time_tz_unaware=start + datetime.timedelta(hours=1),
            timezone="UTC",
        )
        ResourceAllocation.objects.create(organization=organization, event=event, calendar=room)
    return event


def _link(organization: Organization, room: Calendar) -> ResourceCalendarProviderLink:
    with organization_context(organization):
        return ResourceCalendarProviderLink.objects.get(calendar=room)


def _preview_url(room: Calendar) -> str:
    return reverse("api:Calendars-resource-deletion-preview", kwargs={"pk": room.id})


def _resolve_url(room: Calendar) -> str:
    return reverse("api:Calendars-resource-resolve-flagged-bookings", kwargs={"pk": room.id})


@pytest.mark.django_db
class TestResolveFlaggedBookingsEndpoint:
    def test_admin_moves_the_bookings_and_clears_the_flag(
        self,
        admin_client: APIClient,
        organization: Organization,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _booking(organization, room_a)
        fingerprint = admin_client.get(_preview_url(room_a)).data["fingerprint"]

        response = admin_client.post(
            _resolve_url(room_a),
            {
                "fingerprint": fingerprint,
                "default_resolution": "move",
                "target_calendar_id": room_b.id,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == room_a.id
        assert response.data["provider_sync"]["status"] == ResourceSyncStatus.ARCHIVED
        assert response.data["provider_sync"]["flagged_bookings_at"] is None
        assert _link(organization, room_a).flagged_bookings_at is None
        with organization_context(organization):
            assert list(
                ResourceAllocation.objects.filter(event=m1).values_list("calendar_fk_id", flat=True)
            ) == [room_b.id]

    def test_abort_is_400(
        self, admin_client: APIClient, organization: Organization, room_a: Calendar
    ) -> None:
        _booking(organization, room_a)
        fingerprint = admin_client.get(_preview_url(room_a)).data["fingerprint"]

        response = admin_client.post(
            _resolve_url(room_a),
            {"fingerprint": fingerprint, "default_resolution": "abort"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert _link(organization, room_a).flagged_bookings_at is not None

    def test_rejected_move_is_400_with_the_bookings(
        self, admin_client: APIClient, organization: Organization, room_a: Calendar
    ) -> None:
        m1 = _booking(organization, room_a)
        fingerprint = admin_client.get(_preview_url(room_a)).data["fingerprint"]

        response = admin_client.post(
            _resolve_url(room_a),
            {
                "fingerprint": fingerprint,
                "default_resolution": "move",
                "target_calendar_id": room_a.id,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["outcome"] == "rejected"
        assert [r["event_id"] for r in response.data["rejected_bookings"]] == [m1.id]
        assert _link(organization, room_a).flagged_bookings_at is not None

    def test_stale_fingerprint_is_409(
        self, admin_client: APIClient, organization: Organization, room_a: Calendar
    ) -> None:
        response = admin_client.post(
            _resolve_url(room_a),
            {"fingerprint": "stale", "default_resolution": "remove_room"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_a_room_that_is_not_flagged_is_400(
        self, admin_client: APIClient, organization: Organization, room_b: Calendar
    ) -> None:
        response = admin_client.post(
            _resolve_url(room_b),
            {"fingerprint": "x", "default_resolution": "remove_room"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_non_admin_is_forbidden(self, member_client: APIClient, room_a: Calendar) -> None:
        response = member_client.post(
            _resolve_url(room_a),
            {"fingerprint": "x", "default_resolution": "remove_room"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_flag_off_is_404(
        self, admin_client: APIClient, organization: Organization, room_a: Calendar
    ) -> None:
        _set_flag(organization, False)

        response = admin_client.post(
            _resolve_url(room_a),
            {"fingerprint": "x", "default_resolution": "remove_room"},
            format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
