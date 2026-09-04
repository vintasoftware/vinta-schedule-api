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
from calendar_integration.factories import create_calendar_ownership, create_calendar_pool
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotPool,
    CalendarManagementToken,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups


def _grant_calendar_owner_token(user, calendar):
    """Mirror `CalendarService._grant_calendar_owner_permissions` so the
    permission service can resolve a token for the user+calendar pair."""
    OrganizationMembership.objects.get_or_create(user=user, organization=calendar.organization)
    token = CalendarManagementToken.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        membership_user_id=user.id,
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
def admin_user(user, organization):
    """Promote `user`'s membership in `organization` to admin.

    Group create/update/delete is admin-only. Depend on this fixture -- in
    addition to `auth_client` -- in any test that expects a write to succeed.
    """
    membership = OrganizationMembership.objects.get(user=user, organization=organization)
    grant_membership_groups(membership, [GROUP_ORGANIZATION_ADMIN])
    return user


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
    create_calendar_ownership(
        calendar=internal_calendars["phys_a"],
        user=user,
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

    def test_list_excludes_group_member_is_not_part_of(
        self, auth_client, organization, owned_group, internal_calendars
    ):
        """Same-org group the caller owns no pool calendar in is simply absent
        from a non-admin member's list -- not merely 403 on retrieve."""
        foreign_group = CalendarGroup.objects.create(organization=organization, name="Foreign")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=foreign_group, name="Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=internal_calendars["phys_b"]
        )
        url = reverse("api:CalendarGroups-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        ids = [g["id"] for g in response.data["results"]]
        assert ids == [owned_group.id]
        assert foreign_group.id not in ids

    def test_list_admin_sees_every_group_in_organization(
        self, auth_client, organization, owned_group, internal_calendars, admin_user
    ):
        """An org admin sees every group in the org, including ones they own
        no pool calendar in."""
        foreign_group = CalendarGroup.objects.create(organization=organization, name="Foreign")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=foreign_group, name="Slot"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=internal_calendars["phys_b"]
        )
        url = reverse("api:CalendarGroups-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        ids = {g["id"] for g in response.data["results"]}
        assert ids == {owned_group.id, foreign_group.id}

    def test_retrieve(self, auth_client, owned_group):
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data["name"] == "Clinic"
        assert {s["name"] for s in response.data["slots"]} == {"Physicians", "Rooms"}

    def test_retrieve_exposes_public_booking_slug(self, auth_client, owned_group):
        """An org member can read `public_booking_slug` to build a codeless
        public booking link (Phase 3b) -- it is present and matches the
        model's own value exactly."""
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data["public_booking_slug"] == owned_group.public_booking_slug
        assert response.data["public_booking_slug"]

    def test_retrieve_lists_a_doubly_sourced_calendar_once(
        self, auth_client, organization, internal_calendars, owned_group
    ):
        """A calendar reachable both inline and through an attached pool is one
        entry in ``slots[].calendars``, not two.

        Since Calendar Pools projected pool rosters into
        ``CalendarGroupSlotMembership``, the slot's M2M can yield the same
        ``Calendar`` once per source row. ``CalendarGroupSlotVirtualModel``
        deduplicates the prefetch; without that this response would repeat the
        calendar.
        """
        from calendar_integration.factories import create_calendar_pool
        from calendar_integration.models import CalendarGroupSlotPool

        physicians = owned_group.slots.get(name="Physicians")
        pool = create_calendar_pool(
            organization=organization,
            name="Nurses",
            calendars=[internal_calendars["phys_a"]],
        )
        CalendarGroupSlotPool.objects.create(organization=organization, slot=physicians, pool=pool)
        CalendarGroupSlotMembership.objects.create(
            organization=organization,
            slot=physicians,
            calendar=internal_calendars["phys_a"],
            source_pool=pool,
        )

        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.get(url)

        _assert_status(response, status.HTTP_200_OK)
        slot_payload = next(s for s in response.data["slots"] if s["name"] == "Physicians")
        calendar_ids = [c["id"] for c in slot_payload["calendars"]]
        assert sorted(calendar_ids) == sorted(
            [internal_calendars["phys_a"].id, internal_calendars["phys_b"].id]
        )

    def test_retrieve_not_found_if_user_does_not_own_any_pool_calendar(
        self, auth_client, organization, internal_calendars
    ):
        """A same-org group the caller isn't part of is 404 (not merely 403):
        `get_queryset()` scopes a non-admin member's visibility to groups they
        participate in, so a non-part-of group never reaches the object-level
        permission check at all."""
        group = CalendarGroup.objects.create(organization=organization, name="Foreign")
        slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Slot")
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=internal_calendars["phys_b"]
        )
        # user doesn't own phys_b → not a participant → absent from the queryset
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_admin_sees_group_they_do_not_participate_in(
        self, auth_client, organization, internal_calendars, admin_user
    ):
        group = CalendarGroup.objects.create(organization=organization, name="Foreign")
        slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Slot")
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=internal_calendars["phys_b"]
        )
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": group.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data["name"] == "Foreign"

    def test_create_group_member_forbidden(
        self, auth_client, organization, internal_calendars, user
    ):
        """A non-admin member may not create a CalendarGroup, even though they
        own calendars that would be in its slots."""
        create_calendar_ownership(
            calendar=internal_calendars["phys_a"],
            user=user,
        )
        url = reverse("api:CalendarGroups-list")
        payload = {
            "name": "New Clinic",
            "description": "",
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                },
            ],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_403_FORBIDDEN)
        assert (
            not CalendarGroup.objects.filter_by_organization(organization.id)
            .filter(name="New Clinic")
            .exists()
        )

    def test_update_group_member_forbidden(self, auth_client, owned_group, internal_calendars):
        """A non-admin member who participates in `owned_group` still may not
        update it -- participation grants visibility, not management."""
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": "Hijacked",
            "description": "",
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                },
            ],
        }
        response = auth_client.put(url, payload, format="json")
        _assert_status(response, status.HTTP_403_FORBIDDEN)
        owned_group.refresh_from_db()
        assert owned_group.name == "Clinic"

    def test_destroy_group_member_forbidden(self, auth_client, owned_group):
        """A non-admin member who participates in `owned_group` still may not
        delete it."""
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_403_FORBIDDEN)
        assert CalendarGroup.original_manager.filter(id=owned_group.id).exists()

    def test_create_group(self, auth_client, organization, internal_calendars, user, admin_user):
        # The create endpoint uses the serializer which delegates to
        # CalendarGroupService; make sure the user owns one calendar so
        # the subsequent object-level access on retrieve works too.
        create_calendar_ownership(
            calendar=internal_calendars["phys_a"],
            user=user,
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

    def test_create_rejects_client_supplied_public_booking_slug(
        self, auth_client, organization, internal_calendars, user, admin_user
    ):
        """`public_booking_slug` is read-only (Phase 3b): a client-supplied
        value in the create payload is silently ignored, never adopted -- the
        created group still gets its own, distinct, server-generated slug."""
        create_calendar_ownership(
            calendar=internal_calendars["phys_a"],
            user=user,
        )
        url = reverse("api:CalendarGroups-list")
        payload = {
            "name": "Slug Hijack Attempt",
            "description": "",
            "public_booking_slug": "attacker-chosen-slug",
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                },
            ],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_201_CREATED)
        created = CalendarGroup.objects.filter_by_organization(organization.id).get(
            name="Slug Hijack Attempt"
        )
        assert created.public_booking_slug != "attacker-chosen-slug"
        assert created.public_booking_slug
        assert response.data["public_booking_slug"] == created.public_booking_slug

    def test_create_group_rejects_duplicate_slot_name(
        self, auth_client, organization, internal_calendars, user, admin_user
    ):
        create_calendar_ownership(
            calendar=internal_calendars["phys_a"],
            user=user,
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

    def test_update_group(self, auth_client, owned_group, internal_calendars, admin_user):
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

    def test_update_rejects_client_supplied_public_booking_slug(
        self, auth_client, owned_group, internal_calendars, admin_user
    ):
        """`public_booking_slug` is read-only (Phase 3b): attempting to
        overwrite an existing group's slug via update is silently ignored --
        the slug an admin already handed out as a booking link must not be
        able to be invalidated (or hijacked) through this endpoint."""
        original_slug = owned_group.public_booking_slug
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": "Clinic Renamed",
            "description": "New desc",
            "public_booking_slug": "attacker-chosen-slug",
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
        assert owned_group.public_booking_slug == original_slug
        assert response.data["public_booking_slug"] == original_slug

    def test_partial_update_rejects_client_supplied_public_booking_slug(
        self, auth_client, owned_group, internal_calendars, admin_user
    ):
        """Same guarantee as ``test_update_rejects_client_supplied_public_booking_slug``,
        exercised through PATCH rather than PUT -- a client-supplied
        ``public_booking_slug`` must not be able to overwrite the existing
        slug via a partial update either. ``name``/``slots`` are still
        included in the payload -- unrelated to the read-only check this test
        targets, but required because ``CalendarGroupSerializer.update()``
        reconstructs the full group input regardless of HTTP method (PATCH
        is not a true partial update on this endpoint), so a payload missing
        them would 500/wipe slots for reasons this test isn't about."""
        original_slug = owned_group.public_booking_slug
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": owned_group.name,
            "public_booking_slug": "attacker-chosen-slug",
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

        response = auth_client.patch(url, payload, format="json")

        _assert_status(response, status.HTTP_200_OK)
        owned_group.refresh_from_db()
        assert owned_group.public_booking_slug == original_slug
        assert response.data["public_booking_slug"] == original_slug

    def test_destroy(self, auth_client, owned_group, admin_user):
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_204_NO_CONTENT)
        assert not CalendarGroup.original_manager.filter(id=owned_group.id).exists()

    def test_destroy_refused_when_group_has_events(
        self, auth_client, owned_group, internal_calendars, organization, admin_user
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
class TestCalendarGroupPatch:
    """`slots` has no "omitted means unchanged" sentinel, so a PATCH that
    omits it entirely must be rejected rather than silently delete every
    existing slot (and every pool attachment with it) -- pre-existing on
    `main` (`CalendarGroupSerializer._to_input_data`'s
    `validated_data.get("slots", [])`), fixed here at the requester's
    explicit request."""

    @pytest.fixture
    def pool(self, organization, internal_calendars):
        return create_calendar_pool(
            organization=organization,
            name="Nurses",
            calendars=[internal_calendars["phys_b"]],
        )

    def test_patch_omitting_slots_leaves_existing_slots_and_pool_attachments_intact(
        self, auth_client, owned_group, internal_calendars, admin_user, organization, pool
    ):
        physicians = owned_group.slots.get(name="Physicians")
        CalendarGroupSlotPool.objects.create(organization=organization, slot=physicians, pool=pool)

        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        response = auth_client.patch(url, {"description": "x"}, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)
        assert "slots" in response.data

        # Existing slots survive, with their calendar rosters intact.
        assert {s.name for s in owned_group.slots.all()} == {"Physicians", "Rooms"}
        assert set(
            CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
            .filter(slot=physicians)
            .values_list("calendar_fk_id", flat=True)
        ) == {internal_calendars["phys_a"].id, internal_calendars["phys_b"].id}
        # Pool attachment survives.
        assert (
            CalendarGroupSlotPool.objects.filter_by_organization(organization.id)
            .filter(slot=physicians, pool=pool)
            .exists()
        )

    def test_patch_slot_missing_calendar_ids_returns_400_not_500(
        self, auth_client, owned_group, internal_calendars, admin_user
    ):
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": owned_group.name,
            "description": owned_group.description,
            "slots": [
                {
                    "name": "Physicians",
                    # calendar_ids omitted -- must not KeyError.
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
        response = auth_client.patch(url, payload, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)
        assert "slots" in response.data

    def test_patch_supplying_every_key_behaves_like_put(
        self, auth_client, owned_group, internal_calendars, admin_user
    ):
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
        response = auth_client.patch(url, payload, format="json")
        _assert_status(response, status.HTTP_200_OK)
        owned_group.refresh_from_db()
        assert owned_group.name == "Clinic Renamed"
        assert set(
            owned_group.slots.get(name="Physicians").calendars.values_list("external_id", flat=True)
        ) == {"phys_a"}


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
        create_calendar_ownership(
            calendar=internal_calendars["phys_a"],
            user=user,
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


@pytest.mark.django_db
class TestCalendarGroupSlotPoolAttachment:
    """Phase 4: `pool_ids` (write) / `pools` (read) on `CalendarGroupSlotSerializer`.

    The load-bearing behavior is the omit-versus-empty-list distinction:
    omitting `pool_ids` from a slot payload must leave that slot's pool
    attachments untouched, while an explicit `[]` must detach every pool. A
    group update payload never includes `pool_ids` unless the client knows
    about pools at all, so a client that predates this phase must round-trip
    a group's attachments unchanged.
    """

    @pytest.fixture
    def pool(self, organization, internal_calendars):
        return create_calendar_pool(
            organization=organization,
            name="Nurses",
            calendars=[internal_calendars["phys_b"]],
        )

    def test_update_group_omitting_pool_ids_leaves_attachments_untouched(
        self, auth_client, owned_group, internal_calendars, admin_user, organization, pool
    ):
        physicians = owned_group.slots.get(name="Physicians")
        CalendarGroupSlotPool.objects.create(organization=organization, slot=physicians, pool=pool)

        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": owned_group.name,
            "description": owned_group.description,
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                    # pool_ids omitted entirely -- must leave the attachment below untouched.
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
        assert (
            CalendarGroupSlotPool.objects.filter_by_organization(organization.id)
            .filter(slot=physicians, pool=pool)
            .exists()
        )

    def test_update_group_empty_pool_ids_detaches_all(
        self, auth_client, owned_group, internal_calendars, admin_user, organization, pool
    ):
        physicians = owned_group.slots.get(name="Physicians")
        CalendarGroupSlotPool.objects.create(organization=organization, slot=physicians, pool=pool)

        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": owned_group.name,
            "description": owned_group.description,
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                    "pool_ids": [],
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
        assert not (
            CalendarGroupSlotPool.objects.filter_by_organization(organization.id)
            .filter(slot=physicians, pool=pool)
            .exists()
        )

    def test_update_group_explicit_pool_ids_attaches(
        self, auth_client, owned_group, internal_calendars, admin_user, organization, pool
    ):
        physicians = owned_group.slots.get(name="Physicians")
        url = reverse("api:CalendarGroups-detail", kwargs={"pk": owned_group.id})
        payload = {
            "name": owned_group.name,
            "description": owned_group.description,
            "slots": [
                {
                    "name": "Physicians",
                    "calendar_ids": [internal_calendars["phys_a"].id],
                    "required_count": 1,
                    "order": 0,
                    "pool_ids": [pool.id],
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
        assert (
            CalendarGroupSlotPool.objects.filter_by_organization(organization.id)
            .filter(slot=physicians, pool=pool)
            .exists()
        )
        physicians_payload = next(s for s in response.data["slots"] if s["name"] == "Physicians")
        assert [p["id"] for p in physicians_payload["pools"]] == [pool.id]


@pytest.mark.django_db
class TestCalendarGroupListQueryCountWithPools:
    """Item A: `CalendarGroupSlotVirtualModel.pools` was deliberately left off
    in Phase 3, so it has to land together with the serializer field it
    supports -- pinned here so the group list endpoint does not regress into
    one extra query per group.
    """

    def _make_group_with_pool(self, organization, internal_calendars, pool, name):
        group = CalendarGroup.objects.create(organization=organization, name=name)
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=group, name="Physicians"
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=slot, calendar=internal_calendars["phys_a"]
        )
        CalendarGroupSlotPool.objects.create(organization=organization, slot=slot, pool=pool)
        return group

    def test_list_query_count_does_not_grow_with_group_count(
        self, auth_client, organization, admin_user, internal_calendars, django_assert_num_queries
    ):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        pool = create_calendar_pool(
            organization=organization,
            name="Nurses",
            calendars=[internal_calendars["phys_b"]],
        )
        self._make_group_with_pool(organization, internal_calendars, pool, "Group A")

        url = reverse("api:CalendarGroups-list")
        with CaptureQueriesContext(connection) as ctx_one_group:
            response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        query_count = len(ctx_one_group.captured_queries)

        self._make_group_with_pool(organization, internal_calendars, pool, "Group B")
        self._make_group_with_pool(organization, internal_calendars, pool, "Group C")

        with django_assert_num_queries(query_count):
            response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 3
