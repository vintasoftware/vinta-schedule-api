"""Tests for the public GraphQL CalendarPool read surface (Calendar Pools plan,
Phase 5).

Covers:
- ``calendarPools`` / ``calendarPool`` queries: org-wide and scoped-admin
  tokens see every pool in the org; a scoped-member token sees only the
  pools it participates in (owns a roster calendar); a scoped token whose
  membership is missing/inactive sees none (fail closed) -- see
  ``public_api.scoping.scoped_calendar_pool_queryset``.
- The second-hop leak this phase's Scope item 1 calls out: a scoped-member
  token cannot read a non-member pool's roster through nested traversal
  (``calendarGroup -> slots -> pools -> calendars``), even when it CAN see
  the slot for an unrelated reason.
- Query-count guards so the new resolvers do not regress into N+1.
- Every new GraphQL field name is present in
  ``OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING``.
"""

import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_pool
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotPool,
    CalendarOwnership,
    CalendarPool,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.permissions import OrganizationResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import User


CALENDAR_POOLS_QUERY = """
query CalendarPools {
    calendarPools {
        id
        name
        calendars {
            id
        }
    }
}
"""

CALENDAR_POOL_QUERY = """
query CalendarPool($poolId: Int!) {
    calendarPool(poolId: $poolId) {
        id
        name
        calendars {
            id
        }
    }
}
"""

NESTED_TRAVERSAL_QUERY = """
query CalendarGroup($groupId: Int!) {
    calendarGroup(groupId: $groupId) {
        id
        name
        slots {
            id
            pools {
                id
                name
                calendars {
                    id
                }
            }
        }
    }
}
"""


@pytest.mark.django_db
class TestCalendarPoolQueries:
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
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

    def _make_pool(
        self, org: Organization, *, name: str, calendars: tuple[Calendar, ...] = ()
    ) -> CalendarPool:
        return create_calendar_pool(organization=org, name=name, calendars=list(calendars))

    def _make_membership(
        self, org: Organization, *, groups: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER,)
    ) -> tuple[User, OrganizationMembership]:
        unique = uuid.uuid4().hex[:8]
        user = baker.make(User, email=f"user_{unique}@example.com")
        membership = make_membership(user=user, organization=org, groups=groups, is_active=True)
        return user, membership

    def _own(self, org: Organization, user, calendar: Calendar) -> None:
        CalendarOwnership.objects.create(
            organization=org, calendar=calendar, membership_user_id=user.id
        )

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
    # calendarPools -- list
    # ------------------------------------------------------------------

    def test_calendar_pools_org_wide_sees_all_pools(self):
        org = self._org()
        cal = self._make_calendar(org)
        pool_a = self._make_pool(org, name="A", calendars=(cal,))
        pool_b = self._make_pool(org, name="B")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(p["id"]) for p in data["data"]["calendarPools"]}
        assert ids == {pool_a.id, pool_b.id}

    def test_calendar_pools_scoped_admin_sees_all_pools(self):
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        cal = self._make_calendar(org)
        pool_a = self._make_pool(org, name="A", calendars=(cal,))
        pool_b = self._make_pool(org, name="B")
        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(p["id"]) for p in data["data"]["calendarPools"]}
        assert ids == {pool_a.id, pool_b.id}

    def test_calendar_pools_scoped_member_sees_only_participant_pools(self):
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        other_calendar = self._make_calendar(org)

        participant_pool = self._make_pool(org, name="Mine", calendars=(own_calendar,))
        foreign_pool = self._make_pool(org, name="NotMine", calendars=(other_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(p["id"]) for p in data["data"]["calendarPools"]}
        assert ids == {participant_pool.id}
        assert foreign_pool.id not in ids

    def test_calendar_pools_scoped_member_inactive_membership_sees_none(self):
        """Fail closed: a scoped token whose membership is deactivated after
        minting must not fall back to unrestricted (or even its own) access."""
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        self._make_pool(org, name="Mine", calendars=(own_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )
        # Deactivate the membership AFTER minting the token.
        membership.is_active = False
        membership.save(update_fields=["is_active"])

        response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarPools"] == []

    # ------------------------------------------------------------------
    # calendarPool -- single
    # ------------------------------------------------------------------

    def test_calendar_pool_scoped_member_non_participant_returns_none(self):
        org = self._org()
        _member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        other_calendar = self._make_calendar(org)
        foreign_pool = self._make_pool(org, name="NotMine", calendars=(other_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(
            CALENDAR_POOL_QUERY, system_user, token, auth, {"poolId": foreign_pool.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarPool"] is None

    def test_calendar_pool_scoped_admin_returns_any_pool(self):
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        other_calendar = self._make_calendar(org)
        pool = self._make_pool(org, name="NotMine", calendars=(other_calendar,))

        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(CALENDAR_POOL_QUERY, system_user, token, auth, {"poolId": pool.id})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarPool"]["id"] == str(pool.id)

    def test_calendar_pool_cross_organization_returns_none(self):
        org = self._org()
        other_org = self._org()
        other_calendar = self._make_calendar(other_org)
        foreign_pool = self._make_pool(other_org, name="OtherOrg", calendars=(other_calendar,))
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        response = self._post(
            CALENDAR_POOL_QUERY, system_user, token, auth, {"poolId": foreign_pool.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarPool"] is None

    # ------------------------------------------------------------------
    # calendarPool.calendars -- roster owner-scoping (this phase's Scope item 1)
    # ------------------------------------------------------------------

    def test_calendar_pool_roster_scoped_to_owner(self):
        """A scoped-member token reading a pool it participates in only sees the
        roster calendars it owns -- the same owner-scoping
        ``CalendarGroupSlotGraphQLType.calendars`` already applies."""
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        other_calendar = self._make_calendar(org)
        pool = self._make_pool(org, name="Mixed", calendars=(own_calendar, other_calendar))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_POOL]
        )

        response = self._post(CALENDAR_POOL_QUERY, system_user, token, auth, {"poolId": pool.id})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        returned = data["data"]["calendarPool"]
        assert returned["id"] == str(pool.id)
        calendar_ids = {int(c["id"]) for c in returned["calendars"]}
        assert calendar_ids == {own_calendar.id}
        assert other_calendar.id not in calendar_ids

    # ------------------------------------------------------------------
    # Nested-traversal leak: calendarGroup -> slots -> pools -> calendars
    # ------------------------------------------------------------------

    def test_scoped_member_cannot_read_non_member_pool_roster_via_nested_traversal(self):
        """The attack: a scoped-member token that legitimately participates in a
        group (via an INLINE calendar on one of its slots) tries to read the
        roster of a DIFFERENT, foreign pool attached to that same slot by
        walking ``calendarGroup -> slots -> pools -> calendars`` -- a path this
        token was never granted direct access to via ``calendarPool(s)``.

        Must not leak: neither the foreign pool's name (Open Question 4 --
        fail closed) nor, as defence in depth, its roster contents.
        """
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)

        # A group the scoped member legitimately participates in, via an
        # inline calendar on the slot (not via any pool).
        group = CalendarGroup.objects.create(organization=org, name="Group")
        slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
        CalendarGroupSlotMembership.objects.create(
            organization=org, slot=slot, calendar=own_calendar
        )

        # A foreign pool, attached to the SAME slot, whose roster the member
        # owns nothing in -- the attacker's target.
        foreign_calendar = self._make_calendar(org)
        foreign_pool = self._make_pool(org, name="SecretRoster", calendars=(foreign_calendar,))
        CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=foreign_pool)

        system_user, token, auth = self._scoped_token(
            org,
            membership,
            [PublicAPIResources.CALENDAR_GROUP, PublicAPIResources.CALENDAR_POOL],
        )

        response = self._post(
            NESTED_TRAVERSAL_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []

        returned_group = data["data"]["calendarGroup"]
        assert returned_group is not None, "scoped member should see the group it participates in"
        returned_slots = returned_group["slots"]
        assert len(returned_slots) == 1

        returned_pools = returned_slots[0]["pools"]
        # Primary defence (Open Question 4, fail closed): the foreign pool's
        # NAME must not appear at all -- the member owns nothing in it.
        pool_ids = {int(p["id"]) for p in returned_pools}
        assert foreign_pool.id not in pool_ids
        assert returned_pools == []

        # Defence in depth: even if a future change relaxed pool-name
        # visibility, the roster itself must never leak the foreign calendar.
        for returned_pool in returned_pools:
            calendar_ids = {int(c["id"]) for c in returned_pool["calendars"]}
            assert foreign_calendar.id not in calendar_ids

    def test_scoped_admin_sees_pool_via_nested_traversal(self):
        """Regression pin: scoped-admin (unrestricted) still sees pools nested
        under a slot, so the fail-closed filter above is scoped-member-only."""
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        group = CalendarGroup.objects.create(organization=org, name="Group")
        slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
        calendar = self._make_calendar(org)
        pool = self._make_pool(org, name="Pool", calendars=(calendar,))
        CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=pool)

        system_user, token, auth = self._scoped_token(
            org,
            admin_membership,
            [PublicAPIResources.CALENDAR_GROUP, PublicAPIResources.CALENDAR_POOL],
        )

        response = self._post(
            NESTED_TRAVERSAL_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        returned_pools = data["data"]["calendarGroup"]["slots"][0]["pools"]
        assert {int(p["id"]) for p in returned_pools} == {pool.id}
        assert {int(c["id"]) for c in returned_pools[0]["calendars"]} == {calendar.id}

    # ------------------------------------------------------------------
    # Query-count guards
    # ------------------------------------------------------------------

    def test_calendar_pools_query_count_independent_of_result_size(self, django_assert_num_queries):
        org = self._org()
        for i in range(5):
            cal = self._make_calendar(org)
            self._make_pool(org, name=f"Pool {i}", calendars=(cal,))
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        # Warm up (token/org resolution, DB connection) so the assertion below
        # only measures the query the resolver itself issues.
        self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})

        # 8 queries: system_user + org + subscription + entitlement + resource-access
        # (5, shared auth/entitlement overhead every authenticated query pays) + pools
        # + pools__calendars + pools__calendars__ownerships (3, the actual resolver
        # work) -- independent of the number of pools/calendars, which is the property
        # this guards.
        with django_assert_num_queries(8):
            response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        assert response.json().get("errors", []) == []

    def test_calendar_groups_nested_pools_no_n_plus_one(self, django_assert_num_queries):
        """Fetching several groups, each with a slot carrying a pool, must not
        scale query count with the number of groups/slots/pools -- the
        prefetch this phase adds (``slots__pools__calendars__...``) is what
        keeps ``CalendarGroupSlotGraphQLType.pools`` constant-query."""
        org = self._org()
        query = """
        query CalendarGroups {
            calendarGroups {
                id
                slots {
                    id
                    pools {
                        id
                        calendars { id }
                    }
                }
            }
        }
        """
        for i in range(3):
            group = CalendarGroup.objects.create(organization=org, name=f"Group {i}")
            slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
            cal = self._make_calendar(org)
            pool = self._make_pool(org, name=f"Pool {i}", calendars=(cal,))
            CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=pool)

        system_user, token, auth = self._org_wide_token(
            org, [PublicAPIResources.CALENDAR_GROUP, PublicAPIResources.CALENDAR_POOL]
        )

        # Warm up.
        self._post(query, system_user, token, auth, {})

        # 11 queries: system_user + org + subscription + entitlement + resource-access
        # (5, shared auth/entitlement overhead every authenticated query pays) + groups
        # + slots + slots__calendars (union roster) + slots__pools + pools__calendars
        # (via CalendarGroupSlotPool) + pools__calendars__ownerships (6, the actual
        # resolver work) -- independent of the number of groups/slots/pools, which is
        # the property this guards.
        with django_assert_num_queries(11):
            response = self._post(query, system_user, token, auth, {})
        assert response.status_code == 200
        assert response.json().get("errors", []) == []


class TestFieldToResourceMappingCompleteness:
    """Every new field name this phase introduces must be present in
    ``FIELD_TO_RESOURCE_MAPPING`` -- a field absent from that mapping is a
    scope-check gap (see the plan's API Design 4.4)."""

    def test_calendar_pool_fields_are_mapped(self):
        new_fields = {
            "calendarPool",
            "calendarPools",
            "createCalendarPool",
            "updateCalendarPool",
            "deleteCalendarPool",
        }
        mapped = set(OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING)
        missing = new_fields - mapped
        assert not missing, f"Unmapped CalendarPool GraphQL fields (scope-check gap): {missing}"
        for field in new_fields:
            assert (
                OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING[field]
                == PublicAPIResources.CALENDAR_POOL
            )
