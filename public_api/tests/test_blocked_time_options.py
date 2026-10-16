"""Public API: block decline options on ``createBlockedTime``."""

import datetime
import uuid
from unittest.mock import Mock, patch

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import BlockedTime, Calendar, CalendarEvent, EventAttendance
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.tests.helpers import make_token_post
from users.models import User


CREATE_BLOCKED_TIME = """
mutation CreateBlockedTime($input: CreateBlockedTimeInput!) {
    createBlockedTime(input: $input) {
        success
        errorMessage
        cancelledEventCount
        failedEventCount
        blockedTime {
            id
            declineScope
        }
    }
}
"""

PUSH_TASK_DELAY = (
    "calendar_integration.tasks.blocked_time_push_tasks.push_blocked_time_to_provider_task.delay"
)
MAX_BLOCK_CANCELS = "calendar_integration.services.block_conflict_service.MAX_BLOCK_CANCELS"

# The block every test makes: 2026-10-09 10:00-12:00, wall-clock in UTC.
START = "2026-10-09T10:00:00"
END = "2026-10-09T12:00:00"


def _owner_with_calendar(
    organization: Organization,
) -> tuple[User, OrganizationMembership, Calendar]:
    unique = uuid.uuid4().hex[:8]
    owner = User.objects.create_user(email=f"owner_{unique}@example.com", password="pw")  # noqa: S106
    calendar = Calendar.objects.create(
        name="Provider calendar",
        external_id=f"owner_{unique}@example.com",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )
    create_calendar_ownership(calendar=calendar, user=owner, is_default=True)
    membership = OrganizationMembership.objects.get(user=owner, organization=organization)
    return owner, membership, calendar


def _block_input(organization, calendar, **options):
    return {
        "input": {
            "organizationId": organization.id,
            "calendarId": calendar.id,
            "startTime": START,
            "endTime": END,
            "timezone": "UTC",
            "reason": "Conference",
            **options,
        }
    }


def _event(calendar, start_hour, end_hour, external_id) -> CalendarEvent:
    return CalendarEvent.objects.create(
        calendar_fk=calendar,
        organization=calendar.organization,
        title="Visit",
        start_time_tz_unaware=datetime.datetime(2026, 10, 9, start_hour, 0),
        end_time_tz_unaware=datetime.datetime(2026, 10, 9, end_hour, 0),
        timezone="UTC",
        external_id=external_id,
    )


def _events_around_the_block(organization, owner, calendar) -> tuple[CalendarEvent, CalendarEvent]:
    """Two events on ``calendar`` inside the block, one after it, and one the owner
    attends on a colleague's calendar. Returns the one after the block and the colleague's."""
    _event(calendar, 10, 11, "first")
    _event(calendar, 11, 12, "second")
    after = _event(calendar, 12, 13, "after-the-block")
    _colleague, _colleague_membership, colleague_calendar = _owner_with_calendar(organization)
    elsewhere = _event(colleague_calendar, 10, 11, "colleagues-visit")
    EventAttendance.objects.create(
        event=elsewhere, membership_user_id=owner.id, organization=organization
    )
    return after, elsewhere


@pytest.fixture
def adapter():
    write_adapter = Mock()
    with patch.object(
        CalendarService, "_get_write_adapter_for_calendar", return_value=write_adapter
    ):
        yield write_adapter


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization, name="Public API Org")


CREATE_AND_CANCEL = [PublicAPIResources.CREATE_BLOCKED_TIME, PublicAPIResources.CALENDAR_EVENT]


@pytest.mark.django_db
class TestCreateBlockedTimeOptions:
    def test_options_are_stored_and_returned(self, organization, adapter):
        _owner, membership, calendar = _owner_with_calendar(organization)
        post = make_token_post(organization, CREATE_AND_CANCEL, scoped_to_membership=membership)
        visit = _event(calendar, 10, 11, "visit")

        with patch(PUSH_TASK_DELAY):
            data = post(
                CREATE_BLOCKED_TIME,
                _block_input(
                    organization,
                    calendar,
                    declineScope="NEW",
                    declineMessage="Back on Monday.",
                ),
            )

        result = data["data"]["createBlockedTime"]
        assert result == {
            "success": True,
            "errorMessage": None,
            "cancelledEventCount": 0,
            "failedEventCount": 0,
            "blockedTime": {
                "id": result["blockedTime"]["id"],
                "declineScope": "NEW",
            },
        }
        block = BlockedTime.objects.filter_by_organization(organization).get(
            id=int(result["blockedTime"]["id"])
        )
        assert (block.decline_scope, block.decline_message) == ("new", "Back on Monday.")
        # NEW turns away only new invitations: the existing visit stays.
        assert (
            CalendarEvent.objects.filter_by_organization(organization).filter(id=visit.id).exists()
        )

    def test_old_shape_call_is_unchanged_and_queues_nothing(
        self, organization, adapter, django_capture_on_commit_callbacks
    ):
        _owner, membership, calendar = _owner_with_calendar(organization)
        post = make_token_post(
            organization, [PublicAPIResources.CREATE_BLOCKED_TIME], scoped_to_membership=membership
        )
        visit = _event(calendar, 10, 11, "visit")

        with patch(PUSH_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
            data = post(CREATE_BLOCKED_TIME, _block_input(organization, calendar))

        result = data["data"]["createBlockedTime"]
        assert result["success"] is True
        assert result["blockedTime"]["declineScope"] is None
        assert (result["cancelledEventCount"], result["failedEventCount"]) == (0, 0)
        block = BlockedTime.objects.filter_by_organization(organization).get(
            id=int(result["blockedTime"]["id"])
        )
        assert (block.decline_scope, block.provider_push_status) == (None, None)
        delay.assert_not_called()
        adapter.delete_event.assert_not_called()
        assert (
            CalendarEvent.objects.filter_by_organization(organization).filter(id=visit.id).exists()
        )

    def test_new_and_existing_cancels_only_the_calendars_events(self, organization, adapter):
        owner, membership, calendar = _owner_with_calendar(organization)
        post = make_token_post(organization, CREATE_AND_CANCEL, scoped_to_membership=membership)
        kept, elsewhere = _events_around_the_block(organization, owner, calendar)

        with patch(PUSH_TASK_DELAY):
            data = post(
                CREATE_BLOCKED_TIME,
                _block_input(organization, calendar, declineScope="NEW_AND_EXISTING"),
            )

        result = data["data"]["createBlockedTime"]
        assert result["success"] is True
        assert (result["cancelledEventCount"], result["failedEventCount"]) == (2, 0)
        remaining = set(
            CalendarEvent.objects.filter_by_organization(organization).values_list("id", flat=True)
        )
        assert remaining == {kept.id, elsewhere.id}
        assert {c.args[1] for c in adapter.delete_event.call_args_list} == {"first", "second"}
        assert {c.kwargs["send_updates"] for c in adapter.delete_event.call_args_list} == {"all"}

    def test_new_and_existing_needs_the_calendar_event_resource(self, organization, adapter):
        _owner, membership, calendar = _owner_with_calendar(organization)
        post = make_token_post(
            organization, [PublicAPIResources.CREATE_BLOCKED_TIME], scoped_to_membership=membership
        )
        visit = _event(calendar, 10, 11, "visit")

        data = post(
            CREATE_BLOCKED_TIME,
            _block_input(organization, calendar, declineScope="NEW_AND_EXISTING"),
        )

        result = data["data"]["createBlockedTime"]
        assert (result["success"], result["errorMessage"]) == (
            False,
            "This token may not cancel events.",
        )
        assert not BlockedTime.objects.filter_by_organization(organization).exists()
        assert (
            CalendarEvent.objects.filter_by_organization(organization).filter(id=visit.id).exists()
        )

    @pytest.mark.parametrize(
        ("options", "error_message"),
        [
            pytest.param(
                {"declineScope": "NEW", "declineMessage": "x" * 1001},
                "The decline message must be at most 1000 characters.",
                id="overlong-message",
            ),
            pytest.param(
                {"declineScope": "NEW", "rruleString": "FREQ=WEEKLY;BYDAY=FR"},
                "Decline options cannot be used on a recurring blocked time. "
                "Remove the recurrence rule or the decline options.",
                id="recurrence-rule",
            ),
            pytest.param(
                {"declineMessage": "Back on Monday."},
                "A declineMessage needs a declineScope.",
                id="message-without-scope",
            ),
            pytest.param(
                {"declineScope": "NEW_AND_EXISTING"},
                "This block would cancel 2 events, more than the 1 one block may cancel. "
                "Block a shorter period.",
                id="too-many-cancels",
            ),
        ],
    )
    def test_invalid_options_are_refused_and_write_nothing(
        self, organization, adapter, options, error_message
    ):
        _owner, membership, calendar = _owner_with_calendar(organization)
        post = make_token_post(organization, CREATE_AND_CANCEL, scoped_to_membership=membership)
        _event(calendar, 10, 11, "first")
        _event(calendar, 11, 12, "second")

        with patch(MAX_BLOCK_CANCELS, 1), patch(PUSH_TASK_DELAY) as delay:
            data = post(CREATE_BLOCKED_TIME, _block_input(organization, calendar, **options))

        result = data["data"]["createBlockedTime"]
        assert (result["success"], result["errorMessage"]) == (False, error_message)
        assert not BlockedTime.objects.filter_by_organization(organization).exists()
        assert CalendarEvent.objects.filter_by_organization(organization).count() == 2
        adapter.delete_event.assert_not_called()
        delay.assert_not_called()

    @pytest.mark.parametrize("field", ["reason", "declineMessage"])
    def test_blocked_time_does_not_expose_free_text(self, organization, adapter, field):
        _owner, membership, calendar = _owner_with_calendar(organization)
        post = make_token_post(organization, CREATE_AND_CANCEL, scoped_to_membership=membership)

        data = post(
            CREATE_BLOCKED_TIME.replace("declineScope\n        }", f"{field}\n        }}"),
            _block_input(organization, calendar),
        )

        assert data["errors"][0]["message"].startswith(
            f"Cannot query field '{field}' on type 'BlockedTimeGraphQLType'."
        )
        assert not BlockedTime.objects.filter_by_organization(organization).exists()
