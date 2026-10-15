"""Regression suite for the public-API AppointmentType mutation authorization hotfix.

``calendar_integration/mutations.py``'s ``AppointmentTypeMutations`` class used to carry
no ``permission_classes`` on ``create_appointment_type``, ``update_appointment_type``,
``delete_appointment_type``, and ``create_appointment_type_event`` (unlike their sibling
booking-code mutations, e.g. ``create_calendar_booking_code``), and each resolved its
organization from client-supplied ``input.organization_id`` via a bare
``Organization.objects.get(id=...)`` with no ownership check. Any caller -- including
one with NO ``Authorization`` header -- could create, rename, or delete a
``AppointmentType`` (and, once an unrelated DI-wiring bug is fixed, create a
``CalendarEvent``) in an organization it has no relationship to.

The fix adds ``permission_classes=[IsAuthenticated, OrganizationResourceAccess]`` to
all four mutations, resolves the organization from the authenticated token
(``info.context.request.public_api_organization``, bound by
``PublicApiSystemUserMiddleware``) instead of client input, and validates that
``input.organization_id`` -- which callers may still send -- matches the token's
organization, rejecting the request otherwise rather than silently using the token's
org. See ``calendar_integration/mutations.py`` (`create_appointment_type`,
`update_appointment_type`, `delete_appointment_type`, `create_appointment_type_event`) and
``public_api/permissions.py``'s ``OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING``.

This module exercises the real HTTP stack via Django's test client against
``/graphql/`` -- not the resolver methods directly -- so it goes through the real
middleware + permission-class stack rather than giving a false all-clear.

For each of the four mutations:
  - Unauthenticated, targeting another org: REJECTED, no DB row created / modified /
    deleted.
  - Authenticated for org A, targeting org B (valid credentials, wrong org): REJECTED,
    no DB row created / modified / deleted in org B. This is the case that survives if
    someone only adds ``IsAuthenticated`` and forgets the org check.
  - Authenticated for org A, targeting its own org: SUCCEEDS.

``createAppointmentTypeEvent`` also has its own suite for the organization-wide token
bypass: an org-wide token books any appointment type, private ones included, at any
length; member-scoped and org-less tokens do not get that bypass.

A control test proves the unauthenticated ``createCalendarBookingCode`` (a sibling
mutation that already carried ``permission_classes``) is still rejected, so a passing
suite here cannot just mean requests never reached the resolver layer.
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
    CalendarEvent,
)
from organizations.models import Organization
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.constants import PublicAPIResources
from public_api.tests.helpers import make_token_post
from users.models import User


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# GraphQL documents
# ---------------------------------------------------------------------------

CREATE_APPOINTMENT_TYPE_MUTATION = """
mutation CreateAppointmentType($input: AppointmentTypeInput!) {
    createAppointmentType(input: $input) {
        success
        errorMessage
        appointmentType {
            id
            name
        }
    }
}
"""

UPDATE_APPOINTMENT_TYPE_MUTATION = """
mutation UpdateAppointmentType($input: UpdateAppointmentTypeInput!) {
    updateAppointmentType(input: $input) {
        success
        errorMessage
        appointmentType {
            id
            name
        }
    }
}
"""

DELETE_APPOINTMENT_TYPE_MUTATION = """
mutation DeleteAppointmentType($input: DeleteAppointmentTypeInput!) {
    deleteAppointmentType(input: $input) {
        success
        errorMessage
    }
}
"""

CREATE_APPOINTMENT_TYPE_EVENT_MUTATION = """
mutation CreateAppointmentTypeEvent($input: AppointmentTypeEventInput!) {
    createAppointmentTypeEvent(input: $input) {
        success
        errorMessage
        event {
            id
            title
        }
    }
}
"""

CREATE_CALENDAR_BOOKING_CODE_MUTATION = """
mutation CreateCalendarBookingCode($input: CreateBookingCodeInput!) {
    createCalendarBookingCode(input: $input) {
        success
        errorCode
        errorMessage
        code
        id
    }
}
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_a():
    """The caller's own organization -- has no relationship to org B."""
    return baker.make(Organization, name="Org A (caller's own tenant)")


@pytest.fixture
def org_b():
    """The victim organization: the caller holds no token, membership, or
    resource grant for it whatsoever."""
    return baker.make(Organization, name="Org B (victim tenant)")


@pytest.fixture
def anon_client():
    """APIClient that never sets an Authorization header."""
    return APIClient()


def _token_client(org, **kwargs):
    """post() for a fresh token holding APPOINTMENT_TYPE access; see ``make_token_post``."""
    return make_token_post(org, [PublicAPIResources.APPOINTMENT_TYPE], **kwargs)


@pytest.fixture
def org_a_post(org_a):
    """post() for an org-wide token of org A with APPOINTMENT_TYPE access."""
    return _token_client(org_a)


def post_graphql_anon(client: APIClient, query: str, variables: dict) -> tuple[int, dict]:
    """POST to /graphql/ with NO Authorization header. Returns (status_code, body)."""
    response = client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )
    return response.status_code, response.json()


@pytest.fixture
def org_b_calendar(org_b):
    return baker.make(
        Calendar,
        organization=org_b,
        name="Org B Calendar",
        external_id="org-b-cal-auth-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )


@pytest.fixture
def org_a_calendar(org_a):
    return baker.make(
        Calendar,
        organization=org_a,
        name="Org A Calendar",
        external_id="org-a-cal-auth-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )


@pytest.fixture
def org_b_existing_appointment_type(org_b):
    """An AppointmentType that already exists in org B, pre-dating the attempted attack.

    Used for the update / delete / create-event probes: those need something to act
    on that org B "owns" independently of anything the create test may or may not
    have produced.
    """
    return baker.make(
        AppointmentType,
        organization=org_b,
        name="Org B Pre-existing AppointmentType",
        # accepts_public_scheduling=True isolates the org-scoping question this suite
        # is about from the SEPARATE "does this appointment type accept public scheduling"
        # business gate in create_appointment_type_event.
        accepts_public_scheduling=True,
    )


@pytest.fixture
def org_a_existing_appointment_type(org_a):
    """An AppointmentType that already exists in org A -- the caller's own tenant."""
    return baker.make(
        AppointmentType,
        organization=org_a,
        name="Org A Pre-existing AppointmentType",
        accepts_public_scheduling=True,
        duration=datetime.timedelta(hours=1),
    )


@pytest.fixture
def org_b_appointment_type_with_bookable_slot(
    org_b, org_b_calendar, org_b_existing_appointment_type
):
    """Attach a slot + availability window to the pre-existing org B appointment type so a
    createAppointmentTypeEvent test has a real bookable target."""
    slot = AppointmentTypeSlot.objects.create(
        organization=org_b,
        appointment_type=org_b_existing_appointment_type,
        name="Only Slot",
        order=0,
        required_count=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=org_b,
        slot=slot,
        calendar=org_b_calendar,
    )
    AvailableTime.objects.create(
        organization=org_b,
        calendar=org_b_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )
    return org_b_existing_appointment_type, slot


@pytest.fixture
def org_an_appointment_type_with_bookable_slot(
    org_a, org_a_calendar, org_a_existing_appointment_type
):
    """Same as ``org_b_appointment_type_with_bookable_slot`` but for org A -- the caller's own
    tenant, used to prove the legitimate own-org path is reachable."""
    slot = AppointmentTypeSlot.objects.create(
        organization=org_a,
        appointment_type=org_a_existing_appointment_type,
        name="Only Slot",
        order=0,
        required_count=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=org_a,
        slot=slot,
        calendar=org_a_calendar,
    )
    AvailableTime.objects.create(
        organization=org_a,
        calendar=org_a_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )
    return org_a_existing_appointment_type, slot


# ---------------------------------------------------------------------------
# 1. createAppointmentType
# ---------------------------------------------------------------------------


class TestCreateAppointmentType:
    def test_unauthenticated_cross_tenant_rejected(self, anon_client, org_b):
        """No Authorization header, targeting org B: rejected, no row created."""
        status, body = post_graphql_anon(
            anon_client,
            CREATE_APPOINTMENT_TYPE_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "name": "Attacker-Planted AppointmentType",
                    "description": "Created with no Authorization header",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert status == 200, body
        assert body.get("errors"), (
            f"Expected a GraphQL error for an unauthenticated request; got none. body={body!r}"
        )
        assert (
            not AppointmentType.objects.filter_by_organization(org_b.id)
            .filter(name="Attacker-Planted AppointmentType")
            .exists()
        )

    def test_cross_tenant_with_valid_credentials_rejected(self, org_a_post, org_b):
        """A token valid for org A cannot create an appointment type in org B."""
        body = org_a_post(
            CREATE_APPOINTMENT_TYPE_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "name": "Cross-Tenant AppointmentType",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        result = body["data"]["createAppointmentType"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert (
            not AppointmentType.objects.filter_by_organization(org_b.id)
            .filter(name="Cross-Tenant AppointmentType")
            .exists()
        )

    def test_authenticated_own_org_succeeds(self, org_a, org_a_post):
        """A token acting on its own organization still succeeds."""
        body = org_a_post(
            CREATE_APPOINTMENT_TYPE_MUTATION,
            {
                "input": {
                    "organizationId": org_a.id,
                    "name": "Legit AppointmentType",
                    "description": "Created by an authorized caller",
                    "slots": [],
                    # Private on purpose: an appointment type that accepts public scheduling
                    # must carry a duration, and this mutation has no way to set
                    # one. Privacy is incidental to what this suite asserts --
                    # that a caller acting on its own org gets through.
                    "isPrivate": True,
                }
            },
        )

        assert "errors" not in body or not body["errors"]
        result = body["data"]["createAppointmentType"]
        assert result["success"] is True
        assert result["appointmentType"] is not None
        assert (
            AppointmentType.objects.filter_by_organization(org_a.id)
            .filter(name="Legit AppointmentType")
            .exists()
        )


# ---------------------------------------------------------------------------
# 2. updateAppointmentType
# ---------------------------------------------------------------------------


class TestUpdateAppointmentType:
    def test_unauthenticated_cross_tenant_rejected(
        self, anon_client, org_b, org_b_existing_appointment_type
    ):
        original_name = org_b_existing_appointment_type.name

        status, body = post_graphql_anon(
            anon_client,
            UPDATE_APPOINTMENT_TYPE_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "appointmentTypeId": org_b_existing_appointment_type.id,
                    "name": "Renamed By Attacker",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert status == 200, body
        assert body.get("errors")
        org_b_existing_appointment_type.refresh_from_db()
        assert org_b_existing_appointment_type.name == original_name

    def test_cross_tenant_with_valid_credentials_rejected(
        self, org_a_post, org_b, org_b_existing_appointment_type
    ):
        original_name = org_b_existing_appointment_type.name
        body = org_a_post(
            UPDATE_APPOINTMENT_TYPE_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "appointmentTypeId": org_b_existing_appointment_type.id,
                    "name": "Renamed By Cross-Tenant Token",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        result = body["data"]["updateAppointmentType"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        org_b_existing_appointment_type.refresh_from_db()
        assert org_b_existing_appointment_type.name == original_name

    def test_authenticated_own_org_succeeds(
        self, org_a, org_a_post, org_a_existing_appointment_type
    ):
        body = org_a_post(
            UPDATE_APPOINTMENT_TYPE_MUTATION,
            {
                "input": {
                    "organizationId": org_a.id,
                    "appointmentTypeId": org_a_existing_appointment_type.id,
                    "name": "Renamed By Owner",
                    "description": "",
                    "slots": [],
                    # Flips the fixture appointment type private. The invariant is checked
                    # against the resulting state, and the fixture is public with
                    # no duration, so leaving it public here would be rejected on
                    # a suite that is really about org scoping.
                    "isPrivate": True,
                }
            },
        )

        assert "errors" not in body or not body["errors"]
        result = body["data"]["updateAppointmentType"]
        assert result["success"] is True
        org_a_existing_appointment_type.refresh_from_db()
        assert org_a_existing_appointment_type.name == "Renamed By Owner"


# ---------------------------------------------------------------------------
# 3. deleteAppointmentType
# ---------------------------------------------------------------------------


class TestDeleteAppointmentType:
    def test_unauthenticated_cross_tenant_rejected(
        self, anon_client, org_b, org_b_existing_appointment_type
    ):
        appointment_type_id = org_b_existing_appointment_type.id

        status, body = post_graphql_anon(
            anon_client,
            DELETE_APPOINTMENT_TYPE_MUTATION,
            {"input": {"organizationId": org_b.id, "appointmentTypeId": appointment_type_id}},
        )

        assert status == 200, body
        assert body.get("errors")
        assert (
            AppointmentType.objects.filter_by_organization(org_b.id)
            .filter(id=appointment_type_id)
            .exists()
        )

    def test_cross_tenant_with_valid_credentials_rejected(
        self, org_a_post, org_b, org_b_existing_appointment_type
    ):
        appointment_type_id = org_b_existing_appointment_type.id
        body = org_a_post(
            DELETE_APPOINTMENT_TYPE_MUTATION,
            {"input": {"organizationId": org_b.id, "appointmentTypeId": appointment_type_id}},
        )

        result = body["data"]["deleteAppointmentType"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert (
            AppointmentType.objects.filter_by_organization(org_b.id)
            .filter(id=appointment_type_id)
            .exists()
        )

    def test_authenticated_own_org_succeeds(
        self, org_a, org_a_post, org_a_existing_appointment_type
    ):
        appointment_type_id = org_a_existing_appointment_type.id
        body = org_a_post(
            DELETE_APPOINTMENT_TYPE_MUTATION,
            {"input": {"organizationId": org_a.id, "appointmentTypeId": appointment_type_id}},
        )

        assert "errors" not in body or not body["errors"]
        result = body["data"]["deleteAppointmentType"]
        assert result["success"] is True
        assert (
            not AppointmentType.objects.filter_by_organization(org_a.id)
            .filter(id=appointment_type_id)
            .exists()
        )


# ---------------------------------------------------------------------------
# 4. createAppointmentTypeEvent
# ---------------------------------------------------------------------------


def _event_variables(org_id: int, appointment_type_id: int, slot_id: int, calendar_id: int) -> dict:
    return {
        "input": {
            "organizationId": org_id,
            "appointmentTypeId": appointment_type_id,
            "title": "Attempted Event",
            "description": "",
            "startTime": "2030-06-01T10:00:00Z",
            "endTime": "2030-06-01T11:00:00Z",
            "timezone": "UTC",
            "slotSelections": [{"slotId": slot_id, "calendarIds": [calendar_id]}],
            "attendances": [],
            "externalAttendances": [
                {"externalAttendee": {"email": "attacker@example.com", "name": "Attacker"}}
            ],
        }
    }


class TestCreateAppointmentTypeEvent:
    def test_unauthenticated_cross_tenant_rejected(
        self, anon_client, org_b, org_b_appointment_type_with_bookable_slot
    ):
        appointment_type, slot = org_b_appointment_type_with_bookable_slot
        calendar_id = slot.memberships.get().calendar_fk_id

        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

        status, body = post_graphql_anon(
            anon_client,
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _event_variables(org_b.id, appointment_type.id, slot.id, calendar_id),
        )

        assert status == 200, body
        assert body.get("errors")
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

    def test_cross_tenant_with_valid_credentials_rejected(
        self, org_a_post, org_b, org_b_appointment_type_with_bookable_slot
    ):
        appointment_type, slot = org_b_appointment_type_with_bookable_slot
        calendar_id = slot.memberships.get().calendar_fk_id
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

        body = org_a_post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _event_variables(org_b.id, appointment_type.id, slot.id, calendar_id),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

    def test_authenticated_own_org_succeeds(
        self, org_a, org_a_post, org_an_appointment_type_with_bookable_slot
    ):
        """An authenticated caller acting on its own org passes both permission checks
        and the input.organization_id check, and the event is created."""
        appointment_type, slot = org_an_appointment_type_with_bookable_slot
        calendar_id = slot.memberships.get().calendar_fk_id
        body = org_a_post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _event_variables(org_a.id, appointment_type.id, slot.id, calendar_id),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result["errorMessage"] is None
        assert result["success"] is True
        event = CalendarEvent.objects.filter_by_organization(org_a.id).get()
        assert result["event"] == {"id": str(event.id), "title": "Attempted Event"}
        assert event.appointment_type_fk_id == appointment_type.id


# ---------------------------------------------------------------------------
# 5. createAppointmentTypeEvent: org-wide and admin-scoped token bypass
# ---------------------------------------------------------------------------


@pytest.fixture
def org_a_private_pinned_appointment_type(org_a, org_a_calendar):
    """A private appointment type in org A, pinned to 30 minutes, with a bookable slot."""
    appointment_type = baker.make(
        AppointmentType,
        organization=org_a,
        name="Org A Private Pinned AppointmentType",
        accepts_public_scheduling=False,
        duration=datetime.timedelta(minutes=30),
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=org_a,
        appointment_type=appointment_type,
        name="Only Slot",
        order=0,
        required_count=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=org_a, slot=slot, calendar=org_a_calendar
    )
    AvailableTime.objects.create(
        organization=org_a,
        calendar=org_a_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )
    return appointment_type, slot


def _private_event_variables(org_id, appointment_type, slot, *, end_time):
    variables = _event_variables(
        org_id, appointment_type.id, slot.id, slot.memberships.get().calendar_fk_id
    )
    variables["input"]["startTime"] = "2030-06-01T10:00:00"
    variables["input"]["endTime"] = end_time
    return variables


class TestCreateAppointmentTypeEventOrgWideTokenBypass:
    def test_org_wide_token_books_private_appointment_type(
        self, org_a, org_a_private_pinned_appointment_type
    ):
        appointment_type, slot = org_a_private_pinned_appointment_type
        post = _token_client(org_a)

        body = post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _private_event_variables(
                org_a.id, appointment_type, slot, end_time="2030-06-01T10:30:00"
            ),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result["errorMessage"] is None
        assert result["success"] is True
        event = CalendarEvent.objects.filter_by_organization(org_a.id).get()
        assert event.appointment_type_fk_id == appointment_type.id

    def test_org_wide_token_must_book_the_pinned_duration(
        self, org_a, org_a_private_pinned_appointment_type
    ):
        """The token skips only the private-type check: an hour-long booking of an
        appointment type pinned to 30 minutes is refused."""
        appointment_type, slot = org_a_private_pinned_appointment_type
        post = _token_client(org_a)

        body = post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _private_event_variables(
                org_a.id, appointment_type, slot, end_time="2030-06-01T11:00:00"
            ),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result == {
            "success": False,
            "errorMessage": "The requested time does not match this appointment type's duration.",
            "event": None,
        }
        assert not CalendarEvent.objects.filter_by_organization(org_a.id).exists()

    def test_admin_scoped_token_books_private_appointment_type(
        self, org_a, org_a_private_pinned_appointment_type
    ):
        """A token scoped to an organization admin, here handed the APPOINTMENT_TYPE
        resource directly, books a private appointment type, as an org-wide token does:
        the public API gives it the same appointment-type powers (``system_user_scope``)."""
        appointment_type, slot = org_a_private_pinned_appointment_type
        user = baker.make(User, email=f"scoped_{uuid.uuid4().hex[:8]}@example.com")
        membership = make_membership(
            user=user, organization=org_a, groups=(GROUP_ORGANIZATION_ADMIN,), is_active=True
        )
        post = _token_client(org_a, scoped_to_membership=membership)

        body = post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _private_event_variables(
                org_a.id, appointment_type, slot, end_time="2030-06-01T10:30:00"
            ),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result["errorMessage"] is None
        assert result["success"] is True
        event = CalendarEvent.objects.filter_by_organization(org_a.id).get()
        assert event.appointment_type_fk_id == appointment_type.id

    def test_member_scoped_token_gets_no_bypass(self, org_a, org_a_private_pinned_appointment_type):
        """A token scoped to a regular member, here handed the APPOINTMENT_TYPE resource
        directly, is still refused a private appointment type."""
        appointment_type, slot = org_a_private_pinned_appointment_type
        user = baker.make(User, email=f"scoped_{uuid.uuid4().hex[:8]}@example.com")
        membership = make_membership(
            user=user, organization=org_a, groups=(GROUP_ORGANIZATION_MEMBER,), is_active=True
        )
        post = _token_client(org_a, scoped_to_membership=membership)

        body = post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _private_event_variables(
                org_a.id, appointment_type, slot, end_time="2030-06-01T10:30:00"
            ),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result == {
            "success": False,
            "errorMessage": (
                "This appointment type does not accept public scheduling. "
                "A token or scheduling code is required."
            ),
            "event": None,
        }
        assert not CalendarEvent.objects.filter_by_organization(org_a.id).exists()

    def test_org_less_token_gets_no_bypass(self, org_a, org_a_private_pinned_appointment_type):
        """The org-less token reaches org A through the organization header, but it is
        not org A's own token, so the private appointment type stays closed to it."""
        appointment_type, slot = org_a_private_pinned_appointment_type
        post = _token_client(None, organization_header=org_a.id)

        body = post(
            CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
            _private_event_variables(
                org_a.id, appointment_type, slot, end_time="2030-06-01T10:30:00"
            ),
        )

        result = body["data"]["createAppointmentTypeEvent"]
        assert result["success"] is False
        assert result["event"] is None
        assert not CalendarEvent.objects.filter_by_organization(org_a.id).exists()


# ---------------------------------------------------------------------------
# Control: a sibling mutation that DOES carry permission_classes must reject
# the identical unauthenticated request. Proves the harness actually reaches
# the resolver layer -- without this, a "rejected" result above would be
# ambiguous (it could mean the request never got that far for an unrelated
# reason).
# ---------------------------------------------------------------------------


def test_control_gated_mutation_is_rejected_unauthenticated(anon_client, org_b, org_b_calendar):
    """createCalendarBookingCode carries
    permission_classes=[IsAuthenticated, OrganizationResourceAccess]
    (calendar_integration/mutations.py). The identical unauthenticated request
    against it MUST be rejected. If this test fails, the test harness itself is
    broken and the results above cannot be trusted.
    """
    status, body = post_graphql_anon(
        anon_client,
        CREATE_CALENDAR_BOOKING_CODE_MUTATION,
        {"input": {"organizationId": org_b.id, "calendarId": org_b_calendar.id}},
    )

    assert status == 200, body
    # permission_classes failing on a non-nullable field nulls the field and
    # reports a GraphQL error -- IsAuthenticated.has_permission returns False
    # because request.public_api_system_user is None (no Authorization header).
    assert body["data"] is None or body["data"].get("createCalendarBookingCode") is None
    assert body.get("errors"), (
        "Control failed: the gated mutation returned no errors for an "
        "unauthenticated request. The harness is not exercising permission "
        "checks correctly -- any 'rejected' result on the four mutations above "
        "would be meaningless."
    )
    messages = " ".join(e.get("message", "") for e in body["errors"])
    assert (
        "authenticated" in messages.lower()
        or "permission" in messages.lower()
        or "access" in messages.lower()
    ), body["errors"]
