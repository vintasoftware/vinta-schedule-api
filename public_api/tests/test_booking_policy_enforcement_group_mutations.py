"""Integration tests for group booking policy enforcement at the mutation layer.

Coverage:
- ``createCalendarGroupEvent`` (authenticated path):
  - Lead-time / horizon / buffer violations → error_message with explanatory text, no rows.
  - Compliant booking → succeeds.
  - No policy → write unchanged (off-state).

- ``createCalendarGroupEventWithCode`` (code-gated path):
  - Policy violation → SLOT_UNAVAILABLE with explanatory message, code NOT consumed, no rows.
  - Compliant booking → succeeds.
  - No policy → write unchanged (off-state).
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import patch

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    EventManagementPermissions,
)
from calendar_integration.mutations import (
    CalendarGroupEventInput,
    CalendarGroupMutationDependencies,
    CalendarGroupMutations,
    CalendarGroupSlotSelectionInput,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixed instants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_START = datetime.datetime(2030, 6, 1, 10, 0, 0, tzinfo=datetime.UTC)
_END = datetime.datetime(2030, 6, 1, 11, 0, 0, tzinfo=datetime.UTC)

_counter = 0


def _unique() -> int:
    global _counter
    _counter += 1
    return _counter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org(suffix: str = "") -> Organization:
    return Organization.objects.create(
        name=f"GrpPolicyEnforce {suffix} {uuid.uuid4().hex[:6]}", should_sync_rooms=False
    )


def _managed_cal(org: Organization) -> Calendar:
    n = _unique()
    cal = Calendar.objects.create(
        organization=org,
        name=f"grp-policy-cal-{n}",
        external_id=f"grp-policy-ext-{n}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )
    # Seed availability covering the test slot.
    AvailableTime.objects.create(
        organization=org,
        calendar=cal,
        start_time_tz_unaware=_START - datetime.timedelta(hours=2),
        end_time_tz_unaware=_END + datetime.timedelta(hours=2),
        timezone="UTC",
    )
    return cal


def _make_group_and_slot(
    org: Organization, calendar: Calendar
) -> tuple[CalendarGroup, CalendarGroupSlot]:
    """Create a single-slot group containing *calendar* and return (group, slot)."""
    group = CalendarGroup.objects.create(
        organization=org,
        name=f"grp-enforce-{_unique()}",
        accepts_public_scheduling=True,
    )
    slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Main", order=0)
    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)
    return group, slot


def _real_deps(org: Organization) -> CalendarGroupMutationDependencies:
    """Build real (non-mocked) CalendarGroupMutationDependencies with policy support."""
    cs = CalendarService(booking_policy_service=BookingPolicyService())
    cs.initialize_without_provider(user_or_token=None, organization=org)
    gs = CalendarGroupService(
        calendar_service=cs,
        booking_policy_service=BookingPolicyService(),
    )
    gs.initialize(organization=org)
    return CalendarGroupMutationDependencies(calendar_group_service=gs, calendar_service=cs)


def _invoke_mutation(
    org: Organization,
    group: CalendarGroup,
    slot: CalendarGroupSlot,
    calendar: Calendar,
    deps: CalendarGroupMutationDependencies,
) -> object:
    """Invoke the createCalendarGroupEvent mutation directly (no HTTP)."""
    mutations = CalendarGroupMutations()
    input_data = CalendarGroupEventInput(
        organization_id=org.id,
        group_id=group.id,
        title="Test Group Booking",
        description="",
        start_time=_START,
        end_time=_END,
        timezone="UTC",
        slot_selections=[
            CalendarGroupSlotSelectionInput(slot_id=slot.id, calendar_ids=[calendar.id])
        ],
    )
    # Patch the dependency factory so our real-deps (with policy) are used.
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        return mutations.create_calendar_group_event(input_data)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests — createCalendarGroupEvent (authenticated path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarGroupEventPolicyEnforcement:
    """Policy enforcement via the authenticated ``createCalendarGroupEvent`` mutation."""

    def test_no_policy_write_unchanged(self):
        """Off-state: no BookingPolicy → mutation succeeds exactly as before."""
        org = _org("auth-off-state")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)
        deps = _real_deps(org)

        result = _invoke_mutation(org, group, slot, cal, deps)
        assert result.success is True  # type: ignore[attr-defined]
        assert result.event is not None  # type: ignore[attr-defined]

    def test_lead_time_violation_returns_error_message(self):
        """Lead-time violation → success=False with an explanatory error message."""
        org = _org("auth-lead")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)

        lead_s = int((_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar_group=group, lead_time_seconds=lead_s)
        deps = _real_deps(org)

        with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
            mock_tz.now.return_value = _NOW
            result = _invoke_mutation(org, group, slot, cal, deps)

        assert result.success is False  # type: ignore[attr-defined]
        assert result.error_message is not None  # type: ignore[attr-defined]
        msg = result.error_message.lower()  # type: ignore[attr-defined]
        assert "booking policy" in msg or "not available" in msg

        # No rows persisted.
        assert CalendarEvent.objects.filter_by_organization(org.id).count() == 0
        assert BlockedTime.objects.filter_by_organization(org.id).count() == 0
        assert CalendarEventGroupSelection.objects.filter_by_organization(org.id).count() == 0

    def test_horizon_violation_returns_error_message(self):
        """Max-horizon violation → success=False with an explanatory error message."""
        org = _org("auth-horizon")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)

        horizon_s = int((_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar_group=group, max_horizon_seconds=horizon_s)
        deps = _real_deps(org)

        with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
            mock_tz.now.return_value = _NOW
            result = _invoke_mutation(org, group, slot, cal, deps)

        assert result.success is False  # type: ignore[attr-defined]
        assert result.error_message is not None  # type: ignore[attr-defined]

        # No rows persisted.
        assert CalendarEvent.objects.filter_by_organization(org.id).count() == 0
        assert CalendarEventGroupSelection.objects.filter_by_organization(org.id).count() == 0

    def test_buffer_violation_returns_error_message(self):
        """Buffer dead-zone violation → success=False, no rows persisted."""
        org = _org("auth-buffer")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)

        # Existing event ends at 09:00; buffer_after = 3601s → dead zone [09:00, 10:00:01).
        CalendarEvent.objects.create(
            organization=org,
            calendar_fk=cal,
            title="Busy",
            description="",
            external_id=f"busy-{uuid.uuid4().hex[:6]}",
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 8, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        create_booking_policy(calendar_group=group, buffer_after_seconds=3601)
        deps = _real_deps(org)

        with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
            mock_tz.now.return_value = _NOW
            result = _invoke_mutation(org, group, slot, cal, deps)

        assert result.success is False  # type: ignore[attr-defined]
        assert result.error_message is not None  # type: ignore[attr-defined]

        # Only the pre-existing busy event — no new event.
        assert (
            CalendarEvent.objects.filter_by_organization(org.id)
            .filter(title="Test Group Booking")
            .count()
            == 0
        )
        assert CalendarEventGroupSelection.objects.filter_by_organization(org.id).count() == 0

    def test_compliant_booking_succeeds(self):
        """A booking satisfying all policy rules succeeds."""
        org = _org("auth-compliant")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)
        # Zero lead time = no constraint.
        create_booking_policy(calendar_group=group, lead_time_seconds=0)
        deps = _real_deps(org)

        result = _invoke_mutation(org, group, slot, cal, deps)
        assert result.success is True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests — createCalendarGroupEventWithCode (code-gated path)
# ---------------------------------------------------------------------------

_CREATE_GROUP_EVENT_WITH_CODE = """
mutation CreateCalendarGroupEventWithCode($input: CreateGroupEventWithCodeInput!) {
    createCalendarGroupEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event { id }
    }
}
"""


def _group_booking_code(org: Organization, group: CalendarGroup) -> tuple[object, str]:
    """Mint a group booking code for the given group."""
    svc = CalendarPermissionService()
    token, code = svc.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group.id,
    )
    return token, code


def _post_group_code_gated(variables: dict) -> dict:
    from unittest.mock import patch as _patch

    from rest_framework.test import APIClient

    with _patch("public_api.extensions.OrganizationRateLimiter.on_execute") as mock_rl:
        mock_rl.return_value = iter([None])
        client = APIClient()
        response = client.post(
            "/graphql/",
            data={"query": _CREATE_GROUP_EVENT_WITH_CODE, "variables": variables},
            format="json",
        )
    assert response.status_code == 200
    return response.json()


def _group_code_input(
    code: str,
    group: CalendarGroup,
    slot: CalendarGroupSlot,
    calendar: Calendar,
    **overrides: object,
) -> dict:
    base: dict = {
        "code": code,
        "title": "Code Group Booking",
        "description": "",
        "startTime": _START.isoformat(),
        "endTime": _END.isoformat(),
        "timezone": "UTC",
        "externalAttendee": {"email": "patient@example.com", "name": "Patient"},
        "slotSelections": [{"slotId": slot.id, "calendarIds": [calendar.id]}],
    }
    base.update(overrides)
    return base


@pytest.mark.django_db
class TestCreateCalendarGroupEventWithCodePolicyEnforcement:
    """Policy enforcement via the code-gated ``createCalendarGroupEventWithCode`` mutation."""

    def test_no_policy_booking_succeeds_off_state(self):
        """Off-state: no BookingPolicy → code-gated group write behaves exactly as before."""
        org = _org("code-off-state")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)
        token_obj, code = _group_booking_code(org, group)

        data = _post_group_code_gated(
            variables={"input": _group_code_input(code, group, slot, cal)}
        )
        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is True
        assert result["errorCode"] is None

        # Code consumed.
        token_obj.refresh_from_db()  # type: ignore[attr-defined]
        assert token_obj.used_at is not None  # type: ignore[attr-defined]

    def test_lead_time_violation_slot_unavailable_code_not_consumed(self):
        """Lead-time violation → SLOT_UNAVAILABLE, code NOT consumed, no rows."""
        org = _org("code-lead")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)
        token_obj, code = _group_booking_code(org, group)

        lead_s = int((_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar_group=group, lead_time_seconds=lead_s)

        with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_group_code_gated(
                variables={"input": _group_code_input(code, group, slot, cal)}
            )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert (
            "booking policy" in result["errorMessage"].lower()
            or "not available" in result["errorMessage"].lower()
        )

        # Code must NOT be consumed on violation.
        token_obj.refresh_from_db()  # type: ignore[attr-defined]
        assert token_obj.used_at is None  # type: ignore[attr-defined]

        # No rows persisted.
        assert CalendarEvent.objects.filter_by_organization(org.id).count() == 0
        assert BlockedTime.objects.filter_by_organization(org.id).count() == 0
        assert CalendarEventGroupSelection.objects.filter_by_organization(org.id).count() == 0

    def test_horizon_violation_slot_unavailable_code_not_consumed(self):
        """Max-horizon violation → SLOT_UNAVAILABLE, code NOT consumed."""
        org = _org("code-horizon")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)
        token_obj, code = _group_booking_code(org, group)

        horizon_s = int((_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar_group=group, max_horizon_seconds=horizon_s)

        with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_group_code_gated(
                variables={"input": _group_code_input(code, group, slot, cal)}
            )

        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

        token_obj.refresh_from_db()  # type: ignore[attr-defined]
        assert token_obj.used_at is None  # type: ignore[attr-defined]

    def test_compliant_code_gated_booking_succeeds(self):
        """A code-gated group booking satisfying all policy rules succeeds."""
        org = _org("code-compliant")
        cal = _managed_cal(org)
        group, slot = _make_group_and_slot(org, cal)
        token_obj, code = _group_booking_code(org, group)
        create_booking_policy(calendar_group=group, lead_time_seconds=0)

        data = _post_group_code_gated(
            variables={"input": _group_code_input(code, group, slot, cal)}
        )
        result = data["data"]["createCalendarGroupEventWithCode"]
        assert result["success"] is True

        token_obj.refresh_from_db()  # type: ignore[attr-defined]
        assert token_obj.used_at is not None  # type: ignore[attr-defined]
