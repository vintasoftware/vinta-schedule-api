import datetime
import enum

import strawberry
import strawberry_django

from calendar_integration.models import (
    AvailableTime,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeRecurrenceException,
    BookingPolicy,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarOwnership,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    ExternalEventChangeRequest,
    RecurrenceRule,
    ResourceAllocation,
)
from users.graphql import UserGraphQLType


# ---------------------------------------------------------------------------
# Owner-scope helpers for nested GraphQL field traversal
# ---------------------------------------------------------------------------
#
# Strawberry runs the top-level field's permission/resolver logic only on the
# decorated field. A provider-scoped public-API token that fetches one of its
# OWN objects can therefore traverse NESTED GraphQL fields to reach data on
# calendars it does NOT own (an event on a non-owned calendar reached via a
# back-pointer, the cross-provider candidate pool of a group slot, etc.).
#
# The resolvers below close those nested leaks. They are a strict NO-OP for
# org-wide tokens and for internal (non-public-API) GraphQL requests: when
# ``_owner_scoped_calendar_ids(info)`` returns ``None`` every resolver returns
# the original value untouched, so existing consumers are byte-for-byte
# unchanged. Only a provider-scoped token (which yields a concrete ``set[int]``
# of the owner's calendar ids) triggers any filtering.


def _owner_scoped_calendar_ids(info: strawberry.Info) -> set[int] | None:
    """Return the owner's allowed calendar-id set, or ``None`` for no filtering.

    ``None`` means "do not filter" and is returned for BOTH:
      * internal / non-public-API GraphQL requests — the request carries no
        ``public_api_system_user`` attribute (e.g. internal callers or tests
        that mock the request), so there is nothing to scope; and
      * org-wide public-API tokens — ``scoped_calendar_ids`` returns ``None``.

    A concrete ``set[int]`` (possibly empty) is returned only for a
    provider-scoped token; nested resolvers then restrict returned objects to
    that owner's calendars.

    The import of ``public_api.scoping`` is deferred to avoid an import cycle
    between ``calendar_integration`` and ``public_api``.
    """
    request = getattr(info.context, "request", None)
    if request is None:
        return None
    system_user = getattr(request, "public_api_system_user", None)
    if system_user is None:
        return None
    organization = getattr(request, "public_api_organization", None)
    if organization is None:
        return None

    # Lazy import to break the public_api <-> calendar_integration import cycle.
    from public_api.scoping import scoped_calendar_ids

    return scoped_calendar_ids(system_user, organization)


def _scoped_calendar_or_none(
    calendar: "Calendar | None", allowed_ids: set[int] | None
) -> "Calendar | None":
    """Return ``calendar`` unless it is outside the owner's allowed set."""
    if allowed_ids is None or calendar is None:
        return calendar
    return calendar if calendar.id in allowed_ids else None


def _scoped_event_or_none(
    event: "CalendarEvent | None", allowed_ids: set[int] | None
) -> "CalendarEvent | None":
    """Return ``event`` unless its calendar is outside the owner's allowed set."""
    if allowed_ids is None or event is None:
        return event
    return event if getattr(event, "calendar_fk_id", None) in allowed_ids else None


def _scoped_event_list(
    events: "list[CalendarEvent]", allowed_ids: set[int] | None
) -> "list[CalendarEvent]":
    """Filter a list of events to those whose calendar is in the owner's set."""
    if allowed_ids is None:
        return events
    return [e for e in events if getattr(e, "calendar_fk_id", None) in allowed_ids]


def _scoped_blocked_time_or_none(
    blocked_time: "BlockedTime | None", allowed_ids: set[int] | None
) -> "BlockedTime | None":
    """Return ``blocked_time`` unless its calendar is outside the owner's set."""
    if allowed_ids is None or blocked_time is None:
        return blocked_time
    return blocked_time if getattr(blocked_time, "calendar_fk_id", None) in allowed_ids else None


def _scoped_available_time_or_none(
    available_time: "AvailableTime | None", allowed_ids: set[int] | None
) -> "AvailableTime | None":
    """Return ``available_time`` unless its calendar is outside the owner's set."""
    if allowed_ids is None or available_time is None:
        return available_time
    return (
        available_time if getattr(available_time, "calendar_fk_id", None) in allowed_ids else None
    )


def _scoped_calendar_list(
    calendars: "list[Calendar]", allowed_ids: set[int] | None
) -> "list[Calendar]":
    """Filter a list of calendars to those in the owner's set."""
    if allowed_ids is None:
        return calendars
    return [c for c in calendars if c.id in allowed_ids]


@strawberry.type
class OwnershipMembershipGraphQLType:
    """Membership identity for a calendar owner.

    A membership has no scalar id (it is identified by the ``(user_id,
    organization_id)`` pair), so the external representation exposes that pair
    plus the membership ``role``.
    """

    user_id: int
    organization_id: int
    role: str


@strawberry_django.type(CalendarOwnership)
class CalendarOwnershipGraphQLType:
    """GraphQL type for a CalendarOwnership through-model row.

    ``id`` is the ownership row primary key, not the user id.
    ``is_default`` indicates whether this is the default calendar for the owning user.
    ``membership`` exposes the owning membership identity ``{ user_id,
    organization_id, role }``. It is ``None`` for orphan ownership rows whose
    ``(user, organization)`` pair has no active membership.
    """

    id: strawberry.auto  # noqa: A003
    is_default: strawberry.auto

    @strawberry_django.field
    def membership(self) -> OwnershipMembershipGraphQLType | None:
        """Resolve the owning membership identity via the denormalized columns."""
        membership = self.membership  # type: ignore[attr-defined]
        if membership is None:
            return None
        return OwnershipMembershipGraphQLType(
            user_id=membership.user_id,
            organization_id=membership.organization_id,
            role=membership.role,
        )


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

    @strawberry_django.field
    def is_private(self) -> bool:
        return not self.accepts_public_scheduling

    @strawberry_django.field(prefetch_related=["ownerships__membership"])
    def owners(self) -> list["CalendarOwnershipGraphQLType"]:
        """Return all ownership records for this calendar."""
        return list(self.ownerships.all())  # type: ignore[attr-defined]


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


@strawberry.type
class AttendanceMembershipGraphQLType:
    """Membership identity for an internal event attendee.

    A membership has no scalar id (it is identified by the ``(user_id,
    organization_id)`` pair), so the external representation exposes that pair
    plus the membership ``role``.
    """

    user_id: int
    organization_id: int
    role: str


@strawberry_django.type(EventAttendance)
class EventAttendanceGraphQLType:
    """GraphQL type for an EventAttendance through-model row.

    ``membership`` exposes the attendee membership identity ``{ user_id,
    organization_id, role }``. It is ``None`` for orphan attendances whose
    ``(user, organization)`` pair has no matching ``OrganizationMembership``.
    """

    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry_django.field
    def membership(self) -> AttendanceMembershipGraphQLType | None:
        """Resolve the attendee membership identity via the denormalized columns."""
        membership = self.membership  # type: ignore[attr-defined]
        if membership is None:
            return None
        return AttendanceMembershipGraphQLType(
            user_id=membership.user_id,
            organization_id=membership.organization_id,
            role=membership.role,
        )


@strawberry_django.type(EventExternalAttendance)
class EventExternalAttendanceGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    external_attendee: ExternalAttendeeGraphQLType = strawberry_django.field()

    @strawberry.field
    def event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The event back-pointer, suppressed when its calendar is outside the owner's scope."""
        return _scoped_event_or_none(self.event, _owner_scoped_calendar_ids(info))  # type: ignore


@strawberry_django.type(ResourceAllocation)
class ResourceAllocationGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def calendar(self, info: strawberry.Info) -> "CalendarGraphQLType | None":
        """The allocated calendar, suppressed when outside the owner's scope."""
        return _scoped_calendar_or_none(self.calendar, _owner_scoped_calendar_ids(info))  # type: ignore


@strawberry_django.type(EventRecurrenceException)
class EventRecurrenceExceptionGraphQLType:
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def parent_event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """Parent recurring event, suppressed when its calendar is outside the owner's scope."""
        return _scoped_event_or_none(self.parent_event, _owner_scoped_calendar_ids(info))  # type: ignore

    @strawberry.field
    def modified_event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """Modified-occurrence event, suppressed when its calendar is outside the owner's scope."""
        return _scoped_event_or_none(self.modified_event, _owner_scoped_calendar_ids(info))  # type: ignore


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

    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()

    # Many-to-many relationships through intermediary models
    attendances: list[EventAttendanceGraphQLType] = strawberry_django.field()
    external_attendances: list[EventExternalAttendanceGraphQLType] = strawberry_django.field()
    recurrence_exceptions: list[EventRecurrenceExceptionGraphQLType] = strawberry_django.field()

    # Direct many-to-many relationships (simplified access)
    # attendee_memberships/external_attendees return PEOPLE (not calendars) — no
    # cross-owner calendar leak, so they stay plain field exposures.
    external_attendees: list[ExternalAttendeeGraphQLType] = strawberry_django.field()

    @strawberry_django.field(prefetch_related=["attendances__membership"])
    def attendee_memberships(self) -> list["AttendanceMembershipGraphQLType"]:
        """Return the membership identities of internal attendees.

        Resolved through ``EventAttendance.membership``; orphan attendances whose
        ``(user, organization)`` pair has no membership are excluded.
        """
        return [
            AttendanceMembershipGraphQLType(
                user_id=attendance.membership.user_id,
                organization_id=attendance.membership.organization_id,
                role=attendance.membership.role,
            )
            for attendance in self.attendances.all()  # type: ignore[attr-defined]
            if attendance.membership is not None
        ]

    # -- Owner-scoped relationship resolvers ---------------------------------
    # Each is a strict no-op for org-wide/internal requests (allowed_ids is None);
    # for a provider-scoped token it filters to the owner's calendar set.

    @strawberry.field
    def calendar(self, info: strawberry.Info) -> "CalendarGraphQLType | None":
        """The event's own calendar, suppressed when outside the owner's scope."""
        return _scoped_calendar_or_none(self.calendar, _owner_scoped_calendar_ids(info))  # type: ignore

    @strawberry.field
    def bundle_calendar(self, info: strawberry.Info) -> "CalendarGraphQLType | None":
        """The bundle calendar this event belongs to, suppressed when outside the owner's scope."""
        return _scoped_calendar_or_none(self.bundle_calendar, _owner_scoped_calendar_ids(info))  # type: ignore

    @strawberry.field
    def bundle_primary_event(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The bundle primary event, suppressed when its calendar is outside the owner's scope."""
        return _scoped_event_or_none(self.bundle_primary_event, _owner_scoped_calendar_ids(info))  # type: ignore

    @strawberry.field
    def bulk_modification_parent(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The bulk-modification parent event, suppressed when outside the owner's scope."""
        parent = self.bulk_modification_parent  # type: ignore[attr-defined]
        return _scoped_event_or_none(parent, _owner_scoped_calendar_ids(info))  # type: ignore[return-value,arg-type]

    @strawberry.field
    def parent_recurring_object(self, info: strawberry.Info) -> "CalendarEventGraphQLType | None":
        """The parent recurring event, suppressed when its calendar is outside the owner's scope."""
        parent = self.parent_recurring_object  # type: ignore[attr-defined]
        return _scoped_event_or_none(parent, _owner_scoped_calendar_ids(info))  # type: ignore[return-value,arg-type]

    @strawberry.field
    def resource_allocations(self, info: strawberry.Info) -> list["ResourceAllocationGraphQLType"]:
        """Resource allocations, restricted to those on the owner's calendars."""
        allocations = list(self.resource_allocations.all())  # type: ignore[attr-defined]
        allowed_ids = _owner_scoped_calendar_ids(info)
        if allowed_ids is not None:
            allocations = [
                a for a in allocations if getattr(a, "calendar_fk_id", None) in allowed_ids
            ]
        return allocations  # type: ignore

    @strawberry.field
    def resources(self, info: strawberry.Info) -> list["CalendarGraphQLType"]:
        """Resource calendars allocated to this event, restricted to the owner's set."""
        return _scoped_calendar_list(list(self.resources.all()), _owner_scoped_calendar_ids(info))  # type: ignore

    @strawberry.field
    def calendar_group(self, info: strawberry.Info) -> "CalendarGroupGraphQLType | None":
        """The booking CalendarGroup. Suppressed entirely for scoped tokens: a group
        aggregates calendars across providers, so exposing it (and its slots' candidate
        pool) would leak other owners' calendars."""
        if _owner_scoped_calendar_ids(info) is not None:
            return None
        return self.calendar_group  # type: ignore[attr-defined,return-value]

    @strawberry.field
    def group_selections(
        self, info: strawberry.Info
    ) -> list["CalendarEventGroupSelectionGraphQLType"]:
        """Per-slot calendar picks for a group booking, restricted to the owner's calendars.

        Each selection also routes its ``slot`` and ``calendar`` through scoped
        resolvers so the second-hop ``group_selections.slot.calendars`` pool cannot
        leak other owners' calendars."""
        selections = list(self.group_selections.all())  # type: ignore[attr-defined]
        allowed_ids = _owner_scoped_calendar_ids(info)
        if allowed_ids is not None:
            selections = [
                s for s in selections if getattr(s, "calendar_fk_id", None) in allowed_ids
            ]
        return selections  # type: ignore

    @strawberry.field
    def bundle_representations(self, info: strawberry.Info) -> list["CalendarEventGraphQLType"]:
        """Bundle representation events, restricted to the owner's calendars."""
        reps = list(self.bundle_representations.all())  # type: ignore[attr-defined]
        return _scoped_event_list(reps, _owner_scoped_calendar_ids(info))  # type: ignore[return-value]

    @strawberry.field
    def bulk_modifications(self, info: strawberry.Info) -> list["CalendarEventGraphQLType"]:
        """Continuation events from bulk modifications, restricted to the owner's calendars."""
        mods = list(self.bulk_modifications.all())  # type: ignore[attr-defined]
        return _scoped_event_list(mods, _owner_scoped_calendar_ids(info))  # type: ignore[return-value]

    @strawberry.field
    def recurring_instances(self, info: strawberry.Info) -> list["CalendarEventGraphQLType"]:
        """Individual recurring instances, restricted to the owner's calendars.

        The reverse accessor for ``parent_recurring_object`` is the model-derived
        ``calendarevent_recurring_instances`` (``related_name`` uses ``%(class)s``);
        the GraphQL field name ``recurringInstances`` is preserved for the client."""
        instances = list(self.calendarevent_recurring_instances.all())  # type: ignore[attr-defined]
        return _scoped_event_list(instances, _owner_scoped_calendar_ids(info))  # type: ignore

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
        """Parent recurring blocked time, suppressed when its calendar is outside the owner's scope."""
        parent = self.parent_blocked_time  # type: ignore[attr-defined]
        return _scoped_blocked_time_or_none(parent, _owner_scoped_calendar_ids(info))  # type: ignore[return-value,arg-type]

    @strawberry.field
    def modified_blocked_time(self, info: strawberry.Info) -> "BlockedTimeGraphQLType | None":
        """Modified-occurrence blocked time, suppressed when its calendar is outside the owner's scope."""
        modified = self.modified_blocked_time  # type: ignore[attr-defined]
        return _scoped_blocked_time_or_none(modified, _owner_scoped_calendar_ids(info))  # type: ignore[return-value,arg-type]


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
    def calendar(self, info: strawberry.Info) -> "CalendarGraphQLType | None":
        """The blocked time's calendar, suppressed when outside the owner's scope."""
        return _scoped_calendar_or_none(self.calendar, _owner_scoped_calendar_ids(info))  # type: ignore


@strawberry_django.type(AvailableTimeRecurrenceException)
class AvailableTimeRecurringExceptionGraphQLType:
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def parent_available_time(self, info: strawberry.Info) -> "AvailableTimeGraphQLType | None":
        """Parent recurring available time, suppressed when its calendar is outside the owner's scope."""
        parent = self.parent_available_time  # type: ignore[attr-defined]
        return _scoped_available_time_or_none(parent, _owner_scoped_calendar_ids(info))  # type: ignore[return-value,arg-type]

    @strawberry.field
    def modified_available_time(self, info: strawberry.Info) -> "AvailableTimeGraphQLType | None":
        """Modified-occurrence available time, suppressed when its calendar is outside the owner's scope."""
        modified = self.modified_available_time  # type: ignore[attr-defined]
        return _scoped_available_time_or_none(modified, _owner_scoped_calendar_ids(info))  # type: ignore[return-value,arg-type]


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
    def calendar(self, info: strawberry.Info) -> "CalendarGraphQLType | None":
        """The available time's calendar, suppressed when outside the owner's scope."""
        return _scoped_calendar_or_none(self.calendar, _owner_scoped_calendar_ids(info))  # type: ignore


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
    def calendars(self, info: strawberry.Info) -> list["CalendarGraphQLType"]:
        """The slot's candidate-calendar pool, filtered to the owner's set for scoped tokens.

        This is the SECOND-HOP leak: a scoped token cannot reach a slot via the
        suppressed ``calendar_group``, but it can still reach one through the sibling
        path ``calendarEvent.groupSelections.slot.calendars``. Filtering the pool here
        closes that path; the entire cross-provider candidate pool is otherwise exposed."""
        return _scoped_calendar_list(list(self.calendars.all()), _owner_scoped_calendar_ids(info))  # type: ignore


@strawberry_django.type(CalendarGroup)
class CalendarGroupGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    description: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    slots: list[CalendarGroupSlotGraphQLType] = strawberry_django.field()

    @strawberry_django.field
    def is_private(self) -> bool:
        return not self.accepts_public_scheduling


# ---------------------------------------------------------------------------
# CalendarBundle types
# ---------------------------------------------------------------------------
@strawberry_django.type(Calendar)
class CalendarBundleGraphQLType:
    """GraphQL type for a bundle calendar and its children.

    Exposes id, name, description, the list of child calendars, owners,
    and isPrivate.
    """

    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    description: strawberry.auto

    @strawberry_django.field
    def children(self) -> list[CalendarGraphQLType]:
        """Return the child calendars of this bundle calendar."""
        return list(self.bundle_children.all())  # type: ignore[union-attr]

    @strawberry_django.field
    def is_private(self) -> bool:
        return not self.accepts_public_scheduling

    @strawberry_django.field(prefetch_related=["ownerships__membership"])
    def owners(self) -> list["CalendarOwnershipGraphQLType"]:
        """Return all ownership records for this bundle calendar."""
        return list(self.ownerships.all())  # type: ignore[attr-defined]


@strawberry_django.type(CalendarEventGroupSelection)
class CalendarEventGroupSelectionGraphQLType:
    id: strawberry.auto  # noqa: A003
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def slot(self, info: strawberry.Info) -> "CalendarGroupSlotGraphQLType | None":
        """The slot this selection belongs to. Suppressed for scoped tokens: a slot's
        candidate pool aggregates calendars across providers, and reaching it via
        ``groupSelections.slot`` is the second-hop bypass of the ``calendar_group``
        suppression. The pool itself is also filtered in
        ``CalendarGroupSlotGraphQLType.calendars`` as defence in depth."""
        if _owner_scoped_calendar_ids(info) is not None:
            return None
        return self.slot  # type: ignore[attr-defined,return-value]

    @strawberry.field
    def calendar(self, info: strawberry.Info) -> "CalendarGraphQLType | None":
        """The selected calendar, suppressed when outside the owner's scope."""
        return _scoped_calendar_or_none(self.calendar, _owner_scoped_calendar_ids(info))  # type: ignore


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


# ---------------------------------------------------------------------------
# Single-use booking-code types (Phase 0 — defined here, wired in later phases)
# ---------------------------------------------------------------------------


@strawberry.enum
class BookingCodeErrorCode(enum.Enum):
    """Machine-readable error codes for booking-code operations.

    Returned instead of (or alongside) a human-readable ``error_message`` so
    that API consumers can branch on failure category without parsing strings.

    Values:
        INVALID_CODE: The code does not exist, belongs to a different org, or
            its format is malformed.
        EXPIRED: The code's ``expires_at`` has passed.
        ALREADY_USED: The code was already consumed by a prior successful write.
        REVOKED: The code was explicitly revoked by the minting organisation.
        NOT_PERMITTED: The code exists and is active but does not carry the
            permission required for the requested operation (e.g. presenting a
            booking code to reschedule).
        SLOT_UNAVAILABLE: The requested time slot is not available; the code
            remains active so the patient may retry with a different slot.
    """

    INVALID_CODE = "INVALID_CODE"
    EXPIRED = "EXPIRED"
    ALREADY_USED = "ALREADY_USED"
    REVOKED = "REVOKED"
    NOT_PERMITTED = "NOT_PERMITTED"
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"


@strawberry.type
class BookingCodeResult:
    """Result type for booking-code mint and revoke mutations.

    ``code`` and ``id`` are present only on successful mint operations.
    """

    success: bool
    error_code: BookingCodeErrorCode | None = None
    error_message: str | None = None
    # Plaintext code returned once at mint time (never stored in cleartext).
    code: str | None = None
    # Opaque token id for subsequent revoke calls.
    id: int | None = None  # noqa: A003


@strawberry.type
class CodeEventResult:
    """Result type for unauthenticated with-code write mutations (book / reschedule / cancel).

    ``event`` is present only when the operation succeeded.
    """

    success: bool
    error_code: BookingCodeErrorCode | None = None
    error_message: str | None = None
    event: CalendarEventGraphQLType | None = None


# ---------------------------------------------------------------------------
# ExternalEventChangeRequest types
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BookingPolicy types (Phase 4 — public GraphQL CRUD)
# ---------------------------------------------------------------------------


@strawberry_django.type(BookingPolicy)
class BookingPolicyGraphQLType:
    """GraphQL type for a BookingPolicy row.

    Exposes the id, target fields (calendar_id, membership_user_id,
    calendar_group_id, is_organization_default) and the four rule
    second-counts. ``calendar_id`` / ``calendar_group_id`` are the FK
    column values; ``membership_user_id`` is the denormalized column.
    Zero on any rule field means "no constraint".
    """

    id: strawberry.auto  # noqa: A003
    is_organization_default: strawberry.auto
    lead_time_seconds: strawberry.auto
    max_horizon_seconds: strawberry.auto
    buffer_before_seconds: strawberry.auto
    buffer_after_seconds: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def calendar_id(self) -> int | None:
        """Return the FK id of the bound calendar, or None."""
        return self.calendar_fk_id  # type: ignore[attr-defined]

    @strawberry.field
    def calendar_group_id(self) -> int | None:
        """Return the FK id of the bound calendar group, or None."""
        return self.calendar_group_fk_id  # type: ignore[attr-defined]

    @strawberry.field
    def membership_user_id(self) -> int | None:
        """Return the denormalized membership user id, or None."""
        return self.membership_user_id  # type: ignore[return-value]


@strawberry.input
class CreateBookingPolicyInput:
    """Input for creating a new BookingPolicy.

    Exactly one of ``calendar_id``, ``membership_user_id``, ``calendar_group_id``,
    or ``is_organization_default=True`` must be set. All rule fields default to 0
    (no constraint). ``PositiveIntegerField`` rejects negative values.
    """

    calendar_id: int | None = None
    membership_user_id: int | None = None
    calendar_group_id: int | None = None
    is_organization_default: bool = False
    lead_time_seconds: int = 0
    max_horizon_seconds: int = 0
    buffer_before_seconds: int = 0
    buffer_after_seconds: int = 0


@strawberry.input
class UpdateBookingPolicyInput:
    """Input for updating rule fields of an existing BookingPolicy.

    Target fields (calendar, membership, calendar_group, is_organization_default)
    are immutable. Only rule-second-count fields are updatable.
    Any field left as None is not updated.
    """

    policy_id: int
    lead_time_seconds: int | None = None
    max_horizon_seconds: int | None = None
    buffer_before_seconds: int | None = None
    buffer_after_seconds: int | None = None


@strawberry.input
class DeleteBookingPolicyInput:
    """Input for deleting a BookingPolicy (idempotent no-op when absent)."""

    policy_id: int


@strawberry.type
class BookingPolicyResult:
    """Result type for booking-policy write mutations."""

    success: bool
    policy: BookingPolicyGraphQLType | None = None


@strawberry.type
class DeleteBookingPolicyResult:
    """Result type for the deleteBookingPolicy mutation."""

    success: bool


@strawberry.type
class ResolvedByMembershipGraphQLType:
    """Membership identity for the member who resolved a change request.

    A membership has no scalar id (it is identified by the ``(user_id,
    organization_id)`` pair), so the external representation exposes that pair.
    """

    user_id: int
    organization_id: int
    role: str


@strawberry_django.type(ExternalEventChangeRequest)
class ExternalEventChangeRequestGraphQLType:
    """GraphQL type for an ExternalEventChangeRequest.

    Exposes the change request's kind, status, provider, proposed and retained
    value snapshots, and resolution metadata. The ``event`` back-pointer is
    exposed as an id only (not a full CalendarEventGraphQLType traversal) to
    keep the type lightweight and avoid N+1 concerns on list queries.

    Fields intentionally omitted: ``proposed_payload`` (raw provider data, not
    safe for general exposure) and cross-tenant traversal fields.
    """

    id: strawberry.auto  # noqa: A003
    kind: strawberry.auto
    status: strawberry.auto
    provider: strawberry.auto
    proposed_values: strawberry.auto
    retained_values: strawberry.auto
    resolved_at: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def event_id(self) -> int | None:
        """Return the id of the target CalendarEvent (null when the event was deleted)."""
        return self.event_fk_id  # type: ignore[attr-defined]

    @strawberry_django.field
    def resolved_by(self) -> ResolvedByMembershipGraphQLType | None:
        """Resolve the resolver membership identity."""
        if self.resolved_by_user_id is None:  # type: ignore[attr-defined]
            return None
        membership = object.__getattribute__(self, "resolved_by")  # type: ignore[attr-defined]
        if membership is None:
            return None
        return ResolvedByMembershipGraphQLType(
            user_id=membership.user_id,
            organization_id=membership.organization_id,
            role=membership.role,
        )


@strawberry.type
class ApproveExternalEventChangeRequestResult:
    """Result of the approveExternalEventChangeRequest mutation."""

    success: bool
    change_request: ExternalEventChangeRequestGraphQLType | None = None
    error_message: str | None = None


@strawberry.type
class RejectExternalEventChangeRequestResult:
    """Result of the rejectExternalEventChangeRequest mutation."""

    success: bool
    change_request: ExternalEventChangeRequestGraphQLType | None = None
    error_message: str | None = None
