"""Tests for the internal REST API exposing CalendarPool endpoints."""

import json
import uuid
from io import StringIO

from django.core.management import call_command
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_ownership, create_calendar_pool
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotPool,
    CalendarPool,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups


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

    Pool create/update/delete is admin-only. Depend on this fixture -- in
    addition to `auth_client` -- in any test that expects a write to succeed.
    """
    membership = OrganizationMembership.objects.get(user=user, organization=organization)
    grant_membership_groups(membership, [GROUP_ORGANIZATION_ADMIN])
    return user


@pytest.fixture
def internal_calendars(organization):
    calendars = {}
    for name, external in (
        ("Nurse A", "nurse_a"),
        ("Nurse B", "nurse_b"),
        ("Room 1", "room_1"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.INTERNAL,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("nurse_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
            accepts_public_scheduling=True,
        )
    return calendars


@pytest.fixture
def owned_pool(user, organization, internal_calendars):
    """A pool where `user` owns at least one roster calendar so
    `CalendarPoolPermission` passes object-level checks for a non-admin."""
    create_calendar_ownership(calendar=internal_calendars["nurse_a"], user=user)
    return create_calendar_pool(
        organization=organization,
        name="Nurses",
        calendars=[internal_calendars["nurse_a"], internal_calendars["nurse_b"]],
    )


@pytest.mark.django_db
class TestCalendarPoolCrud:
    def test_list_requires_auth(self, anonymous_client):
        url = reverse("api:CalendarPools-list")
        response = anonymous_client.get(url)
        _assert_status(response, status.HTTP_401_UNAUTHORIZED)

    def test_list_scoped_to_organization(self, auth_client, organization, owned_pool):
        other_org = baker.make(Organization)
        create_calendar_pool(organization=other_org, name="Other")
        url = reverse("api:CalendarPools-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        ids = [p["id"] for p in response.data["results"]]
        assert ids == [owned_pool.id]

    def test_list_excludes_pool_member_is_not_part_of(
        self, auth_client, organization, owned_pool, internal_calendars
    ):
        """A same-org pool the caller owns no roster calendar in is simply
        absent from a non-admin member's list."""
        foreign_pool = create_calendar_pool(
            organization=organization,
            name="Rooms",
            calendars=[internal_calendars["room_1"]],
        )
        url = reverse("api:CalendarPools-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        ids = [p["id"] for p in response.data["results"]]
        assert ids == [owned_pool.id]
        assert foreign_pool.id not in ids

    def test_list_admin_sees_every_pool_in_organization(
        self, auth_client, organization, owned_pool, internal_calendars, admin_user
    ):
        foreign_pool = create_calendar_pool(
            organization=organization,
            name="Rooms",
            calendars=[internal_calendars["room_1"]],
        )
        url = reverse("api:CalendarPools-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        ids = {p["id"] for p in response.data["results"]}
        assert ids == {owned_pool.id, foreign_pool.id}

    def test_list_inactive_membership_sees_nothing(
        self, auth_client, organization, owned_pool, user
    ):
        """A missing or inactive membership resolves to no access -- fail
        closed, matching `CalendarGroupPermission`. With 0 active
        memberships and no `X-Organization-Id` header,
        `request.organization_membership` resolves to `None` (gated), which
        `CalendarPoolPermission.has_permission` refuses outright (403) before
        `get_queryset()` -- whose own `membership is None` branch is the same
        defense-in-depth fallback `CalendarGroupViewSet.get_queryset` keeps,
        exercised by a path this permission class already forecloses."""
        membership = OrganizationMembership.objects.get(user=user, organization=organization)
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        url = reverse("api:CalendarPools-list")
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_403_FORBIDDEN)

    def test_retrieve(self, auth_client, owned_pool, internal_calendars):
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_200_OK)
        assert response.data["name"] == "Nurses"
        assert sorted(c["id"] for c in response.data["calendars"]) == sorted(
            [internal_calendars["nurse_a"].id, internal_calendars["nurse_b"].id]
        )

    def test_retrieve_not_found_if_user_does_not_own_any_roster_calendar(
        self, auth_client, organization, internal_calendars
    ):
        pool = create_calendar_pool(
            organization=organization,
            name="Rooms",
            calendars=[internal_calendars["room_1"]],
        )
        url = reverse("api:CalendarPools-detail", kwargs={"pk": pool.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_404_NOT_FOUND)

    def test_create_pool_member_forbidden(
        self, auth_client, organization, internal_calendars, user
    ):
        create_calendar_ownership(calendar=internal_calendars["nurse_a"], user=user)
        url = reverse("api:CalendarPools-list")
        payload = {
            "name": "New Pool",
            "description": "",
            "calendar_ids": [internal_calendars["nurse_a"].id],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_403_FORBIDDEN)
        assert (
            not CalendarPool.objects.filter_by_organization(organization.id)
            .filter(name="New Pool")
            .exists()
        )

    def test_update_pool_member_forbidden(self, auth_client, owned_pool, internal_calendars):
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        payload = {
            "name": "Hijacked",
            "description": "",
            "calendar_ids": [internal_calendars["nurse_a"].id],
        }
        response = auth_client.put(url, payload, format="json")
        _assert_status(response, status.HTTP_403_FORBIDDEN)
        owned_pool.refresh_from_db()
        assert owned_pool.name == "Nurses"

    def test_destroy_pool_member_forbidden(self, auth_client, owned_pool):
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_403_FORBIDDEN)
        assert CalendarPool.original_manager.filter(id=owned_pool.id).exists()

    def test_create_pool(self, auth_client, organization, internal_calendars, user, admin_user):
        create_calendar_ownership(calendar=internal_calendars["nurse_a"], user=user)
        url = reverse("api:CalendarPools-list")
        payload = {
            "name": "New Pool",
            "description": "A roster",
            "calendar_ids": [internal_calendars["nurse_a"].id, internal_calendars["nurse_b"].id],
        }
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_201_CREATED)
        created = CalendarPool.objects.filter_by_organization(organization.id).get(name="New Pool")
        assert set(created.memberships.values_list("calendar_fk_id", flat=True)) == {
            internal_calendars["nurse_a"].id,
            internal_calendars["nurse_b"].id,
        }

    def test_create_pool_rejects_foreign_calendar(self, auth_client, organization, admin_user):
        other_org = baker.make(Organization)
        foreign_calendar = Calendar.objects.create(
            organization=other_org,
            name="Foreign",
            external_id="foreign",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )
        url = reverse("api:CalendarPools-list")
        payload = {"name": "Bad", "calendar_ids": [foreign_calendar.id]}
        response = auth_client.post(url, payload, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)

    def test_update_pool_replaces_roster_wholesale(
        self, auth_client, owned_pool, internal_calendars, admin_user
    ):
        """Update semantics: the roster becomes exactly `calendar_ids`, not a
        merge -- `nurse_b` (present before) is dropped, `room_1` (absent
        before) is added."""
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        payload = {
            "name": "Nurses Renamed",
            "description": "Updated",
            "calendar_ids": [internal_calendars["nurse_a"].id, internal_calendars["room_1"].id],
        }
        response = auth_client.put(url, payload, format="json")
        _assert_status(response, status.HTTP_200_OK)
        owned_pool.refresh_from_db()
        assert owned_pool.name == "Nurses Renamed"
        assert set(owned_pool.memberships.values_list("calendar_fk_id", flat=True)) == {
            internal_calendars["nurse_a"].id,
            internal_calendars["room_1"].id,
        }

    def test_destroy(self, auth_client, owned_pool, admin_user):
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_204_NO_CONTENT)
        assert not CalendarPool.original_manager.filter(id=owned_pool.id).exists()

    def test_destroy_refused_when_pool_attached_to_slot(
        self, auth_client, owned_pool, organization, admin_user
    ):
        group = CalendarGroup.objects.create(organization=organization, name="Clinic")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=group, name="Nurses"
        )
        CalendarGroupSlotPool.objects.create(organization=organization, slot=slot, pool=owned_pool)

        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_409_CONFLICT)
        assert "Clinic" in response.data["groups"]
        assert CalendarPool.original_manager.filter(id=owned_pool.id).exists()

    def test_destroy_refused_names_every_referencing_group(
        self, auth_client, owned_pool, organization, admin_user
    ):
        group_a = CalendarGroup.objects.create(organization=organization, name="Clinic A")
        slot_a = CalendarGroupSlot.objects.create(
            organization=organization, group=group_a, name="Nurses"
        )
        CalendarGroupSlotPool.objects.create(
            organization=organization, slot=slot_a, pool=owned_pool
        )
        group_b = CalendarGroup.objects.create(organization=organization, name="Clinic B")
        slot_b = CalendarGroupSlot.objects.create(
            organization=organization, group=group_b, name="Nurses"
        )
        CalendarGroupSlotPool.objects.create(
            organization=organization, slot=slot_b, pool=owned_pool
        )

        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.delete(url)
        _assert_status(response, status.HTTP_409_CONFLICT)
        assert set(response.data["groups"]) == {"Clinic A", "Clinic B"}


@pytest.mark.django_db
class TestCalendarPoolPatch:
    """`name`/`calendar_ids` have no "omitted means unchanged" sentinel, so a
    PATCH that omits either must be rejected rather than corrupt the pool
    (missing `name` would otherwise KeyError -> 500) or silently wipe the
    roster (missing `calendar_ids` would otherwise reach `update_pool` as
    `[]`, deleting every membership and cascading into every slot the pool
    is attached to)."""

    def test_patch_omitting_name_returns_400_not_500(
        self, auth_client, owned_pool, internal_calendars, admin_user
    ):
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.patch(url, {"description": "x"}, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)
        assert "name" in response.data
        owned_pool.refresh_from_db()
        assert owned_pool.name == "Nurses"

    def test_patch_omitting_calendar_ids_does_not_wipe_roster(
        self, auth_client, owned_pool, internal_calendars, admin_user, organization
    ):
        group = CalendarGroup.objects.create(organization=organization, name="Clinic")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=group, name="Nurses"
        )
        CalendarGroupSlotPool.objects.create(organization=organization, slot=slot, pool=owned_pool)
        for calendar in (internal_calendars["nurse_a"], internal_calendars["nurse_b"]):
            CalendarGroupSlotMembership.objects.create(
                organization=organization,
                slot=slot,
                calendar=calendar,
                source_pool=owned_pool,
            )

        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        response = auth_client.patch(url, {"name": "Renamed"}, format="json")
        _assert_status(response, status.HTTP_400_BAD_REQUEST)
        assert "calendar_ids" in response.data

        owned_pool.refresh_from_db()
        assert owned_pool.name == "Nurses"
        assert set(owned_pool.memberships.values_list("calendar_fk_id", flat=True)) == {
            internal_calendars["nurse_a"].id,
            internal_calendars["nurse_b"].id,
        }
        # The dangerous part: the cascade into the projected slot membership
        # rows must not have happened either.
        projected_calendar_ids = set(
            CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
            .projected()
            .filter(slot_fk=slot, source_pool_fk=owned_pool)
            .values_list("calendar_fk_id", flat=True)
        )
        assert projected_calendar_ids == {
            internal_calendars["nurse_a"].id,
            internal_calendars["nurse_b"].id,
        }

    def test_patch_supplying_every_key_behaves_like_put(
        self, auth_client, owned_pool, internal_calendars, admin_user
    ):
        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        payload = {
            "name": "Nurses Renamed",
            "description": "Updated",
            "calendar_ids": [internal_calendars["nurse_a"].id, internal_calendars["room_1"].id],
        }
        response = auth_client.patch(url, payload, format="json")
        _assert_status(response, status.HTTP_200_OK)
        owned_pool.refresh_from_db()
        assert owned_pool.name == "Nurses Renamed"
        assert set(owned_pool.memberships.values_list("calendar_fk_id", flat=True)) == {
            internal_calendars["nurse_a"].id,
            internal_calendars["room_1"].id,
        }


@pytest.mark.django_db
class TestCalendarPoolFilterSet:
    def test_filter_by_member_calendar(
        self, auth_client, organization, owned_pool, internal_calendars, admin_user
    ):
        other_pool = create_calendar_pool(
            organization=organization,
            name="Rooms",
            calendars=[internal_calendars["room_1"]],
        )
        url = reverse("api:CalendarPools-list")
        response = auth_client.get(url, {"calendar": internal_calendars["room_1"].id})
        _assert_status(response, status.HTTP_200_OK)
        ids = [p["id"] for p in response.data["results"]]
        assert ids == [other_pool.id]


@pytest.mark.django_db
class TestCalendarPoolUpdateProjectionConvergence:
    """Item B: `update_pool` must reconcile every slot the pool is attached
    to, without drifting the projection -- the regression test for the whole
    mechanism Phase 3 built.
    """

    def test_update_pool_roster_reprojects_attached_slots_with_no_drift(
        self, auth_client, owned_pool, organization, internal_calendars, admin_user
    ):
        group = CalendarGroup.objects.create(organization=organization, name="Clinic")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=group, name="Nurses"
        )
        CalendarGroupSlotPool.objects.create(organization=organization, slot=slot, pool=owned_pool)
        # Project the pool's current roster into the slot the way
        # CalendarGroupService._reconcile_slot_pools would on attach.
        for calendar in (internal_calendars["nurse_a"], internal_calendars["nurse_b"]):
            CalendarGroupSlotMembership.objects.create(
                organization=organization,
                slot=slot,
                calendar=calendar,
                source_pool=owned_pool,
            )

        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        payload = {
            "name": "Nurses",
            "description": "",
            # Drop nurse_b, add room_1 -- both a removal and an addition in
            # the same call, exercising the two-pass reconcile path.
            "calendar_ids": [internal_calendars["nurse_a"].id, internal_calendars["room_1"].id],
        }
        response = auth_client.put(url, payload, format="json")
        _assert_status(response, status.HTTP_200_OK)

        projected_calendar_ids = set(
            CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
            .projected()
            .filter(slot_fk=slot, source_pool_fk=owned_pool)
            .values_list("calendar_fk_id", flat=True)
        )
        assert projected_calendar_ids == {
            internal_calendars["nurse_a"].id,
            internal_calendars["room_1"].id,
        }

        out = StringIO()
        call_command(
            "reconcile_calendar_pool_projections",
            organization_id=organization.id,
            dry_run=True,
            stdout=out,
        )
        output = out.getvalue()
        assert "no drift found" in output
        assert "DRIFT DETECTED" not in output

    def test_update_pool_calendar_ids_only_addition_reprojects(
        self, auth_client, owned_pool, organization, internal_calendars, admin_user
    ):
        """Roster edit that only adds (no removal) still converges -- exercises
        the `bulk_create` + explicit `reconcile_pools` branch alone."""
        group = CalendarGroup.objects.create(organization=organization, name="Clinic")
        slot = CalendarGroupSlot.objects.create(
            organization=organization, group=group, name="Nurses"
        )
        CalendarGroupSlotPool.objects.create(organization=organization, slot=slot, pool=owned_pool)
        CalendarGroupSlotMembership.objects.create(
            organization=organization,
            slot=slot,
            calendar=internal_calendars["nurse_a"],
            source_pool=owned_pool,
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization,
            slot=slot,
            calendar=internal_calendars["nurse_b"],
            source_pool=owned_pool,
        )

        url = reverse("api:CalendarPools-detail", kwargs={"pk": owned_pool.id})
        payload = {
            "name": "Nurses",
            "description": "",
            "calendar_ids": [
                internal_calendars["nurse_a"].id,
                internal_calendars["nurse_b"].id,
                internal_calendars["room_1"].id,
            ],
        }
        response = auth_client.put(url, payload, format="json")
        _assert_status(response, status.HTTP_200_OK)

        assert (
            CalendarGroupSlotMembership.objects.filter_by_organization(organization.id)
            .projected()
            .filter(
                slot_fk=slot,
                source_pool_fk=owned_pool,
                calendar_fk_id=internal_calendars["room_1"].id,
            )
            .exists()
        )
        out = StringIO()
        call_command(
            "reconcile_calendar_pool_projections",
            organization_id=organization.id,
            dry_run=True,
            stdout=out,
        )
        assert "no drift found" in out.getvalue()


@pytest.mark.django_db
class TestCalendarPoolCrossOrganizationIsolation:
    def test_cannot_access_other_org_pool(self, auth_client, organization):
        other_org = baker.make(Organization)
        other_pool = create_calendar_pool(organization=other_org, name="Other")
        url = reverse("api:CalendarPools-detail", kwargs={"pk": other_pool.id})
        response = auth_client.get(url)
        _assert_status(response, status.HTTP_404_NOT_FOUND)
