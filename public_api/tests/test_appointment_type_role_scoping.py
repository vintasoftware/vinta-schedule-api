"""Tests for role-aware AppointmentType scoping on the public GraphQL surface
(fix/appointment-type-membership-scoped-permissions).

Covers:
- ``appointmentTypes`` / ``appointmentType`` queries: org-wide and scoped-admin
  tokens see every appointment type in the org; a scoped-member token sees only the
  appointment types it participates in (owns a calendar in one of the appointment type's slots);
  a scoped token whose membership is missing/inactive sees none (fail
  closed) -- see ``public_api.scoping.system_user_scope``.
- ``batchUpsertAppointmentTypeScopedAvailabilityWindows``: a scoped-admin token is
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
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
    CalendarOwnership,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from users.models import User


APPOINTMENT_TYPES_QUERY = """
query AppointmentTypes {
    appointmentTypes {
        id
        name
    }
}
"""

APPOINTMENT_TYPE_QUERY = """
query AppointmentType($appointmentTypeId: Int!) {
    appointmentType(appointmentTypeId: $appointmentTypeId) {
        id
        name
    }
}
"""

BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS_MUTATION = """
mutation BatchUpsertAppointmentTypeScopedAvailabilityWindows($input: BatchAppointmentTypeScopedAvailabilityWindowsInput!) {
    batchUpsertAppointmentTypeScopedAvailabilityWindows(input: $input) {
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
class TestAppointmentTypeRoleScoping:
    def setup_method(self):
        self.client = APIClient()

    # ------------------------------------------------------------------
    # Helpers (mirror public_api/tests/test_appointment_type_scoped_availability_windows.py)
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

    def _make_appointment_type(
        self, org: Organization, *, name: str, calendars: tuple[Calendar, ...] = ()
    ) -> tuple[AppointmentType, AppointmentTypeSlot]:
        appointment_type = AppointmentType.objects.create(organization=org, name=name)
        slot = AppointmentTypeSlot.objects.create(
            organization=org, appointment_type=appointment_type, name="Slot"
        )
        for calendar in calendars:
            AppointmentTypeSlotMembership.objects.create(
                organization=org, slot=slot, calendar=calendar
            )
        return appointment_type, slot

    def _make_membership(
        self,
        org: Organization,
        *,
        groups: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER,),
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
    # appointmentTypes -- list
    # ------------------------------------------------------------------

    def test_appointment_types_org_wide_sees_all_appointment_types(self):
        org = self._org()
        cal = self._make_calendar(org)
        appointment_type_a, _slot_a = self._make_appointment_type(org, name="A", calendars=(cal,))
        appointment_type_b, _slot_b = self._make_appointment_type(org, name="B")
        system_user, token, auth = self._org_wide_token(org, [PublicAPIResources.APPOINTMENT_TYPE])

        response = self._post(APPOINTMENT_TYPES_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(g["id"]) for g in data["data"]["appointmentTypes"]}
        assert ids == {appointment_type_a.id, appointment_type_b.id}

    def test_appointment_types_scoped_admin_sees_all_appointment_types(self):
        """NEW elevation: a scoped-admin token sees every appointment type in the org,
        including ones it does not personally participate in."""
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        cal = self._make_calendar(org)
        appointment_type_a, _slot_a = self._make_appointment_type(org, name="A", calendars=(cal,))
        appointment_type_b, _slot_b = self._make_appointment_type(org, name="B")
        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.APPOINTMENT_TYPE]
        )

        response = self._post(APPOINTMENT_TYPES_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(g["id"]) for g in data["data"]["appointmentTypes"]}
        assert ids == {appointment_type_a.id, appointment_type_b.id}

    def test_appointment_types_scoped_member_sees_only_participant_appointment_types(self):
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        other_calendar = self._make_calendar(org)

        participant_appointment_type, _slot = self._make_appointment_type(
            org, name="Mine", calendars=(own_calendar,)
        )
        foreign_appointment_type, _fslot = self._make_appointment_type(
            org, name="NotMine", calendars=(other_calendar,)
        )

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.APPOINTMENT_TYPE]
        )

        response = self._post(APPOINTMENT_TYPES_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        ids = {int(g["id"]) for g in data["data"]["appointmentTypes"]}
        assert ids == {participant_appointment_type.id}
        assert foreign_appointment_type.id not in ids

    def test_appointment_types_scoped_member_inactive_membership_sees_none(self):
        """Fail closed: a scoped token whose membership is deactivated after
        minting must not fall back to unrestricted (or even its own) access."""
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        self._make_appointment_type(org, name="Mine", calendars=(own_calendar,))

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.APPOINTMENT_TYPE]
        )
        # Deactivate the membership AFTER minting the token.
        membership.is_active = False
        membership.save(update_fields=["is_active"])

        response = self._post(APPOINTMENT_TYPES_QUERY, system_user, token, auth, {})
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["appointmentTypes"] == []

    # ------------------------------------------------------------------
    # appointmentType -- single
    # ------------------------------------------------------------------

    def test_appointment_type_scoped_member_non_participant_returns_none(self):
        org = self._org()
        _member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        other_calendar = self._make_calendar(org)
        foreign_appointment_type, _slot = self._make_appointment_type(
            org, name="NotMine", calendars=(other_calendar,)
        )

        system_user, token, auth = self._scoped_token(
            org, membership, [PublicAPIResources.APPOINTMENT_TYPE]
        )

        response = self._post(
            APPOINTMENT_TYPE_QUERY,
            system_user,
            token,
            auth,
            {"appointmentTypeId": foreign_appointment_type.id},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["appointmentType"] is None

    def test_appointment_type_scoped_admin_returns_any_appointment_type(self):
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        other_calendar = self._make_calendar(org)
        appointment_type, _slot = self._make_appointment_type(
            org, name="NotMine", calendars=(other_calendar,)
        )

        system_user, token, auth = self._scoped_token(
            org, admin_membership, [PublicAPIResources.APPOINTMENT_TYPE]
        )

        response = self._post(
            APPOINTMENT_TYPE_QUERY,
            system_user,
            token,
            auth,
            {"appointmentTypeId": appointment_type.id},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["appointmentType"]["id"] == str(appointment_type.id)

    # ------------------------------------------------------------------
    # batchUpsertAppointmentTypeScopedAvailabilityWindows -- scoped-admin elevation
    # ------------------------------------------------------------------

    def test_batch_upsert_scoped_admin_can_write_any_calendar(self):
        """NEW elevation: a scoped-admin token may write appointment-type-scoped windows
        for a calendar it does not personally own -- matches org-wide power."""
        org = self._org()
        _admin_user, admin_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_ADMIN,)
        )
        other_user, _other_membership = self._make_membership(
            org, groups=(GROUP_ORGANIZATION_MEMBER,)
        )
        target_calendar = self._make_calendar(org)
        self._own(org, other_user, target_calendar)
        _appointment_type, slot = self._make_appointment_type(
            org, name="AppointmentType", calendars=(target_calendar,)
        )

        system_user, token, auth = self._scoped_token(
            org,
            admin_membership,
            [PublicAPIResources.BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS],
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "organizationId": org.id,
                    "appointmentTypeSlotId": slot.id,
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
        result = data["data"]["batchUpsertAppointmentTypeScopedAvailabilityWindows"]
        assert result["success"] is True
        assert result["errorMessage"] is None
        assert len(result["windows"]) == 1
        assert result["windows"][0]["calendarId"] == target_calendar.id

    def test_batch_upsert_scoped_member_inactive_membership_rejected_wholesale(self):
        """Fail closed: a scoped-member token whose membership went inactive
        after minting cannot write even to the calendar it used to own."""
        org = self._org()
        member_user, membership = self._make_membership(org, groups=(GROUP_ORGANIZATION_MEMBER,))
        own_calendar = self._make_calendar(org)
        self._own(org, member_user, own_calendar)
        _appointment_type, slot = self._make_appointment_type(
            org, name="AppointmentType", calendars=(own_calendar,)
        )

        system_user, token, auth = self._scoped_token(
            org,
            membership,
            [PublicAPIResources.BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS],
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "organizationId": org.id,
                    "appointmentTypeSlotId": slot.id,
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
        result = data["data"]["batchUpsertAppointmentTypeScopedAvailabilityWindows"]
        assert result["success"] is False
        assert result["errorMessage"] == "Calendar not found."
        assert (
            AvailableTime.objects.for_appointment_type_slot(slot.id)
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
        _appointment_type, slot = self._make_appointment_type(
            org, name="AppointmentType", calendars=(cal,)
        )
        system_user, token, auth = self._org_wide_token(
            org, [PublicAPIResources.BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS]
        )

        start = datetime.datetime(2026, 9, 1, 9, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 9, 1, 17, 0, 0, tzinfo=datetime.UTC)
        response = self._post(
            BATCH_UPSERT_APPOINTMENT_TYPE_SCOPED_AVAILABILITY_WINDOWS_MUTATION,
            system_user,
            token,
            auth,
            {
                "input": {
                    "organizationId": org.id,
                    "appointmentTypeSlotId": slot.id,
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
        result = data["data"]["batchUpsertAppointmentTypeScopedAvailabilityWindows"]
        assert result["success"] is True
        assert len(result["windows"]) == 1
