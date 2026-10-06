"""The background push engine for provider-backed rooms.

Vinta Schedule accepts a room create, edit or delete right away and records it on
the room's ``ResourceCalendarProviderLink``. ``RoomSyncService`` then writes it to
the provider (Google Workspace or Microsoft 365) from a Celery task, retrying with
backoff, and moves the link through its lifecycle:

- ``request_push(link, operation, fields)`` records the change on the link, marks
  it pending and queues a push once the caller's transaction commits.
- ``push(link_id, attempt_count)`` runs in ``push_room_to_provider_task``. It
  locks the link row, calls the provider through the
  ``ResourceDirectoryAdapterResolver``, and records the result. It returns a ``RoomPushOutcome`` that tells the task whether to
  queue another attempt; the task owns the backoff countdown.
- ``retry(link)`` starts a failed link over, from the operation that failed.

Retry rules (plan **Guiding Decisions → Push transport**):

- A transient error (``ResourceDirectoryError.is_transient``, which includes
  permission errors) is retried until ``retry_deadline``, which ``request_push``
  sets to six hours after the push was first requested.
- A non-transient error (invalid input, room not found on an update), or any
  error after the deadline, moves the link to ``SYNC_FAILED``. Org admins get an
  email and Vinta ops get a Sentry event with opaque ids only.

Every step is safe to replay, because tasks are acknowledged late
(``CELERY_TASK_ACKS_LATE``) and may run twice: a push of a link that is no longer
pending does nothing, and provider creates are idempotent on the link's
``provisional_key``.

Every method reads tenant-scoped rows through the organization-scoped managers, so
it must run with the link's organization bound (the task binds it).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

import sentry_sdk

from audit_integration.constants import AuditAction
from calendar_integration.constants import (
    RESOURCE_SYNCED_FIELDS,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import (
    ResourceDirectoryError,
    ResourceDirectoryNotFoundError,
    RoomSyncStateError,
)
from calendar_integration.models import Calendar, ResourceCalendarProviderLink
from calendar_integration.services.dataclasses import RoomDirectoryData, RoomWriteData
from calendar_integration.services.room_sync_notifier import RoomSyncOperation
from calendar_integration.signals import resource_room_archived, resource_room_synced


if TYPE_CHECKING:
    from audit_integration.services import OrganizationAuditService
    from calendar_integration.services.protocols.resource_directory_adapter import (
        ResourceDirectoryAdapterResolver,
    )
    from calendar_integration.services.room_sync_notifier import RoomSyncNotifier


logger = logging.getLogger(__name__)


# How long a push keeps retrying transient errors before the link goes to sync
# failed. Counted from the moment the push was first requested.
PUSH_RETRY_WINDOW = datetime.timedelta(hours=6)

# ``last_error`` is shown to admins. Provider messages are short, but keep the
# column bounded in case one is not.
_LAST_ERROR_MAX_LENGTH = 1000

_UNEXPECTED_ERROR_MESSAGE = "Unexpected error while pushing the room to the provider."

_PENDING_STATUS_BY_OPERATION: dict[str, str] = {
    ResourceSyncOperation.CREATE: ResourceSyncStatus.PENDING_CREATION,
    ResourceSyncOperation.UPDATE: ResourceSyncStatus.PENDING_UPDATE,
    ResourceSyncOperation.DELETE: ResourceSyncStatus.PENDING_DELETION,
}
_OPERATION_BY_PENDING_STATUS: dict[str, str] = {
    status: operation for operation, status in _PENDING_STATUS_BY_OPERATION.items()
}

# The link's sync state columns. ``request_push`` and ``retry`` re-read them (and
# ``pending_fields``) under the row lock and save only those columns.
_STATE_FIELDS: tuple[str, ...] = (
    "sync_status",
    "failed_operation",
    "last_error",
    "attempt_count",
    "retry_deadline",
    "archived_at",
)


@dataclass(frozen=True)
class RoomPushOutcome:
    """What ``RoomSyncService.push`` did, for the task that ran it.

    ``retry_attempt`` is set when the push failed transiently before its deadline:
    it is the link's new ``attempt_count``, and the task queues the next attempt
    with that count and the backoff countdown for it. ``retry_deadline`` is the
    link's deadline, so the task can run the last attempt at the deadline rather
    than after it.
    ``retry_attempt`` ``None`` means nothing more to schedule.
    """

    retry_attempt: int | None = None
    retry_deadline: datetime.datetime | None = None


class RoomSyncService:
    """Pushes Vinta Schedule room changes to the provider in the background.

    Stateless: every dependency arrives through ``di_core.containers``.
    """

    def __init__(
        self,
        resource_directory_adapter_resolver: ResourceDirectoryAdapterResolver,
        room_sync_notifier: RoomSyncNotifier,
        audit_service: OrganizationAuditService | None = None,
    ) -> None:
        self.resolver = resource_directory_adapter_resolver
        self.notifier = room_sync_notifier
        self.audit_service = audit_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_push(
        self,
        link: ResourceCalendarProviderLink,
        operation: str,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Record ``operation`` on ``link`` and queue a push after commit.

        ``fields`` is the edit to send, keyed by ``RESOURCE_SYNCED_FIELDS``. It is
        merged into ``pending_fields`` here, under the link's row lock, so pass the
        edit in rather than writing ``pending_fields`` yourself: an edit saved
        outside the lock can overwrite what a push in flight records. A delete
        stamps ``archived_at`` when it is not set yet.

        The row lock waits for a push already in flight, and the status is decided
        from the row as that push left it. ``link`` is refreshed from the row and
        updated in place. The push is queued with ``transaction.on_commit``, so a
        rolled-back write pushes nothing.

        Transitions (spec **State transitions & edge cases**):

        - ``CREATE``: from pending creation, or sync failed on a create, to pending
          creation.
        - ``UPDATE``: from synced or pending update to pending update. While the
          create is still pending, the edit is merged into the create and the
          status stays. While the link is in sync failed, the edit is kept and
          waits for a manual ``retry``.
        - ``DELETE``: to pending deletion. A room whose create was never
          confirmed (pending creation, or sync failed on a create) goes straight
          to archived, with no provider call.

        ``retry_deadline`` is set to now + ``PUSH_RETRY_WINDOW`` when the link was
        not already pending, and kept otherwise, so a stream of edits cannot keep a
        failing push retrying forever. Every request that leaves the link pending
        queues a push with the link's ``attempt_count``; ``push`` drops any task
        whose count is stale, so a link never has more than one retry chain.

        Raises:
            RoomSyncStateError: the operation is not allowed from the link's status,
                for example an edit of a room that is pending deletion or archived.
            ValueError: ``fields`` names a field that is not synced to the provider.
        """
        unknown = set(fields or {}) - set(RESOURCE_SYNCED_FIELDS)
        if unknown:
            raise ValueError(f"Not synced to the provider: {sorted(unknown)!r}")

        with transaction.atomic():
            self._lock_and_refresh(link)
            status = link.sync_status
            failed_operation = (
                link.failed_operation if status == ResourceSyncStatus.SYNC_FAILED else ""
            )
            create_unconfirmed = (
                status == ResourceSyncStatus.PENDING_CREATION
                or failed_operation == ResourceSyncOperation.CREATE
            )

            if status == ResourceSyncStatus.ARCHIVED:
                raise RoomSyncStateError("An archived room cannot be changed.")
            if operation == ResourceSyncOperation.UPDATE:
                if status == ResourceSyncStatus.PENDING_DELETION or (
                    failed_operation == ResourceSyncOperation.DELETE
                ):
                    raise RoomSyncStateError("A room that is being deleted cannot be edited.")
            elif operation == ResourceSyncOperation.CREATE:
                if not create_unconfirmed:
                    raise RoomSyncStateError("This room was already created on the provider.")
            elif operation != ResourceSyncOperation.DELETE:
                raise ValueError(f"Unknown room sync operation: {operation!r}")

            if fields:
                link.pending_fields = {**link.pending_fields, **fields}

            if operation == ResourceSyncOperation.DELETE:
                link.archived_at = link.archived_at or timezone.now()
                if create_unconfirmed:
                    # The create was never confirmed: a confirmed create moves the
                    # link to synced in the same transaction that records the
                    # provider id. So there is no known provider room to delete.
                    self._archive_without_provider(link)
                else:
                    self._mark_pending(link, ResourceSyncStatus.PENDING_DELETION)
            elif operation == ResourceSyncOperation.CREATE or not (
                create_unconfirmed or status == ResourceSyncStatus.SYNC_FAILED
            ):
                self._mark_pending(link, _PENDING_STATUS_BY_OPERATION[operation])
            # Otherwise an update while the create is pending (the create sends the
            # merged values) or while sync failed (the edit goes out with the retry).

            link.save(update_fields=[*_STATE_FIELDS, "pending_fields", "modified"])
            if link.sync_status in _OPERATION_BY_PENDING_STATUS:
                self._enqueue(link)

    def push(self, link_id: int, attempt_count: int) -> RoomPushOutcome:
        """Push the change ``link_id`` is waiting on to the provider.

        Holds the link's row lock for the whole push, provider call included, so
        two pushes of one link run one after the other and the second sees what
        the first did. A link that is not pending (already synced, failed or
        archived, or deleted) is left as it is: that is a replayed task.

        ``attempt_count`` is the link's ``attempt_count`` when the task was
        queued. A task whose count no longer matches is stale and does nothing:
        another task with the same count already ran, and either finished the
        push or queued the next attempt. That keeps one retry chain per link, no
        matter how many times ``request_push`` queued a task.

        A link whose organization is no longer write-enabled for its provider is
        also left untouched. ``is_write_enabled`` is False when the
        ``resource_calendar_provider_sync`` flag is off, so turning the flag off
        while pushes are queued stops them without changing any link.

        Returns:
            The outcome the task needs to schedule a retry.
        """
        with transaction.atomic():
            link = ResourceCalendarProviderLink.objects.locked_for_update(link_id).first()
            if (
                link is None
                or link.sync_status not in _OPERATION_BY_PENDING_STATUS
                or link.attempt_count != attempt_count
            ):
                return RoomPushOutcome()
            organization = link.organization
            if not self.resolver.is_write_enabled(organization, link.provider):
                logger.info("Skipping room push for link %s: room writes are not enabled.", link.pk)
                return RoomPushOutcome()

            operation = _OPERATION_BY_PENDING_STATUS[link.sync_status]
            calendar = Calendar.objects.get(id=link.calendar_fk_id)
            pushed_fields = self._fields_to_push(link, calendar, operation)
            try:
                room = self._call_provider(link, calendar, operation, pushed_fields)
            except ResourceDirectoryError as exc:
                if not (
                    isinstance(exc, ResourceDirectoryNotFoundError)
                    and operation == ResourceSyncOperation.DELETE
                ):
                    return self._handle_failure(link, calendar, operation, exc)
                # Already gone on the provider: the delete is done.
                room = None
            except Exception:  # noqa: BLE001 -- see the comment below
                # A bug in an adapter must not leave the link pending with no task
                # left to push it. Treated as transient, so it is retried and, if it
                # keeps happening, ends in sync failed where admins and Sentry see it.
                logger.exception("Unexpected error pushing room link %s.", link.pk)
                return self._handle_failure(
                    link, calendar, operation, ResourceDirectoryError(_UNEXPECTED_ERROR_MESSAGE)
                )
            self._handle_success(link, calendar, operation, pushed_fields, room)
            return RoomPushOutcome()

    def retry(self, link: ResourceCalendarProviderLink) -> None:
        """Start a failed link over from the operation that failed.

        Sync failed goes back to pending creation, pending update or pending
        deletion, with the attempts reset and a fresh retry deadline, and a push
        is queued after commit. Edits made while the link was in sync failed are
        still in ``pending_fields`` and go out with it.

        Raises:
            RoomSyncStateError: the link is not in sync failed.
        """
        with transaction.atomic():
            self._lock_and_refresh(link)
            if link.sync_status != ResourceSyncStatus.SYNC_FAILED or not link.failed_operation:
                raise RoomSyncStateError("Only a room whose sync failed can be retried.")
            self._mark_pending(link, _PENDING_STATUS_BY_OPERATION[link.failed_operation])
            link.save(update_fields=[*_STATE_FIELDS, "modified"])
            self._enqueue(link)

    # ------------------------------------------------------------------
    # Status changes
    # ------------------------------------------------------------------

    def _lock_and_refresh(self, link: ResourceCalendarProviderLink) -> None:
        # Waits for a push in flight, then reads the state that push left.
        ResourceCalendarProviderLink.objects.locked_for_update(link.pk).get()
        link.refresh_from_db(fields=[*_STATE_FIELDS, "pending_fields"])

    def _mark_pending(self, link: ResourceCalendarProviderLink, status: str) -> None:
        already_pending = link.sync_status in _OPERATION_BY_PENDING_STATUS
        if not already_pending:
            # A new push: a fresh retry window.
            link.attempt_count = 0
            link.retry_deadline = None
            link.last_error = ""
        link.sync_status = status
        link.failed_operation = ""
        if link.retry_deadline is None:
            link.retry_deadline = timezone.now() + PUSH_RETRY_WINDOW

    def _archive_without_provider(self, link: ResourceCalendarProviderLink) -> None:
        link.sync_status = ResourceSyncStatus.ARCHIVED
        link.failed_operation = ""
        link.last_error = ""
        link.attempt_count = 0
        link.retry_deadline = None
        link.archived_at = link.archived_at or timezone.now()
        link.save(update_fields=[*_STATE_FIELDS, "modified"])

    def _handle_success(
        self,
        link: ResourceCalendarProviderLink,
        calendar: Calendar,
        operation: str,
        pushed_fields: Mapping[str, Any],
        room: RoomDirectoryData | None,
    ) -> None:
        if operation == ResourceSyncOperation.CREATE and room is not None:
            calendar.external_id = room.external_id
            calendar.email = room.email
            calendar.save(update_fields=["external_id", "email", "modified"])

        if operation == ResourceSyncOperation.DELETE:
            link.sync_status = ResourceSyncStatus.ARCHIVED
            link.archived_at = link.archived_at or timezone.now()
            link.last_synced_at = timezone.now()
        else:
            link.mark_pushed(pushed_fields, room.synced_values() if room is not None else None)
            link.sync_status = ResourceSyncStatus.SYNCED
        link.failed_operation = ""
        link.last_error = ""
        link.attempt_count = 0
        link.retry_deadline = None
        link.save(
            update_fields=[
                *_STATE_FIELDS,
                "pending_fields",
                "provider_snapshot",
                "last_synced_at",
                "modified",
            ]
        )

        logger.info("Room link %s pushed to the provider (%s).", link.pk, operation)
        self._audit(
            AuditAction.ROOM_PROVIDER_SYNCED,
            link,
            calendar,
            {"operation": operation, "provider": link.provider, "fields": sorted(pushed_fields)},
        )
        calendar_id = calendar.id
        organization_id = link.organization_id
        provider = link.provider
        if operation == ResourceSyncOperation.CREATE:
            transaction.on_commit(
                lambda: resource_room_synced.send(
                    sender=ResourceCalendarProviderLink,
                    calendar_id=calendar_id,
                    organization_id=organization_id,
                    provider=provider,
                    created=True,
                )
            )
        elif operation == ResourceSyncOperation.DELETE:
            transaction.on_commit(
                lambda: resource_room_archived.send(
                    sender=ResourceCalendarProviderLink,
                    calendar_id=calendar_id,
                    organization_id=organization_id,
                    provider=provider,
                )
            )

    def _handle_failure(
        self,
        link: ResourceCalendarProviderLink,
        calendar: Calendar,
        operation: str,
        exc: ResourceDirectoryError,
    ) -> RoomPushOutcome:
        now = timezone.now()
        if link.retry_deadline is None:
            link.retry_deadline = now + PUSH_RETRY_WINDOW
        link.attempt_count += 1
        link.last_error = str(exc)[:_LAST_ERROR_MAX_LENGTH]

        if exc.is_transient and now < link.retry_deadline:
            link.save(update_fields=[*_STATE_FIELDS, "modified"])
            logger.info(
                "Room link %s push failed (attempt %s); retrying.", link.pk, link.attempt_count
            )
            return RoomPushOutcome(
                retry_attempt=link.attempt_count, retry_deadline=link.retry_deadline
            )

        link.sync_status = ResourceSyncStatus.SYNC_FAILED
        link.failed_operation = operation
        link.save(update_fields=[*_STATE_FIELDS, "modified"])
        logger.warning("Room link %s push failed for good (%s).", link.pk, operation)

        self._audit(
            AuditAction.ROOM_PROVIDER_SYNC_FAILED,
            link,
            calendar,
            {"operation": operation, "provider": link.provider, "error": link.last_error},
        )
        self.notifier.notify_sync_failed(calendar.id, RoomSyncOperation(operation), link.last_error)
        self._capture_sync_failed(link, calendar, operation, exc)
        return RoomPushOutcome()

    # ------------------------------------------------------------------
    # Provider calls
    # ------------------------------------------------------------------

    def _fields_to_push(
        self, link: ResourceCalendarProviderLink, calendar: Calendar, operation: str
    ) -> dict[str, Any]:
        """The synced fields this push sends, in the ``pending_fields`` shape.

        A create sends every synced field: the room's current values with the
        pending edits on top, so the snapshot is complete afterwards. An update
        sends only the pending edits. A delete sends nothing.
        """
        if operation == ResourceSyncOperation.DELETE:
            return {}
        pending = {
            field: value
            for field, value in link.pending_fields.items()
            if field in RESOURCE_SYNCED_FIELDS
        }
        if operation == ResourceSyncOperation.UPDATE:
            return pending
        return {**self._current_values(link, calendar), **pending}

    def _current_values(
        self, link: ResourceCalendarProviderLink, calendar: Calendar
    ) -> dict[str, Any]:
        location = link.location
        return {
            "name": calendar.name,
            "description": calendar.description,
            "capacity": calendar.capacity,
            "location_ref": location.location_ref if location is not None else None,
        }

    def _call_provider(
        self,
        link: ResourceCalendarProviderLink,
        calendar: Calendar,
        operation: str,
        pushed_fields: Mapping[str, Any],
    ) -> RoomDirectoryData | None:
        if operation == ResourceSyncOperation.UPDATE and not pushed_fields:
            # Nothing left to send (an earlier push already sent it).
            return None
        adapter = self.resolver.adapter_for(link.organization, link.provider)
        if operation == ResourceSyncOperation.DELETE:
            adapter.delete_room(calendar.external_id)
            return None
        data = RoomWriteData.from_synced_values(
            {**self._current_values(link, calendar), **pushed_fields}, link.provisional_key
        )
        if operation == ResourceSyncOperation.CREATE:
            return adapter.create_room(data)
        return adapter.update_room(calendar.external_id, data, fields=list(pushed_fields))

    # ------------------------------------------------------------------
    # Side effects
    # ------------------------------------------------------------------

    def _enqueue(self, link: ResourceCalendarProviderLink) -> None:
        # Late import: ``calendar_integration.tasks`` imports this module through
        # the task's type annotation, so a top-level import would be circular.
        from calendar_integration.tasks import push_room_to_provider_task

        link_id = link.pk
        organization_id = link.organization_id
        attempt_count = link.attempt_count
        transaction.on_commit(
            lambda: push_room_to_provider_task.delay(  # type: ignore[attr-defined]
                link_id=link_id, organization_id=organization_id, attempt_count=attempt_count
            )
        )

    def _audit(
        self,
        action: str,
        link: ResourceCalendarProviderLink,
        calendar: Calendar,
        diff: dict[str, Any],
    ) -> None:
        if self.audit_service is None:
            return
        self.audit_service.record(
            action=action,
            actor=self.audit_service.system_actor(),
            subject=self.audit_service.subject_from_instance(calendar, label=calendar.name),
            diff=diff,
            scope=self.audit_service.scope_from_organization_id(link.organization_id),
        )

    def _capture_sync_failed(
        self,
        link: ResourceCalendarProviderLink,
        calendar: Calendar,
        operation: str,
        exc: ResourceDirectoryError,
    ) -> None:
        # Opaque ids and the error class only: no room name, no provider message.
        tags = {
            "room_sync.link_id": str(link.pk),
            "room_sync.calendar_id": str(calendar.id),
            "room_sync.organization_id": str(link.organization_id),
            "room_sync.provider": link.provider,
            "room_sync.operation": operation,
            "room_sync.error_type": type(exc).__name__,
        }

        def _capture() -> None:
            with sentry_sdk.new_scope() as scope:
                for key, value in tags.items():
                    scope.set_tag(key, value)
                sentry_sdk.capture_message("Room provider sync failed", level="error")

        transaction.on_commit(_capture)
