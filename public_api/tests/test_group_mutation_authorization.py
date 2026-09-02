"""Regression suite for the public-API CalendarGroup mutation authorization hotfix.

``calendar_integration/mutations.py``'s ``CalendarGroupMutations`` class used to carry
no ``permission_classes`` on ``create_calendar_group``, ``update_calendar_group``,
``delete_calendar_group``, and ``create_calendar_group_event`` (unlike their sibling
booking-code mutations, e.g. ``create_calendar_booking_code``), and each resolved its
organization from client-supplied ``input.organization_id`` via a bare
``Organization.objects.get(id=...)`` with no ownership check. Any caller -- including
one with NO ``Authorization`` header -- could create, rename, or delete a
``CalendarGroup`` (and, once an unrelated DI-wiring bug is fixed, create a
``CalendarEvent``) in an organization it has no relationship to.

The fix adds ``permission_classes=[IsAuthenticated, OrganizationResourceAccess]`` to
all four mutations, resolves the organization from the authenticated token
(``info.context.request.public_api_organization``, bound by
``PublicApiSystemUserMiddleware``) instead of client input, and validates that
``input.organization_id`` -- which callers may still send -- matches the token's
organization, rejecting the request otherwise rather than silently using the token's
org. See ``calendar_integration/mutations.py`` (`create_calendar_group`,
`update_calendar_group`, `delete_calendar_group`, `create_calendar_group_event`) and
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
  - Authenticated for org A, targeting its own org: SUCCEEDS (except
    ``createCalendarGroupEvent``, which -- pre-existing and unrelated to this fix -- is
    broken for every caller by a DI-wiring bug in ``di_core/containers.py``; see that
    test's docstring).

A control test proves the unauthenticated ``createCalendarBookingCode`` (a sibling
mutation that already carried ``permission_classes``) is still rejected, so a passing
suite here cannot just mean requests never reached the resolver layer.
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# GraphQL documents
# ---------------------------------------------------------------------------

CREATE_CALENDAR_GROUP_MUTATION = """
mutation CreateCalendarGroup($input: CalendarGroupInput!) {
    createCalendarGroup(input: $input) {
        success
        errorMessage
        group {
            id
            name
        }
    }
}
"""

UPDATE_CALENDAR_GROUP_MUTATION = """
mutation UpdateCalendarGroup($input: UpdateCalendarGroupInput!) {
    updateCalendarGroup(input: $input) {
        success
        errorMessage
        group {
            id
            name
        }
    }
}
"""

DELETE_CALENDAR_GROUP_MUTATION = """
mutation DeleteCalendarGroup($input: DeleteCalendarGroupInput!) {
    deleteCalendarGroup(input: $input) {
        success
        errorMessage
    }
}
"""

CREATE_CALENDAR_GROUP_EVENT_MUTATION = """
mutation CreateCalendarGroupEvent($input: CalendarGroupEventInput!) {
    createCalendarGroupEvent(input: $input) {
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


def _grant_and_client(system_user, token, auth_service):
    """Return an APIClient + a post() helper bound to the given credentials."""
    from di_core.containers import container

    assert container is not None  # noqa: S101

    client = APIClient()

    def post(query: str, variables: dict):
        with container.public_api_auth_service.override(auth_service):
            return client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    return post


@pytest.fixture
def org_a_client(org_a):
    """(post_fn, system_user, org) for a token scoped to org A with CALENDAR_GROUP access."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="group_auth_probe_org_a", organization=org_a
    )
    baker.make(
        ResourceAccess,
        system_user=system_user,
        resource_name=PublicAPIResources.CALENDAR_GROUP,
    )
    post = _grant_and_client(system_user, token, auth_service)
    return post, system_user, org_a


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
def org_b_existing_group(org_b):
    """A CalendarGroup that already exists in org B, pre-dating the attempted attack.

    Used for the update / delete / create-event probes: those need something to act
    on that org B "owns" independently of anything the create test may or may not
    have produced.
    """
    return baker.make(
        CalendarGroup,
        organization=org_b,
        name="Org B Pre-existing Group",
        # accepts_public_scheduling=True isolates the org-scoping question this suite
        # is about from the SEPARATE "does this group accept public scheduling"
        # business gate in create_grouped_event.
        accepts_public_scheduling=True,
    )


@pytest.fixture
def org_a_existing_group(org_a):
    """A CalendarGroup that already exists in org A -- the caller's own tenant."""
    return baker.make(
        CalendarGroup,
        organization=org_a,
        name="Org A Pre-existing Group",
        accepts_public_scheduling=True,
    )


@pytest.fixture
def org_b_group_with_bookable_slot(org_b, org_b_calendar, org_b_existing_group):
    """Attach a slot + availability window to the pre-existing org B group so a
    createCalendarGroupEvent test has a real bookable target."""
    slot = CalendarGroupSlot.objects.create(
        organization=org_b,
        group=org_b_existing_group,
        name="Only Slot",
        order=0,
        required_count=1,
    )
    CalendarGroupSlotMembership.objects.create(
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
    return org_b_existing_group, slot


@pytest.fixture
def org_a_group_with_bookable_slot(org_a, org_a_calendar, org_a_existing_group):
    """Same as ``org_b_group_with_bookable_slot`` but for org A -- the caller's own
    tenant, used to prove the legitimate own-org path is reachable."""
    slot = CalendarGroupSlot.objects.create(
        organization=org_a,
        group=org_a_existing_group,
        name="Only Slot",
        order=0,
        required_count=1,
    )
    CalendarGroupSlotMembership.objects.create(
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
    return org_a_existing_group, slot


# ---------------------------------------------------------------------------
# 1. createCalendarGroup
# ---------------------------------------------------------------------------


class TestCreateCalendarGroup:
    def test_unauthenticated_cross_tenant_rejected(self, anon_client, org_b):
        """No Authorization header, targeting org B: rejected, no row created."""
        status, body = post_graphql_anon(
            anon_client,
            CREATE_CALENDAR_GROUP_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "name": "Attacker-Planted Group",
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
            not CalendarGroup.objects.filter_by_organization(org_b.id)
            .filter(name="Attacker-Planted Group")
            .exists()
        )

    def test_cross_tenant_with_valid_credentials_rejected(self, org_a_client, org_b):
        """A token valid for org A cannot create a group in org B."""
        post, _system_user, _org_a = org_a_client

        response = post(
            CREATE_CALENDAR_GROUP_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "name": "Cross-Tenant Group",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        body = response.json()
        result = body["data"]["createCalendarGroup"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert (
            not CalendarGroup.objects.filter_by_organization(org_b.id)
            .filter(name="Cross-Tenant Group")
            .exists()
        )

    def test_authenticated_own_org_succeeds(self, org_a_client):
        """A token acting on its own organization still succeeds."""
        post, _system_user, org_a = org_a_client

        response = post(
            CREATE_CALENDAR_GROUP_MUTATION,
            {
                "input": {
                    "organizationId": org_a.id,
                    "name": "Legit Group",
                    "description": "Created by an authorized caller",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert "errors" not in body or not body["errors"]
        result = body["data"]["createCalendarGroup"]
        assert result["success"] is True
        assert result["group"] is not None
        assert (
            CalendarGroup.objects.filter_by_organization(org_a.id)
            .filter(name="Legit Group")
            .exists()
        )


# ---------------------------------------------------------------------------
# 2. updateCalendarGroup
# ---------------------------------------------------------------------------


class TestUpdateCalendarGroup:
    def test_unauthenticated_cross_tenant_rejected(self, anon_client, org_b, org_b_existing_group):
        original_name = org_b_existing_group.name

        status, body = post_graphql_anon(
            anon_client,
            UPDATE_CALENDAR_GROUP_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "groupId": org_b_existing_group.id,
                    "name": "Renamed By Attacker",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert status == 200, body
        assert body.get("errors")
        org_b_existing_group.refresh_from_db()
        assert org_b_existing_group.name == original_name

    def test_cross_tenant_with_valid_credentials_rejected(
        self, org_a_client, org_b, org_b_existing_group
    ):
        original_name = org_b_existing_group.name
        post, _system_user, _org_a = org_a_client

        response = post(
            UPDATE_CALENDAR_GROUP_MUTATION,
            {
                "input": {
                    "organizationId": org_b.id,
                    "groupId": org_b_existing_group.id,
                    "name": "Renamed By Cross-Tenant Token",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        body = response.json()
        result = body["data"]["updateCalendarGroup"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        org_b_existing_group.refresh_from_db()
        assert org_b_existing_group.name == original_name

    def test_authenticated_own_org_succeeds(self, org_a_client, org_a_existing_group):
        post, _system_user, org_a = org_a_client

        response = post(
            UPDATE_CALENDAR_GROUP_MUTATION,
            {
                "input": {
                    "organizationId": org_a.id,
                    "groupId": org_a_existing_group.id,
                    "name": "Renamed By Owner",
                    "description": "",
                    "slots": [],
                    "isPrivate": False,
                }
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert "errors" not in body or not body["errors"]
        result = body["data"]["updateCalendarGroup"]
        assert result["success"] is True
        org_a_existing_group.refresh_from_db()
        assert org_a_existing_group.name == "Renamed By Owner"


# ---------------------------------------------------------------------------
# 3. deleteCalendarGroup
# ---------------------------------------------------------------------------


class TestDeleteCalendarGroup:
    def test_unauthenticated_cross_tenant_rejected(self, anon_client, org_b, org_b_existing_group):
        group_id = org_b_existing_group.id

        status, body = post_graphql_anon(
            anon_client,
            DELETE_CALENDAR_GROUP_MUTATION,
            {"input": {"organizationId": org_b.id, "groupId": group_id}},
        )

        assert status == 200, body
        assert body.get("errors")
        assert CalendarGroup.objects.filter_by_organization(org_b.id).filter(id=group_id).exists()

    def test_cross_tenant_with_valid_credentials_rejected(
        self, org_a_client, org_b, org_b_existing_group
    ):
        group_id = org_b_existing_group.id
        post, _system_user, _org_a = org_a_client

        response = post(
            DELETE_CALENDAR_GROUP_MUTATION,
            {"input": {"organizationId": org_b.id, "groupId": group_id}},
        )

        assert response.status_code == 200
        body = response.json()
        result = body["data"]["deleteCalendarGroup"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert CalendarGroup.objects.filter_by_organization(org_b.id).filter(id=group_id).exists()

    def test_authenticated_own_org_succeeds(self, org_a_client, org_a_existing_group):
        group_id = org_a_existing_group.id
        post, _system_user, org_a = org_a_client

        response = post(
            DELETE_CALENDAR_GROUP_MUTATION,
            {"input": {"organizationId": org_a.id, "groupId": group_id}},
        )

        assert response.status_code == 200
        body = response.json()
        assert "errors" not in body or not body["errors"]
        result = body["data"]["deleteCalendarGroup"]
        assert result["success"] is True
        assert (
            not CalendarGroup.objects.filter_by_organization(org_a.id).filter(id=group_id).exists()
        )


# ---------------------------------------------------------------------------
# 4. createCalendarGroupEvent
# ---------------------------------------------------------------------------


def _event_variables(org_id: int, group_id: int, slot_id: int, calendar_id: int) -> dict:
    return {
        "input": {
            "organizationId": org_id,
            "groupId": group_id,
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


class TestCreateCalendarGroupEvent:
    def test_unauthenticated_cross_tenant_rejected(
        self, anon_client, org_b, org_b_group_with_bookable_slot
    ):
        group, slot = org_b_group_with_bookable_slot
        calendar_id = slot.memberships.get().calendar_fk_id

        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

        status, body = post_graphql_anon(
            anon_client,
            CREATE_CALENDAR_GROUP_EVENT_MUTATION,
            _event_variables(org_b.id, group.id, slot.id, calendar_id),
        )

        assert status == 200, body
        assert body.get("errors")
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

    def test_cross_tenant_with_valid_credentials_rejected(
        self, org_a_client, org_b, org_b_group_with_bookable_slot
    ):
        group, slot = org_b_group_with_bookable_slot
        calendar_id = slot.memberships.get().calendar_fk_id
        post, _system_user, _org_a = org_a_client

        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

        response = post(
            CREATE_CALENDAR_GROUP_EVENT_MUTATION,
            _event_variables(org_b.id, group.id, slot.id, calendar_id),
        )

        assert response.status_code == 200
        body = response.json()
        result = body["data"]["createCalendarGroupEvent"]
        assert result["success"] is False
        assert result["errorMessage"] is not None
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

    def test_authenticated_own_org_reaches_resolver_body(
        self, org_a_client, org_a_group_with_bookable_slot
    ):
        """An authenticated caller acting on its own org passes BOTH permission
        checks and the input.organization_id-matches-token-org validation -- proving
        the auth fix does not block a legitimate same-org caller.

        It does NOT assert ``success is True``. A pre-existing, unrelated DI-wiring
        bug (``di_core/containers.py``: ``calendar_service`` and
        ``calendar_group_service`` are declared as separate ``providers.Factory``
        instances, so the resolver's ``deps.calendar_service`` is never the same
        object as ``deps.calendar_group_service``'s internal ``calendar_service``)
        makes ``createCalendarGroupEvent`` fail for EVERY caller over real HTTP right
        now, authenticated or not. Fixing that bug is explicitly out of scope for
        this security hotfix. What this test pins down is that the failure is that
        known DI error -- not an organization-mismatch rejection -- which is what
        would prove the org check is over-broad and blocking legitimate same-org
        traffic.
        """
        group, slot = org_a_group_with_bookable_slot
        calendar_id = slot.memberships.get().calendar_fk_id
        post, _system_user, org_a = org_a_client

        response = post(
            CREATE_CALENDAR_GROUP_EVENT_MUTATION,
            _event_variables(org_a.id, group.id, slot.id, calendar_id),
        )

        assert response.status_code == 200
        body = response.json()
        result = body["data"]["createCalendarGroupEvent"]
        # Must NOT be the organization-mismatch/not-found message -- that would mean
        # a legitimate same-org caller is being wrongly rejected by this fix.
        assert result["errorMessage"] != "Organization not found"
        assert result["errorMessage"] == (
            "The injected CalendarService is not initialized with an organization."
        ), (
            "Expected the known, pre-existing DI-wiring failure message; got a "
            f"different outcome -- re-investigate. result={result!r}"
        )


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
