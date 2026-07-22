"""Integration tests for createCalendarGroupEventWithCode.

All requests are unauthenticated (no Authorization header).  The booking
code provides the org scope, group scope, and CREATE permission.

Scenario coverage:
1. Happy path on a RESTRICTED primary calendar (accepts_public_scheduling=False)
   → event created, code consumed.  This verifies the can_perform_scheduling fix
   that authorizes group-scoped tokens against member calendars.
2. Replay — same code again → ALREADY_USED, no second event.
3. Failed write (slot outside availability) → SLOT_UNAVAILABLE, code NOT consumed.
4. Calendar-scoped code (no group) → NOT_PERMITTED.
5. Missing CREATE permission → NOT_PERMITTED.
6. Lifecycle rejections — expired / revoked / invalid → respective error codes.
7. Cross-org:
   a. Org-A code with org-A slot selections books ONLY org-A resources (event.organization_id == org A).
   b. Org-A code with an org-B calendar id in slot_selections → rejected (SLOT_UNAVAILABLE),
      no event created, code NOT consumed.
"""

import datetime
from unittest.mock import patch

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
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


# ---------------------------------------------------------------------------
# GraphQL mutation string
# ---------------------------------------------------------------------------

CREATE_GROUP_EVENT_WITH_CODE = """
mutation CreateCalendarGroupEventWithCode($input: CreateGroupEventWithCodeInput!) {
    createCalendarGroupEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event {
            id
            title
            externalAttendances {
                externalAttendee {
                    email
                    name
                }
            }
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="Group-Book-With-Code Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def primary_calendar(organization):
    """The primary (first-slot) calendar.  RESTRICTED: accepts_public_scheduling=False.

    Using a restricted calendar ensures the test exercises the new group-scoped
    branch of can_perform_scheduling.  A public calendar would mask the fix.
    """
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar (Dr. A)",
        external_id="primary-cal-group-code-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,  # RESTRICTED — must fail without the fix
    )


@pytest.fixture
def secondary_calendar(organization):
    """A second calendar belonging to slot 2."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Room Calendar",
        external_id="room-cal-group-code-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup with two slots: slot_a (primary_calendar) and slot_b (secondary_calendar)."""
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization,
        group=grp,
        name="Physicians",
        order=0,
        required_count=1,
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization,
        group=grp,
        name="Rooms",
        order=1,
        required_count=1,
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization,
        slot=slot_a,
        calendar=primary_calendar,
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization,
        slot=slot_b,
        calendar=secondary_calendar,
    )
    return grp


@pytest.fixture
def availability_windows(organization, primary_calendar, secondary_calendar):
    """Availability windows for both calendars: 09:00-17:00 UTC on 2030-06-01."""
    windows = []
    for cal in (primary_calendar, secondary_calendar):
        windows.append(
            AvailableTime.objects.create(
                organization=organization,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
                timezone="UTC",
            )
        )
    return windows


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def group_booking_code(permission_service, organization, group):
    """A valid single-use CREATE code scoped to the group."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.fixture
def calendar_scoped_code(permission_service, organization, primary_calendar):
    """A CREATE code scoped to a single calendar — wrong scope for group mutation."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=primary_calendar.id,
    )
    return token, code


@pytest.fixture
def reschedule_group_code(permission_service, organization, group):
    """A RESCHEDULE-only group code — wrong permission for booking."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)


def _group_booking_input(
    code: str,
    slot_selections: list[dict],
    **overrides,
) -> dict:
    """Build the default happy-path group mutation input dict."""
    base = {
        "code": code,
        "title": "Group Appointment",
        "description": "A group booking",
        "startTime": BOOKING_START.isoformat(),
        "endTime": BOOKING_END.isoformat(),
        "timezone": "UTC",
        "slotSelections": slot_selections,
        "externalAttendee": {
            "email": "patient@example.com",
            "name": "Pat Patient",
        },
    }
    base.update(overrides)
    return base


def post_graphql(client: APIClient, query: str, variables: dict) -> dict:
    response = client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )
    assert response.status_code == 200, response.content.decode()
    return response.json()


def _slot_selections(
    group: CalendarGroup, primary_calendar: Calendar, secondary_calendar: Calendar
):
    """Build slot selections that pick one calendar per slot."""
    slot_a = group.slots.get(name="Physicians")
    slot_b = group.slots.get(name="Rooms")
    return [
        {"slotId": slot_a.id, "calendarIds": [primary_calendar.id]},
        {"slotId": slot_b.id, "calendarIds": [secondary_calendar.id]},
    ]


# ---------------------------------------------------------------------------
# Scenario 1: Happy path on RESTRICTED primary calendar
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeHappyPath:
    """Scenario 1: Valid group code + restricted primary calendar + available slot → success.

    This is the critical test that validates the can_perform_scheduling fix.  With a
    RESTRICTED primary calendar (accepts_public_scheduling=False) and a group-scoped
    token, the fix must authorize via the group-membership branch.  Before the fix
    this test fails with NOT_PERMITTED / PermissionDenied.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_creates_grouped_event_and_consumes_code(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002 — seeds DB rows
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is True, result
        assert result["errorCode"] is None
        assert result["event"] is not None
        assert result["event"]["title"] == "Group Appointment"

        # Code must be consumed.
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        # The event must exist in the DB, on the primary calendar, linked to the group.
        event_id = int(result["event"]["id"])
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
        assert event.calendar_fk_id == primary_calendar.id
        assert event.calendar_group_fk_id == group.id
        assert event.organization_id == organization.id

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_event_has_external_attendee(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        """The created event carries the external attendee supplied in the input."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is True, result
        event_id = int(result["event"]["id"])
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
        external_attendances = list(event.external_attendances.select_related("external_attendee"))
        assert len(external_attendances) == 1
        assert external_attendances[0].external_attendee.email == "patient@example.com"
        assert external_attendances[0].external_attendee.name == "Pat Patient"


# ---------------------------------------------------------------------------
# Scenario 2: Replay
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeReplay:
    """Scenario 2: Replay with same code → ALREADY_USED, no second event."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_replay_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)
        input_data = _group_booking_input(code, selections)

        # First call — must succeed.
        first = post_graphql(anon_client, CREATE_GROUP_EVENT_WITH_CODE, {"input": input_data})
        assert first["data"]["createCalendarGroupEventWithCode"]["success"] is True

        # Second call — same code must return ALREADY_USED.
        second = post_graphql(anon_client, CREATE_GROUP_EVENT_WITH_CODE, {"input": input_data})
        result = second["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"

        # Only one event should have been created.
        event_count = CalendarEvent.objects.filter_by_organization(organization.id).count()
        assert event_count == 1


# ---------------------------------------------------------------------------
# Scenario 3: Failed write does not consume
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeFailedWriteDoesNotConsume:
    """Scenario 3: Failed write (SLOT_UNAVAILABLE) leaves the code active for retry."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_slot_outside_availability_does_not_consume_code(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002 — window is 09:00-17:00 UTC
    ):
        """Booking outside availability window → SLOT_UNAVAILABLE, code stays active."""
        mock_rate_limiter.return_value = iter([None])
        token, code = group_booking_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        # 22:00-23:00 UTC is outside the 09:00-17:00 window.
        out_of_window_input = _group_booking_input(
            code,
            selections,
            startTime=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            endTime=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": out_of_window_input},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert result["errorMessage"] == "The requested time slot is not available."

        # Code must still be unused.
        token.refresh_from_db()
        assert token.used_at is None

        # No event must have been created.
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_slot_selection_does_not_consume_code(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        availability_windows,  # noqa: ARG002
    ):
        """An invalid slot selection (non-existent slot_id) returns SLOT_UNAVAILABLE
        and leaves the code active."""
        mock_rate_limiter.return_value = iter([None])
        token, code = group_booking_code

        # Use a bogus slot_id.
        bad_selections = [
            {"slotId": 999999, "calendarIds": [primary_calendar.id]},
        ]

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, bad_selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

        # Code must still be unused.
        token.refresh_from_db()
        assert token.used_at is None


# ---------------------------------------------------------------------------
# Scenario 4: Calendar-scoped code → NOT_PERMITTED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeCalendarScopedCode:
    """Scenario 4: Calendar-scoped code (token.calendar set, no group) → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_calendar_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        calendar_scoped_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_scoped_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # No event should have been created.
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 5: Missing CREATE permission → NOT_PERMITTED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeMissingPermission:
    """Scenario 5: Code without CREATE permission → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_reschedule_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_group_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = reschedule_group_code
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # No event should have been created.
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 6: Lifecycle rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeLifecycleRejections:
    """Scenario 6: Expired / revoked / invalid codes are rejected correctly."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_expired_code_returns_expired(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
            expires_at=past,
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "EXPIRED"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_revoked_code_returns_revoked(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "REVOKED"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_invalid_code(
        self,
        mock_rate_limiter,
        anon_client,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input("aW52YWxpZGJvb2tpbmdjb2Rl", selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_used_code_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=group.id,
        )
        CalendarManagementToken.objects.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )
        selections = _slot_selections(group, primary_calendar, secondary_calendar)

        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"


# ---------------------------------------------------------------------------
# Scenario 7: Cross-org
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateGroupEventWithCodeCrossOrg:
    """Scenario 7: Org isolation exercised, not just code-read.

    7a — Org-A code + org-A slot selections → success, event.organization_id == org A.
         org-B group/calendars exist in the DB but are untouched.

    7b — Org-A code + org-B calendar id injected into slot_selections → rejected
         (SLOT_UNAVAILABLE), no event created, code NOT consumed.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_org_a_code_books_only_org_a_resources(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        availability_windows,  # noqa: ARG002 — seeds org-A calendar windows
    ):
        """7a: Org-A code with org-A slot selections books in org A.

        Org B's group, slots, calendars, and availability windows are created in the DB
        to confirm they don't interfere.  After booking we verify event.organization_id
        equals org A and no event rows exist in org B.
        """
        mock_rate_limiter.return_value = iter([None])

        # --- build org A (uses shared fixtures already created) ---
        org_a = organization
        grp_a = baker.make(CalendarGroup, organization=org_a, name="Org-A Group")
        cal_a1 = baker.make(
            Calendar,
            organization=org_a,
            external_id="cross-org-test-a1",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )
        cal_a2 = baker.make(
            Calendar,
            organization=org_a,
            external_id="cross-org-test-a2",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )
        slot_a1 = CalendarGroupSlot.objects.create(
            organization=org_a, group=grp_a, name="A-Physicians", order=0, required_count=1
        )
        slot_a2 = CalendarGroupSlot.objects.create(
            organization=org_a, group=grp_a, name="A-Rooms", order=1, required_count=1
        )
        CalendarGroupSlotMembership.objects.create(
            organization=org_a, slot=slot_a1, calendar=cal_a1
        )
        CalendarGroupSlotMembership.objects.create(
            organization=org_a, slot=slot_a2, calendar=cal_a2
        )
        for cal in (cal_a1, cal_a2):
            AvailableTime.objects.create(
                organization=org_a,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
                timezone="UTC",
            )

        # --- build org B (distinct DB rows; group has same structural shape) ---
        org_b = baker.make(Organization, name="Org-B (cross-org target)")
        grp_b = baker.make(CalendarGroup, organization=org_b, name="Org-B Group")
        cal_b1 = baker.make(
            Calendar,
            organization=org_b,
            external_id="cross-org-test-b1",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )
        slot_b1 = CalendarGroupSlot.objects.create(
            organization=org_b, group=grp_b, name="B-Physicians", order=0, required_count=1
        )
        CalendarGroupSlotMembership.objects.create(
            organization=org_b, slot=slot_b1, calendar=cal_b1
        )
        AvailableTime.objects.create(
            organization=org_b,
            calendar=cal_b1,
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
            timezone="UTC",
        )

        # --- mint an org-A group code ---
        _token, code = permission_service.create_booking_token(
            organization_id=org_a.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=grp_a.id,
        )

        # --- call mutation with org-A slot selections ---
        selections = [
            {"slotId": slot_a1.id, "calendarIds": [cal_a1.id]},
            {"slotId": slot_a2.id, "calendarIds": [cal_a2.id]},
        ]
        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is True, result

        # Event must live in org A
        event_id = int(result["event"]["id"])
        event = CalendarEvent.objects.filter_by_organization(org_a.id).get(id=event_id)
        assert event.organization_id == org_a.id
        assert event.calendar_group_fk_id == grp_a.id

        # No events exist in org B
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_org_a_code_cannot_book_org_b_calendar(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        availability_windows,  # noqa: ARG002
    ):
        """7b: Org-A code + org-B calendar id in slot_selections → rejected, code NOT consumed.

        This proves the org isolation guard: the slot_selection passes an org-B calendar id
        to an org-A group booking code.  The server must reject it (SLOT_UNAVAILABLE, since
        the foreign calendar is not a member of the org-A group's slots) and must NOT consume
        the code — so the patient could theoretically retry.
        """
        mock_rate_limiter.return_value = iter([None])

        # --- build org A ---
        org_a = organization
        grp_a = baker.make(CalendarGroup, organization=org_a, name="Org-A Group")
        cal_a = baker.make(
            Calendar,
            organization=org_a,
            external_id="cross-org-b-test-a",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )
        slot_a = CalendarGroupSlot.objects.create(
            organization=org_a, group=grp_a, name="A-Physicians", order=0, required_count=1
        )
        CalendarGroupSlotMembership.objects.create(organization=org_a, slot=slot_a, calendar=cal_a)
        AvailableTime.objects.create(
            organization=org_a,
            calendar=cal_a,
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
            timezone="UTC",
        )

        # --- build org B with its own calendar ---
        org_b = baker.make(Organization, name="Org-B (cross-org attacker)")
        cal_b = baker.make(
            Calendar,
            organization=org_b,
            external_id="cross-org-b-test-b",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )

        # --- mint an org-A group code ---
        token, code = permission_service.create_booking_token(
            organization_id=org_a.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=grp_a.id,
        )

        # --- inject org-B's calendar id into the slot selection for org-A's slot ---
        bad_selections = [
            {"slotId": slot_a.id, "calendarIds": [cal_b.id]},
        ]
        data = post_graphql(
            anon_client,
            CREATE_GROUP_EVENT_WITH_CODE,
            {"input": _group_booking_input(code, bad_selections)},
        )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

        # Code must NOT have been consumed
        token.refresh_from_db()
        assert token.used_at is None

        # No event must exist in either org
        assert not CalendarEvent.objects.filter_by_organization(org_a.id).exists()
        assert not CalendarEvent.objects.filter_by_organization(org_b.id).exists()
