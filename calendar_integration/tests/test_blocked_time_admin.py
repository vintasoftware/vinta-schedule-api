"""``BlockedTimeAdmin``'s "Retry provider push" action."""

from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

import pytest

from calendar_integration.admin import BlockedTimeAdmin
from calendar_integration.constants import CalendarProvider, ProviderPushStatus
from calendar_integration.models import BlockedTime, Calendar
from calendar_integration.tests.blocked_time_factories import make_pending_block
from organizations.models import Organization


PUSH_TASK_DELAY = (
    "calendar_integration.tasks.blocked_time_push_tasks.push_blocked_time_to_provider_task.delay"
)


@pytest.fixture
def model_admin() -> BlockedTimeAdmin:
    return BlockedTimeAdmin(BlockedTime, AdminSite())


@pytest.fixture
def request_():
    return RequestFactory().post("/admin/calendar_integration/blockedtime/")


def _block(calendar: Calendar, status: str | None, suffix: str) -> BlockedTime:
    if status is None:
        return make_pending_block(
            calendar,
            decline_scope=None,
            decline_message="",
            provider_push_status=None,
            external_id=f"manual-{suffix}",
        )
    return make_pending_block(calendar, provider_push_status=status, external_id=f"manual-{suffix}")


@pytest.mark.django_db
def test_retry_requeues_only_failed_blocks_across_organizations(
    model_admin, request_, google_calendar, django_capture_on_commit_callbacks
):
    other_calendar = Calendar.objects.create(
        name="Other calendar",
        external_id="other@example.com",
        provider=CalendarProvider.GOOGLE,
        organization=Organization.objects.create(name="Other org"),
    )
    failed = _block(google_calendar, ProviderPushStatus.FAILED, "failed")
    failed_elsewhere = _block(other_calendar, ProviderPushStatus.FAILED, "failed-elsewhere")
    pushed = _block(google_calendar, ProviderPushStatus.PUSHED, "pushed")
    pending = _block(google_calendar, ProviderPushStatus.PENDING, "pending")
    no_options = _block(google_calendar, None, "no-options")
    queryset = model_admin.get_queryset(request_).filter(
        id__in=[b.id for b in (failed, failed_elsewhere, pushed, pending, no_options)]
    )

    with (
        patch(PUSH_TASK_DELAY) as delay,
        patch.object(model_admin, "message_user") as message_user,
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            model_admin.retry_provider_push(request_, queryset)
        delay.assert_not_called()
        for callback in callbacks:
            callback()

    assert sorted(c.args for c in delay.call_args_list) == sorted(
        [
            (failed.id, google_calendar.organization_id),
            (failed_elsewhere.id, other_calendar.organization_id),
        ]
    )
    statuses = dict(BlockedTime.objects.unscoped().values_list("id", "provider_push_status"))
    assert statuses == {
        failed.id: ProviderPushStatus.PENDING,
        failed_elsewhere.id: ProviderPushStatus.PENDING,
        pushed.id: ProviderPushStatus.PUSHED,
        pending.id: ProviderPushStatus.PENDING,
        no_options.id: None,
    }
    message_user.assert_called_once_with(
        request_, "Queued a provider push for 2 blocked times. Skipped 3 that had not failed."
    )


@pytest.mark.django_db
def test_retry_with_nothing_failed_queues_nothing(
    model_admin, request_, google_calendar, django_capture_on_commit_callbacks
):
    pushed = _block(google_calendar, ProviderPushStatus.PUSHED, "pushed")
    queryset = model_admin.get_queryset(request_).filter(id=pushed.id)

    with (
        patch(PUSH_TASK_DELAY) as delay,
        patch.object(model_admin, "message_user") as message_user,
        django_capture_on_commit_callbacks(execute=True),
    ):
        model_admin.retry_provider_push(request_, queryset)

    delay.assert_not_called()
    message_user.assert_called_once_with(
        request_, "Queued a provider push for 0 blocked times. Skipped 1 that had not failed."
    )


def test_retry_is_registered_as_an_action(model_admin):
    assert "retry_provider_push" in model_admin.actions
