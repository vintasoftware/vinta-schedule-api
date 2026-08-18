"""Shared utility functions for CalendarService and its sub-services.

These are module-level functions (not methods) extracted from ``CalendarService``.
They take all required state as explicit parameters so they are independently
testable without constructing a full service instance.

The per-instance calendar-lookup cache (``get_calendar_by_id`` /
``get_calendar_by_external_id``) uses a plain dict keyed on
``(organization_id, <id>)`` — NOT ``functools.lru_cache``.  The old
``@lru_cache`` on instance methods (``# noqa: B019``) is a multi-tenant bug:
``lru_cache`` keys only on positional args; when the same service instance is
reused across two organizations (a pattern the DI container allows), the first
org's ``Calendar`` is returned for the second org's query.
"""

from __future__ import annotations

import datetime
import zoneinfo
from typing import TYPE_CHECKING, Literal, cast

from calendar_integration.constants import CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    EventAttendance,
    EventExternalAttendance,
)
from calendar_integration.services.dataclasses import (
    CalendarEventData,
    CalendarEventInputData,
    CalendarSettingsData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
    ExternalAttendeeInputData,
    ExternalClientIdentifierData,
    ResourceData,
)
from organizations.models import OrganizationMembership
from users.models import User


def resolve_acting_single_use_token(
    user_or_token: object,
    permission_service: object | None,
) -> CalendarManagementToken | None:
    """Return the resolved single-use token backing a ``str`` ``user_or_token``.

    When the caller acts via a single-use code, ``user_or_token`` is the raw code
    string and the permission service has already resolved it into a
    ``CalendarManagementToken`` row (``permission_service.token``). Audit actor
    resolution uses that row to attribute the action to SINGLE_USE_CODE. Returns
    ``None`` for non-token principals (User / SystemUser / None) or when no token
    has been resolved.
    """
    if not isinstance(user_or_token, str) or permission_service is None:
        return None
    return getattr(permission_service, "token", None)


if TYPE_CHECKING:
    from collections.abc import Iterable

    from calendar_integration.services.calendar_permission_service import CalendarPermissionService
    from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
    from organizations.models import Organization


# ---------------------------------------------------------------------------
# Membership resolution helpers
# ---------------------------------------------------------------------------


def resolve_member_user_ids(user_ids: Iterable[int], organization_id: int) -> set[int]:
    """Return the subset of ``user_ids`` that have an ``OrganizationMembership``.

    The raw-SQL composite PROTECT FK on ``EventAttendance.membership`` (added in the
    cutover migration) requires a non-NULL ``membership_user_id`` to reference a real
    ``OrganizationMembership(user_id, organization_id)``. This guard mirrors the
    single-row ``_resolve_owner_membership_user_id`` helper for the bulk attendance
    write paths: a user that is not a member of ``organization_id`` resolves to a
    NULL membership (an orphan attendance) instead of triggering an FK IntegrityError.
    """
    user_id_list = list(dict.fromkeys(user_ids))
    if not user_id_list:
        return set()
    return set(
        OrganizationMembership.objects.filter(
            organization_id=organization_id,
            user_id__in=user_id_list,
        ).values_list("user_id", flat=True)
    )


# ---------------------------------------------------------------------------
# Timezone conversion
# ---------------------------------------------------------------------------


def convert_naive_utc_datetime_to_timezone(
    datetime_obj: datetime.datetime, iana_tz: str
) -> datetime.datetime:
    """Return the naive local wall-clock of an instant in the given IANA timezone.

    Used to populate ``*_tz_unaware`` fields: the model stores the naive local
    wall-clock paired with the IANA ``timezone``, and the DB-generated
    ``start_time``/``end_time`` re-derive the true instant from them via
    ``convert_naive_utc_to_timezone``. Naive inputs are treated as UTC.

    Previously this did ``datetime_obj.replace(tzinfo=target_tz)``, which keeps the
    wall-clock and swaps the zone — storing the instant instead of the local
    wall-clock and shifting synced times by the zone offset. ``astimezone`` converts
    the instant correctly.

    e.g. 12:00Z + "America/Recife" -> 09:00 (naive).
    """
    try:
        target_tz = zoneinfo.ZoneInfo(iana_tz)
    except zoneinfo.ZoneInfoNotFoundError as e:
        raise ValueError(f"Invalid IANA timezone: {iana_tz}") from e

    if datetime_obj.tzinfo is None:
        datetime_obj = datetime_obj.replace(tzinfo=datetime.UTC)
    # Local wall-clock, tagged UTC so it stores cleanly under USE_TZ (matching how
    # the batch-create path stores tz_unaware). The generated start_time/end_time
    # re-interpret this wall-clock in `timezone` to recover the true instant.
    local_wall_clock = datetime_obj.astimezone(target_tz).replace(tzinfo=None)
    return local_wall_clock.replace(tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Event serialization helpers
# ---------------------------------------------------------------------------


def serialize_event_internal_attendee(
    attendance: EventAttendance,
    user_by_id: dict[int, User] | None = None,
) -> EventInternalAttendeeData | None:
    """Serialize an internal event attendance to ``EventInternalAttendeeData``.

    The attendee identity is resolved from the denormalized ``membership_user_id``
    (the legacy ``attendance.user`` FK has been dropped, and
    ``attendance.membership.user`` resolves to ``None`` for a stale membership). An
    orphan attendance whose ``membership_user_id`` is ``None`` (the
    attendee is not — or is no longer — a member of the organization) has no
    membership-backed identity and is skipped: the caller drops the ``None``.

    ``user_by_id`` is an optional ``{user_id: User}`` map pre-fetched by the caller
    (``serialize_event`` batches one query per event). When omitted the user is
    fetched per call so single-attendance callers are unaffected.
    """
    if attendance.membership_user_id is None:
        return None
    if user_by_id is not None:
        user = user_by_id.get(attendance.membership_user_id)
    else:
        user = User.objects.filter(id=attendance.membership_user_id).first()
    if user is None:
        return None
    return EventInternalAttendeeData(
        user_id=user.id,
        email=user.email,
        name=user.get_full_name(),
        status=cast(Literal["accepted", "declined", "pending"], attendance.status),
    )


def serialize_event_external_attendee(
    external_attendance: EventExternalAttendance,
) -> EventExternalAttendeeData | None:
    """Serialize an external event attendance to ``EventExternalAttendeeData``.

    ``EventExternalAttendance.external_attendee`` is ``null=True``, so a row can exist
    with no attendee behind it. Such a row has no email and no name to report, and a
    payload carrying ``email=""`` would be worse than no payload at all for a webhook
    consumer routing on it -- so it serializes to ``None`` and every caller drops it,
    exactly as ``serialize_event_internal_attendee`` already does for an orphan
    internal attendance. Before this guard the dereference below raised
    ``AttributeError`` on any ``update_event`` (or event serialization) touching such
    a row, for every caller, not just the public API.
    """
    attendee = external_attendance.external_attendee_fk  # type: ignore[attr-defined]
    if attendee is None:
        return None
    return EventExternalAttendeeData(
        email=attendee.email,
        name=attendee.name,
        status=cast(
            Literal["accepted", "declined", "pending"],
            external_attendance.status,
        ),
    )


def serialize_event(event: CalendarEvent) -> CalendarEventData:
    """Build a ``CalendarEventData`` payload from a ``CalendarEvent`` instance.

    For recurring instances the attendees, external attendees, and resource
    allocations are pulled from the parent recurring object.
    """
    # Resolve the attendances iterable once (parent for recurring instances) so the
    # attendee users can be batch-fetched in a single query instead of one per
    # attendee (N+1 against users.User).
    attendances = list(
        event.parent_recurring_object.attendances.all()
        if event.parent_recurring_object
        else (event.attendances.all() if event.id else [])
    )
    attendee_user_by_id = {
        u.id: u
        for u in User.objects.filter(
            id__in={a.membership_user_id for a in attendances if a.membership_user_id is not None}
        )
    }
    return CalendarEventData(
        id=event.id,
        calendar_id=event.calendar_fk_id,  # type: ignore[arg-type]
        start_time=event.start_time,
        end_time=event.end_time,
        timezone=event.timezone,
        title=event.title,
        description=event.description,
        calendar_settings=CalendarSettingsData(
            manage_available_windows=event.calendar_fk.manage_available_windows,  # type: ignore[attr-defined]
            accepts_public_scheduling=event.calendar_fk.accepts_public_scheduling,  # type: ignore[attr-defined]
        ),
        original_payload=event.meta.get("latest_original_payload", {})
        if hasattr(event, "meta") and event.meta
        else {},
        attendees=[
            serialized
            for attendance in attendances
            # Orphan attendances (no membership-backed identity) serialize to None
            # and are intentionally excluded from the membership-scoped attendee list.
            if (serialized := serialize_event_internal_attendee(attendance, attendee_user_by_id))
            is not None
        ],
        external_attendees=[
            serialized_external
            # For recurring instances, get external attendances from the parent event; for regular events, use their own
            for external_attendance in (
                event.parent_recurring_object.external_attendances.all()
                if event.parent_recurring_object
                else (event.external_attendances.all() if event.id else [])
            )
            # A row with a NULL ``external_attendee`` serializes to None and is
            # dropped, matching the orphan-internal-attendance handling above.
            if (serialized_external := serialize_event_external_attendee(external_attendance))
            is not None
        ],
        resources=[
            ResourceData(
                title=resource_allocation.calendar.name,  # type: ignore[union-attr]
                email=resource_allocation.calendar.email,  # type: ignore[union-attr]
                external_id=resource_allocation.calendar.external_id,  # type: ignore[union-attr]
                status=cast(
                    Literal["accepted", "declined", "pending"],
                    resource_allocation.status,
                ),
            )
            # For recurring instances, get resource allocations from the parent event; for regular events, use their own
            for resource_allocation in (
                event.parent_recurring_object.resource_allocations.all()
                if event.parent_recurring_object
                else (event.resource_allocations.all() if event.id else [])
            )
        ],
        external_id=event.external_id,
        recurrence_rule=(
            event.recurrence_rule.to_rrule_string() if event.recurrence_rule else None
        ),
        status="confirmed",
        is_recurring=event.is_recurring,
        recurring_event_id=(
            event.parent_recurring_object_fk_id  # type: ignore[arg-type]
            if event.parent_recurring_object_fk_id
            else None
        ),
        external_client_identifiers=[
            ExternalClientIdentifierData(system=identifier.system, identifier=identifier.identifier)
            # Identifiers live on the master row, same as attendees/external
            # attendees/resources above: a recurring instance reads its parent's.
            # ``.all()`` reuses the prefetch cache when the caller prefetched
            # ``external_client_identifiers`` on the queryset that produced ``event``
            # (or ``parent_recurring_object``) -- no per-event query in that case.
            for identifier in (
                event.parent_recurring_object.external_client_identifiers.all()
                if event.parent_recurring_object
                else (event.external_client_identifiers.all() if event.id else [])
            )
        ],
    )


def serialize_event_data_input(
    event: CalendarEvent,
    event_data: CalendarEventInputData,
    organization: Organization,
    current_event: CalendarEventData | None = None,
) -> CalendarEventData:
    """Build a ``CalendarEventData`` payload from input data and an existing event.

    This is used when creating or updating events to produce the payload for
    side-effects (webhooks, notifications) before the ORM object has been fully
    reloaded with related data.

    ``organization`` is required for the resource-calendar lookup (multi-tenant
    guard: only calendars belonging to ``organization`` are considered).

    ``event_data``'s tri-state fields (``title``, ``description``, ``attendances``,
    ``external_attendances``) may be ``None``, meaning the caller left them
    untouched. This payload describes the event's state AFTER the write, so an
    omitted field resolves to what is currently stored -- never to empty. Pass
    ``current_event`` (the already-serialized pre-write state) to resolve the two
    attendee lists without re-reading them; without it they are read from ``event``.
    """
    input_attendances = event_data.attendances
    input_external_attendances = event_data.external_attendances
    attendees_override: list[EventInternalAttendeeData] | None = None
    external_attendees_override: list[EventExternalAttendeeData] | None = None

    if input_attendances is None:
        if current_event is not None:
            attendees_override = current_event.attendees
            input_attendances = []
        else:
            input_attendances = [
                EventAttendanceInputData(user_id=attendance.membership_user_id)
                for attendance in (event.attendances.all() if event.id else [])
                if attendance.membership_user_id is not None
            ]
    if input_external_attendances is None:
        if current_event is not None:
            external_attendees_override = current_event.external_attendees
            input_external_attendances = []
        else:
            input_external_attendances = [
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        id=external_attendance.external_attendee_fk_id,  # type: ignore[arg-type]
                        email=external_attendance.external_attendee.email,  # type: ignore[union-attr]
                        name=external_attendance.external_attendee.name or "",  # type: ignore[union-attr]
                    )
                )
                for external_attendance in (
                    event.external_attendances.select_related("external_attendee")
                    if event.id
                    else []
                )
                if external_attendance.external_attendee_fk_id is not None
            ]

    new_attendance_user_ids = [a.user_id for a in input_attendances]
    new_external_attendances_attendee_ids = [
        a.external_attendee.id for a in input_external_attendances
    ]
    # Resolve which input attendee user_ids back an OrganizationMembership. Only
    # members produce a membership-scoped internal attendee; non-member (orphan)
    # user_ids are excluded from the serialized list so this payload agrees with
    # ``serialize_event`` (which drops orphan attendances). Without this guard the
    # permission diff — keyed by user_id — would see a persisted orphan only on the
    # ``new`` side and falsely flag it as an attendee change.
    member_user_ids = resolve_member_user_ids(new_attendance_user_ids, organization.id)
    attendances_users_by_id = {u.id: u for u in User.objects.filter(id__in=member_user_ids)}
    # ``unscoped()`` on both: ``event`` is an organization-safe relation, so
    # filtering it by instance puts the event's ``organization_id`` in the ``ON``
    # clause -- the tenant boundary is in the query already.
    existing_attendances_by_user_id = {
        a.membership_user_id: a
        for a in EventAttendance.objects.unscoped().filter(
            event=event, membership_user_id__in=new_attendance_user_ids
        )
    }
    existing_external_attendances_by_attendee_id = {
        a.external_attendee.id: a
        for a in EventExternalAttendance.objects.unscoped().filter(
            event=event, external_attendee__id__in=new_external_attendances_attendee_ids
        )
    }
    return CalendarEventData(
        id=event.id,
        calendar_id=event.calendar_fk_id,  # type: ignore[arg-type]
        start_time=event_data.start_time,
        end_time=event_data.end_time,
        timezone=event_data.timezone,
        # Tri-state: an omitted title/description is unchanged, so this
        # post-write snapshot carries the event's stored value for it.
        title=event_data.title if event_data.title is not None else event.title,
        description=(
            event_data.description if event_data.description is not None else event.description
        ),
        calendar_settings=CalendarSettingsData(
            manage_available_windows=event.calendar_fk.manage_available_windows,  # type: ignore[attr-defined]
            accepts_public_scheduling=event.calendar_fk.accepts_public_scheduling,  # type: ignore[attr-defined]
        ),
        original_payload={},  # doesn't matter
        attendees=attendees_override
        if attendees_override is not None
        else [
            EventInternalAttendeeData(
                user_id=attendance.user_id,
                email=attendances_users_by_id[attendance.user_id].email,
                name=attendances_users_by_id[attendance.user_id].get_full_name(),
                status=cast(
                    Literal["accepted", "declined", "pending"],
                    existing_attendances_by_user_id.get(attendance.user_id, "pending"),
                ),
            )
            # For recurring instances, get attendances from the parent event; for regular events, use their own
            for attendance in input_attendances
            # Non-member (orphan) attendees have no membership-backed identity and are
            # excluded — matching ``serialize_event`` so both sides of the diff agree.
            if attendance.user_id in member_user_ids
        ],
        external_attendees=external_attendees_override
        if external_attendees_override is not None
        else [
            EventExternalAttendeeData(
                email=external_attendance.external_attendee.email,
                name=external_attendance.external_attendee.name,
                status=cast(
                    Literal["accepted", "declined", "pending"],
                    existing_external_attendances_by_attendee_id.get(
                        external_attendance.external_attendee.id, "pending"
                    ),
                ),
            )
            # For recurring instances, get external attendances from the parent event; for regular events, use their own
            for external_attendance in input_external_attendances
        ],
        resources=[
            ResourceData(
                title=resource_allocation.calendar.name,
                email=resource_allocation.calendar.email,
                external_id=resource_allocation.calendar.external_id,
                status=cast(
                    Literal["accepted", "declined", "pending"],
                    resource_allocation.status,
                ),
            )
            # For recurring instances, get resource allocations from the parent event; for regular events, use their own
            for resource_allocation in Calendar.objects.filter_by_organization(organization).filter(
                id__in=[r.resource_id for r in event_data.resource_allocations],
                calendar_type=CalendarType.RESOURCE,
            )
        ],
        external_id=event.external_id,
        recurrence_rule=event_data.recurrence_rule,
        status="confirmed",
        is_recurring=bool(event_data.recurrence_rule),
        recurring_event_id=None,
        # ``None`` (omitted -- untouched) maps to `[]` here: this snapshot only
        # feeds the permission-diff check (`can_perform_update`), which does not
        # examine identifiers, so there is no observable difference between "empty"
        # and "unspecified" for any current consumer. Input identifiers are already
        # `ExternalClientIdentifierData`, so they are passed through as-is.
        external_client_identifiers=event_data.external_client_identifiers or [],
    )


# ---------------------------------------------------------------------------
# Permission-granting helpers
# ---------------------------------------------------------------------------


def grant_calendar_owner_permissions(
    permission_service: CalendarPermissionService, calendar: Calendar
) -> None:
    """Grant calendar management permissions to all owners of a calendar.

    No-ops gracefully if ``permission_service`` is ``None``.
    """
    # Grant permissions to all calendar owners (resolved via membership; orphan
    # ownerships with a null membership are intentionally skipped).
    calendar_owners = User.objects.filter(
        memberships__calendar_ownerships__calendar_fk_id=calendar.id,
        memberships__calendar_ownerships__organization_id=calendar.organization_id,
    ).distinct()

    for owner in calendar_owners:
        # Check if user already has a token for this calendar
        existing_token = (
            CalendarManagementToken.objects.filter_by_organization(calendar.organization_id)
            .filter(
                membership_user_id=owner.id,
                calendar_fk_id=calendar.id,
                event_fk_id__isnull=True,
                revoked_at__isnull=True,
            )
            .first()
        )

        if not existing_token:
            permission_service.create_calendar_owner_token(
                organization_id=calendar.organization_id,
                user=owner,
                calendar_id=calendar.id,
            )


def grant_event_attendee_permissions(
    permission_service: CalendarPermissionService, event: CalendarEvent
) -> None:
    """Grant event management permissions to all attendees of an event.

    No-ops gracefully if ``permission_service`` is ``None``.
    """
    # Grant permissions to internal attendees (resolved via the denormalized
    # membership_user_id; orphan attendances without a membership-backed identity
    # are intentionally skipped).
    attendances = list(event.attendances.all())
    attendee_user_by_id = {
        u.id: u
        for u in User.objects.filter(
            id__in={a.membership_user_id for a in attendances if a.membership_user_id is not None}
        )
    }
    for attendance in attendances:
        if attendance.membership_user_id is None:
            continue
        attendee_user = attendee_user_by_id.get(attendance.membership_user_id)
        if attendee_user is None:
            continue
        # Check if user already has a token for this event
        existing_token = (
            CalendarManagementToken.objects.filter_by_organization(event.organization_id)
            .filter(
                membership_user_id=attendee_user.id,
                event_fk_id=event.id,
                revoked_at__isnull=True,
            )
            .first()
        )

        if not existing_token:
            permission_service.create_attendee_token(
                organization_id=attendance.organization_id,
                user=attendee_user,
                event_id=event.id,
            )

    # Grant permissions to external attendees
    for external_attendance in event.external_attendances.filter_by_organization(  # type: ignore[attr-defined]
        event.organization_id
    ):
        # Check if external attendee already has a token for this event
        existing_token = (
            CalendarManagementToken.objects.filter_by_organization(event.organization_id)
            .filter(
                external_attendee_fk_id=external_attendance.external_attendee_fk_id,
                event_fk_id=event.id,
                revoked_at__isnull=True,
            )
            .first()
        )

        if not existing_token:
            permission_service.create_external_attendee_update_token(
                organization_id=external_attendance.organization_id,
                event_id=event.id,
                external_attendee_id=external_attendance.external_attendee_fk_id,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Calendar lookups — org-scoped per-instance cache (lru_cache bug fix)
# ---------------------------------------------------------------------------
# The original code used @lru_cache on instance methods.  lru_cache keys only
# on positional args, so it ignores ``self.organization`` — a reused service
# instance can return a cached Calendar from org A when queried for org B.
#
# The fix: accept an explicit ``cache`` dict keyed on ``(organization_id, <id>)``
# that the facade owns and initialises in ``__init__``.  The cache is per-instance
# (allocated in ``CalendarService.__init__``), not module-level.
#
# CallerAPI:
#   cache: dict[tuple[int, str | int], Calendar]  (facade owns it)
#   get_calendar_by_external_id(cache, calendar_external_id, organization, calendar_adapter)
#   get_calendar_by_id(cache, calendar_id, organization)


def get_calendar_by_external_id(
    cache: dict[tuple[int, str | int], Calendar],
    calendar_external_id: str,
    organization: Organization,
    calendar_adapter: CalendarAdapter | None,
) -> Calendar:
    """Look up a ``Calendar`` by external ID, scoped to ``organization``.

    Results are memoized in ``cache`` keyed on ``(organization_id, external_id)``
    so repeated calls within the same service lifecycle avoid extra DB round-trips
    without leaking data across organizations.
    """
    cache_key: tuple[int, str | int] = (organization.id, calendar_external_id)
    if cache_key in cache:
        return cache[cache_key]

    query_kwargs: dict[str, object] = {
        "external_id": calendar_external_id,
    }
    if calendar_adapter:
        query_kwargs["provider"] = calendar_adapter.provider

    result = Calendar.objects.filter_by_organization(organization.id).get(**query_kwargs)
    cache[cache_key] = result
    return result


def get_calendar_by_id(
    cache: dict[tuple[int, str | int], Calendar],
    calendar_id: int,
    organization: Organization,
) -> Calendar:
    """Look up a ``Calendar`` by internal ID, scoped to ``organization``.

    Results are memoized in ``cache`` keyed on ``(organization_id, calendar_id)``
    so repeated calls within the same service lifecycle avoid extra DB round-trips
    without leaking data across organizations.
    """
    cache_key: tuple[int, str | int] = (organization.id, calendar_id)
    if cache_key in cache:
        return cache[cache_key]

    result = Calendar.objects.filter_by_organization(organization.id).get(
        id=calendar_id,
    )
    cache[cache_key] = result
    return result
