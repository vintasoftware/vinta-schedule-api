"""The hourly room and location resync.

``resync_rooms_for_flagged_organizations_task`` runs from beat every hour and
enqueues one ``resync_organization_rooms_task`` per flag-on organization and
provider. Organizations with the ``resource_calendar_provider_sync`` flag off are
never enqueued, and the on-demand room import is a separate path this does not
touch.

Both tasks are safe to run twice (``CELERY_TASK_ACKS_LATE`` redelivers): the
fan-out only enqueues, and a resync with no provider change between two runs
changes nothing the second time.
"""

import logging

from dependency_injector.wiring import Provide, inject
from vinta_billing.services.entitlement_service import EntitlementService

from calendar_integration.exceptions import ResourceDirectoryError
from calendar_integration.services.room_resync_service import (
    ROOM_RESYNC_PROVIDERS,
    RoomResyncService,
)
from calendar_integration.tasks.calendar_sync_tasks import _restricted_or_skip
from common.feature_flags import (
    RESOURCE_CALENDAR_PROVIDER_SYNC,
    is_enabled,
    organization_ids_with_flag,
)
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


@app.task
def resync_rooms_for_flagged_organizations_task() -> None:
    """Enqueue a room resync for every flag-on organization, once per provider.

    Whether an organization is write-enabled for a provider is checked by the
    per-organization task, under that organization's own binding, so this task
    reads nothing but the flag rows.
    """
    for organization_id in organization_ids_with_flag(RESOURCE_CALENDAR_PROVIDER_SYNC):
        for provider in ROOM_RESYNC_PROVIDERS:
            resync_organization_rooms_task.delay(organization_id=organization_id, provider=provider)


# Injected services are declared with `Provide[...]` as the default, for the reason
# given above the tasks in `calendar_sync_tasks`.
@app.task
@inject
def resync_organization_rooms_task(
    organization_id: int,
    provider: str,
    room_resync_service: RoomResyncService = Provide["room_resync_service"],
    entitlement_service: EntitlementService = Provide["entitlement_service"],
) -> None:
    """Resync one organization's rooms and locations on one provider.

    A no-op when the organization is gone, its flag was turned off after the
    fan-out ran, its billing root is restricted (sync is paused), or it is not
    write-enabled for ``provider``. A provider failure is logged and dropped
    rather than raised: nothing was written yet, and the next hourly run tries
    again, as the existing calendar sync does with a broken connection.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        return

    with organization_context(organization):
        if not is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization_id):
            return
        if _restricted_or_skip(entitlement_service, organization):
            return
        try:
            room_resync_service.resync(organization, provider)
        except ResourceDirectoryError as error:
            logger.warning(
                "Room resync for organization %s on %s failed: %s (transient=%s)",
                organization_id,
                provider,
                type(error).__name__,
                error.is_transient,
            )
