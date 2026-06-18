import datetime
from typing import cast

import strawberry
import strawberry_django

from calendar_integration.models import (
    AvailableTime,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    RecurrenceRule,
    ResourceAllocation,
)
from users.graphql import UserGraphQLType


def _owner_scoped_calendar_ids(info: strawberry.Info) -> set[int] | None:
    """Return the owner's allowed calendar-id set for the current request, or None.

    None => org-wide token (no owner scoping; nested fields resolve unchanged).
    A set (possibly empty) => a provider-scoped token; nested relations that reach
    other calendars must be intersected against this set.

    This is the per-field-resolver counterpart of the top-level read enforcement in
    ``public_api.queries`` / ``public_api.helpers``. A scoped token may legitimately
    fetch its OWN event/calendar, but GraphQL field traversal can follow relations
    (bundle representations, the bundle calendar, resource calendars, group
    selections, recurring instances) onto calendars owned by ANOTHER provider. Each
    such nested field filters its queryset through this set to close that leak.
    """
    # Imported lazily to avoid a public_api -> calendar_integration -> public_api
    # import cycle at module load.
    from public_api.scoping import scoped_calendar_ids

    request = info.context.request
    system_user = getattr(request, "public_api_system_user", None)
    organization = getattr(request, "public_api_organization", None)
    if system_user is None or organization is None:
        return None
    return scoped_calendar_ids(system_user, organization)


def _scoped_calendar_or_none(
    calendar: "Calendar | None", info: strawberry.Info
) -> "Calendar | None":
    """Return a related Calendar only if it is in the owner's set.

    Org-wide tokens (allowed is None) get the calendar unchanged.
    """
    if calendar is None:
        return None
    allowed = _owner_scoped_calendar_ids(info)
    if allowed is not None and calendar.id not in allowed:
        return None
    return calendar


def _scoped_event_or_none(
    event: "CalendarEvent | None", info: strawberry.Info
) -> "CalendarEvent | None":
    """Return a related CalendarEvent only if its calendar is in the owner's set.

    Org-wide tokens (allowed is None) get the event unchanged.
    """
    if event is None:
        return None
    allowed = _owner_scoped_calendar_ids(info)
    if allowed is not None and getattr(event, "calendar_fk_id", None) not in allowed:
        return None
    return event


def _scoped_event_list(
    events, info: strawberry.Info, *, organization_id: int
) -> list["CalendarEvent"]:
    """Filter a related CalendarEvent queryset to the owner's calendar set.

    Org-wide tokens (allowed is None) get the queryset unchanged except for the
    explicit organization filter required by the tenant safety net (the parent's
    organization, which every related row shares).
    """
    events = events.filter(organization_id=organization_id)
    allowed = _owner_scoped_calendar_ids(info)
    if allowed is not None:
        events = events.filter(calendar_fk_id__in=allowed)
    return list(events)


def _scoped_blocked_time_or_none(
    blocked_time: "BlockedTime | None", info: strawberry.Info
) -> "BlockedTime | None":
    """Return a related BlockedTime only if its calendar is in the owner's set.

    Org-wide tokens (allowed is None) get the blocked time unchanged.
    """
    if blocked_time is None:
        return None
    allowed = _owner_scoped_calendar_ids(info)
    if allowed is not None and getattr(blocked_time, "calendar_fk_id", None) not in allowed:
        return None
    return blocked_time


def _scoped_available_time_or_none(
    available_time: "AvailableTime | None", info: strawberry.Info
) -> "AvailableTime | None":
    """Return a related AvailableTime only if its calendar is in the owner's set.

    Org-wide tokens (allowed is None) get the available time unchanged.
    """
    if available_time is None:
        return None
    allowed = _owner_scoped_calendar_ids(info)
    if allowed is not None and getattr(available_time, "calendar_fk_id", None) not in allowed:
        return None
    return available_time


@strawberry_django.type(Calendar)
class CalendarGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    description: strawberry.auto
    email: strawberry.auto
    external_id: strawberry.auto
    provider: strawberry.auto
    calendar_type: strawberry.auto
    capacity: strawberry.auto
    manage_available_windows: strawberry.auto
    sync_enabled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(RecurrenceRule)
class RecurrenceRuleGraphQLType:
    id: strawberry.auto  # noqa: A003
    frequency: strawberry.auto
    interval: strawberry.auto
    count: strawberry.auto
    until: strawberry.auto
    by_weekday: strawberry.auto
    by_month_day: strawberry.auto
    by_month: strawberry.auto
    by_year_day: strawberry.auto
    by_week_number: strawberry.auto
    by_hour: strawberry.auto
    by_minute: strawberry.auto
    by_second: strawberry.auto
    week_start: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def rrule_string(self) -> str:
        return self.to_rrule_string()  # type: ignore


@strawberry_django.type(ExternalAttendee)
class ExternalAttendeeGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    email: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(EventAttendance)
class EventAttendanceGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    user: UserGraphQLType = strawberry_django.field()


@strawberry_django.type(EventExternalAttendance)
class EventExternalAttendanceGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    external_attendee: ExternalAttendeeGraphQLType = strawberry_django.field()

    @strawberry.field
    def event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The event this external attendance record belongs to.

        Guarded so a scoped token cannot reach an event whose calendar is outside the
        owner's set via this back-pointer.
        """
        attendance = cast(EventExternalAttendance, self)
        return cast(
            "CalendarEventGraphQLType | None",
            _scoped_event_or_none(attendance.event, info),
        )


@strawberry_django.type(ResourceAllocation)
class ResourceAllocationGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    calendar: CalendarGraphQLType = strawberry_django.field()


@strawberry_django.type(EventRecurrenceException)
class EventRecurrenceExceptionGraphQLType:
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def parent_event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The parent recurring event this exception applies to.

        Guarded so a scoped token cannot reach an event whose calendar is outside the
        owner's set via this second-hop pointer.
        """
        exc = cast(EventRecurrenceException, self)
        return cast(
            "CalendarEventGraphQLType | None",
            _scoped_event_or_none(exc.parent_event, info),
        )

    @strawberry.field
    def modified_event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The modified event instance for this exception (None if cancelled).

        Guarded so a scoped token cannot reach an event whose calendar is outside the
        owner's set via this second-hop pointer.
        """
        exc = cast(EventRecurrenceException, self)
        return cast(
            "CalendarEventGraphQLType | None",
            _scoped_event_or_none(exc.modified_event, info),
        )


@strawberry_django.type(CalendarEvent)
class CalendarEventGraphQLType:
    id: strawberry.auto  # noqa: A003
    title: strawberry.auto
    description: strawberry.auto
    external_id: strawberry.auto
    start_time: strawberry.auto
    end_time: strawberry.auto
    recurrence_id: strawberry.auto
    is_recurring_exception: strawberry.auto
    is_bundle_primary: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    # Same-event scalars / own-calendar relations (inherently safe — see field resolvers
    # and the Phase 5 security review for why each cannot reach another owner's data).
    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()

    # Many-to-many relationships through intermediary models. attendances /
    # external_attendances / recurrence_exceptions describe THIS event (people and this
    # event's own occurrence exceptions); they carry no other-owner calendar data, so
    # they resolve unchanged. resource_allocations is owner-guarded below because it
    # exposes resource calendars.
    attendances: list[EventAttendanceGraphQLType] = strawberry_django.field()
    external_attendances: list[EventExternalAttendanceGraphQLType] = strawberry_django.field()
    recurrence_exceptions: list[EventRecurrenceExceptionGraphQLType] = strawberry_django.field()

    # Direct many-to-many relationships (simplified access). attendees / external_attendees
    # are people, not calendar-scoped data, so they resolve unchanged.
    attendees: list[UserGraphQLType] = strawberry_django.field()
    external_attendees: list[ExternalAttendeeGraphQLType] = strawberry_django.field()

    # ------------------------------------------------------------------ #
    # Owner-scoped nested resolvers (Phase 5 — close nested-field traversal) #
    # ------------------------------------------------------------------ #
    # A provider-scoped token may legitimately fetch its OWN event, but the relations
    # below can point at calendars owned by ANOTHER provider in the same org. Each
    # resolver intersects against the request's owner calendar-id set; org-wide tokens
    # (allowed is None) keep the original behavior. The model attributes (`self.calendar`,
    # `self.bundle_representations`, etc.) are populated from the parent query's
    # org-scoped queryset, so reading them never re-triggers the tenant safety net.

    @strawberry.field
    def calendar(self, info: strawberry.Info) -> CalendarGraphQLType | None:
        """The event's own calendar. Hidden when the calendar is outside the owner's set."""
        event = cast(CalendarEvent, self)
        return cast("CalendarGraphQLType | None", _scoped_calendar_or_none(event.calendar, info))

    @strawberry.field
    def bundle_calendar(self, info: strawberry.Info) -> CalendarGraphQLType | None:
        """The bundle calendar this event was created through (may be another owner's)."""
        event = cast(CalendarEvent, self)
        return cast(
            "CalendarGraphQLType | None", _scoped_calendar_or_none(event.bundle_calendar, info)
        )

    @strawberry.field
    def bundle_primary_event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The bundle primary event (hosted on the bundle's primary calendar)."""
        event = cast(CalendarEvent, self)
        return cast(
            "CalendarEventGraphQLType | None",
            _scoped_event_or_none(event.bundle_primary_event, info),
        )

    @strawberry.field
    def bulk_modification_parent(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The original recurring event this continuation was split from."""
        event = cast(CalendarEvent, self)
        return cast(
            "CalendarEventGraphQLType | None",
            _scoped_event_or_none(event.bulk_modification_parent, info),
        )

    @strawberry.field
    def parent_recurring_object(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The recurring parent event this instance belongs to."""
        event = cast(CalendarEvent, self)
        return cast(
            "CalendarEventGraphQLType | None",
            _scoped_event_or_none(event.parent_recurring_object, info),
        )

    @strawberry.field
    def resource_allocations(self, info: strawberry.Info) -> list[ResourceAllocationGraphQLType]:
        """Resource allocations; resource calendars outside the owner's set are excluded."""
        event = cast(CalendarEvent, self)
        qs = event.resource_allocations.filter(organization_id=event.organization_id)
        allowed = _owner_scoped_calendar_ids(info)
        if allowed is not None:
            qs = qs.filter(calendar_fk_id__in=allowed)
        return cast(list[ResourceAllocationGraphQLType], list(qs))

    @strawberry.field
    def resources(self, info: strawberry.Info) -> list[CalendarGraphQLType]:
        """Resource calendars allocated to this event; other-owner calendars excluded."""
        event = cast(CalendarEvent, self)
        qs = event.resources.filter(organization_id=event.organization_id)
        allowed = _owner_scoped_calendar_ids(info)
        if allowed is not None:
            qs = qs.filter(id__in=allowed)
        return cast(list[CalendarGraphQLType], list(qs))

    @strawberry.field
    def calendar_group(self, info: strawberry.Info) -> "CalendarGroupGraphQLType | None":
        """The CalendarGroup this event was booked through.

        A CalendarGroup aggregates calendars across providers via its slots, so for a
        scoped token we suppress it entirely (group membership is not owner data and the
        group's slots would expose other-owner calendars). Org-wide tokens see it.
        """
        if _owner_scoped_calendar_ids(info) is not None:
            return None
        event = cast(CalendarEvent, self)
        return cast("CalendarGroupGraphQLType | None", event.calendar_group)

    @strawberry.field
    def group_selections(
        self, info: strawberry.Info
    ) -> list["CalendarEventGroupSelectionGraphQLType"]:
        """Per-slot calendar picks; selections on other-owner calendars are excluded."""
        event = cast(CalendarEvent, self)
        qs = event.group_selections.filter(organization_id=event.organization_id)
        allowed = _owner_scoped_calendar_ids(info)
        if allowed is not None:
            qs = qs.filter(calendar_fk_id__in=allowed)
        return cast(list["CalendarEventGroupSelectionGraphQLType"], list(qs))

    @strawberry.field
    def bundle_representations(self, info: strawberry.Info) -> list["CalendarEventGraphQLType"]:
        """Representation events in child calendars; other-owner ones are excluded."""
        event = cast(CalendarEvent, self)
        return cast(
            list["CalendarEventGraphQLType"],
            _scoped_event_list(
                event.bundle_representations, info, organization_id=event.organization_id
            ),
        )

    @strawberry.field
    def bulk_modifications(self, info: strawberry.Info) -> list["CalendarEventGraphQLType"]:
        """Continuation events created by bulk modifications; other-owner ones excluded."""
        event = cast(CalendarEvent, self)
        return cast(
            list["CalendarEventGraphQLType"],
            _scoped_event_list(
                event.bulk_modifications, info, organization_id=event.organization_id
            ),
        )

    @strawberry.field
    def recurring_instances(self, info: strawberry.Info) -> list["CalendarEventGraphQLType"]:
        """Individual instances of this recurring event; other-owner ones excluded."""
        # The Django related accessor for parent_recurring_object is
        # `calendarevent_recurring_instances` (RecurringMixin uses a `%(class)s`-templated
        # related_name); the GraphQL field is still exposed as `recurringInstances`.
        event = cast(CalendarEvent, self)
        return cast(
            list["CalendarEventGraphQLType"],
            _scoped_event_list(
                event.calendarevent_recurring_instances,
                info,
                organization_id=event.organization_id,
            ),
        )

    # Properties
    @strawberry.field
    def is_recurring(self) -> bool:
        return self.is_recurring  # type: ignore

    @strawberry.field
    def is_recurring_instance(self) -> bool:
        return self.is_recurring_instance  # type: ignore

    @strawberry.field
    def is_bundle_event(self) -> bool:
        return self.is_bundle_event  # type: ignore

    @strawberry.field
    def is_bundle_representation(self) -> bool:
        return self.is_bundle_representation  # type: ignore

    @strawberry.field
    def duration_seconds(self) -> int:
        """Duration of the event in seconds"""
        return int(self.duration.total_seconds())  # type: ignore


@strawberry_django.type(BlockedTimeRecurrenceException)
class BlockedTimeRecurringExceptionGraphQLType:
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def parent_blocked_time(self, info: strawberry.Info) -> "BlockedTimeGraphQLType | None":
        """The parent recurring blocked time this exception applies to.

        Guarded so a scoped token cannot reach a blocked time whose calendar is outside
        the owner's set via this second-hop pointer.
        """
        exc = cast(BlockedTimeRecurrenceException, self)
        return cast(
            "BlockedTimeGraphQLType | None",
            _scoped_blocked_time_or_none(exc.parent_blocked_time, info),
        )

    @strawberry.field
    def modified_blocked_time(self, info: strawberry.Info) -> "BlockedTimeGraphQLType | None":
        """The modified blocked time instance for this exception (None if cancelled).

        Guarded so a scoped token cannot reach a blocked time whose calendar is outside
        the owner's set via this second-hop pointer.
        """
        exc = cast(BlockedTimeRecurrenceException, self)
        return cast(
            "BlockedTimeGraphQLType | None",
            _scoped_blocked_time_or_none(exc.modified_blocked_time, info),
        )


@strawberry_django.type(BlockedTime)
class BlockedTimeGraphQLType:
    id: strawberry.auto  # noqa: A003
    start_time: strawberry.auto
    end_time: strawberry.auto
    external_id: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    user: UserGraphQLType = strawberry_django.field()
    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()
    recurrence_exceptions: list[BlockedTimeRecurringExceptionGraphQLType] = (
        strawberry_django.field()
    )

    @strawberry.field
    def calendar(self, info: strawberry.Info) -> CalendarGraphQLType | None:
        """The blocked time's calendar. Hidden when outside the owner's set."""
        blocked_time = cast(BlockedTime, self)
        return cast(
            "CalendarGraphQLType | None",
            _scoped_calendar_or_none(blocked_time.calendar, info),
        )


@strawberry_django.type(AvailableTimeRecurrenceException)
class AvailableTimeRecurringExceptionGraphQLType:
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def parent_available_time(self, info: strawberry.Info) -> "AvailableTimeGraphQLType | None":
        """The parent recurring available time this exception applies to.

        Guarded so a scoped token cannot reach an available time whose calendar is outside
        the owner's set via this second-hop pointer.
        """
        exc = cast(AvailableTimeRecurrenceException, self)
        return cast(
            "AvailableTimeGraphQLType | None",
            _scoped_available_time_or_none(exc.parent_available_time, info),
        )

    @strawberry.field
    def modified_available_time(self, info: strawberry.Info) -> "AvailableTimeGraphQLType | None":
        """The modified available time instance for this exception (None if cancelled).

        Guarded so a scoped token cannot reach an available time whose calendar is outside
        the owner's set via this second-hop pointer.
        """
        exc = cast(AvailableTimeRecurrenceException, self)
        return cast(
            "AvailableTimeGraphQLType | None",
            _scoped_available_time_or_none(exc.modified_available_time, info),
        )


@strawberry_django.type(AvailableTime)
class AvailableTimeGraphQLType:
    id: strawberry.auto  # noqa: A003
    start_time: strawberry.auto
    end_time: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    user: UserGraphQLType = strawberry_django.field()
    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()
    recurrence_exceptions: list[AvailableTimeRecurringExceptionGraphQLType] = (
        strawberry_django.field()
    )

    @strawberry.field
    def calendar(self, info: strawberry.Info) -> CalendarGraphQLType | None:
        """The available time's calendar. Hidden when outside the owner's set."""
        available_time = cast(AvailableTime, self)
        return cast(
            "CalendarGraphQLType | None",
            _scoped_calendar_or_none(available_time.calendar, info),
        )


@strawberry.type
class AvailableTimeWindowGraphQLType:
    start_time: datetime.datetime
    end_time: datetime.datetime
    id: int | None  # noqa: A003
    can_book_partially: bool


@strawberry.type
class UnavailableTimeWindowGraphQLType:
    """Minimal GraphQL representation for unavailable time windows.

    The full unavailable window carries either a CalendarEventData or
    BlockedTimeData payload. For the public API we expose the time range,
    id and reason so clients can identify and fetch details separately if
    required.
    """

    start_time: datetime.datetime
    end_time: datetime.datetime
    id: int  # noqa: A003
    reason: str


@strawberry_django.type(CalendarWebhookSubscription)
class CalendarWebhookSubscriptionGraphQLType:
    """GraphQL type for calendar webhook subscriptions."""

    id: strawberry.auto  # noqa: A003
    provider: strawberry.auto
    external_subscription_id: strawberry.auto
    external_resource_id: strawberry.auto
    callback_url: strawberry.auto
    channel_id: strawberry.auto
    resource_uri: strawberry.auto
    verification_token: strawberry.auto
    expires_at: strawberry.auto
    is_active: strawberry.auto
    last_notification_at: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    calendar: CalendarGraphQLType


@strawberry_django.type(CalendarWebhookEvent)
class CalendarWebhookEventGraphQLType:
    """GraphQL type for calendar webhook events."""

    id: strawberry.auto  # noqa: A003
    provider: strawberry.auto
    event_type: strawberry.auto
    external_calendar_id: strawberry.auto
    external_event_id: strawberry.auto
    processed_at: strawberry.auto
    processing_status: strawberry.auto
    sync_triggered: bool
    error_message: str | None
    created: datetime.datetime
    modified: datetime.datetime

    subscription: CalendarWebhookSubscriptionGraphQLType | None


@strawberry.type
class WebhookSubscriptionStatusGraphQLType:
    """GraphQL type for webhook subscription health status."""

    total_subscriptions: int
    active_subscriptions: int
    expired_subscriptions: int
    expiring_soon_subscriptions: int  # expiring within 24 hours
    recent_events_count: int  # events in last 24 hours
    failed_events_count: int  # failed events in last 24 hours
    success_rate: float  # percentage of successful events in last 24 hours


# ---------------------------------------------------------------------------
# CalendarGroup types
# ---------------------------------------------------------------------------
@strawberry_django.type(CalendarGroupSlot)
class CalendarGroupSlotGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    description: strawberry.auto
    order: strawberry.auto
    required_count: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def calendars(self, info: strawberry.Info) -> list[CalendarGraphQLType]:
        """The pool of calendars eligible for this slot.

        Defense-in-depth: for a scoped token the pool is filtered to the owner's
        calendar-id set so that even if the slot is reached through an unguarded path,
        other-owner calendars in the pool are not enumerable. Org-wide tokens see the
        entire pool unchanged.
        """
        slot = cast(CalendarGroupSlot, self)
        qs = slot.calendars.filter(organization_id=slot.organization_id)
        allowed = _owner_scoped_calendar_ids(info)
        if allowed is not None:
            qs = qs.filter(id__in=allowed)
        return cast(list[CalendarGraphQLType], list(qs))


@strawberry_django.type(CalendarGroup)
class CalendarGroupGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    description: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    slots: list[CalendarGroupSlotGraphQLType] = strawberry_django.field()


@strawberry_django.type(CalendarEventGroupSelection)
class CalendarEventGroupSelectionGraphQLType:
    id: strawberry.auto  # noqa: A003
    created: datetime.datetime
    modified: datetime.datetime

    calendar: CalendarGraphQLType = strawberry_django.field()

    @strawberry.field
    def slot(self, info: strawberry.Info) -> "CalendarGroupSlotGraphQLType | None":
        """The CalendarGroupSlot this selection corresponds to.

        Suppressed entirely for scoped tokens: a slot's calendar pool spans ALL
        providers in the group, so exposing it would enumerate other-owner calendars
        even after the per-slot ``calendars`` filter. Org-wide tokens see the slot
        unchanged (mirror of the ``calendarGroup`` suppression at ~L340).
        """
        if _owner_scoped_calendar_ids(info) is not None:
            return None
        sel = cast(CalendarEventGroupSelection, self)
        return cast("CalendarGroupSlotGraphQLType | None", sel.slot)


@strawberry.type
class CalendarGroupSlotAvailabilityGraphQLType:
    """How many of a slot's pool calendars are available for a given range."""

    slot_id: int
    available_calendar_ids: list[int]
    required_count: int

    @strawberry.field
    def is_bookable(self) -> bool:
        """True when enough calendars are free to satisfy the slot's required_count."""
        return len(self.available_calendar_ids) >= self.required_count


@strawberry.type
class CalendarGroupRangeAvailabilityGraphQLType:
    """Per-slot availability for a single range."""

    start_time: datetime.datetime
    end_time: datetime.datetime
    slots: list[CalendarGroupSlotAvailabilityGraphQLType]


@strawberry.type
class BookableSlotProposalGraphQLType:
    """A concrete time window where every slot in a group is satisfied."""

    start_time: datetime.datetime
    end_time: datetime.datetime
