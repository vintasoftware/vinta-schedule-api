"""The blocked-time provider tasks: writing a block to Google and deleting its event,
retrying on errors."""

from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from googleapiclient.errors import HttpError

from calendar_integration.constants import ProviderPushStatus
from calendar_integration.models import BlockedTime
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.tasks import (
    delete_blocked_time_from_provider_task,
    push_blocked_time_to_provider_task,
)
from calendar_integration.tasks.blocked_time_push_tasks import (
    PUSH_MAX_RETRIES,
    PUSH_RETRY_BASE_DELAY,
)
from calendar_integration.tests.blocked_time_factories import make_pending_block


@pytest.mark.django_db
def test_task_pushes_the_block(google_calendar, write_adapter):
    """Which event the push writes, and which blocks it skips, is covered by the
    ``push_blocked_time_to_provider`` service tests."""
    block = make_pending_block(google_calendar)

    push_blocked_time_to_provider_task.apply(args=(block.id, google_calendar.organization_id))

    block.refresh_from_db()
    write_adapter.write_busy_event.assert_called_once()
    assert block.provider_push_status == ProviderPushStatus.PUSHED


def _unavailable() -> HttpError:
    return HttpError(Mock(status=503, reason="unavailable"), b"{}")


def _run_as_retry(block: BlockedTime, retries: int) -> None:
    """Run the task body as a worker would on its ``retries``-th retry."""
    task = push_blocked_time_to_provider_task
    task.push_request(retries=retries, is_eager=False)
    try:
        task.run(block.id, block.organization_id)
    finally:
        task.pop_request()


@pytest.mark.django_db
def test_provider_error_schedules_a_retry_with_a_growing_delay(google_calendar, write_adapter):
    write_adapter.write_busy_event.side_effect = _unavailable()
    block = make_pending_block(google_calendar)

    with (
        patch.object(push_blocked_time_to_provider_task, "retry", return_value=Retry()) as retry,
        pytest.raises(Retry),
    ):
        _run_as_retry(block, retries=2)

    retry.assert_called_once_with(countdown=PUSH_RETRY_BASE_DELAY * 4)
    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.PENDING


@pytest.mark.django_db
def test_failed_after_the_last_retry_keeps_the_block(google_calendar, write_adapter):
    write_adapter.write_busy_event.side_effect = _unavailable()
    block = make_pending_block(google_calendar)

    with patch.object(push_blocked_time_to_provider_task, "retry") as retry:
        _run_as_retry(block, retries=PUSH_MAX_RETRIES)

    retry.assert_not_called()
    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.FAILED
    assert block.reason == "Conference"


@pytest.mark.django_db
def test_eager_run_gives_up_at_once(google_calendar, write_adapter):
    write_adapter.write_busy_event.side_effect = _unavailable()
    block = make_pending_block(google_calendar)

    push_blocked_time_to_provider_task.apply(args=(block.id, google_calendar.organization_id))

    write_adapter.write_busy_event.assert_called_once()
    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.FAILED


@pytest.mark.django_db
def test_no_write_account_ends_failed(google_calendar):
    block = make_pending_block(google_calendar)

    with patch.object(CalendarService, "_get_write_adapter_for_calendar", return_value=None):
        push_blocked_time_to_provider_task.apply(args=(block.id, google_calendar.organization_id))

    block.refresh_from_db()
    assert block.provider_push_status == ProviderPushStatus.FAILED
    assert block.reason == "Conference"


@pytest.mark.django_db
def test_missing_organization_does_nothing(write_adapter):
    push_blocked_time_to_provider_task.apply(args=(1, 999_999))

    write_adapter.write_busy_event.assert_not_called()


# ---------------------------------------------------------------------------
# delete_blocked_time_from_provider_task
# ---------------------------------------------------------------------------

PROVIDER_EVENT_ID = "0123456789abcdef"


@pytest.mark.django_db
def test_delete_task_deletes_the_provider_event(google_calendar, write_adapter):
    delete_blocked_time_from_provider_task.apply(
        args=(google_calendar.id, PROVIDER_EVENT_ID, google_calendar.organization_id)
    )

    write_adapter.delete_blocked_time_event.assert_called_once_with(
        "provider@example.com", PROVIDER_EVENT_ID
    )


def _run_delete_as_retry(google_calendar, retries: int) -> None:
    task = delete_blocked_time_from_provider_task
    task.push_request(retries=retries, is_eager=False)
    try:
        task.run(google_calendar.id, PROVIDER_EVENT_ID, google_calendar.organization_id)
    finally:
        task.pop_request()


@pytest.mark.django_db
def test_delete_task_provider_error_schedules_a_retry_with_a_growing_delay(
    google_calendar, write_adapter
):
    write_adapter.delete_blocked_time_event.side_effect = _unavailable()

    with (
        patch.object(
            delete_blocked_time_from_provider_task, "retry", return_value=Retry()
        ) as retry,
        pytest.raises(Retry),
    ):
        _run_delete_as_retry(google_calendar, retries=3)

    retry.assert_called_once_with(countdown=PUSH_RETRY_BASE_DELAY * 8)


@pytest.mark.django_db
def test_delete_task_gives_up_after_the_last_retry(google_calendar, write_adapter, caplog):
    write_adapter.delete_blocked_time_event.side_effect = _unavailable()

    with patch.object(delete_blocked_time_from_provider_task, "retry") as retry:
        _run_delete_as_retry(google_calendar, retries=PUSH_MAX_RETRIES)

    retry.assert_not_called()
    assert "HttpError" in caplog.text


@pytest.mark.django_db
def test_delete_task_eager_run_gives_up_at_once(google_calendar, write_adapter):
    write_adapter.delete_blocked_time_event.side_effect = _unavailable()

    result = delete_blocked_time_from_provider_task.apply(
        args=(google_calendar.id, PROVIDER_EVENT_ID, google_calendar.organization_id)
    )

    assert result.successful()
    write_adapter.delete_blocked_time_event.assert_called_once()


@pytest.mark.django_db
def test_delete_task_missing_organization_does_nothing(write_adapter):
    delete_blocked_time_from_provider_task.apply(args=(1, PROVIDER_EVENT_ID, 999_999))

    write_adapter.delete_blocked_time_event.assert_not_called()
