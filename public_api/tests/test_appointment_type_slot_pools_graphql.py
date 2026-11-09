"""Tests for ``poolIds`` on ``AppointmentTypeSlotInput`` in the public GraphQL API.

``createAppointmentType`` / ``updateAppointmentType`` can attach ``CalendarPool``s
to a slot. Omitted or ``null`` leaves the slot's pools unchanged, ``[]`` detaches
them all, and a list replaces them. Sending ``poolIds`` needs the access a pool
write needs: the ``CALENDAR_POOL`` resource and an org-wide or scoped-admin token.

Every test goes through ``/graphql/`` so the middleware and permission classes run.
"""

import uuid

import pytest
from model_bakery import baker

from calendar_integration.factories import create_calendar_pool
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotPool,
    Calendar,
    CalendarPool,
)
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.constants import PublicAPIResources
from public_api.tests.helpers import GraphQLPost, make_token_post
from users.models import User


pytestmark = pytest.mark.django_db


SLOT_SELECTION = """
        success
        errorMessage
        appointmentType {
            id
            slots {
                name
                calendars { id }
                pools { id }
            }
        }
"""

CREATE_APPOINTMENT_TYPE_MUTATION = f"""
mutation CreateAppointmentType($input: AppointmentTypeInput!) {{
    createAppointmentType(input: $input) {{ {SLOT_SELECTION} }}
}}
"""

UPDATE_APPOINTMENT_TYPE_MUTATION = f"""
mutation UpdateAppointmentType($input: UpdateAppointmentTypeInput!) {{
    updateAppointmentType(input: $input) {{ {SLOT_SELECTION} }}
}}
"""

BOTH_RESOURCES = [PublicAPIResources.APPOINTMENT_TYPE, PublicAPIResources.CALENDAR_POOL]
FORBIDDEN = "You do not have permission to manage calendar pools."
# A scoped member is refused before poolIds is looked at: it may not write
# appointment types at all.
APPOINTMENT_TYPE_FORBIDDEN = "You do not have permission to manage appointment types."


def _make_calendar(org: Organization) -> Calendar:
    unique = uuid.uuid4().hex[:8]
    return Calendar.objects.create(
        organization=org,
        name=f"Calendar {unique}",
        external_id=f"cal-{unique}",
        provider="google",
        calendar_type="personal",
        manage_available_windows=True,
    )


def _make_membership(org: Organization, group: str) -> OrganizationMembership:
    user = baker.make(User, email=f"user_{uuid.uuid4().hex[:8]}@example.com")
    return make_membership(user=user, organization=org, groups=(group,), is_active=True)


def _slot(name: str, calendar_ids: list[int], **extra: object) -> dict:
    return {"name": name, "calendarIds": calendar_ids, **extra}


def _create(post: GraphQLPost, org: Organization, slots: list[dict]) -> dict:
    body = post(
        CREATE_APPOINTMENT_TYPE_MUTATION,
        {"input": {"organizationId": org.id, "name": "Consult", "slots": slots}},
    )
    return body["data"]["createAppointmentType"]


def _update(
    post: GraphQLPost, org: Organization, appointment_type_id: int, slots: list[dict]
) -> dict:
    body = post(
        UPDATE_APPOINTMENT_TYPE_MUTATION,
        {
            "input": {
                "organizationId": org.id,
                "appointmentTypeId": appointment_type_id,
                "name": "Consult",
                "slots": slots,
            }
        },
    )
    return body["data"]["updateAppointmentType"]


def _attached_pool_ids(org: Organization, slot_name: str) -> set[int]:
    slot = AppointmentTypeSlot.objects.filter_by_organization(org.id).get(name=slot_name)
    return set(
        AppointmentTypeSlotPool.objects.filter_by_organization(org.id)
        .filter(slot=slot)
        .values_list("pool_fk_id", flat=True)
    )


@pytest.fixture
def org() -> Organization:
    return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")


@pytest.fixture
def host_calendar(org: Organization) -> Calendar:
    return _make_calendar(org)


@pytest.fixture
def pool_a(org: Organization) -> tuple[CalendarPool, Calendar]:
    calendar = _make_calendar(org)
    return create_calendar_pool(organization=org, name="Pool A", calendars=[calendar]), calendar


@pytest.fixture
def pool_b(org: Organization) -> tuple[CalendarPool, Calendar]:
    calendar = _make_calendar(org)
    return create_calendar_pool(organization=org, name="Pool B", calendars=[calendar]), calendar


@pytest.fixture
def admin_post(org: Organization) -> GraphQLPost:
    return make_token_post(org, BOTH_RESOURCES)


class TestCreateAppointmentTypeWithPools:
    def test_org_wide_token_attaches_pools(self, org, admin_post, host_calendar, pool_a):
        pool, pool_calendar = pool_a

        result = _create(admin_post, org, [_slot("Host", [host_calendar.id], poolIds=[pool.id])])

        assert result["success"] is True, result
        assert result["errorMessage"] is None
        [slot] = result["appointmentType"]["slots"]
        assert slot["name"] == "Host"
        assert slot["pools"] == [{"id": str(pool.id)}]
        assert {c["id"] for c in slot["calendars"]} == {
            str(host_calendar.id),
            str(pool_calendar.id),
        }
        assert _attached_pool_ids(org, "Host") == {pool.id}

    def test_scoped_admin_token_attaches_pools(self, org, host_calendar, pool_a):
        pool, _ = pool_a
        membership = _make_membership(org, GROUP_ORGANIZATION_ADMIN)
        post = make_token_post(org, BOTH_RESOURCES, scoped_to_membership=membership)

        result = _create(post, org, [_slot("Host", [host_calendar.id], poolIds=[pool.id])])

        assert result["success"] is True, result
        assert _attached_pool_ids(org, "Host") == {pool.id}

    def test_omitting_pool_ids_needs_no_pool_access(self, org, host_calendar):
        post = make_token_post(org, [PublicAPIResources.APPOINTMENT_TYPE])

        result = _create(post, org, [_slot("Host", [host_calendar.id])])

        assert result["success"] is True, result
        assert result["appointmentType"]["slots"][0]["pools"] == []
        assert _attached_pool_ids(org, "Host") == set()

    def test_token_without_calendar_pool_resource_refused(self, org, host_calendar, pool_a):
        pool, _ = pool_a
        post = make_token_post(org, [PublicAPIResources.APPOINTMENT_TYPE])

        result = _create(post, org, [_slot("Host", [host_calendar.id], poolIds=[pool.id])])

        assert result == {"success": False, "errorMessage": FORBIDDEN, "appointmentType": None}
        assert not AppointmentType.objects.filter_by_organization(org.id).exists()

    def test_scoped_member_token_refused(self, org, host_calendar, pool_a):
        pool, _ = pool_a
        membership = _make_membership(org, GROUP_ORGANIZATION_MEMBER)
        post = make_token_post(org, BOTH_RESOURCES, scoped_to_membership=membership)

        result = _create(post, org, [_slot("Host", [host_calendar.id], poolIds=[pool.id])])

        assert result == {
            "success": False,
            "errorMessage": APPOINTMENT_TYPE_FORBIDDEN,
            "appointmentType": None,
        }
        assert not AppointmentType.objects.filter_by_organization(org.id).exists()

    def test_pool_from_another_organization_rejected(self, org, admin_post, host_calendar):
        other_org = baker.make(Organization, name="Other org")
        foreign_pool = create_calendar_pool(
            organization=other_org, name="Foreign", calendars=[_make_calendar(other_org)]
        )

        result = _create(
            admin_post, org, [_slot("Host", [host_calendar.id], poolIds=[foreign_pool.id])]
        )

        assert result == {
            "success": False,
            "errorMessage": (
                f"Calendar pools [{foreign_pool.id}] do not belong to this organization."
            ),
            "appointmentType": None,
        }
        assert not AppointmentType.objects.filter_by_organization(org.id).exists()


class TestUpdateAppointmentTypePools:
    @pytest.fixture
    def appointment_type_id(self, org, admin_post, host_calendar, pool_a) -> int:
        pool, _ = pool_a
        result = _create(admin_post, org, [_slot("Host", [host_calendar.id], poolIds=[pool.id])])
        assert result["success"] is True, result
        return int(result["appointmentType"]["id"])

    def test_list_replaces_pools(self, org, admin_post, appointment_type_id, host_calendar, pool_b):
        pool, pool_calendar = pool_b

        result = _update(
            admin_post,
            org,
            appointment_type_id,
            [_slot("Host", [host_calendar.id], poolIds=[pool.id])],
        )

        assert result["success"] is True, result
        [slot] = result["appointmentType"]["slots"]
        assert slot["pools"] == [{"id": str(pool.id)}]
        assert {c["id"] for c in slot["calendars"]} == {
            str(host_calendar.id),
            str(pool_calendar.id),
        }
        assert _attached_pool_ids(org, "Host") == {pool.id}

    def test_omitted_pool_ids_keeps_pools(self, org, appointment_type_id, host_calendar, pool_a):
        pool, _ = pool_a
        # A token without CALENDAR_POOL can still update the appointment type, as
        # long as it does not touch the pools.
        post = make_token_post(org, [PublicAPIResources.APPOINTMENT_TYPE])

        result = _update(post, org, appointment_type_id, [_slot("Host", [host_calendar.id])])

        assert result["success"] is True, result
        assert _attached_pool_ids(org, "Host") == {pool.id}

    def test_null_pool_ids_keeps_pools(
        self, org, admin_post, appointment_type_id, host_calendar, pool_a
    ):
        pool, _ = pool_a

        result = _update(
            admin_post, org, appointment_type_id, [_slot("Host", [host_calendar.id], poolIds=None)]
        )

        assert result["success"] is True, result
        assert _attached_pool_ids(org, "Host") == {pool.id}

    def test_empty_list_detaches_every_pool(
        self, org, admin_post, appointment_type_id, host_calendar
    ):
        result = _update(
            admin_post, org, appointment_type_id, [_slot("Host", [host_calendar.id], poolIds=[])]
        )

        assert result["success"] is True, result
        [slot] = result["appointmentType"]["slots"]
        assert slot["pools"] == []
        assert slot["calendars"] == [{"id": str(host_calendar.id)}]
        assert _attached_pool_ids(org, "Host") == set()

    def test_detach_without_calendar_pool_resource_refused(
        self, org, appointment_type_id, host_calendar, pool_a
    ):
        pool, _ = pool_a
        post = make_token_post(org, [PublicAPIResources.APPOINTMENT_TYPE])

        result = _update(
            post, org, appointment_type_id, [_slot("Host", [host_calendar.id], poolIds=[])]
        )

        assert result == {"success": False, "errorMessage": FORBIDDEN, "appointmentType": None}
        assert _attached_pool_ids(org, "Host") == {pool.id}

    def test_scoped_member_token_refused(
        self, org, appointment_type_id, host_calendar, pool_a, pool_b
    ):
        pool, _ = pool_a
        membership = _make_membership(org, GROUP_ORGANIZATION_MEMBER)
        post = make_token_post(org, BOTH_RESOURCES, scoped_to_membership=membership)

        result = _update(
            post,
            org,
            appointment_type_id,
            [_slot("Host", [host_calendar.id], poolIds=[pool_b[0].id])],
        )

        assert result == {
            "success": False,
            "errorMessage": APPOINTMENT_TYPE_FORBIDDEN,
            "appointmentType": None,
        }
        assert _attached_pool_ids(org, "Host") == {pool.id}
