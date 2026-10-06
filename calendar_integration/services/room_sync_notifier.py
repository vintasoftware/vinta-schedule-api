"""Email notifications for the room (resource calendar) provider sync.

``RoomSyncNotifier`` is the one place the sync engines reach for when a human
has to hear about a room: a push to the provider gave up, a queued edit was
dropped because the provider changed the same field first, a room vanished on
the provider side and left bookings flagged, or a booking lost or changed its
room during a delete.

Who gets the email:

* **Org admins** for the three room-level notices: the active memberships
  ``OrganizationMembership.objects.administrators(...)`` returns, the same set
  ``ExternalEventChangeRequestService._notify_eligible_approvers`` emails, so
  the people told about a room are the people allowed to manage the
  organization.
* **The organizer** for a booking that moved room or lost its room. The caller
  names the organizer; this service does not work out who owns an event.

Every send is deferred with ``transaction.on_commit``, so a rolled-back sync
step sends nothing. Room-level emails carry only the room's id and name plus a
short reason the caller wrote for admins; they never carry attendee or event
content. The organizer email names the organizer's own event.

The room is read through the organization-scoped manager, so the caller must
run under a bound organization (every sync task binds one from its own
arguments). The organization id is taken from the room, not from the caller,
so a wrong binding cannot address the admins of another tenant.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import TextChoices

from vintasend.constants import NotificationTypes
from vintasend.services.notification_service import NotificationContextDict

from calendar_integration.constants import BookingRoomChange
from calendar_integration.models import Calendar, CalendarEvent
from organizations.models import OrganizationMembership


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationService


class RoomSyncOperation(TextChoices):
    """The provider write that failed, as shown to org admins."""

    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"


_EMAIL_DIR = "calendar_integration/emails"


class RoomSyncNotifier:
    """Emails org admins and organizers about room sync events.

    Stateless: the only dependency is the vintasend ``NotificationService``,
    injected by ``di_core.containers``.
    """

    def __init__(self, notification_service: NotificationService) -> None:
        self.notification_service = notification_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_sync_failed(
        self, calendar_id: int, operation: RoomSyncOperation, reason: str
    ) -> None:
        """Tell org admins a provider write for a room gave up.

        Args:
            calendar_id: PK of the room's ``Calendar``.
            operation: The write that failed.
            reason: A short, generic sentence written for admins. Callers
                must not put attendee or event content in it.
        """
        room = self._get_room(calendar_id)
        self._notify_admins(
            organization_id=room.organization_id,
            title="Room sync failed",
            template_name="room_sync_failed",
            context_name="room_sync_failed_context",
            context_kwargs={
                "room_id": room.id,
                "room_name": room.name,
                "operation": operation.value,
                "reason": reason,
                "organization_id": room.organization_id,
            },
        )

    def notify_edit_discarded(self, calendar_id: int, fields: Sequence[str]) -> None:
        """Tell org admins a queued edit to a room was dropped.

        The provider changed the same fields since the last successful sync, so
        the provider's values won and the queued values were discarded.

        Args:
            calendar_id: PK of the room's ``Calendar``.
            fields: Names of the fields whose queued values were dropped. At
                least one: an email saying nothing was discarded is a bug in
                the caller, refused here before anything is queued.
        """
        if not fields:
            raise ValueError("notify_edit_discarded needs at least one discarded field")
        room = self._get_room(calendar_id)
        self._notify_admins(
            organization_id=room.organization_id,
            title="Room edit discarded",
            template_name="room_edit_discarded",
            context_name="room_edit_discarded_context",
            context_kwargs={
                "room_id": room.id,
                "room_name": room.name,
                # vintasend stores the context as JSON and only accepts
                # dict-shaped list items, so each name travels as ``{"name": ...}``;
                # the context function flattens them back for the template.
                "fields": [NotificationContextDict({"name": field}) for field in fields],
                "organization_id": room.organization_id,
            },
        )

    def notify_bookings_flagged(self, calendar_id: int, count: int) -> None:
        """Tell org admins a room deleted on the provider side has flagged bookings.

        Args:
            calendar_id: PK of the archived room's ``Calendar``.
            count: How many future bookings were flagged for resolution.
        """
        room = self._get_room(calendar_id)
        self._notify_admins(
            organization_id=room.organization_id,
            title="Room bookings need attention",
            template_name="room_bookings_flagged",
            context_name="room_bookings_flagged_context",
            context_kwargs={
                "room_id": room.id,
                "room_name": room.name,
                "count": count,
                "organization_id": room.organization_id,
            },
        )

    def notify_booking_room_changed(
        self, event_id: int, organizer_user_id: int, change: BookingRoomChange
    ) -> None:
        """Tell an organizer their booking moved room, lost its room, or was cancelled.

        Args:
            event_id: PK of the booking's ``CalendarEvent``.
            organizer_user_id: PK of the ``User`` to email. Resolved by the
                caller, which knows how the booking was made.
            change: What happened to the booking's room.
        """
        event = CalendarEvent.objects.get(id=event_id)
        self._schedule(
            user_id=organizer_user_id,
            title="Your room booking changed",
            template_name="booking_room_changed",
            context_name="booking_room_changed_context",
            context_kwargs={
                "event_id": event.id,
                "event_title": event.title,
                # The stored wall-clock with the event's own timezone attached
                # (see ``CalendarEvent.local_start``), formatted here because the
                # context is stored as JSON.
                "event_start": f"{event.local_start:%Y-%m-%d %H:%M} ({event.timezone})",
                "change": change.value,
                "organization_id": event.organization_id,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_room(self, calendar_id: int) -> Calendar:
        return Calendar.objects.get(id=calendar_id)

    def _notify_admins(
        self,
        *,
        organization_id: int,
        title: str,
        template_name: str,
        context_name: str,
        context_kwargs: dict[str, object],
    ) -> None:
        admin_user_ids = OrganizationMembership.objects.administrators(organization_id).values_list(
            "user_id", flat=True
        )
        for user_id in admin_user_ids:
            self._schedule(
                user_id=user_id,
                title=title,
                template_name=template_name,
                context_name=context_name,
                context_kwargs=context_kwargs,
            )

    def _schedule(
        self,
        *,
        user_id: int,
        title: str,
        template_name: str,
        context_name: str,
        context_kwargs: dict[str, object],
    ) -> None:
        """Queue one email for ``user_id``, sent only after the transaction commits."""
        transaction.on_commit(
            self._make_send(
                user_id=user_id,
                title=title,
                template_name=template_name,
                context_name=context_name,
                context_kwargs=context_kwargs,
            )
        )

    def _make_send(
        self,
        *,
        user_id: int,
        title: str,
        template_name: str,
        context_name: str,
        context_kwargs: dict[str, object],
    ) -> Callable[[], None]:
        # A factory, not a lambda in the loop, so each callback binds its own
        # user id and mypy can see the callback returns None.
        notification_service = self.notification_service

        def _send() -> None:
            notification_service.create_notification(
                user_id=user_id,
                notification_type=NotificationTypes.EMAIL.value,
                title=title,
                body_template=f"{_EMAIL_DIR}/{template_name}.body.html",
                context_name=context_name,
                context_kwargs=NotificationContextDict(dict(context_kwargs)),
                subject_template=f"{_EMAIL_DIR}/{template_name}.subject.txt",
                preheader_template=f"{_EMAIL_DIR}/{template_name}.pre_header.txt",
            )

        return _send
