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

    def _post_anon(self, query, variables):
        """POST with no Authorization header -- drives the unauthenticated
        path through the real middleware + strawberry permission_classes,
        unlike a resolver-level call which bypasses both."""
        return self.client.post(
            "/graphql/",
            data={"query": query, "variables": variables},
            format="json",
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
    # Security guard: anonymous + wrong-resource tokens must be refused
    # (pins the gate that let the unauthenticated cross-tenant
    # createCalendarGroup hole live undetected in this same file).
    # ------------------------------------------------------------------

    def test_calendar_pools_unauthenticated_refused(self):
        org = self._org()
        self._make_pool(org, name="Secret")

        response = self._post_anon(CALENDAR_POOLS_QUERY, {})
        assert response.status_code == 200
        data = response.json()
        assert data["data"] is None
        assert data["errors"][0]["message"] == "You must be authenticated to access this resource."

    def test_calendar_pools_wrong_resource_token_refused(self):
        org = self._org()
        self._make_pool(org, name="Secret")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])

        response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data["data"] is None
        assert data["errors"][0]["message"] == "You don't have access to query this resource."

    def test_calendar_pool_unauthenticated_refused(self):
        org = self._org()
        pool = self._make_pool(org, name="Secret")

        response = self._post_anon(CALENDAR_POOL_QUERY, {"poolId": pool.id})
        assert response.status_code == 200
        data = response.json()
        # `calendarPool` is a nullable field, so a resolver-level error nulls
        # only that field, not the whole `data` object (unlike the
        # non-null-list `calendarPools`).
        assert data["data"] == {"calendarPool": None}
        assert data["errors"][0]["message"] == "You must be authenticated to access this resource."

    def test_calendar_pool_wrong_resource_token_refused(self):
        org = self._org()
        pool = self._make_pool(org, name="Secret")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])

        response = self._post(CALENDAR_POOL_QUERY, system_user, token, auth, {"poolId": pool.id})
        assert response.status_code == 200
        data = response.json()
        assert data["data"] == {"calendarPool": None}
        assert data["errors"][0]["message"] == "You don't have access to query this resource."

    # ------------------------------------------------------------------
    # Nested `pools` resource gate (this phase's Scope item 4): a token
    # holding CALENDAR_GROUP but explicitly denied CALENDAR_POOL must not
    # read pool names/rosters through the nested `slots.pools` path --
    # OrganizationResourceAccess only runs on root fields, so this has to be
    # enforced inside the resolver itself (`_scoped_pool_list`).
    # ------------------------------------------------------------------

    def test_nested_pools_empty_for_token_without_calendar_pool_resource(self):
        org = self._org()
        group = CalendarGroup.objects.create(organization=org, name="Group")
        slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
        cal = self._make_calendar(org)
        pool = self._make_pool(org, name="SecretRoster", calendars=(cal,))
        CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=pool)

        # Org-wide token: unrestricted by owner-scope, holds CALENDAR_GROUP but
        # deliberately NOT CALENDAR_POOL.
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])

        response = self._post(
            NESTED_TRAVERSAL_QUERY, system_user, token, auth, {"groupId": group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        returned_group = data["data"]["calendarGroup"]
        assert returned_group is not None
        assert returned_group["slots"][0]["pools"] == []

    # ------------------------------------------------------------------
    # Query-count guards -- invariance, not magic numbers: the same query is
    # measured at two fixture sizes and the counts must be EQUAL, so an N+1
    # introduced later produces a different pair of numbers instead of a
    # single constant silently "fixed" by updating it.
    # ------------------------------------------------------------------

    def _pools_query_count(self, n, system_user, token, auth):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert len(data["data"]["calendarPools"]) == n
        return len(ctx)

    def test_calendar_pools_query_count_independent_of_result_size(self):
        org = self._org()
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_POOL])

        def make_pools(count):
            for i in range(count):
                cal = self._make_calendar(org)
                self._make_pool(org, name=f"Pool {uuid.uuid4().hex[:8]}-{i}", calendars=(cal,))

        # Warm up (token/org resolution, DB connection) so the measurements
        # below only measure the resolver's own queries.
        make_pools(1)
        self._post(CALENDAR_POOLS_QUERY, system_user, token, auth, {})

        small = self._pools_query_count(1, system_user, token, auth)

        make_pools(5)
        big = self._pools_query_count(6, system_user, token, auth)

        assert small == big, f"N+1: {small} queries for 1 pool vs {big} queries for 6 pools"

    def _nested_groups_query_count(self, n_groups, system_user, token, auth):
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
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert len(data["data"]["calendarGroups"]) == n_groups
        return len(ctx)

    def test_calendar_groups_nested_pools_no_n_plus_one(self):
        """Fetching several groups, each with a slot carrying a pool, must not
        scale query count with the number of groups/slots/pools -- the
        prefetch this phase adds (``slots__pools__calendars__...``) is what
        keeps ``CalendarGroupSlotGraphQLType.pools`` constant-query."""
        org = self._org()
        system_user, token, auth = self._org_wide_token(
            org, [PublicAPIResources.CALENDAR_GROUP, PublicAPIResources.CALENDAR_POOL]
        )

        def make_groups(count):
            for _i in range(count):
                unique = uuid.uuid4().hex[:8]
                group = CalendarGroup.objects.create(organization=org, name=f"Group {unique}")
                slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
                cal = self._make_calendar(org)
                pool = self._make_pool(org, name=f"Pool {unique}", calendars=(cal,))
                CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=pool)

        # Warm up.
        make_groups(1)
        self._post(
            """
            query CalendarGroups {
                calendarGroups { id slots { id pools { id calendars { id } } } }
            }
            """,
            system_user,
            token,
            auth,
            {},
        )

        small = self._nested_groups_query_count(1, system_user, token, auth)

        make_groups(2)
        big = self._nested_groups_query_count(3, system_user, token, auth)

        assert small == big, f"N+1: {small} queries for 1 group vs {big} queries for 3 groups"

    def _singular_group_query_count(self, group_id, n_slots, system_user, token, auth):
        query = """
        query CalendarGroup($groupId: Int!) {
            calendarGroup(groupId: $groupId) {
                id
                slots {
                    id
                    calendars { id }
                    pools {
                        id
                        calendars { id }
                    }
                }
            }
        }
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, {"groupId": group_id})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert len(data["data"]["calendarGroup"]["slots"]) == n_slots
        return len(ctx)

    def test_calendar_group_singular_nested_pools_no_n_plus_one(self):
        """The singular ``calendarGroup`` field must get the same
        ``slots__pools__calendars__...`` prefetch the plural ``calendarGroups``
        field does -- without it, one group with several slots each carrying a
        pool N+1s on the pools hop, unbounded by tenant configuration."""
        org = self._org()
        system_user, token, auth = self._org_wide_token(
            org, [PublicAPIResources.CALENDAR_GROUP, PublicAPIResources.CALENDAR_POOL]
        )
        group = CalendarGroup.objects.create(organization=org, name="Group")

        def make_slots(count):
            for _ in range(count):
                unique = uuid.uuid4().hex[:8]
                slot = CalendarGroupSlot.objects.create(
                    organization=org, group=group, name=f"Slot {unique}"
                )
                cal = self._make_calendar(org)
                pool = self._make_pool(org, name=f"Pool {unique}", calendars=(cal,))
                CalendarGroupSlotPool.objects.create(organization=org, slot=slot, pool=pool)

        # Warm up.
        make_slots(1)
        self._post(
            """
            query CalendarGroup($groupId: Int!) {
                calendarGroup(groupId: $groupId) { id slots { id pools { id calendars { id } } } }
            }
            """,
            system_user,
            token,
            auth,
            {"groupId": group.id},
        )

        small = self._singular_group_query_count(group.id, 1, system_user, token, auth)

        make_slots(3)
        big = self._singular_group_query_count(group.id, 4, system_user, token, auth)

        assert small == big, f"N+1: {small} queries for 1 slot vs {big} queries for 4 slots"


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
