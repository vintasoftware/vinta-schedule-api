"""Contract for telling an organizer what happened to their room booking.

``BookingResolutionService.apply`` codes against this protocol. The real
implementation is ``RoomSyncNotifier.notify_booking_room_changed``, which emails the
organizer through vintasend once the caller's transaction commits.
"""

from typing import Protocol

from calendar_integration.constants import BookingRoomChange


class BookingRoomChangeNotifier(Protocol):
    """Emails the organizer of a booking whose room was moved, removed or cancelled."""

    def notify_booking_room_changed(
        self, event_id: int, organizer_user_id: int, change: BookingRoomChange
    ) -> None:
        """Tell ``organizer_user_id`` that the booking ``event_id`` changed.

        Reads the event when called, so call it before the event is deleted. The
        email itself is sent on commit.
        """
        ...
