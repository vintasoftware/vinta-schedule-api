"""Tests for the internal REST API exposing CalendarGroup endpoints."""

import datetime
import json
import uuid
from datetime import timedelta

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    EventManagementPermissions,
)
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    CalendarOwnership,
)
from organizations.models import Organization, OrganizationMembership


def _grant_calendar_owner_token(user, calendar):
    """Mirror `CalendarService._grant_calendar_owner_permissions` so the
    permission service can resolve a token for the user+calendar pair."""
    token = CalendarManagementToken.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        user=user,
        token_hash=f"test-{uuid.uuid4().hex}",
    )
    for perm in (
        EventManagementPermissions.CREATE,
        EventManagementPermissions.UPDATE_ATTENDEES,
        EventManagementPermissions.UPDATE_DETAILS,
        EventManagementPermissions.RESCHEDULE,
        EventManagementPermissions.CANCEL,
    ):
        token.permissions.create(permission=perm, organization_id=calendar.organization_id)
    return token


def _assert_status(response, expected):
    assert response.status_code == expected, (
        f"{response.status_code} != {expected}\n"
        f"Response: {json.dumps(response.json() if response.content else {}, indent=2, default=str)}"
    )


@pytest.fixture
def organization(user):
    org = baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")
    baker.make(OrganizationMembership, user=user, organization=org)
    return org


@pytest.fixture
def internal_calendars(organization):
    calendars = {}
    for name, external in (
        ("Dr. A", "phys_a"),
        ("Dr. B", "phys_b"),
        ("Room 1", "room_1"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.INTERNAL,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
            accepts_public_scheduling=True,
        )
    return calendars


@pytest.fixture
def owned_group(user, organization, internal_calendars):
    """A group where `user` owns at least one of the pool calendars so the
    CalendarGroupPermission passes object-level checks."""
    CalendarOwnership.objects.create(
        organization=organization, calendar=internal_calendars["phys_a"], user=user
    )
    group = CalendarGroup.objects.create(organization=organization, name="Clinic")
    physicians = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Physicians", order=0
    )
    rooms = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Rooms", order=1
    )
    for cal in (internal_calendars["phys_a"], internal_calendars["phys_b"]):
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=physicians, calendar=cal
        )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=rooms, calendar=internal_calendars["room_1"]
    )
    return group


@pytest.mark.django_db
class TestCalendarGroupCrud:
    def test_list_requires_auth(self, anonymous_client):
        url = reverse("api:CalendarGroups-list")
        response = anonymous_client.get(url)
        _assert_status(response, status.HTTP_401_UNAUTHORIZED)

    def test_list_scoped_to_organization(self, auth_client, organization, owned_group):
        other_org = baker.make(Organization)
        CalendarGroup.objects.create(organization=other_org, name="Other")
        url = reverse("api:CalendarGroups-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        ids = [g["id"] for g in response.data["results"]]
        assert ids == [owned_group.id]

    def test_retrieve(self, auth_client, owned_group):
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data["name"] == "Clinic"
        assert {s["name"] for s in response.data["slots"]} == {"Physicians", "Rooms"}

    def test_retrieve_forbidden_if_user_does_not_own_any_pool_calendar(
        self, auth_client, organization, internal_calendars
    ):
        group = CalendarGroup.objects.create(organization=organization, name="Foreign")
        slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Slot")
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=internal_calendars["phys_b"]
        )
        # user doesn't own phys_b → no permission
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_403_FORBIDDEN)

    def test_create_group(self, auth_client, organization, internal_calendars, user):
        # The create endpoint uses the serializer which delegates to
        # CalendarGroupService; make sure the user owns one calendar so
        # the subsequent object-level access on retrieve works too.
        CalendarOwnership.objects.create(
            organization=organization, calendar=internal_calendars["phys_a"], user=user
        )
        url = reverse("api:CalendarGroups-list")
        payload = {
            "name": "New Clinic",
            "description": "",
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [
                        internal_calendars["phys_a"].id,
                        internal_calendars["phys_b"].id,
                    ],
                    "required_count": 1,
                    "order": 0,
                },
                {
                    "name": "Rooms",
                    "calendar_ids": [internal_calendars["room_1"].id],
                    "required_count": 1,
                    "order": 1,
                },
            ],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_201_CREATED)
        created = CalendarGroup.objects.filter_by_organization(organization.id).get(
            name="New Clinic"
        )
        assert set(created.slots.values_list("name", flat=True)) == {"Physicians", "Rooms"}

    def test_create_group_rejects_duplicate_slot_name(
        self, auth_client, organization, internal_calendars, user
    ):
        CalendarOwnership.objects.create(
            organization=organization, calendar=internal_calendars["phys_a"], user=user
        )
        url = reverse("api:CalendarGroups-list")
        payload = {
            "name": "Bad",
            "slots": [
                {"name": "Dup", "calendar_ids": [internal_calendars["phys_a"].id]},
                {"name": "Dup", "calendar_ids": [internal_calendars["phys_b"].id]},
            ],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)
        assert "duplicate" in json.dumps(response.data).lower()

    def test_update_group(self, auth_client, owned_group, internal_calendars):
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": "Clinic Renamed",
            "description": "New desc",
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                },
                {
                    "name": "Rooms",
                    "calendar_ids": [internal_calendars["room_1"].id],
                    "required_count": 1,
                    "order": 1,
                },
            ],
        }
        response = auth_client.put(url, payload, format="json")
        _assert_status(response, status.HTTP_200_OK)
        owned_group.refresh_from_db()
        assert owned_group.name == "Clinic Renamed"
        assert set(
            owned_group.slots.get(name="Physicians").calendars.values_list("external_id", flat=True)
        ) == {"phys_a"}

    def test_destroy(self, auth_client, owned_group):
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_204_NO_CONTENT)
        assert not CalendarGroup.objects.filter(id=owned_group.id).exists()

    def test_destroy_refused_when_group_has_events(
        self, auth_client, owned_group, internal_calendars, organization
    ):
        baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=internal_calendars["phys_a"],
            calendar_group_fk=owned_group,
            title="Pinned",
            external_id="ev_pinned",
            start_time_tz_unaware=datetime.datetime.now(datetime.UTC) + timedelta(hours=1),
            end_time_tz_unaware=datetime.datetime.now(datetime.UTC) + timedelta(hours=2),
            timezone="UTC",
        )
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
class TestCalendarGroupEventActions:
    def _make_window_available(self, calendars, start, end):
        for cal in calendars:
            AvailableTime.objects.create(
                organization=cal.organization,
                calendar=cal,
                start_time_tz_unaware=start,
                end_time_tz_unaware=end,
                timezone="UTC",
            )

    def test_create_event_action(
        self, auth_client, user, owned_group, internal_calendars, organization
    ):
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(hours=1)
        self._make_window_available(internal_calendars.values(), start, end)
        # The create_event flow needs a management token for the primary calendar.
        _grant_calendar_owner_token(user, internal_calendars["phys_a"])
        physicians = owned_group.slots.get(name="Physicians")
        rooms = owned_group.slots.get(name="Rooms")

        url = reverse("api:CalendarGroups-create-event", kwargs={"pk": owned_group.id})
        payload = {
            "title": "Follow-up",
            "description": "",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "timezone": "UTC",
            "slot_selections": [
                {"slot_id": physicians.id, "calendar_ids": [internal_calendars["phys_a"].id]},
                {"slot_id": rooms.id, "calendar_ids": [internal_calendars["room_1"].id]},
            ],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_201_CREATED)
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(title="Follow-up")
        assert event.calendar_fk_id == internal_calendars["phys_a"].id
        assert event.calendar_group_fk_id == owned_group.id
        assert (
            CalendarEventGroupSelection.objects.filter_by_organization(organization.id)
            .filter(event_fk=event)
            .count()
            == 2
        )

    def test_create_event_action_rejects_unavailable_calendar(
        self, auth_client, user, owned_group, internal_calendars
    ):
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(hours=1)
        # no AvailableTime — calendars aren't available
        _grant_calendar_owner_token(user, internal_calendars["phys_a"])
        physicians = owned_group.slots.get(name="Physicians")
        rooms = owned_group.slots.get(name="Rooms")

        url = reverse("api:CalendarGroups-create-event", kwargs={"pk": owned_group.id})
        payload = {
            "title": "Nope",
            "description": "",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "timezone": "UTC",
            "slot_selections": [
                {"slot_id": physicians.id, "calendar_ids": [internal_calendars["phys_a"].id]},
                {"slot_id": rooms.id, "calendar_ids": [internal_calendars["room_1"].id]},
            ],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)
        assert "not available" in json.dumps(response.data).lower()

    def test_list_events_action(self, auth_client, owned_group, internal_calendars, organization):
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(hours=1)
        in_range = baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=internal_calendars["phys_a"],
            calendar_group_fk=owned_group,
            title="Grouped",
            external_id="ev_grouped",
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )
        baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=internal_calendars["phys_a"],
            title="Standalone",
            external_id="ev_standalone",
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )
        url = reverse("api:CalendarGroups-list-events", kwargs={"pk": owned_group.id})
        response = auth_client.get(
            url,
            {
                "start_datetime": start.isoformat(),
                "end_datetime": (end + timedelta(hours=1)).isoformat(),
            },
        )
        _assert_status(response, status.HTTP_200_OK)
        assert [e["id"] for e in response.data] == [in_range.id]

    def test_availability_action(self, auth_client, owned_group, internal_calendars, organization):
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(hours=1)
        self._make_window_available(internal_calendars.values(), start, end)
        url = reverse("api:CalendarGroups-availability", kwargs={"pk": owned_group.id})
        response = auth_client.post(
            url,
            {"ranges": [{"start_time": start.isoformat(), "end_time": end.isoformat()}]},
            format="json",
        )
        _assert_status(response, status.HTTP_200_OK)
        assert len(response.data) == 1
        slot_ids_in_payload = {s["slot_id"] for s in response.data[0]["slots"]}
        assert slot_ids_in_payload == set(owned_group.slots.values_list("id", flat=True))

    def test_bookable_slots_action(
        self, auth_client, owned_group, internal_calendars, organization
    ):
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(hours=1)
        self._make_window_available(internal_calendars.values(), start, end)
        url = reverse("api:CalendarGroups-bookable-slots", kwargs={"pk": owned_group.id})
        response = auth_client.get(
            url,
            {
                "search_window_start": start.isoformat(),
                "search_window_end": end.isoformat(),
                "duration_seconds": str(60 * 60),
                "slot_step_seconds": str(60 * 60),
            },
        )
        _assert_status(response, status.HTTP_200_OK)
        assert len(response.data) == 1

    def test_bookable_slots_missing_params(self, auth_client, owned_group):
        url = reverse("api:CalendarGroups-bookable-slots", kwargs={"pk": owned_group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
class TestPermissionBoundary:
    def test_cannot_access_other_org_group(
        self, auth_client, user, organization, internal_calendars
    ):
        other_org = baker.make(Organization)
        other_cal = Calendar.objects.create(
            organization=other_org,
            name="Other",
            external_id="other",
            provider=CalendarProvider.INTERNAL,
        )
        # User owns a calendar in THEIR org, but other_group belongs to another org.
        CalendarOwnership.objects.create(
            organization=organization, calendar=internal_calendars["phys_a"], user=user
        )
        other_group = CalendarGroup.objects.create(organization=other_org, name="Other")
        other_slot = CalendarGroupSlot.objects.create(
            organization=other_org, group=other_group, name="Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=other_org, slot=other_slot, calendar=other_cal
        )
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": other_group.id})
        response = auth_client.get(url)
        # Queryset is org-scoped, so it should 404 rather than 403.
        _assert_status(response, status.HTTP_404_NOT_FOUND)
