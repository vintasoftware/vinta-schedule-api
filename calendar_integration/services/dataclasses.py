import datetime
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from calendar_integration.constants import (
    BookingCancelMode,
    BookingRejectionReason,
    BookingResolutionKind,
    CalendarProvider,
    RoomDeletionOutcome,
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

    Any object (typically a ``Calendar`` or ``AppointmentType`` fetched through an
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
    # Appointment-type-booking authorization flag. When True, the per-calendar
    # ``accepts_public_scheduling`` gate is bypassed because the appointment-type-level
    # authorization check has already been performed by ``AppointmentTypeService``
    # before delegating to ``CalendarEventService``. Must NOT be set by external
    # callers outside of the appointment-type-booking flow.
    appointment_type_authorized: bool = False
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
    # ``None`` for an event that exists only locally: one created without a calendar
    # provider has nothing to name it by. Adapter-built instances always carry one.
    external_id: str | None
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
class AppointmentTypeSlotInputData:
    """Input data describing a slot (pool) inside an AppointmentType."""

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
    #: strip them from an appointment type it round-trips.
    pool_ids: list[int] | None = None


@dataclass
class AppointmentTypeInputData:
    """Input data for creating/updating an AppointmentType with its slots.

    ``duration`` and ``accepts_public_scheduling`` are both tri-state:
    ``None`` means "omitted, leave unchanged" on update (and "not set" on
    create). Both are settable on both client-facing surfaces -- the REST
    ``AppointmentTypeSerializer`` takes ``duration`` / ``accepts_public_scheduling``,
    and the GraphQL ``AppointmentTypeInput`` / ``UpdateAppointmentTypeInput`` take
    ``duration_seconds`` / ``is_private`` -- so an appointment type can be made publicly
    schedulable in a single call on either. See
    ``AppointmentTypeService.create_appointment_type`` / ``update_appointment_type`` for the invariant
    tying the two together and why.

    Neither surface can *clear* a duration: ``None`` already means "leave
    unchanged", so there is no value that says "set it back to null". That is
    deliberate -- clearing one on a publicly schedulable appointment type would fail open.
    """

    name: str
    description: str = ""
    slots: list[AppointmentTypeSlotInputData] = dataclass_field(default_factory=list)
    accepts_public_scheduling: bool | None = None
    duration: datetime.timedelta | None = None


@dataclass
class CalendarPoolInputData:
    """Input data for creating/updating a ``CalendarPool`` and its roster.

    Unlike ``AppointmentTypeSlotInputData.pool_ids``, ``calendar_ids`` here has
    no "omitted means unchanged" sentinel -- a pool write always replaces the
    roster wholesale (mirrors how ``AppointmentTypeSlotSerializer.calendar_ids``
    is required, not optional).
    """

    name: str
    calendar_ids: list[int]
    description: str = ""


@dataclass
class AppointmentTypeSlotSelectionInputData:
    """Per-slot calendar picks for an appointment-type booking. `len(calendar_ids)` must be
    >= the slot's `required_count`."""

    slot_id: int
    calendar_ids: list[int]


@dataclass
class AppointmentTypeEventInputData:
    """CalendarEventInputData-like payload + per-slot calendar selections used
    when booking an event through an AppointmentType."""

    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    appointment_type_id: int
    slot_selections: list[AppointmentTypeSlotSelectionInputData] = dataclass_field(
        default_factory=list
    )
    attendances: list[EventAttendanceInputData] = dataclass_field(default_factory=list)
    external_attendances: list[EventExternalAttendanceInputData] = dataclass_field(
        default_factory=list
    )


@dataclass
class AppointmentTypeSlotAvailability:
    """Per-slot view of which calendars in its pool are available for a range."""

    slot_id: int
    available_calendar_ids: list[int]
    required_count: int = 1

    @property
    def is_satisfied_for_required_count(self) -> bool:
        return len(self.available_calendar_ids) >= self.required_count


@dataclass
class AppointmentTypeRangeAvailability:
    """Availability of every slot in an appointment type for a single range."""

    start_time: datetime.datetime
    end_time: datetime.datetime
    slots: list[AppointmentTypeSlotAvailability]


@dataclass
class BookableSlotProposal:
    """A concrete time window where every slot of an appointment type is satisfied."""

    start_time: datetime.datetime
    end_time: datetime.datetime


@dataclass
class StaleSelection:
    """A `(event, slot, calendar)` triple whose calendar has left its slot's
    roster since the selection was made.

    Staleness definition (Calendar Pools plan, Guiding Decisions -> Staleness
    definition): no ``AppointmentTypeSlotMembership`` row exists for the
    selection's ``(slot, calendar)`` pair, regardless of source -- inline or
    projected from a ``CalendarPool``. Carries scalar ids only, matching the
    plan's Data Model Changes -> Type plumbing, so ops-sweep consumers (REST,
    GraphQL) do not have to load full ``CalendarEvent`` / ``AppointmentTypeSlot``
    / ``Calendar`` rows just to list the backlog.
    """

    event_id: int
    slot_id: int
    calendar_id: int


@dataclass
class AppointmentTypeScopedAvailabilityWriteResult:
    """Result of an appointment-type-scoped availability window write (create/update/delete).

    ``window`` is the saved ``AvailableTime`` row, or ``None`` after a delete.
    ``orphaned_bookings`` lists confirmed future ``CalendarEvent`` bookings in
    the window's appointment type slot, for the window's calendar, that fall outside the
    calendar's appointment-type-scoped configuration *after* the write is applied --
    populated only by the update path (spec UC-6: "admin tightens a window
    that orphans bookings"). Nothing about the orphaned bookings is modified;
    this is a read-only report for the caller to act on.
    """

    window: AvailableTime | None
    orphaned_bookings: list[CalendarEvent] = dataclass_field(default_factory=list)


@dataclass
class AppointmentTypeScopedBlockWriteResult:
    """Result of an appointment-type-scoped blocked-time write (create/update/delete).

    ``block`` is the saved ``BlockedTime`` row, or ``None`` after a delete.
    ``orphaned_bookings`` lists confirmed future ``CalendarEvent`` bookings in
    the block's appointment type slot, for the block's calendar, that fall INSIDE the
    calendar's appointment-type-scoped blocked time *after* the write is applied (spec
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
    """The resolved set of booking guardrails for a calendar, bundle, or appointment type.

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
        ``AppointmentType`` instance fetched through an annotated queryset:

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


@dataclass(frozen=True)
class ResourceLocationRef:
    """Where a room sits on the provider: a building, plus a floor when it has one.

    Google: the Directory ``buildingId`` and the ``floorName`` (Google has no floor
    id). Microsoft: the building place id and the floor or section place id.
    ``external_floor_id`` is ``""`` for a building with no floors.

    Stored as a dict under ``location_ref`` in a link's ``provider_snapshot`` and
    ``pending_fields``; ``to_dict`` / ``from_dict`` convert between the two.
    """

    external_building_id: str
    external_floor_id: str = ""

    def to_dict(self) -> dict[str, str]:
        """The JSON shape stored under ``location_ref``."""
        return {
            "external_building_id": self.external_building_id,
            "external_floor_id": self.external_floor_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ResourceLocationRef | None":
        """Read a stored ``location_ref`` back. ``None`` means the room has no location."""
        if data is None:
            return None
        return cls(
            external_building_id=data["external_building_id"],
            external_floor_id=data.get("external_floor_id", ""),
        )


@dataclass(frozen=True)
class ResourceLocationData:
    """A building and floor as the provider's directory lists it."""

    external_building_id: str
    building_name: str
    external_floor_id: str = ""
    floor_name: str = ""

    @property
    def ref(self) -> ResourceLocationRef:
        """The provider-side reference rooms use to point at this location."""
        return ResourceLocationRef(
            external_building_id=self.external_building_id,
            external_floor_id=self.external_floor_id,
        )


@dataclass
class RoomDirectoryData:
    """A room as the provider's directory returns it.

    ``description`` is ``None`` when the provider has no room description field.
    That is how Microsoft rooms are read until the Phase 0 spike says otherwise.
    A ``None`` description is left out of ``synced_values()``, so the resync never
    reads it as the provider clearing the description.
    """

    external_id: str
    email: str
    name: str
    description: str | None
    capacity: int | None
    location_ref: ResourceLocationRef | None
    provider_payload: dict[str, Any] = dataclass_field(default_factory=dict)

    def synced_values(self) -> dict[str, Any]:
        """The room's synced fields in the shape of a link's ``provider_snapshot``.

        Pass the result to ``ResourceCalendarProviderLink.fields_changed_by_provider``
        or ``mark_pushed``.
        """
        values: dict[str, Any] = {
            "name": self.name,
            "capacity": self.capacity,
            "location_ref": self.location_ref.to_dict() if self.location_ref else None,
        }
        if self.description is not None:
            values["description"] = self.description
        return values


@dataclass(frozen=True)
class RoomWriteData:
    """The room fields Vinta Schedule sends on a provider create or update.

    ``provisional_key`` is the link's ``provisional_key``. Adapters derive the
    provider-side idempotency id from it (Google ``resourceId`` ``vinta-<key>``,
    Microsoft tag ``vinta-link-<key>``), so a replayed create finds the room it
    already made instead of making a second one.
    """

    name: str
    description: str
    capacity: int | None
    location_ref: ResourceLocationRef | None
    provisional_key: uuid.UUID

    @classmethod
    def from_synced_values(
        cls, values: Mapping[str, Any], provisional_key: uuid.UUID
    ) -> "RoomWriteData":
        """Build from a dict in the ``provider_snapshot`` / ``pending_fields`` shape.

        ``values`` must hold ``name``. A missing ``description`` is ``""`` and a
        missing ``capacity`` or ``location_ref`` is ``None``.
        """
        return cls(
            name=values["name"],
            description=values.get("description") or "",
            capacity=values.get("capacity"),
            location_ref=ResourceLocationRef.from_dict(values.get("location_ref")),
            provisional_key=provisional_key,
        )


@dataclass(frozen=True)
class BusyWindow:
    """A time range in which the provider reports a room as busy.

    ``start`` and ``end`` are timezone-aware. The range is half-open: ``[start, end)``.
    """

    start: datetime.datetime
    end: datetime.datetime


@dataclass(frozen=True)
class RoomBooking:
    """One future booking of a room, as a deletion preview lists it.

    ``start`` / ``end`` are the first occurrence that has not ended yet. A one-off
    event is that occurrence. A series (``is_series``) is resolved as one unit:
    ``series_from`` is set when the series started before now, and is the start of
    that first occurrence, so the series is resolved "from now on". A series that
    has not started yet has ``series_from=None`` and is resolved as a whole.
    ``calendar_id`` is the calendar that holds the event.
    """

    event_id: int
    calendar_id: int | None
    title: str
    start: datetime.datetime
    end: datetime.datetime
    is_series: bool
    series_from: datetime.datetime | None


@dataclass(frozen=True)
class RoomBookingPreview:
    """A room's future bookings, plus the fingerprint a delete must send back."""

    fingerprint: str
    bookings: tuple[RoomBooking, ...]


@dataclass(frozen=True)
class AbortDeletion:
    """Resolution: cancel the room deletion; the booking and the room stay as they are."""


@dataclass(frozen=True)
class MoveBooking:
    """Resolution: move the booking to the room ``target_calendar_id``."""

    target_calendar_id: int


@dataclass(frozen=True)
class CancelBooking:
    """Resolution: drop the room from the booking, or cancel the whole event."""

    mode: BookingCancelMode


BookingResolution = AbortDeletion | MoveBooking | CancelBooking


def booking_resolution_from(kind: str, target_calendar_id: int | None = None) -> BookingResolution:
    """The ``BookingResolution`` a caller names with a ``BookingResolutionKind``.

    Raises ``ValueError`` when ``MOVE`` has no ``target_calendar_id``, or another kind
    has one.
    """
    if kind == BookingResolutionKind.MOVE:
        if target_calendar_id is None:
            raise ValueError("A move needs a target room.")
        return MoveBooking(target_calendar_id)
    if target_calendar_id is not None:
        raise ValueError("A target room only applies to a move.")
    if kind == BookingResolutionKind.ABORT:
        return AbortDeletion()
    if kind == BookingResolutionKind.REMOVE_ROOM:
        return CancelBooking(BookingCancelMode.REMOVE_ROOM)
    if kind == BookingResolutionKind.CANCEL_EVENT:
        return CancelBooking(BookingCancelMode.CANCEL_EVENT)
    raise ValueError(f"Unknown booking resolution {kind!r}.")


def booking_resolutions_from(
    default_kind: str,
    target_calendar_id: int | None,
    overrides: Iterable[tuple[int, str, int | None]] = (),
) -> tuple[BookingResolution, dict[int, BookingResolution]]:
    """The default resolution and the per-booking ones a caller names.

    ``overrides`` holds ``(event_id, kind, target_calendar_id)`` per booking. Returns
    the default and the overrides keyed by event id. Raises ``ValueError`` when a
    resolution is invalid (see ``booking_resolution_from``) or a booking is overridden
    more than once.
    """
    default = booking_resolution_from(default_kind, target_calendar_id)
    by_event: dict[int, BookingResolution] = {}
    for event_id, kind, target in overrides:
        if event_id in by_event:
            raise ValueError(f"Booking {event_id} has more than one override.")
        by_event[event_id] = booking_resolution_from(kind, target)
    return default, by_event


@dataclass(frozen=True)
class ResolvedBooking:
    """A booking together with the resolution chosen for it."""

    booking: RoomBooking
    resolution: BookingResolution


@dataclass(frozen=True)
class BookingResolutionPlan:
    """A validated resolution for every future booking of ``room_id``, in preview order."""

    room_id: int
    organization_id: int
    fingerprint: str
    bookings: tuple[ResolvedBooking, ...]


@dataclass(frozen=True)
class ApplyResult:
    """What ``BookingResolutionService.apply`` did with a plan, by event id, in plan order.

    ``applied`` holds the bookings that are resolved: changed by this run, or already
    no longer referencing the room (a re-run). ``pending`` holds the bookings left
    untouched because the apply stopped, starting with ``failed_at``, the booking
    whose step failed. ``failed_at`` is ``None`` and ``pending`` is empty when every
    booking was applied.
    """

    applied: tuple[int, ...]
    pending: tuple[int, ...]
    failed_at: int | None


@dataclass(frozen=True)
class RejectedBooking:
    """A booking whose resolution is invalid. ``reason.label`` is the message to show."""

    event_id: int
    reason: BookingRejectionReason


@dataclass(frozen=True)
class RoomDeletionResult:
    """What ``CalendarService.delete_synced_resource_calendar`` did with a room.

    ``outcome`` says which one happened, and its label is the message to show:

    - ``DELETED``: every booking is resolved and the room is archived, or on its
      way to being archived once the provider delete runs. Also when the room was
      already archived or being deleted, so a repeated delete succeeds.
    - ``ABORTED``: a booking's resolution cancelled the deletion. ``bookings`` lists
      the room's future bookings, and nothing changed.
    - ``REJECTED``: some resolutions are invalid, all listed in ``rejected``;
      nothing changed.
    - ``INCOMPLETE``: the apply stopped part way. The bookings in
      ``applied_event_ids`` are resolved, those in ``pending_event_ids`` are not, and
      the room is not deleted. Preview again and retry.
    """

    outcome: RoomDeletionOutcome
    bookings: tuple[RoomBooking, ...] = ()
    rejected: tuple[RejectedBooking, ...] = ()
    apply_result: ApplyResult | None = None

    @property
    def deleted(self) -> bool:
        return self.outcome == RoomDeletionOutcome.DELETED

    @property
    def applied_event_ids(self) -> tuple[int, ...]:
        return self.apply_result.applied if self.apply_result else ()

    @property
    def pending_event_ids(self) -> tuple[int, ...]:
        return self.apply_result.pending if self.apply_result else ()

    @property
    def failed_at_event_id(self) -> int | None:
        return self.apply_result.failed_at if self.apply_result else None


@dataclass(frozen=True)
class GoogleWriteAccessResult:
    """The outcome of verifying room write access for an organization's Google service account.

    ``error`` is ``""`` on success, otherwise a message an org admin can act on.
    """

    write_enabled: bool
    write_verified_at: datetime.datetime | None
    error: str
