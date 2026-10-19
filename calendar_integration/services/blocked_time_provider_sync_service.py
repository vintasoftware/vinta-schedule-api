"""Write blocked times with decline options to their provider calendar.

A block made with decline options on a Google calendar is written there as an Out of
office or busy event, kept in step when the block is edited, and deleted with it. The
writes run in Celery tasks queued after the transaction that changed the block commits
(``calendar_integration.tasks.blocked_time_push_tasks``).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.functions import Now

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
from calendar_integration.models import BlockedTime, Calendar


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service import CalendarService
    from calendar_integration.services.dataclasses import BlockDeclineOptions
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
    from organizations.models import Organization


logger = logging.getLogger(__name__)


class BlockedTimeProviderSyncService:
    """Keeps a blocked time's provider event in step with the block.

    The static methods run inside the request that changes a block: they save the
    options and queue the provider write. The instance methods run in the tasks and
    reach the provider through the injected ``CalendarService``; call ``initialize``
    first.
    """

    def __init__(self, calendar_service: CalendarService) -> None:
        self.calendar_service = calendar_service

    def initialize(self, organization: Organization) -> None:
        """Bind the calendar service to ``organization``, with no caller."""
        self.calendar_service.initialize_without_provider(organization=organization)

    @staticmethod
    def check_recurrence(decline_scope: str | None, rrule_string: str | None) -> None:
        """Refuse decline options on a recurring block.

        A recurring block is never written to the provider and cancels nothing, so its
        options would have no effect.

        :raises BlockDeclineOptionsWithRecurrenceError: When the block has a
            ``decline_scope`` and ``rrule_string`` is set.
        """
        if decline_scope is not None and rrule_string:
            raise BlockDeclineOptionsWithRecurrenceError()

    @classmethod
    def apply_decline_options(
        cls, blocked_time: BlockedTime, decline_options: BlockDeclineOptions
    ) -> None:
        """Save ``decline_options`` on a new block and queue its provider write.

        Only a block on a Google calendar is written to its provider; on any other
        calendar the options are saved and nothing is queued.
        """
        blocked_time.decline_scope = decline_options.scope
        blocked_time.decline_message = decline_options.message
        update_fields = ["decline_scope", "decline_message", "modified"]
        pushable = blocked_time.calendar.provider == CalendarProvider.GOOGLE
        if pushable:
            blocked_time.provider_push_status = ProviderPushStatus.PENDING
            update_fields.append("provider_push_status")
        blocked_time.save(update_fields=update_fields)
        if pushable:
            cls.queue_push(blocked_time)

    @classmethod
    def mark_changed(cls, blocked_time: BlockedTime) -> None:
        """Mark an edited block to be written again, before it is saved.

        Does nothing for a block that is not written to its provider. Otherwise the block
        is set pending, its ``provider_push_version`` moves on so a push already running
        does not mark the edit written, and a new push is queued for after the commit.
        """
        if blocked_time.provider_push_status is None:
            return
        blocked_time.provider_push_status = ProviderPushStatus.PENDING
        blocked_time.provider_push_version += 1
        cls.queue_push(blocked_time)

    @staticmethod
    def queue_push(blocked_time: BlockedTime) -> None:
        """Queue ``push_blocked_time_to_provider_task`` for after the transaction commits."""
        # Imported here: the task module imports this one.
        from calendar_integration.tasks.blocked_time_push_tasks import (
            push_blocked_time_to_provider_task,
        )

        blocked_time_id = blocked_time.id
        organization_id = blocked_time.organization_id
        transaction.on_commit(
            lambda: push_blocked_time_to_provider_task.delay(  # type: ignore[attr-defined]
                blocked_time_id, organization_id
            )
        )

    @staticmethod
    def queue_delete(blocked_time: BlockedTime) -> None:
        """Queue the delete of a block's provider event for after the transaction commits.

        Does nothing for a block that has no provider event. Call it before the block is
        deleted: the task gets the calendar and the event id as arguments.
        """
        provider_event_external_id = blocked_time.provider_event_external_id
        if not provider_event_external_id:
            return
        # Imported here: the task module imports this one.
        from calendar_integration.tasks.blocked_time_push_tasks import (
            delete_blocked_time_from_provider_task,
        )

        calendar_id = blocked_time.calendar_fk_id
        organization_id = blocked_time.organization_id
        transaction.on_commit(
            lambda: delete_blocked_time_from_provider_task.delay(  # type: ignore[attr-defined]
                calendar_id, provider_event_external_id, organization_id
            )
        )

    def push(self, blocked_time_id: int) -> None:
        """Write a blocked time to its provider calendar, creating or updating its event.

        On the calendar its owner marks as default the block becomes an Out of office
        event that declines conflicting invitations per its ``decline_scope``, with the
        provider's default reply. On any other calendar, or when the provider refuses Out
        of office, it becomes a plain busy event. Either way the event gets a fixed,
        neutral title: the block's ``reason`` and ``decline_message`` are never sent. The
        provider event id is saved on the row before the write and sent with it, so a
        repeated run updates the same event instead of creating a second one.

        Does nothing for a block that is missing, that is not to be written, or whose
        latest version was already written. A block changed while this runs keeps its
        newer ``provider_push_version``, stays pending, and is written by the push its
        change queued.

        :raises: The adapter's errors, for the caller to retry.
        """
        scoped = BlockedTime.objects.filter_by_organization(self._organization_id())
        blocked_time = scoped.select_related("calendar").filter(id=blocked_time_id).first()
        if blocked_time is None or blocked_time.provider_push_status not in (
            ProviderPushStatus.PENDING,
            ProviderPushStatus.FAILED,
        ):
            return
        calendar = blocked_time.calendar
        write_adapter = self._write_adapter(calendar)
        version = blocked_time.provider_push_version

        if not blocked_time.provider_event_external_id:
            # Google event ids use the characters a-v and 0-9; a hex uuid fits.
            blocked_time.provider_event_external_id = uuid.uuid4().hex
            saved = scoped.filter(id=blocked_time.id, provider_push_version=version).update(
                provider_event_external_id=blocked_time.provider_event_external_id,
                modified=Now(),
            )
            if not saved:
                # Deleted, or changed and queued again, since it was read.
                return

        event_fields = {
            "calendar_id": calendar.provider_calendar_id,
            "event_id": blocked_time.provider_event_external_id,
            "start_time": blocked_time.start_time,
            "end_time": blocked_time.end_time,
            "timezone": blocked_time.timezone,
        }
        out_of_office = (
            BlockedTimeDeclineScope(blocked_time.decline_scope)
            if calendar.ownerships.filter(is_default=True).exists()
            else None
        )
        try:
            write_adapter.write_blocked_time_event(**event_fields, out_of_office=out_of_office)
        except OutOfOfficeNotSupportedError:
            # Only the block id: the provider's error message can echo the event's details.
            logger.warning(
                "Provider refused an Out of office event for blocked time %s; "
                "writing a busy event instead.",
                blocked_time.id,
            )
            write_adapter.write_blocked_time_event(**event_fields, out_of_office=None)

        scoped.filter(id=blocked_time.id, provider_push_version=version).update(
            provider_push_status=ProviderPushStatus.PUSHED
        )

    def delete(self, calendar_id: int, provider_event_external_id: str) -> None:
        """Delete the provider event a deleted blocked time was written to.

        The provider is not asked to email anyone. An event the provider no longer has
        counts as deleted, and a calendar that no longer exists leaves nothing to do.

        :raises: The adapter's errors, for the caller to retry.
        """
        calendar = (
            Calendar.objects.filter_by_organization(self._organization_id())
            .filter(id=calendar_id)
            .first()
        )
        if calendar is None:
            return
        self._write_adapter(calendar).delete_blocked_time_event(
            calendar.provider_calendar_id, provider_event_external_id
        )

    def mark_failed(self, blocked_time_id: int) -> None:
        """Record that writing a blocked time to its provider gave up.

        The block itself stays and keeps blocking time in Vinta Schedule.
        """
        BlockedTime.objects.filter_by_organization(self._organization_id()).filter(
            id=blocked_time_id
        ).exclude(provider_push_status=ProviderPushStatus.PUSHED).update(
            provider_push_status=ProviderPushStatus.FAILED
        )

    def _organization_id(self) -> int:
        organization = self.calendar_service.organization
        if organization is None:
            raise CalendarIntegrationError(
                "BlockedTimeProviderSyncService requires an organization. Call initialize()."
            )
        return organization.id

    def _write_adapter(self, calendar: Calendar) -> CalendarAdapter:
        write_adapter = self.calendar_service._get_write_adapter_for_calendar(calendar)
        if write_adapter is None:
            raise CalendarIntegrationError("No account can write to this calendar.")
        return write_adapter
