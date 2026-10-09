"""Blocked-time decline options: how a block stores them and is written to Google."""

import datetime
import logging
import uuid
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.constants import (
    BlockedTimeDeclineScope,
    CalendarProvider,
    ProviderPushStatus,
)
from calendar_integration.exceptions import (
    BlockDeclineOptionsWithRecurrenceError,
    CalendarIntegrationError,
    OutOfOfficeNotSupportedError,
)
from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import BlockedTime, Calendar
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import BlockDeclineOptions
from calendar_integration.tests.blocked_time_factories import make_pending_block
from organizations.models import Organization


PUSH_TASK_DELAY = (
    "calendar_integration.tasks.blocked_time_push_tasks.push_blocked_time_to_provider_task.delay"
)
DELETE_TASK_DELAY = (
    "calendar_integration.tasks.blocked_time_push_tasks."
    "delete_blocked_time_from_provider_task.delay"
)


@pytest.fixture
def internal_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Internal calendar",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
    )


@pytest.fixture
def outlook_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Outlook calendar",
        external_id="outlook-cal",
        provider=CalendarProvider.MICROSOFT,
        organization=organization,
    )


@pytest.fixture
def service(organization: Organization) -> CalendarService:
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(organization=organization)
    return calendar_service


def _create_block(service: CalendarService, calendar: Calendar, **options) -> BlockedTime:
    return service.create_blocked_time(
        calendar=calendar,
        start_time=datetime.datetime(2026, 10, 9, 10, 0),
        end_time=datetime.datetime(2026, 10, 9, 12, 0),
        timezone="America/Sao_Paulo",
        reason="Conference",
        **options,
    )


# ---------------------------------------------------------------------------
# create_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_without_options_stores_and_queues_nothing_new(
    service, google_calendar, django_capture_on_commit_callbacks
):
    with patch(PUSH_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        block = _create_block(service, google_calendar)

    block.refresh_from_db()
    assert (
        block.reason,
        block.decline_scope,
        block.decline_message,
        block.provider_event_external_id,
        block.provider_push_status,
    ) == ("Conference", None, "", None, None)
    delay.assert_not_called()


@pytest.mark.django_db
def test_create_with_options_on_google_stores_them_and_queues_the_push(
    service, google_calendar, django_capture_on_commit_callbacks
):
    with patch(PUSH_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        block = _create_block(
            service,
            google_calendar,
            decline_options=BlockDeclineOptions(
                scope=BlockedTimeDeclineScope.NEW_AND_EXISTING, message="Back on Monday."
            ),
        )

    block.refresh_from_db()
    assert (
        block.reason,
        block.decline_scope,
        block.decline_message,
        block.provider_push_status,
    ) == ("Conference", "new_and_existing", "Back on Monday.", "pending")
    delay.assert_called_once_with(block.id, google_calendar.organization_id)


@pytest.mark.django_db
def test_create_with_options_queues_the_push_only_after_commit(
    service, google_calendar, django_capture_on_commit_callbacks
):
    with patch(PUSH_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=False):
        _create_block(
            service,
            google_calendar,
            decline_options=BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW),
        )
        delay.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("calendar_fixture", "fields"),
    [
        pytest.param("internal_calendar", {}, id="internal-calendar"),
        pytest.param("outlook_calendar", {}, id="outlook"),
    ],
)
def test_create_with_options_that_cannot_be_pushed_queues_nothing(
    service, request, django_capture_on_commit_callbacks, calendar_fixture, fields
):
    calendar = request.getfixturevalue(calendar_fixture)

    with patch(PUSH_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        block = _create_block(
            service,
            calendar,
            decline_options=BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW),
            **fields,
        )

    block.refresh_from_db()
    assert (block.decline_scope, block.provider_push_status) == ("new", None)
    delay.assert_not_called()


@pytest.mark.django_db
def test_create_with_options_and_a_recurrence_rule_is_refused(
    service, google_calendar, django_capture_on_commit_callbacks
):
    with (
        patch(PUSH_TASK_DELAY) as delay,
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(BlockDeclineOptionsWithRecurrenceError),
    ):
        _create_block(
            service,
            google_calendar,
            rrule_string="FREQ=WEEKLY;BYDAY=FR",
            decline_options=BlockDeclineOptions(scope=BlockedTimeDeclineScope.NEW),
        )

    assert not BlockedTime.objects.filter_by_organization(google_calendar.organization).exists()
    delay.assert_not_called()


@pytest.mark.django_db
def test_create_with_a_recurrence_rule_and_no_options_still_works(service, google_calendar):
    block = _create_block(service, google_calendar, rrule_string="FREQ=WEEKLY;BYDAY=FR")

    block.refresh_from_db()
    assert block.recurrence_rule is not None
    assert (block.decline_scope, block.provider_push_status) == (None, None)


# ---------------------------------------------------------------------------
# push_blocked_time_to_provider
# ---------------------------------------------------------------------------


START_UTC = datetime.datetime(2026, 10, 9, 13, 0, tzinfo=datetime.UTC)
END_UTC = datetime.datetime(2026, 10, 9, 15, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_push_on_the_primary_calendar_writes_out_of_office(
    service, google_calendar, owner, write_adapter
):
    create_calendar_ownership(calendar=google_calendar, user=owner, is_default=True)
    block = make_pending_block(
        google_calendar, decline_scope=BlockedTimeDeclineScope.NEW_AND_EXISTING
    )

    service.push_blocked_time_to_provider(block.id)

    block.refresh_from_db()
    write_adapter.write_out_of_office_event.assert_called_once_with(
        calendar_id="provider@example.com",
        event_id=block.provider_event_external_id,
        start_time=START_UTC,
        end_time=END_UTC,
        timezone="America/Sao_Paulo",
        decline_scope="new_and_existing",
    )
    write_adapter.write_busy_event.assert_not_called()
    assert block.provider_push_status == ProviderPushStatus.PUSHED
    assert len(block.provider_event_external_id) == 32


@pytest.mark.django_db
def test_push_of_a_block_deleted_after_it_was_read_writes_nothing(
    service, google_calendar, write_adapter
):
    block = make_pending_block(google_calendar)

    def delete_then_make_id():
        # Runs after the push read the block and before it saves the event id.
        BlockedTime.objects.filter_by_organization(google_calendar.organization).filter(
            id=block.id
        ).delete()
        return uuid.UUID(int=1)

    with patch(
        "calendar_integration.services.availability_service.uuid.uuid4", delete_then_make_id
    ):
        service.push_blocked_time_to_provider(block.id)

    write_adapter.write_out_of_office_event.assert_not_called()
    write_adapter.write_busy_event.assert_not_called()


@pytest.mark.django_db
def test_push_on_another_calendar_writes_a_busy_event(
    service, google_calendar, owner, write_adapter
):
    create_calendar_ownership(calendar=google_calendar, user=owner, is_default=False)
    block = make_pending_block(google_calendar)

    service.push_blocked_time_to_provider(block.id)

    block.refresh_from_db()
    write_adapter.write_out_of_office_event.assert_not_called()
    write_adapter.write_busy_event.assert_called_once_with(
        calendar_id="provider@example.com",
        event_id=block.provider_event_external_id,
        start_time=START_UTC,
        end_time=END_UTC,
        timezone="America/Sao_Paulo",
    )
    assert block.provider_push_status == ProviderPushStatus.PUSHED


@pytest.mark.django_db
def test_push_falls_back_to_busy_when_google_refuses_out_of_office(
    service, google_calendar, owner, write_adapter
):
    create_calendar_ownership(calendar=google_calendar, user=owner, is_default=True)
    write_adapter.write_out_of_office_event.side_effect = OutOfOfficeNotSupportedError()
    block = make_pending_block(google_calendar)

    service.push_blocked_time_to_provider(block.id)

    block.refresh_from_db()
    write_adapter.write_busy_event.assert_called_once_with(
        calendar_id="provider@example.com",
        event_id=block.provider_event_external_id,
        start_time=START_UTC,
        end_time=END_UTC,
        timezone="America/Sao_Paulo",
    )
    assert block.provider_push_status == ProviderPushStatus.PUSHED


@pytest.mark.django_db
def test_push_fallback_logs_only_the_block_id_and_googles_reason_code(
    service, google_calendar, owner, write_adapter, caplog
):
    create_calendar_ownership(calendar=google_calendar, user=owner, is_default=True)
    write_adapter.write_out_of_office_event.side_effect = OutOfOfficeNotSupportedError(
        "Google said: Dr. Smith's calendar cannot hold this", reason_code="eventTypeRestriction"
    )
    block = make_pending_block(google_calendar, reason="Patient follow-up")

    with caplog.at_level(logging.WARNING, logger="calendar_integration.services"):
        service.push_blocked_time_to_provider(block.id)

    [record] = [r for r in caplog.records if "Out of office" in r.getMessage()]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert str(block.id) in message
    assert "eventTypeRestriction" in message
    for secret in ("Dr. Smith", "Patient follow-up", "Back on Monday."):
        assert secret not in caplog.text
    write_adapter.write_busy_event.assert_called_once()


@pytest.mark.django_db
def test_push_reuses_the_event_id(service, google_calendar, write_adapter):
    """A retry after a write that was not recorded writes the same event again."""
    block = make_pending_block(google_calendar, provider_event_external_id="0123456789abcdef")

    service.push_blocked_time_to_provider(block.id)

    block.refresh_from_db()
    assert write_adapter.write_busy_event.call_args.kwargs["event_id"] == "0123456789abcdef"
    assert block.provider_event_external_id == "0123456789abcdef"
    assert block.provider_push_status == ProviderPushStatus.PUSHED


@pytest.mark.django_db
def test_push_never_sends_the_reason(service, google_calendar, owner, write_adapter):
    create_calendar_ownership(calendar=google_calendar, user=owner, is_default=True)
    block = make_pending_block(google_calendar, reason="Patient follow-up")

    service.push_blocked_time_to_provider(block.id)

    sent = write_adapter.write_out_of_office_event.call_args.kwargs
    assert "Patient follow-up" not in sent.values()
    assert "title" not in sent


@pytest.mark.django_db
def test_push_leaves_a_block_changed_meanwhile_pending(service, google_calendar, write_adapter):
    """An update during the write queued its own push, which writes the new times."""
    block = make_pending_block(google_calendar)

    def update_meanwhile(**_fields):
        BlockedTime.objects.filter_by_organization(google_calendar.organization).filter(
            id=block.id
        ).update(end_time_tz_unaware=datetime.datetime(2026, 10, 9, 13, 0))

    write_adapter.write_busy_event.side_effect = update_meanwhile

    service.push_blocked_time_to_provider(block.id)

    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["already-pushed", "no-options"])
def test_push_skips_blocks_it_must_not_write(
    service, organization, google_calendar, write_adapter, case
):
    if case == "already-pushed":
        block = make_pending_block(google_calendar, provider_push_status=ProviderPushStatus.PUSHED)
    else:
        block = make_pending_block(
            google_calendar, decline_scope=None, decline_message="", provider_push_status=None
        )

    service.push_blocked_time_to_provider(block.id)

    write_adapter.write_out_of_office_event.assert_not_called()
    write_adapter.write_busy_event.assert_not_called()
    block.refresh_from_db()
    assert block.provider_event_external_id is None


@pytest.mark.django_db
def test_mark_push_failed_keeps_the_block(service, google_calendar):
    block = make_pending_block(google_calendar)

    service.mark_blocked_time_push_failed(block.id)

    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.FAILED
    assert block.reason == "Conference"


# ---------------------------------------------------------------------------
# update_blocked_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "push_status",
    [ProviderPushStatus.PUSHED, ProviderPushStatus.FAILED, ProviderPushStatus.PENDING],
)
def test_update_of_a_pushable_block_queues_a_new_push(
    service, google_calendar, write_adapter, django_capture_on_commit_callbacks, push_status
):
    block = make_pending_block(
        google_calendar,
        provider_push_status=push_status,
        provider_event_external_id="0123456789abcdef",
    )

    with patch(PUSH_TASK_DELAY) as delay:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            service.update_blocked_time(
                calendar=google_calendar,
                blocked_time_id=block.id,
                end_time=datetime.datetime(2026, 10, 9, 13, 0),
            )
        delay.assert_not_called()
        for callback in callbacks:
            callback()

    delay.assert_called_once_with(block.id, google_calendar.organization_id)
    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.PENDING
    assert block.provider_event_external_id == "0123456789abcdef"


@pytest.mark.django_db
def test_update_locks_the_block_row(service, google_calendar):
    """A push or sync writing the provider event id waits for the update's lock, so the
    update's full save cannot erase that id."""
    block = make_pending_block(
        google_calendar,
        provider_push_status=ProviderPushStatus.PUSHED,
        provider_event_external_id="0123456789abcdef",
    )

    with (
        patch(PUSH_TASK_DELAY),
        CaptureQueriesContext(connection) as queries,
    ):
        service.update_blocked_time(
            calendar=google_calendar, blocked_time_id=block.id, reason="Moved"
        )

    block_reads = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].startswith("SELECT") and f'FROM "{BlockedTime._meta.db_table}"' in q["sql"]
    ]
    assert block_reads[0].endswith(f'FOR UPDATE OF "{BlockedTime._meta.db_table}"')


@pytest.mark.django_db
def test_update_then_push_rewrites_the_same_event(
    service, google_calendar, write_adapter, django_capture_on_commit_callbacks
):
    block = make_pending_block(
        google_calendar,
        provider_push_status=ProviderPushStatus.PUSHED,
        provider_event_external_id="0123456789abcdef",
    )

    with patch(PUSH_TASK_DELAY), django_capture_on_commit_callbacks(execute=True):
        service.update_blocked_time(
            calendar=google_calendar,
            blocked_time_id=block.id,
            end_time=datetime.datetime(2026, 10, 9, 13, 0),
        )
    service.push_blocked_time_to_provider(block.id)

    write_adapter.write_busy_event.assert_called_once_with(
        calendar_id="provider@example.com",
        event_id="0123456789abcdef",
        start_time=START_UTC,
        end_time=datetime.datetime(2026, 10, 9, 16, 0, tzinfo=datetime.UTC),
        timezone="America/Sao_Paulo",
    )
    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.PUSHED


@pytest.mark.django_db
def test_update_of_a_block_without_options_queues_nothing(
    service, google_calendar, django_capture_on_commit_callbacks
):
    block = _create_block(service, google_calendar)

    with patch(PUSH_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        service.update_blocked_time(
            calendar=google_calendar, blocked_time_id=block.id, reason="Moved"
        )

    delay.assert_not_called()
    block.refresh_from_db()
    assert block.provider_push_status is None


@pytest.mark.django_db
def test_update_with_a_recurrence_rule_is_refused_for_a_block_with_options(
    service, google_calendar, django_capture_on_commit_callbacks
):
    block = make_pending_block(google_calendar, provider_push_status=ProviderPushStatus.PUSHED)

    with (
        patch(PUSH_TASK_DELAY) as delay,
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(BlockDeclineOptionsWithRecurrenceError),
    ):
        service.update_blocked_time(
            calendar=google_calendar,
            blocked_time_id=block.id,
            reason="Moved",
            rrule_string="FREQ=WEEKLY;BYDAY=FR",
        )

    delay.assert_not_called()
    block.refresh_from_db()
    assert (block.reason, block.recurrence_rule_fk_id, block.provider_push_status) == (
        "Conference",
        None,
        ProviderPushStatus.PUSHED,
    )


@pytest.mark.django_db
def test_update_with_a_recurrence_rule_still_works_without_options(service, google_calendar):
    block = _create_block(service, google_calendar)

    service.update_blocked_time(
        calendar=google_calendar, blocked_time_id=block.id, rrule_string="FREQ=WEEKLY;BYDAY=FR"
    )

    block.refresh_from_db()
    assert block.recurrence_rule is not None


# ---------------------------------------------------------------------------
# delete_blocked_time / delete_blocked_time_from_provider
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_of_a_pushed_block_queues_the_provider_delete_after_commit(
    service, google_calendar, django_capture_on_commit_callbacks
):
    block = make_pending_block(
        google_calendar,
        provider_push_status=ProviderPushStatus.PUSHED,
        provider_event_external_id="0123456789abcdef",
    )

    with patch(DELETE_TASK_DELAY) as delay:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            service.delete_blocked_time(calendar=google_calendar, blocked_time_id=block.id)
        delay.assert_not_called()
        for callback in callbacks:
            callback()

    delay.assert_called_once_with(
        google_calendar.id, "0123456789abcdef", google_calendar.organization_id
    )
    assert not BlockedTime.objects.filter_by_organization(google_calendar.organization).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("with_options", [True, False])
def test_delete_of_a_block_never_written_queues_nothing(
    service, google_calendar, django_capture_on_commit_callbacks, with_options
):
    if with_options:
        block = make_pending_block(google_calendar)
    else:
        block = _create_block(service, google_calendar)

    with patch(DELETE_TASK_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        service.delete_blocked_time(calendar=google_calendar, blocked_time_id=block.id)

    delay.assert_not_called()


@pytest.mark.django_db
def test_delete_from_provider_deletes_the_event(service, google_calendar, write_adapter):
    service.delete_blocked_time_from_provider(google_calendar.id, "0123456789abcdef")

    write_adapter.delete_blocked_time_event.assert_called_once_with(
        "provider@example.com", "0123456789abcdef"
    )


@pytest.mark.django_db
def test_delete_from_provider_of_a_missing_calendar_does_nothing(service, write_adapter):
    service.delete_blocked_time_from_provider(999_999, "0123456789abcdef")

    write_adapter.delete_blocked_time_event.assert_not_called()


@pytest.mark.django_db
def test_delete_from_provider_of_another_organizations_calendar_does_nothing(
    service, write_adapter
):
    other_calendar = Calendar.objects.create(
        name="Other calendar",
        external_id="other@example.com",
        provider=CalendarProvider.GOOGLE,
        organization=Organization.objects.create(name="Other org"),
    )

    service.delete_blocked_time_from_provider(other_calendar.id, "0123456789abcdef")

    write_adapter.delete_blocked_time_event.assert_not_called()


@pytest.mark.django_db
def test_delete_from_provider_without_a_write_account_raises(service, google_calendar):
    with (
        patch.object(CalendarService, "_get_write_adapter_for_calendar", return_value=None),
        pytest.raises(CalendarIntegrationError),
    ):
        service.delete_blocked_time_from_provider(google_calendar.id, "0123456789abcdef")
