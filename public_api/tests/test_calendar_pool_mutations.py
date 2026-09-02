"""Integration tests for the CalendarPool GraphQL mutation surface (Calendar
Pools plan, Phase 5).

Covers:
- createCalendarPool / updateCalendarPool / deleteCalendarPool: org-wide and
  scoped-admin tokens get full CRUD; a scoped-member token cannot write at
  all (fail closed, matching ``CalendarPoolPermission`` on REST).
- deleteCalendarPool refused while the pool is attached to a slot -- reported
  as data (``success=False`` + ``referencingGroups``), never as a GraphQL
  ``errors[]`` entry.
- Cross-organization rejection: a pool id from another org resolves to "not
  found", not a cross-tenant read/write or an existence leak.
"""

import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.factories import create_calendar_pool
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotPool,
    CalendarPool,
    CalendarPoolMembership,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import User


CREATE_CALENDAR_POOL_MUTATION = """
mutation CreateCalendarPool($input: CreateCalendarPoolInput!) {
    createCalendarPool(input: $input) {
        success
        errorMessage
        pool {
            id
            name
            description
            calendars { id }
        }
    }
}
"""

UPDATE_CALENDAR_POOL_MUTATION = """
mutation UpdateCalendarPool($input: UpdateCalendarPoolInput!) {
    updateCalendarPool(input: $input) {
        success
        errorMessage
        pool {
            id
            name
            description
            calendars { id }
        }
    }
}
"""

DELETE_CALENDAR_POOL_MUTATION = """
mutation DeleteCalendarPool($input: DeleteCalendarPoolInput!) {
    deleteCalendarPool(input: $input) {
        success
        errorMessage
        referencingGroups
    }
}
"""


@pytest.mark.django_db
class TestCalendarPoolMutations:
    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers (mirror public_api/tests/test_calendar_group_role_scoping.py)
    # ------------------------------------------------------------------

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _make_calendar(self, org: Organization) -> Calendar:
        unique = uuid.uuid4().hex[:8]
        return Calendar.objects.create(
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            provider="google",
            calendar_type="personal",
            manage_available_windows=True,
        )

    def _make_membership(
        self, org: Organization, *, groups: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER,)
    ) -> tuple[User, OrganizationMembership]:
        unique = uuid.uuid4().hex[:8]
        user = baker.make(User, email=f"user_{unique}@example.com")
        membership = make_membership(user=user, organization=org, groups=groups, is_active=True)
        return user, membership

    def _org_wide_token(self, org: Organization, resources: list[str]):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _scoped_token(
        self, org: Organization, membership: OrganizationMembership, resources: list[str]
    ):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"scoped_{uuid.uuid4().hex[:8]}",
            organization=org,
            scoped_to_membership=membership,
        )
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    # ------------------------------------------------------------------
    # createCalendarPool
    # ------------------------------------------------------------------

    def test_create_calendar_pool_org_wide_succeeds(self):
        org = self._org()
        cal = self._make_calendar(org)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            CREATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"name": "Nurses", "description": "Nursing staff", "calendarIds": [cal.id]}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["createCalendarPool"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert result["pool"]["name"] == "Nurses"
        assert result["pool"]["description"] == "Nursing staff"
        assert {int(c["id"]) for c in result["pool"]["calendars"]} == {cal.id}
        assert CalendarPool.objects.filter_by_organization(org.id).filter(name="Nurses").exists()

    def test_create_calendar_pool_scoped_admin_succeeds(self):
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(
            CREATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"name": "Rooms", "calendarIds": []}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["createCalendarPool"]
        assert result["success"] is True
        assert result["pool"]["name"] == "Rooms"

    def test_create_calendar_pool_scoped_member_forbidden(self):
        """A pool has no per-member write surface -- any non-admin scope is
        refused as data, not raised, mirroring CalendarPoolPermission on REST."""
        org = self._org()
        _member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(
            CREATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"name": "Nurses", "calendarIds": []}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["createCalendarPool"]
        assert result["success"] is False
        assert result["pool"] is None
        assert result["errorMessage"] == "You do not have permission to manage calendar pools."
        assert (
            not CalendarPool.objects.filter_by_organization(org.id).filter(name="Nurses").exists()
        )

    def test_create_calendar_pool_calendar_from_other_org_rejected(self):
        org = self._org()
        other_org = self._org()
        foreign_calendar = self._make_calendar(other_org)
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            CREATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"name": "Nurses", "calendarIds": [foreign_calendar.id]}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["createCalendarPool"]
        assert result["success"] is False
        assert result["pool"] is None
        assert str(foreign_calendar.id) in result["errorMessage"]

    # ------------------------------------------------------------------
    # updateCalendarPool
    # ------------------------------------------------------------------

    def test_update_calendar_pool_replaces_roster_wholesale(self):
        org = self._org()
        cal_a = self._make_calendar(org)
        cal_b = self._make_calendar(org)
        pool = create_calendar_pool(organization=org, name="Nurses", calendars=[cal_a])
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            UPDATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "poolId": pool.id,
                    "name": "Senior Nurses",
                    "calendarIds": [cal_b.id],
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["updateCalendarPool"]
        assert result["success"] is True
        assert result["pool"]["name"] == "Senior Nurses"
        calendar_ids = {int(c["id"]) for c in result["pool"]["calendars"]}
        assert calendar_ids == {cal_b.id}
        assert cal_a.id not in calendar_ids

    def test_update_calendar_pool_scoped_member_forbidden(self):
        org = self._org()
        _member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        pool = create_calendar_pool(organization=org, name="Nurses")
        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(
            UPDATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"poolId": pool.id, "name": "Hijacked", "calendarIds": []}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["updateCalendarPool"]
        assert result["success"] is False
        pool.refresh_from_db()
        assert pool.name == "Nurses"

    def test_update_calendar_pool_cross_organization_not_found(self):
        org = self._org()
        other_org = self._org()
        foreign_pool = create_calendar_pool(organization=other_org, name="OtherOrgPool")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            UPDATE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"poolId": foreign_pool.id, "name": "Hijacked", "calendarIds": []}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["updateCalendarPool"]
        assert result["success"] is False
        assert result["errorMessage"] == "Pool not found."
        foreign_pool.refresh_from_db()
        assert foreign_pool.name == "OtherOrgPool"

    # ------------------------------------------------------------------
    # deleteCalendarPool
    # ------------------------------------------------------------------

    def test_delete_calendar_pool_succeeds_when_unattached(self):
        org = self._org()
        pool = create_calendar_pool(organization=org, name="Nurses")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            DELETE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"poolId": pool.id}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["deleteCalendarPool"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert not CalendarPool.objects.filter_by_organization(org.id).filter(id=pool.id).exists()

    def test_delete_calendar_pool_refused_when_attached_surfaces_as_data(self):
        """Refusal is data (success=False + referencingGroups), never a GraphQL
        errors[] entry -- matches how this API surfaces every other domain
        failure (e.g. CalendarGroupMutations)."""
        org = self._org()
        cal = self._make_calendar(org)
        pool = create_calendar_pool(organization=org, name="Nurses", calendars=[cal])
        group = CalendarGroup.objects.create(organization=org, name="Appointments")
        slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
        CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=pool)

        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            DELETE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"poolId": pool.id}},
        )
        assert response.status_code == 200
        data = response.json()
        # Never surfaced as a GraphQL error -- this is the load-bearing assertion.
        assert data.get("errors", []) == []
        result = data["data"]["deleteCalendarPool"]
        assert result["success"] is False
        assert "Appointments" in result["errorMessage"]
        assert result["referencingGroups"] == ["Appointments"]
        assert CalendarPool.objects.filter_by_organization(org.id).filter(id=pool.id).exists()

    def test_delete_calendar_pool_scoped_member_forbidden(self):
        org = self._org()
        _member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        pool = create_calendar_pool(organization=org, name="Nurses")
        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(
            DELETE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"poolId": pool.id}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["deleteCalendarPool"]
        assert result["success"] is False
        assert CalendarPool.objects.filter_by_organization(org.id).filter(id=pool.id).exists()

    def test_delete_calendar_pool_cross_organization_not_found(self):
        org = self._org()
        other_org = self._org()
        foreign_pool = create_calendar_pool(organization=other_org, name="OtherOrgPool")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            DELETE_CALENDAR_POOL_MUTATION,
            system_user,
            token,
            auth,
            {"input": {"poolId": foreign_pool.id}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["deleteCalendarPool"]
        assert result["success"] is False
        assert result["errorMessage"] == "Pool not found."
        assert (
            CalendarPool.objects.filter_by_organization(other_org.id)
            .filter(id=foreign_pool.id)
            .exists()
        )


@pytest.mark.django_db
def test_calendar_pool_membership_query_sanity():
    """Smoke test that CalendarPoolMembership rows are queryable org-scoped --
    guards the fixtures other tests in this module build on."""
    org = baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")
    cal = Calendar.objects.create(
        organization=org,
        name="Room 1",
        external_id="room-1",
        provider="google",
        calendar_type="resource",
        manage_available_windows=True,
    )
    pool = create_calendar_pool(organization=org, name="Rooms", calendars=[cal])
    assert (
        CalendarPoolMembership.objects.filter_by_organization(org.id)
        .filter(pool_fk=pool, calendar_fk=cal)
        .exists()
    )
