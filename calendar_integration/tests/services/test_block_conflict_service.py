"""``BlockConflictService``: the events a new block overlaps, and cancelling them."""

import datetime
from unittest.mock import Mock, call, patch

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
    CalendarIntegrationError,
    InvalidBlockDeclineOptionsError,
    TooManyBlockCancelsError,
)
from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import (
    AppointmentType,
    BlockedTime,
    Calendar,
    CalendarEvent,
    EventAttendance,
    EventRecurrenceException,
    RecurrenceRule,
)
from calendar_integration.services.block_conflict_service import (
    MAX_BLOCK_CANCELS,
    BlockConflictEvent,
    BlockConflicts,
    BlockConflictService,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import BlockDeclineOptions
from organizations.models import Organization, OrganizationMembership
from public_api.services import PublicAPIAuthService
from users.models import User
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration, WebhookEvent


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


@pytest.fixture
def other_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Colleague calendar",
        external_id="colleague@example.com",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


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


def _find(calendar: Calendar) -> BlockConflicts:
    return BlockConflictService().find_block_conflicts(calendar, BLOCK_START, BLOCK_END, "UTC")


def _ids(conflicts: list[BlockConflictEvent]) -> list[tuple[int, datetime.datetime | None]]:
    return [(c.event.id, c.occurrence_start) for c in conflicts]


# ---------------------------------------------------------------------------
# find_block_conflicts
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_finds_events_inside_partly_over_and_around_the_block(calendar):
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

    conflicts = _find(calendar)

    assert sorted(_ids(conflicts.events_to_cancel)) == sorted(
        [(inside.id, None), (partly.id, None), (around.id, None)]
    )
    assert conflicts.conflict_events == []


@pytest.mark.django_db
def test_events_touching_the_block_edges_are_left_out(calendar):
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

    conflicts = _find(calendar)

    assert conflicts.events_to_cancel == []
    assert conflicts.conflict_events == []


@pytest.mark.django_db
def test_a_recurring_series_yields_only_the_overlapping_occurrence(calendar):
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", calendar.organization)
    rule.save()
    master = _event(
        calendar,
        datetime.datetime(2026, 10, 7, 10, 30),
        datetime.datetime(2026, 10, 7, 11, 0),
        "series",
        recurrence_rule=rule,
    )

    conflicts = _find(calendar)

    assert _ids(conflicts.events_to_cancel) == [
        (master.id, datetime.datetime(2026, 10, 9, 10, 30, tzinfo=datetime.UTC))
    ]


@pytest.mark.django_db
def test_events_elsewhere_with_the_owner_attending_are_conflicts(calendar, other_calendar, owner):
    attended = _event(
        other_calendar,
        datetime.datetime(2026, 10, 9, 10, 0),
        datetime.datetime(2026, 10, 9, 11, 0),
        "attended-elsewhere",
    )
    EventAttendance.objects.create(
        event=attended, membership_user_id=owner.id, organization=calendar.organization
    )
    _event(
        other_calendar,
        datetime.datetime(2026, 10, 9, 10, 0),
        datetime.datetime(2026, 10, 9, 11, 0),
        "not-attended",
    )

    conflicts = _find(calendar)

    assert conflicts.events_to_cancel == []
    assert _ids(conflicts.conflict_events) == [(attended.id, None)]


@pytest.mark.django_db
def test_find_writes_nothing(calendar, django_assert_max_num_queries):
    _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 30),
        datetime.datetime(2026, 10, 9, 11, 0),
        "inside",
    )

    with django_assert_max_num_queries(10) as captured:
        _find(calendar)

    statements = [query["sql"].split()[0].upper() for query in captured.captured_queries]
    assert set(statements) == {"SELECT"}


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


def test_writes_without_services_are_refused(calendar):
    with pytest.raises(CalendarIntegrationError):
        BlockConflictService().cancel_block_conflicts(
            BlockConflicts(events_to_cancel=[], conflict_events=[])
        )


def _cancel(calendar, services):
    calendar_service, appointment_type_service = services
    service = BlockConflictService(
        calendar_service=calendar_service, appointment_type_service=appointment_type_service
    )
    return service.cancel_block_conflicts(
        service.find_block_conflicts(calendar, BLOCK_START, BLOCK_END, "UTC")
    )


@pytest.mark.django_db
def test_cancel_deletes_events_telling_every_guest_and_fires_the_delete_webhook(
    calendar, owner_token_services, write_adapter, django_capture_on_commit_callbacks
):
    baker.make(
        WebhookConfiguration,
        organization=calendar.organization,
        event_type=WebhookEventType.CALENDAR_EVENT_DELETED,
        url="https://example.com/hooks/deleted",
    )
    first = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 0),
        datetime.datetime(2026, 10, 9, 11, 0),
        "first",
    )
    second = _event(
        calendar,
        datetime.datetime(2026, 10, 9, 11, 0),
        datetime.datetime(2026, 10, 9, 12, 0),
        "second",
    )

    with (
        patch("webhooks.services.webhook_service.process_webhook_event.delay"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        result = _cancel(calendar, owner_token_services)

    assert (
        result.cancelled_event_count,
        result.failed_event_count,
        result.conflict_event_count,
    ) == (2, 0, 0)
    assert not CalendarEvent.objects.filter_by_organization(calendar.organization).exists()
    assert write_adapter.delete_event.call_count == 2
    write_adapter.delete_event.assert_has_calls(
        [
            call("provider@example.com", "first", send_updates="all"),
            call("provider@example.com", "second", send_updates="all"),
        ],
        any_order=True,
    )
    deleted_ids = sorted(
        WebhookEvent.objects.filter_by_organization(calendar.organization)
        .filter(event_type=WebhookEventType.CALENDAR_EVENT_DELETED)
        .values_list("payload__id", flat=True)
    )
    assert deleted_ids == sorted([first.id, second.id])


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


@pytest.mark.django_db
def test_conflicts_are_counted_and_left_alone(
    calendar, other_calendar, owner, owner_token_services, write_adapter
):
    attended = _event(
        other_calendar,
        datetime.datetime(2026, 10, 9, 10, 0),
        datetime.datetime(2026, 10, 9, 11, 0),
        "attended-elsewhere",
    )
    EventAttendance.objects.create(
        event=attended, membership_user_id=owner.id, organization=calendar.organization
    )

    result = _cancel(calendar, owner_token_services)

    assert (
        result.cancelled_event_count,
        result.failed_event_count,
        result.conflict_event_count,
    ) == (0, 0, 1)
    assert (
        CalendarEvent.objects.filter_by_organization(calendar.organization)
        .filter(id=attended.id)
        .exists()
    )


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


def _events_inside_the_block(calendar, count):
    """``count`` back-to-back 1-minute events inside the block."""
    for minute in range(count):
        start = BLOCK_START + datetime.timedelta(minutes=minute)
        _event(calendar, start, start + datetime.timedelta(minutes=1), f"inside-{minute}")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scope", "expected_counts", "event_left"),
    [
        (BlockedTimeDeclineScope.NEW_AND_EXISTING, (1, 0, 0), False),
        (BlockedTimeDeclineScope.NEW, (0, 0, 0), True),
    ],
    ids=["new-and-existing-cancels", "new-cancels-nothing"],
)
def test_create_block_cancels_the_overlapped_events_only_for_new_and_existing(
    calendar, owner_token_services, write_adapter, scope, expected_counts, event_left
):
    _event(
        calendar,
        datetime.datetime(2026, 10, 9, 10, 30),
        datetime.datetime(2026, 10, 9, 11, 0),
        "inside",
    )

    with patch(PUSH_TASK_DELAY):
        blocked_time, result = _create_block(
            calendar, owner_token_services, BlockDeclineOptions(scope=scope)
        )

    blocked_time.refresh_from_db()
    assert blocked_time.decline_scope == scope
    assert (
        result.cancelled_event_count,
        result.failed_event_count,
        result.conflict_event_count,
    ) == expected_counts
    assert CalendarEvent.objects.filter_by_organization(calendar.organization).exists() is (
        event_left
    )


@pytest.mark.django_db
def test_create_block_with_an_unknown_timezone_writes_nothing(calendar, owner_token_services):
    with pytest.raises(ValueError, match="Invalid IANA timezone"):
        _create_block(
            calendar,
            owner_token_services,
            BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW_AND_EXISTING),
            timezone="Mars/Olympus",
        )

    assert not BlockedTime.objects.filter_by_organization(calendar.organization).exists()


def test_decline_options_refuse_an_overlong_message():
    BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW, message="x" * 1000)

    with pytest.raises(InvalidBlockDeclineOptionsError):
        BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW, message="x" * 1001)


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


def test_max_block_cancels_is_25():
    assert MAX_BLOCK_CANCELS == 25


@pytest.mark.django_db
def test_create_block_at_the_cancel_limit_cancels_them_all(
    calendar, owner_token_services, write_adapter
):
    _events_inside_the_block(calendar, 3)

    with (
        patch("calendar_integration.services.block_conflict_service.MAX_BLOCK_CANCELS", 3),
        patch(PUSH_TASK_DELAY),
    ):
        blocked_time, result = _create_block(
            calendar,
            owner_token_services,
            BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW_AND_EXISTING),
        )

    assert blocked_time.pk is not None
    assert (result.cancelled_event_count, result.failed_event_count) == (3, 0)
    assert not CalendarEvent.objects.filter_by_organization(calendar.organization).exists()


@pytest.mark.django_db
def test_create_block_over_the_cancel_limit_writes_and_cancels_nothing(
    calendar, owner_token_services, write_adapter, django_capture_on_commit_callbacks
):
    _events_inside_the_block(calendar, MAX_BLOCK_CANCELS + 1)

    with (
        patch(PUSH_TASK_DELAY) as delay,
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(TooManyBlockCancelsError, match="Block a shorter period"),
    ):
        _create_block(
            calendar,
            owner_token_services,
            BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW_AND_EXISTING),
        )

    assert not BlockedTime.objects.filter_by_organization(calendar.organization).exists()
    assert (
        CalendarEvent.objects.filter_by_organization(calendar.organization).count()
        == MAX_BLOCK_CANCELS + 1
    )
    write_adapter.delete_event.assert_not_called()
    delay.assert_not_called()


@pytest.mark.django_db
def test_create_block_with_new_ignores_the_cancel_limit(
    calendar, owner_token_services, write_adapter
):
    """NEW cancels nothing, so a busy period does not stop the block."""
    _events_inside_the_block(calendar, 2)

    with (
        patch("calendar_integration.services.block_conflict_service.MAX_BLOCK_CANCELS", 1),
        patch(PUSH_TASK_DELAY),
    ):
        blocked_time, result = _create_block(
            calendar, owner_token_services, BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW)
        )

    assert blocked_time.pk is not None
    assert result.cancelled_event_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scope", [BlockedTimeDeclineScope.NEW, BlockedTimeDeclineScope.NEW_AND_EXISTING]
)
def test_create_block_with_options_and_a_recurrence_rule_writes_nothing(
    calendar, owner_token_services, write_adapter, scope
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
            BlockDeclineOptions(scope=scope),
            rrule_string="FREQ=WEEKLY;BYDAY=FR",
        )

    assert not BlockedTime.objects.filter_by_organization(calendar.organization).exists()
    assert CalendarEvent.objects.filter_by_organization(calendar.organization).count() == 1
    write_adapter.delete_event.assert_not_called()
    delay.assert_not_called()
