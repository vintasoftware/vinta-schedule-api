import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

from calendar_integration.constants import (
    CalendarProvider,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    CalendarEvent,
    EventAttendance,
    EventExternalAttendance,
)


if TYPE_CHECKING:
    from calendar_integration.models import BookingPolicy


class _EffectivePolicyRow(Protocol):
    """Row shape consumed by ``EffectivePolicy.from_annotation``.

    Any object (typically a ``Calendar`` or ``CalendarGroup`` fetched through an
    annotated queryset) exposing the four ``effective_*_seconds`` columns produced
    by ``annotate_effective_policy``. Each is ``int | None`` (NULL when no policy
    resolved).
    """

    effective_lead_time_seconds: int | None
    effective_max_horizon_seconds: int | None
    effective_buffer_before_seconds: int | None
    effective_buffer_after_seconds: int | None


@dataclass
class ExternalClientIdentifierData:
    """One ``(system, identifier)`` pair -- the client-owned reference carried by
    ``ExternalClientIdentifier``. ``system`` is normalized (see
    ``calendar_integration.external_client_identifiers.normalize_system``) by the
    service before it is ever compared or persisted; callers may pass an
    un-normalized value.
    """

    system: str
    identifier: str


@dataclass
class EventAttendeeData:
    email: str
    name: str
    status: Literal["accepted", "declined", "pending"]


@dataclass
class ResourceData:
    email: str
    title: str
    external_id: str | None = None
    status: Literal["accepted", "declined", "pending"] | None = None


@dataclass
class EventAttendanceInputData:
    user_id: int


@dataclass
class ExternalAttendeeInputData:
    email: str
    name: str = ""
    id: int | None = None  # noqa: A003
    # None = omitted, leave untouched. [] = clear all. See
    # ``ExternalClientIdentifierService.replace_for_target``.
    external_client_identifiers: list[ExternalClientIdentifierData] | None = None


@dataclass
class EventExternalAttendanceInputData:
    external_attendee: ExternalAttendeeInputData


@dataclass
class ResourceAllocationInputData:
    resource_id: int


@dataclass
class CalendarEventInputData:
    """Input payload for ``CalendarEventService.create_event`` / ``update_event``.

    ``title``, ``description``, ``attendances`` and ``external_attendances`` are
    **tri-state**, matching ``external_client_identifiers``: a value replaces what is
    stored, and ``None`` means "omitted -- leave untouched". On ``update_event`` an
    omitted field skips its write entirely: no assignment, no reconciliation, no
    attendee webhook. On ``create_event`` there is nothing to leave untouched, so
    ``None`` behaves exactly as the empty value did before (``""`` for the two strings,
    ``[]`` for the two lists) rather than raising.

    ``resource_allocations`` is deliberately NOT tri-state: it stays always-replace,
    defaulting to ``[]``.
    """

    title: str | None
    description: str | None
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    # None = omitted, leave untouched (update) / empty (create).
    attendances: list[EventAttendanceInputData] | None = None
    external_attendances: list[EventExternalAttendanceInputData] | None = None
    resource_allocations: list[ResourceAllocationInputData] = dataclass_field(default_factory=list)
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string
    parent_event_id: int | None = None  # For creating instances/exceptions
    is_recurring_exception: bool = False
    # Group-booking authorization flag. When True, the per-calendar
    # ``accepts_public_scheduling`` gate is bypassed because the group-level
    # authorization check has already been performed by ``CalendarGroupService``
    # before delegating to ``CalendarEventService``. Must NOT be set by external
    # callers outside of the group-booking flow.
    group_authorized: bool = False
    # None = omitted, leave untouched. [] = clear all. See
    # ``ExternalClientIdentifierService.replace_for_target``.
    external_client_identifiers: list[ExternalClientIdentifierData] | None = None


@dataclass
class CalendarEventAdapterInputData:
    calendar_external_id: str
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    attendees: list[EventAttendeeData]
    resources: list[ResourceData] = dataclass_field(default_factory=list)
    original_payload: dict | None = None

    external_id: str | None = None  # only for update

    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string for creating recurring events
    is_recurring_instance: bool = False  # True if this is a single instance of a recurring event


@dataclass
class CalendarEventAdapterOutputData:
    calendar_external_id: str
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    attendees: list[EventAttendeeData]
    external_id: str
    status: Literal["confirmed", "cancelled"] = "confirmed"
    original_payload: dict | None = None
    id: int | None = None  # noqa: A003
    resources: list[ResourceData] = dataclass_field(default_factory=list)
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string
    recurring_event_id: str | None = None  # ID of the master recurring event


@dataclass
class CalendarResourceData:
    name: str
    description: str
    provider: str
    external_id: str
    email: str | None = None
    capacity: int | None = None
    original_payload: dict | None = None
    is_default: bool = False
    # Provider access role for the authenticated account on this calendar.
    # Google: "owner" | "writer" | "reader" | "freeBusyReader". Used to decide
    # whether a freshly imported calendar should sync by default (own vs subscribed).
    access_role: str | None = None


@dataclass
class EventsSyncChanges:
    events_to_update: list[CalendarEvent] = dataclass_field(default_factory=list)
    events_to_create: list[CalendarEvent] = dataclass_field(default_factory=list)
    blocked_times_to_create: list[BlockedTime] = dataclass_field(default_factory=list)
    blocked_times_to_update: list[BlockedTime] = dataclass_field(default_factory=list)
    attendances_to_create: list[EventAttendance] = dataclass_field(default_factory=list)
    external_attendances_to_create: list[EventExternalAttendance] = dataclass_field(
        default_factory=list
    )
    events_to_delete: list[str] = dataclass_field(default_factory=list)
    blocks_to_delete: list[str] = dataclass_field(default_factory=list)
    matched_event_ids: set[str] = dataclass_field(default_factory=set)
    # New fields for recurring events
    recurrence_rules_to_create: list = dataclass_field(
        default_factory=list
    )  # RecurrenceRule objects


@dataclass
class ApplicationCalendarData:
    id: int | None  # noqa: A003
    organization_id: int | None
    external_id: str
    name: str
    description: str | None = None
    email: str | None = None
    provider: CalendarProvider = CalendarProvider.GOOGLE
    original_payload: dict | None = None


class CalendarEventsSyncTypedDict(TypedDict):
    events: Iterable[CalendarEventAdapterOutputData]
    next_sync_token: str | None


@dataclass
class AvailableTimeWindow:
    start_time: datetime.datetime
    end_time: datetime.datetime
    id: int | None = None  # noqa: A003
    can_book_partially: bool = False
    # IANA timezone the window should be rendered in; None falls back to UTC.
    timezone: str | None = None


@dataclass
class BlockedTimeData:
    id: int | None  # noqa: A003
    calendar_external_id: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    reason: str
    external_id: str | None
    meta: dict | None


@dataclass
class EventInternalAttendeeData:
    user_id: int
    email: str
    name: str | None
    status: Literal["accepted", "declined", "pending"]


@dataclass
class EventExternalAttendeeData:
    email: str
    name: str | None
    status: Literal["accepted", "declined", "pending"]
    external_client_identifiers: list[ExternalClientIdentifierData] = dataclass_field(
        default_factory=list
    )


@dataclass
class CalendarSettingsData:
    manage_available_windows: bool
    accepts_public_scheduling: bool


@dataclass
class CalendarEventData:
    id: int  # noqa: A003
    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    title: str
    description: str
    external_id: str
    calendar_settings: CalendarSettingsData | None
    status: Literal["confirmed", "cancelled"]
    attendees: list[EventInternalAttendeeData]
    external_attendees: list[EventExternalAttendeeData]
    resources: list[ResourceData]
    recurrence_rule: str | None
    is_recurring: bool
    recurring_event_id: str | None  # ID of the master recurring event
    original_payload: dict | None = None
    external_client_identifiers: list[ExternalClientIdentifierData] = dataclass_field(
        default_factory=list
    )


@dataclass
class UnavailableTimeWindow:
    start_time: datetime.datetime
    end_time: datetime.datetime
    reason: Literal["blocked_time"] | Literal["calendar_event"]
    id: int  # noqa: A003
    data: BlockedTimeData | CalendarEventData


@dataclass
class BlockedTimeInputData:
    """Input data for creating blocked times."""

    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    reason: str = ""
    external_id: str = ""
    recurrence_rule: str | None = None
    parent_object_id: int | None = None
    is_recurring_exception: bool = False


@dataclass
class AvailableTimeInputData:
    """Input data for creating available times."""

    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    recurrence_rule: str | None = None
    parent_object_id: int | None = None
    is_recurring_exception: bool = False


@dataclass
class CalendarGroupSlotInputData:
    """Input data describing a slot (pool) inside a CalendarGroup."""

    name: str
    calendar_ids: list[int]
    required_count: int = 1
    description: str = ""
    order: int = 0
    #: Ids of the ``CalendarPool``s attached to this slot, whose rosters are
    #: projected into the slot's memberships alongside ``calendar_ids``.
    #:
    #: ``None`` (the default, and what every pre-pools caller sends) means
    #: "leave the slot's pool attachments exactly as they are" -- NOT "detach
    #: everything". An empty list is the explicit detach-all. The distinction
    #: matters because a client that never learned about pools must not silently
    #: strip them from a group it round-trips.
    pool_ids: list[int] | None = None


@dataclass
class CalendarGroupInputData:
    """Input data for creating/updating a CalendarGroup with its slots.

    ``duration`` and ``accepts_public_scheduling`` are both tri-state:
    ``None`` means "omitted, leave unchanged" on update (and "not set" on
    create). The REST ``CalendarGroupSerializer`` exposes ``duration``; the
    GraphQL ``CalendarGroupInput`` / ``UpdateCalendarGroupInput`` input types
    do not. So a group becomes publicly schedulable by being given a duration
    over REST (or by a caller building this dataclass directly) before it is
    flipped public -- see ``CalendarGroupService.create_group`` /
    ``update_group`` for the invariant this enforces and why.

    Neither surface can *clear* a duration: ``None`` already means "leave
    unchanged", so there is no value that says "set it back to null".
    """

    name: str
    description: str = ""
    slots: list[CalendarGroupSlotInputData] = dataclass_field(default_factory=list)
    accepts_public_scheduling: bool | None = None
    duration: datetime.timedelta | None = None


@dataclass
class CalendarPoolInputData:
    """Input data for creating/updating a ``CalendarPool`` and its roster.

    Unlike ``CalendarGroupSlotInputData.pool_ids``, ``calendar_ids`` here has
    no "omitted means unchanged" sentinel -- a pool write always replaces the
    roster wholesale (mirrors how ``CalendarGroupSlotSerializer.calendar_ids``
    is required, not optional).
    """

    name: str
    calendar_ids: list[int]
    description: str = ""


@dataclass
class CalendarGroupSlotSelectionInputData:
    """Per-slot calendar picks for a grouped booking. `len(calendar_ids)` must be
    >= the slot's `required_count`."""

    slot_id: int
    calendar_ids: list[int]


@dataclass
class CalendarGroupEventInputData:
    """CalendarEventInputData-like payload + per-slot calendar selections used
    when booking an event through a CalendarGroup."""

    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    group_id: int
    slot_selections: list[CalendarGroupSlotSelectionInputData] = dataclass_field(
        default_factory=list
    )
    attendances: list[EventAttendanceInputData] = dataclass_field(default_factory=list)
    external_attendances: list[EventExternalAttendanceInputData] = dataclass_field(
        default_factory=list
    )


@dataclass
class CalendarGroupSlotAvailability:
    """Per-slot view of which calendars in its pool are available for a range."""

    slot_id: int
    available_calendar_ids: list[int]
    required_count: int = 1

    @property
    def is_satisfied_for_required_count(self) -> bool:
        return len(self.available_calendar_ids) >= self.required_count


@dataclass
class CalendarGroupRangeAvailability:
    """Availability of every slot in a group for a single range."""

    start_time: datetime.datetime
    end_time: datetime.datetime
    slots: list[CalendarGroupSlotAvailability]


@dataclass
class BookableSlotProposal:
    """A concrete time window where every slot of a group is satisfied."""

    start_time: datetime.datetime
    end_time: datetime.datetime


@dataclass
class StaleSelection:
    """A `(event, slot, calendar)` triple whose calendar has left its slot's
    roster since the selection was made.

    Staleness definition (Calendar Pools plan, Guiding Decisions -> Staleness
    definition): no ``CalendarGroupSlotMembership`` row exists for the
    selection's ``(slot, calendar)`` pair, regardless of source -- inline or
    projected from a ``CalendarPool``. Carries scalar ids only, matching the
    plan's Data Model Changes -> Type plumbing, so ops-sweep consumers (REST,
    GraphQL) do not have to load full ``CalendarEvent`` / ``CalendarGroupSlot``
    / ``Calendar`` rows just to list the backlog.
    """

    event_id: int
    slot_id: int
    calendar_id: int


@dataclass
class GroupScopedAvailabilityWriteResult:
    """Result of a group-scoped availability window write (create/update/delete).

    ``window`` is the saved ``AvailableTime`` row, or ``None`` after a delete.
    ``orphaned_bookings`` lists confirmed future ``CalendarEvent`` bookings in
    the window's group slot, for the window's calendar, that fall outside the
    calendar's group-scoped configuration *after* the write is applied --
    populated only by the update path (spec UC-6: "admin tightens a window
    that orphans bookings"). Nothing about the orphaned bookings is modified;
    this is a read-only report for the caller to act on.
    """

    window: AvailableTime | None
    orphaned_bookings: list[CalendarEvent] = dataclass_field(default_factory=list)


@dataclass
class GroupScopedBlockWriteResult:
    """Result of a group-scoped blocked-time write (create/update/delete).

    ``block`` is the saved ``BlockedTime`` row, or ``None`` after a delete.
    ``orphaned_bookings`` lists confirmed future ``CalendarEvent`` bookings in
    the block's group slot, for the block's calendar, that fall INSIDE the
    calendar's group-scoped blocked time *after* the write is applied (spec
    UC-6's rule applied to blocks). Unlike a window write -- where only the
    FIRST window flips the calendar from fall-through to narrowed, so only it
    can orphan a booking -- a block always independently removes time, so
    orphaned-booking detection runs on every create and every update, never
    on delete (a delete only widens available time). Nothing about the
    orphaned bookings is modified; this is a read-only report for the caller
    to act on.
    """

    block: BlockedTime | None
    orphaned_bookings: list[CalendarEvent] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class EffectivePolicy:
    """The resolved set of booking guardrails for a calendar, bundle, or group.

    Field semantics (mirrors ``BookingPolicy`` field encoding):
    - ``lead_time``: minimum advance notice required before a slot can start.
      Zero means "bookable now."
    - ``max_horizon``: how far ahead a slot may be offered. ``None`` means
      unbounded (no horizon constraint). A stored ``max_horizon_seconds=0`` on the
      model maps to ``None`` here — "0 = no constraint" per spec.
    - ``buffer_before``: dead zone before an existing event; candidate slots whose
      window extends into ``[event.start - buffer_before, event.start)`` are blocked.
      Zero means flush booking is allowed.
    - ``buffer_after``: dead zone after an existing event. Zero means flush allowed.
    """

    lead_time: datetime.timedelta
    max_horizon: datetime.timedelta | None  # None = unbounded
    buffer_before: datetime.timedelta
    buffer_after: datetime.timedelta

    @classmethod
    def unconstrained(cls) -> "EffectivePolicy":
        """Return an EffectivePolicy with no constraints on any field."""
        return cls(
            lead_time=datetime.timedelta(0),
            max_horizon=None,
            buffer_before=datetime.timedelta(0),
            buffer_after=datetime.timedelta(0),
        )

    @classmethod
    def from_model(cls, policy: "BookingPolicy") -> "EffectivePolicy":
        """Build an EffectivePolicy from a BookingPolicy model instance.

        ``max_horizon_seconds=0`` on the model means "unbounded" and maps to
        ``max_horizon=None`` here, consistent with the spec's "0 = no constraint."
        """
        return cls(
            lead_time=datetime.timedelta(seconds=policy.lead_time_seconds),
            max_horizon=(
                datetime.timedelta(seconds=policy.max_horizon_seconds)
                if policy.max_horizon_seconds > 0
                else None
            ),
            buffer_before=datetime.timedelta(seconds=policy.buffer_before_seconds),
            buffer_after=datetime.timedelta(seconds=policy.buffer_after_seconds),
        )

    @classmethod
    def from_annotation(cls, row: "_EffectivePolicyRow") -> "EffectivePolicy":
        """Build an EffectivePolicy from the four ``effective_*_seconds`` annotations.

        ``row`` is any object exposing the four annotated attributes produced by
        ``annotate_effective_policy`` — typically a ``Calendar`` or
        ``CalendarGroup`` instance fetched through an annotated queryset:

        - ``effective_lead_time_seconds``
        - ``effective_max_horizon_seconds``
        - ``effective_buffer_before_seconds``
        - ``effective_buffer_after_seconds``

        A ``0`` or ``NULL`` horizon maps to ``max_horizon=None`` ("0 = unbounded",
        mirroring ``from_model``). ``0`` / ``NULL`` lead-time and buffers map to
        ``timedelta(0)``. The annotation resolves the entire precedence chain in
        SQL, so this method does nothing more than decode the four columns.
        """
        lead = row.effective_lead_time_seconds or 0
        horizon = row.effective_max_horizon_seconds or 0
        buffer_before = row.effective_buffer_before_seconds or 0
        buffer_after = row.effective_buffer_after_seconds or 0

        return cls(
            lead_time=datetime.timedelta(seconds=lead),
            max_horizon=(datetime.timedelta(seconds=horizon) if horizon > 0 else None),
            buffer_before=datetime.timedelta(seconds=buffer_before),
            buffer_after=datetime.timedelta(seconds=buffer_after),
        )

    @staticmethod
    def most_restrictive(policies: Iterable["EffectivePolicy"]) -> "EffectivePolicy":
        """Combine multiple EffectivePolicy instances into the most-restrictive one.

        Field combination rules:
        - ``lead_time``: max (the longest required advance notice wins).
        - ``max_horizon``: min of the non-None values (the shortest horizon wins;
          ``None`` = unbounded = effectively infinite, so it is excluded from the
          min — only binding if ALL policies have ``None`` horizon).
        - ``buffer_before``: max (the largest buffer wins).
        - ``buffer_after``: max.

        An empty input sequence returns ``unconstrained()``.
        """
        policy_list = list(policies)
        if not policy_list:
            return EffectivePolicy.unconstrained()

        max_lead = max(p.lead_time for p in policy_list)
        max_buffer_before = max(p.buffer_before for p in policy_list)
        max_buffer_after = max(p.buffer_after for p in policy_list)

        # Finite horizons only; None (unbounded) acts as +∞ and is skipped.
        finite_horizons = [p.max_horizon for p in policy_list if p.max_horizon is not None]
        min_horizon = min(finite_horizons) if finite_horizons else None

        return EffectivePolicy(
            lead_time=max_lead,
            max_horizon=min_horizon,
            buffer_before=max_buffer_before,
            buffer_after=max_buffer_after,
        )
