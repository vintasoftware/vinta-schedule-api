"""The calendar tasks accept the arguments their call sites pass to ``.delay()``.

Celery checks those arguments against the task's signature (``Task.__header__``)
before it sends anything. On Python 3.14 the check sees through ``@inject`` to the
real function, so an injected service declared without a default is a required
argument that no caller passes, and ``.delay()`` raises ``TypeError``. Every other
test patches ``.delay``, which skips the check, so nothing else would catch it.

Each case mirrors a real call site, positional or keyword, as that call site
writes it.
"""

from typing import Any

import pytest
from celery import Task

from calendar_integration.tasks import (
    import_account_calendars_task,
    import_organization_calendar_resources_task,
    purge_expired_resource_calendar_create_requests_task,
    push_room_to_provider_task,
    start_room_event_sync_task,
    subscribe_microsoft_room_task,
    sync_calendar_task,
    sync_microsoft_room_events_task,
    unsubscribe_microsoft_room_task,
)
from calendar_integration.tasks.calendar_sync_tasks import resync_organization_calendars_task


@pytest.mark.parametrize(
    ("task", "args", "kwargs"),
    [
        pytest.param(
            import_account_calendars_task,
            (),
            {"account_type": "social_account", "account_id": 1, "organization_id": 1},
            id="import_account_calendars-accounts.account_adapters",
        ),
        pytest.param(
            import_account_calendars_task,
            (),
            {
                "account_type": "social_account",
                "account_id": 1,
                "organization_id": 1,
                "sync_after_import": False,
            },
            id="import_account_calendars-CalendarSyncService.request_calendars_import",
        ),
        pytest.param(
            sync_calendar_task,
            ("social_account", 1, 1, 1),
            {},
            id="sync_calendar-CalendarSyncService.request_calendar_sync",
        ),
        pytest.param(
            subscribe_microsoft_room_task,
            (1, 1),
            {},
            id="subscribe_microsoft_room-resource_room_synced receiver",
        ),
        pytest.param(
            unsubscribe_microsoft_room_task,
            (1, 1),
            {},
            id="unsubscribe_microsoft_room-resource_room_archived receiver",
        ),
        pytest.param(
            sync_microsoft_room_events_task,
            (1, 1),
            {},
            id="sync_microsoft_room_events-room webhook",
        ),
        pytest.param(
            import_organization_calendar_resources_task,
            (),
            {
                "account_type": "google_service_account",
                "account_id": 1,
                "organization_id": 1,
                "import_workflow_state_id": 1,
            },
            id="import_organization_calendar_resources-CalendarSyncService",
        ),
        pytest.param(
            push_room_to_provider_task,
            (),
            {"link_id": 1, "organization_id": 1, "attempt_count": 0},
            # Also the kwargs the task's own retry passes to `apply_async`.
            id="push_room_to_provider-RoomSyncService._enqueue",
        ),
        pytest.param(
            purge_expired_resource_calendar_create_requests_task,
            (),
            {},
            id="purge_expired_resource_calendar_create_requests-celerybeat_schedule",
        ),
        pytest.param(
            start_room_event_sync_task,
            (),
            {"calendar_id": 1, "organization_id": 1},
            id="start_room_event_sync-room_sync_receivers",
        ),
        pytest.param(
            resync_organization_calendars_task,
            (),
            {"organization_id": 1},
            id="resync_organization_calendars-payments.seams.resync",
        ),
    ],
)
def test_delay_arguments_pass_celerys_signature_check(
    task: Task, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    # The exact check `Task.apply_async` runs before sending, with nothing sent.
    # Celery attaches `__header__` to each task class when it builds it, so the
    # celery-types stubs do not declare it.
    task.__header__(*args, **kwargs)  # type: ignore[attr-defined]
