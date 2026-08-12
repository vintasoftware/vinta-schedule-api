"""Tests for role-aware CalendarGroup scoping on the public GraphQL surface
(fix/calendar-group-membership-scoped-permissions).

Covers:
- ``calendarGroups`` / ``calendarGroup`` queries: org-wide and scoped-admin
  tokens see every group in the org; a scoped-member token sees only the
  groups it participates in (owns a calendar in one of the group's slots);
  a scoped token whose membership is missing/inactive sees none (fail
  closed) -- see ``public_api.scoping.system_user_scope``.
- ``batchUpsertGroupScopedAvailabilityWindows``: a scoped-admin token is
  elevated to write ANY calendar (not just its own), matching org-wide
  power; a scoped-member token whose membership went inactive is rejected
  wholesale (empty owner scope, fail closed) even for the calendar it used
  to own.
- Org-wide token behavior is unchanged throughout (regression pin).
"""

import datetime
import uuid

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
)
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from tenancy.models import Organization, OrganizationMembership, OrganizationRole
from users.models import User


CALENDAR_GROUPS_QUERY = """
query CalendarGroups {
    calendarGroups {
        id
        name
    }
}
"""

CALENDAR_GROUP_QUERY = """
query CalendarGroup($groupId: Int!) {
    calendarGroup(groupId: $groupId) {
        id
        name
    }
}
"""

BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION = """
mutation BatchUpsertGroupScopedAvailabilityWindows($input: BatchGroupScopedAvailabilityWindowsInput!) {
    batchUpsertGroupScopedAvailabilityWindows(input: $input) {
        success
        errorMessage
        windows {
            id
            calendarId
        }
    }
}
"""


@pytest.mark.django_db
class TestCalendarGroupRoleScoping:
    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers (mirror public_api/tests/test_group_scoped_availability_windows.py)
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

    def _make_group(
        self, org: Organization, *, name: str, calendars: tuple[Calendar, ...] = ()
    ) -> tuple[CalendarGroup, CalendarGroupSlot]:
        group = CalendarGroup.objects.create(organization=org, name=name)
        slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Slot")
        for calendar in calendars:
            CalendarGroupSlotMembership.objects.create(
                organization=org, slot=slot, calendar=calendar
            )
        return group, slot

    def _make_membership(
        self, org: Organization, *, role: str = OrganizationRole.MEMBER
    ) -> tuple[User, OrganizationMembership]:
        unique = uuid.uuid4().hex[:8]
        user = baker.make(User, email=f"user_{unique}@example.com")
        membership = baker.make(
            OrganizationMembership, user=user, organization=org, role=role, is_active=True
        )
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
    # calendarGroups -- list
    # ------------------------------------------------------------------

    def test_calendar_groups_org_wide_sees_all_groups(self):
        org = self._org()
        cal = self._make_calendar(org)
        group_a, _slot_a = self._make_group(org, name="A", calendars=(cal,))
        group_b, _slot_b = self._make_group(org, name="B")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.CALENDAR_GROUP])

        response = self._post(CALENDAR_GROUPS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(g["id"]) for g in data["data"]["calendarGroups"]}
        assert ids == {group_a.id, group_b.id}

    def test_calendar_groups_scoped_admin_sees_all_groups(self):
        """NEW elevation: a scoped-admin token sees every group in the org,
        including ones it does not personally participate in."""
        org = self._org()
        _admin_user, admin_membership = self._make_membership(org, role=OrganizationRole.ADMIN)
        cal = self._make_calendar(org)
        group_a, _slot_a = self._make_group(org, name="A", calendars=(cal,))
        group_b, _slot_b = self._make_group(org, name="B")
        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.CALENDAR_GROUP]
        )

        response = self._post(CALENDAR_GROUPS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(g["id"]) for g in data["data"]["calendarGroups"]}
        assert ids == {group_a.id, group_b.id}

    def test_calendar_groups_scoped_member_sees_only_participant_groups(self):
        org = self._org()
        member_user, membership = self._make_membership(org, role=OrganizationRole.MEMBER)
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        other_calendar = self._make_calendar(org)

        participant_group, _slot = self._make_group(org, name="Mine", calendars=(own_calendar,))
        foreign_group, _fslot = self._make_group(org, name="NotMine", calendars=(other_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_GROUP]
        )

        response = self._post(CALENDAR_GROUPS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(g["id"]) for g in data["data"]["calendarGroups"]}
        assert ids == {participant_group.id}
        assert foreign_group.id not in ids

    def test_calendar_groups_scoped_member_inactive_membership_sees_none(self):
        """Fail closed: a scoped token whose membership is deactivated after
        minting must not fall back to unrestricted (or even its own) access."""
        org = self._org()
        member_user, membership = self._make_membership(org, role=OrganizationRole.MEMBER)
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        self._make_group(org, name="Mine", calendars=(own_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_GROUP]
        )
        # Deactivate the membership AFTER minting the token.
        membership.is_active = False
        membership.save(update_fields=["is_active"])

        response = self._post(CALENDAR_GROUPS_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarGroups"] == []

    # ------------------------------------------------------------------
    # calendarGroup -- single
    # ------------------------------------------------------------------

    def test_calendar_group_scoped_member_non_participant_returns_none(self):
        org = self._org()
        _member_user, membership = self._make_membership(org, role=OrganizationRole.MEMBER)
        other_calendar = self._make_calendar(org)
        foreign_group, _slot = self._make_group(org, name="NotMine", calendars=(other_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.CALENDAR_GROUP]
        )

        response = self._post(
            CALENDAR_GROUP_QUERY, system_user, token, auth, {"groupId": foreign_group.id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarGroup"] is None

    def test_calendar_group_scoped_admin_returns_any_group(self):
        org = self._org()
        _admin_user, admin_membership = self._make_membership(org, role=OrganizationRole.ADMIN)
        other_calendar = self._make_calendar(org)
        group, _slot = self._make_group(org, name="NotMine", calendars=(other_calendar,))

        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.CALENDAR_GROUP]
        )

        response = self._post(CALENDAR_GROUP_QUERY, system_user, token, auth, {"groupId": group.id})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarGroup"]["id"] == str(group.id)

    # ------------------------------------------------------------------
    # batchUpsertGroupScopedAvailabilityWindows -- scoped-admin elevation
    # ------------------------------------------------------------------

    def test_batch_upsert_scoped_admin_can_write_any_calendar(self):
        """NEW elevation: a scoped-admin token may write group-scoped windows
        for a calendar it does not personally own -- matches org-wide power."""
        org = self._org()
        _admin_user, admin_membership = self._make_membership(org, role=OrganizationRole.ADMIN)
        other_user, _other_membership = self._make_membership(org, role=OrganizationRole.MEMBER)
        target_calendar = self._make_calendar(org)
        self._own(org, other_user, target_calendar)
        _group, slot = self._make_group(org, name="Group", calendars=(target_calendar,))

        system_user, token, auth = self._scoped_token(
            org,
            admin_membership,
            [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS],
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": target_calendar.id,
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert len(result["windows"]) == 1
        assert result["windows"][0]["calendarId"] == target_calendar.id

    def test_batch_upsert_scoped_member_inactive_membership_rejected_wholesale(self):
        """Fail closed: a scoped-member token whose membership went inactive
        after minting cannot write even to the calendar it used to own."""
        org = self._org()
        member_user, membership = self._make_membership(org, role=OrganizationRole.MEMBER)
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        _group, slot = self._make_group(org, name="Group", calendars=(own_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": own_calendar.id,
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] == "Calendar not found."
        assert (
            AvailableTime.objects.for_group_slot(slot.id)
            .filter_by_organization(org.id)
            .filter(calendar_fk_id=own_calendar.id)
            .count()
            == 0
        )

    # ------------------------------------------------------------------
    # Org-wide token unchanged (regression pin)
    # ------------------------------------------------------------------

    def test_batch_upsert_org_wide_unchanged(self):
        org = self._org()
        cal = self._make_calendar(org)
        _group, slot = self._make_group(org, name="Group", calendars=(cal,))
        system_user, token, auth = self._org_wide_token(
            org, [PublicAPIResources.BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS]
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "organizationId": org.id,
                    "groupSlotId": slot.id,
                    "operations": [
                        {
                            "action": "create",
                            "calendarId": cal.id,
                            "startTime": start.isoformat(),
                            "endTime": end.isoformat(),
                            "timezone": "UTC",
                        }
                    ],
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        result = data["data"]["batchUpsertGroupScopedAvailabilityWindows"]
        assert result["success"] is True
        assert len(result["windows"]) == 1
