"""``BlockConflictService``: the events a new block overlaps, and cancelling them."""

import datetime
from unittest.mock import Mock, patch

from django.db import IntegrityError, transaction

import pytest
from googleapiclient.errors import HttpError
from model_bakery import baker

from calendar_integration.constants import (
    BlockedTimeDeclineScope,
    CalendarProvider,
    CalendarType,
    ProviderPushStatus,
)
from calendar_integration.exceptions import (
    BlockDeclineOptionsWithRecurrenceError,
)
from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import (
    AppointmentType,
    BlockedTime,
    Calendar,
    CalendarEvent,
    EventRecurrenceException,
    RecurrenceRule,
)
from calendar_integration.services.block_conflict_service import (
    BlockConflictEvent,
    BlockConflictService,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import BlockDeclineOptions
from organizations.models import Organization, OrganizationMembership
from public_api.services import PublicAPIAuthService
from users.models import User


# The block every test asks about: 2026-10-09 10:00-12:00 UTC.
BLOCK_START = datetime.datetime(2026, 10, 9, 10, 0)
BLOCK_END = datetime.datetime(2026, 10, 9, 12, 0)


@pytest.fixture
def calendar(organization: Organization, owner: User) -> Calendar:
    blocked_calendar = Calendar.objects.create(
        name="Provider calendar",
        external_id="provider@example.com",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )
    create_calendar_ownership(calendar=blocked_calendar, user=owner, is_default=True)
    return blocked_calendar


def _event(
    calendar: Calendar,
    start: datetime.datetime,
    end: datetime.datetime,
    external_id: str,
    **fields,
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        calendar_fk=calendar,
        organization=calendar.organization,
        title="Visit",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        external_id=external_id,
        **fields,
    )


def _find(calendar: Calendar) -> list[BlockConflictEvent]:
    return BlockConflictService.find_events_to_cancel(
        calendar, BLOCK_START.replace(tzinfo=datetime.UTC), BLOCK_END.replace(tzinfo=datetime.UTC)
    )


def _ids(conflicts: list[BlockConflictEvent]) -> list[tuple[int, datetime.datetime | None]]:
    return [(c.event.id, c.occurrence_start) for c in conflicts]


# ---------------------------------------------------------------------------
# find_events_to_cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_finds_overlapping_events_but_not_ones_touching_the_edges(calendar):
    inside = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 30),
        datetime.datetime(2026, 10, 9, 11, 0),
        "inside",
    )
    partly = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 11, 30),
        datetime.datetime(2026, 10, 9, 12, 30),
        "partly",
    )
    around = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 9, 0),
        datetime.datetime(2026, 10, 9, 13, 0),
        "around",
    )
    _event(
        calendar,
        datetime.datetime(2026, 10, 9, 9, 0),
        datetime.datetime(2026, 10, 9, 10, 0),
        "ends-at-start",
    )
    _event(
        calendar,
        datetime.datetime(2026, 10, 9, 12, 0),
        datetime.datetime(2026, 10, 9, 13, 0),
        "starts-at-end",
    )

    assert sorted(_ids(_find(calendar))) == sorted(
        [(inside.id, None), (partly.id, None), (around.id, None)]
    )


# ---------------------------------------------------------------------------
# cancel_block_conflicts
# ---------------------------------------------------------------------------


@pytest.fixture
def owner_token_services(organization, owner, calendar):
    """Calendar and appointment-type services acting as the owner's scoped API token."""
    membership = OrganizationMembership.objects.get(user=owner, organization=organization)
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="owner-scoped", organization=organization, scoped_to_membership=membership
    )
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(
        user_or_token=system_user, organization=organization
    )
    appointment_type_service = Mock()
    return calendar_service, appointment_type_service


def test_initialize_binds_every_calendar_service_to_the_caller(organization):
    calendar_service, appointment_type_service = Mock(), Mock()
    service = BlockConflictService(
        calendar_service=calendar_service, appointment_type_service=appointment_type_service
    )

    service.initialize(user_or_token="token", organization=organization)

    calendar_service.initialize_without_provider.assert_called_once_with(
        user_or_token="token", organization=organization
    )
    appointment_type_service.calendar_service.initialize_without_provider.assert_called_once_with(
        user_or_token="token", organization=organization
    )
    appointment_type_service.initialize.assert_called_once_with(organization=organization)


def _cancel(calendar, services):
    calendar_service, appointment_type_service = services
    service = BlockConflictService(
        calendar_service=calendar_service, appointment_type_service=appointment_type_service
    )
    return service.cancel_block_conflicts(_find(calendar))


@pytest.mark.django_db
def test_cancel_of_an_occurrence_keeps_the_series(calendar, owner_token_services, write_adapter):
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", calendar.organization)
    rule.save()
    master = _event(
        calendar,
        datetime.datetime(2026, 10, 7, 10, 30),
        datetime.datetime(2026, 10, 7, 11, 0),
        "series",
        recurrence_rule=rule,
    )

    result = _cancel(calendar, owner_token_services)

    assert result.cancelled_event_count == 1
    assert (
        CalendarEvent.objects.filter_by_organization(calendar.organization)
        .filter(id=master.id)
        .exists()
    )
    exception = EventRecurrenceException.objects.filter_by_organization(calendar.organization).get(
        parent_event_fk=master
    )
    assert exception.is_cancelled is True
    assert exception.exception_date == datetime.datetime(2026, 10, 9, 10, 30, tzinfo=datetime.UTC)
    write_adapter.delete_event.assert_not_called()


@pytest.mark.django_db
def test_cancel_sends_appointment_type_events_through_their_service(
    calendar, owner_token_services, write_adapter
):
    appointment_type = baker.make(AppointmentType, organization=calendar.organization)
    visit = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 0),
        datetime.datetime(2026, 10, 9, 11, 0),
        "appointment",
        appointment_type=appointment_type,
    )
    _calendar_service, appointment_type_service = owner_token_services

    result = _cancel(calendar, owner_token_services)

    assert result.cancelled_event_count == 1
    appointment_type_service.cancel_appointment_type_event.assert_called_once_with(
        event_id=visit.id, send_updates="all"
    )
    write_adapter.delete_event.assert_not_called()


@pytest.mark.django_db
def test_one_failed_cancel_leaves_the_rest(calendar, owner_token_services, write_adapter):
    def delete_event(calendar_id, event_id, send_updates=None):
        if event_id == "bad":
            raise HttpError(Mock(status=500, reason="error"), b"{}")

    write_adapter.delete_event.side_effect = delete_event
    bad = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 0),
        datetime.datetime(2026, 10, 9, 11, 0),
        "bad",
    )
    good = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 11, 0),
        datetime.datetime(2026, 10, 9, 12, 0),
        "good",
    )

    result = _cancel(calendar, owner_token_services)

    assert (result.cancelled_event_count, result.failed_event_count) == (1, 1)
    remaining = CalendarEvent.objects.filter_by_organization(calendar.organization)
    assert list(remaining.values_list("id", flat=True)) == [bad.id]
    assert good.id not in remaining.values_list("id", flat=True)


# ---------------------------------------------------------------------------
# create_block
# ---------------------------------------------------------------------------

PUSH_TASK_DELAY = (
    "calendar_integration.tasks.blocked_time_push_tasks.push_blocked_time_to_provider_task.delay"
)


def _create_block(calendar, services, decline_options, timezone="UTC", rrule_string=None):
    calendar_service, appointment_type_service = services
    return BlockConflictService(
        calendar_service=calendar_service, appointment_type_service=appointment_type_service
    ).create_block(
        calendar=calendar,
        start_time=BLOCK_START,
        end_time=BLOCK_END,
        timezone=timezone,
        reason="Conference",
        rrule_string=rrule_string,
        decline_options=decline_options,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields",
    [
        {"decline_message": "Back on Monday."},
        {"provider_push_status": ProviderPushStatus.PENDING},
        {"provider_event_external_id": "0123456789abcdef"},
    ],
    ids=["message", "push-status", "provider-event"],
)
def test_the_database_refuses_block_options_without_a_decline_scope(calendar, fields):
    with pytest.raises(IntegrityError), transaction.atomic():
        BlockedTime.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            start_time_tz_unaware=BLOCK_START,
            end_time_tz_unaware=BLOCK_END,
            timezone="UTC",
            reason="Conference",
            external_id="manual-check",
            **fields,
        )


@pytest.mark.django_db
def test_create_block_with_options_and_a_recurrence_rule_writes_nothing(
    calendar, owner_token_services, write_adapter
):
    _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 30),
        datetime.datetime(2026, 10, 9, 11, 0),
        "inside",
    )

    with patch(PUSH_TASK_DELAY) as delay, pytest.raises(BlockDeclineOptionsWithRecurrenceError):
        _create_block(
            calendar,
            owner_token_services,
            BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW_AND_EXISTING),
            rrule_string="FREQ=WEEKLY;BYDAY=FR",
        )

    assert not BlockedTime.objects.filter_by_organization(calendar.organization).exists()
    assert CalendarEvent.objects.filter_by_organization(calendar.organization).count() == 1
    write_adapter.delete_event.assert_not_called()
    delay.assert_not_called()
