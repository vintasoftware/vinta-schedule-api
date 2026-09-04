"""Read-only viewsets for the unauthenticated booking-code REST surface.

Ports the six code-gated ``*WithCode`` GraphQL query fields
(``public_api/queries.py``) to REST. Every endpoint here:

- Requires ``X-Booking-Code``.
- Is repeatable and NEVER consumes the code (no ``consume_code`` call anywhere
  in this module).
- Returns one uniform ``403 {"detail": "Invalid or expired code."}``
  (:class:`~calendar_integration.booking_exceptions.OpaqueCodeError`) for
  EVERY code failure -- invalid, expired, used, revoked, or wrong-scope. This
  is deliberately different from the write viewsets in ``booking_views.py``,
  which discriminate failures via ``BookingCodeErrorCode`` -- see the
  "Error contract (reads)" Guiding Decision. Do not "improve" consistency
  with the writes here.
- Validates its time range (via ``validate_code_gated_range``) BEFORE
  resolving the code, so a malformed/backwards/too-large range is always a
  ``400`` reachable without a valid code -- see the "Range validation
  ordering" Guiding Decision. Response status must never become a second
  oracle for probing code state.

**Scope resolution** mirrors the GraphQL originals: calendar-scoped reads
take ``token.calendar``, falling back to ``token.event.calendar``;
group-scoped reads take ``token.calendar_group``, falling back to
``token.event.calendar_group``. A code resolving to neither raises the
uniform ``OpaqueCodeError`` -- this is also what naturally rejects a
single-calendar code on the two group-scoped endpoints (their ``group``
resolves to ``None``). It is NOT enough, however, to reject a group-scoped
code on the calendar-scoped reads: ``CalendarGroupService.create_grouped_event``
always creates the underlying ``CalendarEvent`` on a real single calendar, so
``token.event.calendar`` is populated even for a token that is itself scoped
to a group -- naively falling through to it would leak that specific
calendar's availability to a patient holding only a group code. So each
resolver first checks the token's OWN scope column
(``calendar_group_fk_id`` / ``calendar_fk_id``) and raises immediately if it
belongs to the OTHER scope, before ever consulting the ``event`` fallback.
See ``_resolve_calendar_scope_opaquely`` / ``_resolve_group_scope_opaquely``.

**Pinned duration** (the two bookable-slots endpoints only): duration pinning
lives on ``CalendarGroup.duration``, not on the token -- a calendar-scoped
code carries no duration constraint at all (no ``Calendar.duration`` exists),
so its ``duration_seconds`` always stands as given. ``duration_seconds``
presence is still required regardless of pin state on EITHER endpoint -- a
request that omits it entirely is a ``400`` whether or not the resolved
group-scoped code's group pins a duration, so that status alone cannot
disclose pin state. Once present, on the group-scoped endpoint a group that
carries a ``duration`` uses it UNCONDITIONALLY: a wrong or malformed (but
non-empty) ``duration_seconds`` on a pinned code still produces the exact
same response as the correct one. See ``_resolve_duration``.

**Phase 9 addendum -- codeless, slug-addressed public-group reads.** Below
the code-gated section, this module also carries a small, separately-scoped
set of endpoints that carry no ``X-Booking-Code`` at all: the discovery
reads for a ``CalendarGroup`` whose ``accepts_public_scheduling`` is
``True``. These are a **different addressing scheme** (path-based
``public_slug``, not a header-borne code) and a **different error
contract** (real ``404``/``403``, not the uniform opaque ``403`` above) --
see the "Codeless, slug-addressed public-group reads" section below for why,
and do not conflate the two. They mirror the codeless branch of
``BookingCodeGroupEventViewSet.create`` in ``booking_views.py`` exactly.
"""

import datetime

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from calendar_integration.booking_auth import (
    BOOKING_CODE_HEADER,
    resolve_booking_code_opaquely,
    validate_code_gated_range,
)
from calendar_integration.booking_exceptions import NotPermittedAPIException, OpaqueCodeError
from calendar_integration.booking_views import BookingCodeViewMixin
from calendar_integration.exceptions import CalendarServiceNotInjectedError
from calendar_integration.models import Calendar, CalendarGroup, CalendarManagementToken
from calendar_integration.serializers import (
    AvailableTimeSerializer,
    AvailableTimeWindowSerializer,
    BookableSlotProposalSerializer,
    CalendarGroupAvailabilityQuerySerializer,
    CalendarGroupRangeAvailabilitySerializer,
    UnavailableTimeWindowSerializer,
)
from organizations.models import Organization


_DEFAULT_SLOT_STEP = datetime.timedelta(seconds=15 * 60)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_org_opaquely(token: CalendarManagementToken) -> Organization:
    """Resolve ``token.organization_id``, mapping a missing org to the uniform 403.

    Mirrors ``public_api.queries._get_org_from_token``, but raises
    ``OpaqueCodeError`` (read-side uniform failure) instead of a
    ``GraphQLError``.
    """
    try:
        return Organization.objects.get(id=token.organization_id)
    except Organization.DoesNotExist as exc:
        raise OpaqueCodeError() from exc


def _resolve_calendar_scope_opaquely(token: CalendarManagementToken) -> Calendar:
    """Resolve the calendar bound to ``token`` (``token.calendar`` first, then
    ``token.event.calendar``), or raise the uniform 403.

    This is also what rejects a group-scoped code on the calendar-scoped
    reads -- a token scoped to a group (never a calendar) resolves to
    ``None`` here, same as any other invalid scope.

    Guard: a token that is itself scoped to a calendar GROUP (``calendar_group``
    set on the token) must never fall through to ``token.event.calendar`` --
    for a group booking, ``event.calendar`` is always the specific staff
    member's underlying calendar the event landed on, and disclosing it here
    would defeat the point of group scoping. That fallback is only for a
    token that carries no group scope of its own.
    """
    if token.calendar_group_fk_id is not None:
        raise OpaqueCodeError()
    calendar = token.calendar
    if calendar is None and token.event is not None:
        calendar = token.event.calendar
    if calendar is None:
        raise OpaqueCodeError()
    return calendar


def _resolve_group_scope_opaquely(token: CalendarManagementToken) -> CalendarGroup:
    """Resolve the calendar group bound to ``token`` (``token.calendar_group``
    first, then ``token.event.calendar_group``), or raise the uniform 403.

    This is also what rejects a single-calendar code on the group-scoped
    reads -- a token scoped to a single calendar (never a group) resolves to
    ``None`` here, same as any other invalid scope.

    Guard: symmetric to ``_resolve_calendar_scope_opaquely`` -- a token that
    is itself scoped to a single CALENDAR must never fall through to
    ``token.event.calendar_group``.
    """
    if token.calendar_fk_id is not None:
        raise OpaqueCodeError()
    group = token.calendar_group
    if group is None and token.event is not None:
        group = token.event.calendar_group
    if group is None:
        raise OpaqueCodeError()
    return group


def _parse_datetime_query_param(request: Request, name: str) -> datetime.datetime:
    raw = request.query_params.get(name)
    if not raw:
        raise ValidationError({"non_field_errors": [f"{name} is required."]})
    try:
        value = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError({"non_field_errors": [f"Invalid {name}; use ISO 8601."]}) from exc
    if value.tzinfo is None:
        # A naive value would otherwise be interpreted in the server's default
        # timezone rather than rejected, silently comparing against the
        # timezone-aware generated columns with an offset the client never
        # specified. GraphQL's ``datetime`` scalar already requires one.
        raise ValidationError(
            {"non_field_errors": [f"{name} must include a UTC offset (ISO 8601)."]}
        )
    return value


def _resolve_duration(group: CalendarGroup | None, request: Request) -> datetime.timedelta:
    """Resolve the search duration for a bookable-slots read.

    ``group`` is the token's own resolved ``CalendarGroup`` on the
    group-scoped endpoint, or ``None`` on the calendar-scoped endpoint --
    duration pinning lives on ``CalendarGroup.duration``, not on the token,
    and a calendar-scoped code carries no duration constraint at all (no
    ``Calendar.duration`` exists), so its ``duration_seconds`` always stands.

    ``duration_seconds`` presence is validated identically regardless of pin
    state -- a request that omits it is always a ``400``, so that status
    alone never discloses whether a resolved group pins a duration. Only
    once presence is established does the pin take over: when ``group`` is
    set and ``group.duration`` is set, it is returned UNCONDITIONALLY and the
    parameter's parsed VALUE is never even inspected, so a wrong or
    malformed (but non-empty) value on a pinned code still produces
    byte-identical output to the correct one (see the "Duration pinning --
    reads" Guiding Decision). Only when there is no pin (no group, or a group
    with no duration) does ``duration_seconds`` also have to be a valid
    positive integer.
    """
    raw = request.query_params.get("duration_seconds")
    if not raw:
        raise ValidationError({"non_field_errors": ["duration_seconds is required."]})

    if group is not None and group.duration is not None:
        return group.duration

    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValidationError(
            {"non_field_errors": ["duration_seconds must be an integer."]}
        ) from exc
    if seconds <= 0:
        raise ValidationError({"non_field_errors": ["duration_seconds must be positive."]})
    return datetime.timedelta(seconds=seconds)


def _resolve_slot_step(request: Request) -> datetime.timedelta:
    raw = request.query_params.get("slot_step_seconds")
    if raw is None:
        return _DEFAULT_SLOT_STEP
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValidationError(
            {"non_field_errors": ["slot_step_seconds must be an integer."]}
        ) from exc
    if seconds <= 0:
        raise ValidationError({"non_field_errors": ["slot_step_seconds must be positive."]})
    return datetime.timedelta(seconds=seconds)


_CODE_HEADER_PARAMETER = OpenApiParameter(
    name=BOOKING_CODE_HEADER,
    type=str,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Single-use booking code. Every failure -- invalid, expired, used, "
    'revoked, or wrong-scope -- returns the same 403 {"detail": "Invalid or '
    'expired code."}; this endpoint never discloses which one occurred. '
    "Repeatable: never consumed by a read.",
)

_START_DATETIME_PARAMETER = OpenApiParameter(
    name="start_datetime",
    type=str,
    location=OpenApiParameter.QUERY,
    required=True,
    description="Start of the query window (ISO 8601).",
)

_END_DATETIME_PARAMETER = OpenApiParameter(
    name="end_datetime",
    type=str,
    location=OpenApiParameter.QUERY,
    required=True,
    description="End of the query window (ISO 8601).",
)

_SEARCH_WINDOW_START_PARAMETER = OpenApiParameter(
    name="search_window_start",
    type=str,
    location=OpenApiParameter.QUERY,
    required=True,
    description="Start of the search window (ISO 8601).",
)

_SEARCH_WINDOW_END_PARAMETER = OpenApiParameter(
    name="search_window_end",
    type=str,
    location=OpenApiParameter.QUERY,
    required=True,
    description="End of the search window (ISO 8601).",
)

_DURATION_SECONDS_PARAMETER = OpenApiParameter(
    name="duration_seconds",
    type=int,
    location=OpenApiParameter.QUERY,
    required=True,
    description="Desired event duration, in seconds. ALWAYS REQUIRED to be present "
    "(a request omitting it is a 400 whether or not the resolved code's group pins "
    "a duration -- presence alone must never disclose pin state). Duration pinning "
    "lives on the CalendarGroup, not on the code: on the calendar-scoped endpoint "
    "this value always stands as given -- a calendar-scoped code carries no "
    "duration constraint at all. On the group-scoped endpoint, when the resolved "
    "code's group pins a duration, the pin silently overrides this parameter's "
    "VALUE: a mismatched or malformed value produces the exact same response as "
    "the pinned value, so this endpoint cannot be used to probe whether a group is "
    "pinned or what the pin is. When the group pins no duration, the value must "
    "also be a valid positive integer.",
)

_SLOT_STEP_SECONDS_PARAMETER = OpenApiParameter(
    name="slot_step_seconds",
    type=int,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Search step, in seconds (default 900 = 15min).",
)


# ---------------------------------------------------------------------------
# Calendar-scoped reads
# ---------------------------------------------------------------------------


@extend_schema(tags=["Booking Codes"])
class BookingCodeAvailableTimesViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated read of a calendar's available times.

    ``GET /public/booking/available-times/`` ports
    ``public_api.queries.Query.available_times_with_code`` to REST. The
    calendar comes from the resolved token (``token.calendar``, falling back
    to ``token.event.calendar``); no calendar id is accepted from the client.
    """

    pagination_class = None  # returns a bare array, not a paginated page
    # No natural model-bound queryset (scope comes from the resolved token, not a
    # URL object) -- set purely to silence drf-spectacular's "Failed to obtain
    # model through view's queryset" schema-generation warning.
    queryset = Calendar.objects.none()

    @extend_schema(
        responses={200: AvailableTimeSerializer(many=True)},
        parameters=[_CODE_HEADER_PARAMETER, _START_DATETIME_PARAMETER, _END_DATETIME_PARAMETER],
        summary="Available times for the calendar bound to a booking code",
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        permission_service = self.calendar_permission_service
        calendar_service = self.calendar_service
        if permission_service is None or calendar_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_service not configured; "
                "check the DI container."
            )

        start_datetime = _parse_datetime_query_param(request, "start_datetime")
        end_datetime = _parse_datetime_query_param(request, "end_datetime")

        # --- range validation BEFORE code resolution -- see module docstring. ---
        validate_code_gated_range(start_datetime, end_datetime)

        token = resolve_booking_code_opaquely(request, permission_service)
        calendar = _resolve_calendar_scope_opaquely(token)
        org = _resolve_org_opaquely(token)

        calendar_service.initialize_without_provider(user_or_token=None, organization=org)
        available_times = calendar_service.get_available_times_expanded(
            calendar, start_datetime, end_datetime
        )
        # Every returned instance belongs to the same `calendar` already resolved
        # above -- attach it directly rather than letting the (Virtual-Model-backed)
        # serializer's "calendar" field lazily re-fetch it per instance, which the
        # N+1 guard on AvailableTimeSerializer would otherwise trip.
        for available_time in available_times:
            available_time.calendar = calendar

        context = self.get_serializer_context()
        serializer = AvailableTimeSerializer(available_times, many=True, context=context)
        return Response(serializer.data)


@extend_schema(tags=["Booking Codes"])
class BookingCodeAvailabilityWindowsViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated read of a calendar's availability windows.

    ``GET /public/booking/availability-windows/`` ports
    ``public_api.queries.Query.availability_windows_with_code`` to REST. The
    calendar comes from the resolved token (``token.calendar``, falling back
    to ``token.event.calendar``); no calendar id is accepted from the client.
    """

    pagination_class = None  # returns a bare array, not a paginated page
    # No natural model-bound queryset (scope comes from the resolved token, not a
    # URL object) -- set purely to silence drf-spectacular's "Failed to obtain
    # model through view's queryset" schema-generation warning.
    queryset = Calendar.objects.none()

    @extend_schema(
        responses={200: AvailableTimeWindowSerializer(many=True)},
        parameters=[_CODE_HEADER_PARAMETER, _START_DATETIME_PARAMETER, _END_DATETIME_PARAMETER],
        summary="Availability windows for the calendar bound to a booking code",
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        permission_service = self.calendar_permission_service
        calendar_service = self.calendar_service
        if permission_service is None or calendar_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_service not configured; "
                "check the DI container."
            )

        start_datetime = _parse_datetime_query_param(request, "start_datetime")
        end_datetime = _parse_datetime_query_param(request, "end_datetime")

        # --- range validation BEFORE code resolution -- see module docstring. ---
        validate_code_gated_range(start_datetime, end_datetime)

        token = resolve_booking_code_opaquely(request, permission_service)
        calendar = _resolve_calendar_scope_opaquely(token)
        org = _resolve_org_opaquely(token)

        calendar_service.initialize_without_provider(user_or_token=None, organization=org)
        windows = calendar_service.get_availability_windows_in_range(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )

        serializer = AvailableTimeWindowSerializer(list(windows), many=True)
        return Response(serializer.data)


@extend_schema(tags=["Booking Codes"])
class BookingCodeUnavailableWindowsViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated read of a calendar's unavailable windows.

    ``GET /public/booking/unavailable-windows/`` ports
    ``public_api.queries.Query.unavailable_windows_with_code`` to REST. The
    calendar comes from the resolved token (``token.calendar``, falling back
    to ``token.event.calendar``); no calendar id is accepted from the client.
    """

    pagination_class = None  # returns a bare array, not a paginated page
    # No natural model-bound queryset (scope comes from the resolved token, not a
    # URL object) -- set purely to silence drf-spectacular's "Failed to obtain
    # model through view's queryset" schema-generation warning.
    queryset = Calendar.objects.none()

    @extend_schema(
        responses={200: UnavailableTimeWindowSerializer(many=True)},
        parameters=[_CODE_HEADER_PARAMETER, _START_DATETIME_PARAMETER, _END_DATETIME_PARAMETER],
        summary="Unavailable windows for the calendar bound to a booking code",
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        permission_service = self.calendar_permission_service
        calendar_service = self.calendar_service
        if permission_service is None or calendar_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_service not configured; "
                "check the DI container."
            )

        start_datetime = _parse_datetime_query_param(request, "start_datetime")
        end_datetime = _parse_datetime_query_param(request, "end_datetime")

        # --- range validation BEFORE code resolution -- see module docstring. ---
        validate_code_gated_range(start_datetime, end_datetime)

        token = resolve_booking_code_opaquely(request, permission_service)
        calendar = _resolve_calendar_scope_opaquely(token)
        org = _resolve_org_opaquely(token)

        calendar_service.initialize_without_provider(user_or_token=None, organization=org)
        unavailable = calendar_service.get_unavailable_time_windows_in_range(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )

        serializer = UnavailableTimeWindowSerializer(list(unavailable), many=True)
        return Response(serializer.data)


@extend_schema(tags=["Booking Codes"])
class BookingCodeCalendarBookableSlotsViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated read of bookable slots for a single calendar.

    ``GET /public/booking/calendar-bookable-slots/`` ports
    ``public_api.queries.Query.calendar_bookable_slots_with_code`` to REST.
    The calendar comes from the resolved token (``token.calendar``, falling
    back to ``token.event.calendar``); a group-scoped code is rejected via
    the same uniform 403 (its ``calendar`` resolves to ``None``).

    This is the FIRST REST surface for
    ``BookableSlotsService.find_bookable_slots_for_calendar`` -- previously
    reachable only from GraphQL.
    """

    pagination_class = None  # returns a bare array, not a paginated page
    # No natural model-bound queryset (scope comes from the resolved token, not a
    # URL object) -- set purely to silence drf-spectacular's "Failed to obtain
    # model through view's queryset" schema-generation warning.
    queryset = Calendar.objects.none()

    @extend_schema(
        responses={200: BookableSlotProposalSerializer(many=True)},
        parameters=[
            _CODE_HEADER_PARAMETER,
            _SEARCH_WINDOW_START_PARAMETER,
            _SEARCH_WINDOW_END_PARAMETER,
            _DURATION_SECONDS_PARAMETER,
            _SLOT_STEP_SECONDS_PARAMETER,
        ],
        summary="Bookable slot proposals for the calendar bound to a booking code",
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        permission_service = self.calendar_permission_service
        bookable_slots_service = self.bookable_slots_service
        if permission_service is None or bookable_slots_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / bookable_slots_service not configured; "
                "check the DI container."
            )

        search_window_start = _parse_datetime_query_param(request, "search_window_start")
        search_window_end = _parse_datetime_query_param(request, "search_window_end")

        # --- range validation BEFORE code resolution -- see module docstring. ---
        validate_code_gated_range(search_window_start, search_window_end)

        token = resolve_booking_code_opaquely(request, permission_service)
        calendar = _resolve_calendar_scope_opaquely(token)
        org = _resolve_org_opaquely(token)

        # --- no duration constraint on a calendar-scoped code -- see module
        # docstring / _resolve_duration. ---
        duration = _resolve_duration(None, request)
        slot_step = _resolve_slot_step(request)

        bookable_slots_service.initialize(organization=org)
        proposals = bookable_slots_service.find_bookable_slots_for_calendar(
            calendar_id=calendar.id,
            search_window_start=search_window_start,
            search_window_end=search_window_end,
            duration=duration,
            slot_step=slot_step,
        )

        payload = [{"start_time": p.start_time, "end_time": p.end_time} for p in proposals]
        return Response(BookableSlotProposalSerializer(payload, many=True).data)


# ---------------------------------------------------------------------------
# Group-scoped reads
# ---------------------------------------------------------------------------


@extend_schema(tags=["Booking Codes"])
class BookingCodeCalendarGroupBookableSlotsViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated read of bookable slots for a calendar group.

    ``GET /public/booking/calendar-group-bookable-slots/`` ports
    ``public_api.queries.Query.calendar_group_bookable_slots_with_code`` to
    REST. The group comes from the resolved token (``token.calendar_group``,
    falling back to ``token.event.calendar_group``); a single-calendar code
    is rejected via the same uniform 403 (its ``calendar_group`` resolves to
    ``None``).
    """

    pagination_class = None  # returns a bare array, not a paginated page
    # No natural model-bound queryset (scope comes from the resolved token, not a
    # URL object) -- set purely to silence drf-spectacular's "Failed to obtain
    # model through view's queryset" schema-generation warning.
    queryset = CalendarGroup.objects.none()

    @extend_schema(
        responses={200: BookableSlotProposalSerializer(many=True)},
        parameters=[
            _CODE_HEADER_PARAMETER,
            _SEARCH_WINDOW_START_PARAMETER,
            _SEARCH_WINDOW_END_PARAMETER,
            _DURATION_SECONDS_PARAMETER,
            _SLOT_STEP_SECONDS_PARAMETER,
        ],
        summary="Bookable slot proposals for the calendar group bound to a booking code",
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        permission_service = self.calendar_permission_service
        calendar_group_service = self.calendar_group_service
        if permission_service is None or calendar_group_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_group_service not configured; "
                "check the DI container."
            )

        search_window_start = _parse_datetime_query_param(request, "search_window_start")
        search_window_end = _parse_datetime_query_param(request, "search_window_end")

        # --- range validation BEFORE code resolution -- see module docstring. ---
        validate_code_gated_range(search_window_start, search_window_end)

        token = resolve_booking_code_opaquely(request, permission_service)
        group = _resolve_group_scope_opaquely(token)
        org = _resolve_org_opaquely(token)

        # --- pinned-duration silent override -- see module docstring / _resolve_duration. ---
        duration = _resolve_duration(group, request)
        slot_step = _resolve_slot_step(request)

        calendar_group_service.initialize(organization=org)
        proposals = calendar_group_service.find_bookable_slots(
            group_id=group.id,
            search_window_start=search_window_start,
            search_window_end=search_window_end,
            duration=duration,
            slot_step=slot_step,
        )

        payload = [{"start_time": p.start_time, "end_time": p.end_time} for p in proposals]
        return Response(BookableSlotProposalSerializer(payload, many=True).data)


@extend_schema(tags=["Booking Codes"])
class BookingCodeCalendarGroupAvailabilityViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, code-gated per-range availability for a calendar group.

    ``POST /public/booking/calendar-group-availability/`` ports
    ``public_api.queries.Query.calendar_group_availability_with_code`` to
    REST. A ``POST`` (not ``GET``) because it takes a list of ranges,
    matching the existing authenticated ``CalendarGroupViewSet.availability``
    action, which is also a ``POST`` for the same reason. The group comes
    from the resolved token (``token.calendar_group``, falling back to
    ``token.event.calendar_group``); a single-calendar code is rejected via
    the same uniform 403.
    """

    serializer_class = CalendarGroupAvailabilityQuerySerializer
    # No natural model-bound queryset (scope comes from the resolved token, not a
    # URL object) -- set purely to silence drf-spectacular's "Failed to obtain
    # model through view's queryset" schema-generation warning.
    queryset = CalendarGroup.objects.none()

    @extend_schema(
        request=CalendarGroupAvailabilityQuerySerializer,
        responses={200: CalendarGroupRangeAvailabilitySerializer(many=True)},
        parameters=[_CODE_HEADER_PARAMETER],
        summary="Per-range availability for the calendar group bound to a booking code",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        permission_service = self.calendar_permission_service
        calendar_group_service = self.calendar_group_service
        if permission_service is None or calendar_group_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_permission_service / calendar_group_service not configured; "
                "check the DI container."
            )

        # --- structural + range validation BEFORE code resolution -- see module
        # docstring. Every range is checked, matching the GraphQL original's
        # ``for r in ranges: _validate_code_gated_range(...)``. ---
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ranges = [(r["start_time"], r["end_time"]) for r in serializer.validated_data["ranges"]]
        for start_time, end_time in ranges:
            validate_code_gated_range(start_time, end_time)

        token = resolve_booking_code_opaquely(request, permission_service)
        group = _resolve_group_scope_opaquely(token)
        org = _resolve_org_opaquely(token)

        calendar_group_service.initialize(organization=org)
        result = calendar_group_service.check_group_availability(group_id=group.id, ranges=ranges)

        payload = [
            {
                "start_time": r.start_time,
                "end_time": r.end_time,
                "slots": [
                    {
                        "slot_id": s.slot_id,
                        "available_calendar_ids": s.available_calendar_ids,
                        "required_count": s.required_count,
                    }
                    for s in r.slots
                ],
            }
            for r in result
        ]
        return Response(CalendarGroupRangeAvailabilitySerializer(payload, many=True).data)


# ---------------------------------------------------------------------------
# Codeless, slug-addressed public-group reads (Phase 9)
# ---------------------------------------------------------------------------
#
# Everything above this line is code-gated: it reads `X-Booking-Code` and
# resolves scope from the resolved token, per the module docstring's "Scope
# resolution". The two endpoints below are the opposite: no code exists, so
# none is read. The group instead comes from the path's `public_slug` --
# the client's OWN input, exactly like the codeless branch of
# `BookingCodeGroupEventViewSet.create` in `booking_views.py`, which these
# mirror. Follow THAT branch's 404/403 split, not the uniform-403 contract
# above:
#
# - Unknown slug -> `404`. The slug is not a secret here (unlike a coded
#   read's token-bound scope), so disclosing "no such group" tells the
#   caller nothing it did not already know -- it had to have the slug to
#   ask in the first place.
# - A slug that resolves to a real, but non-public
#   (`accepts_public_scheduling=False`), group -> `403`. The group exists,
#   but this route is not open to it.
#
# Do NOT reuse `resolve_booking_code_opaquely` / `_resolve_group_scope_opaquely`
# here -- those exist for the opposite contract (one indistinguishable 403,
# never a 404, because a CODE's scope must stay secret). A group-scoped read
# down here must also never fall back to resolving one specific member
# calendar the way the calendar-scoped helpers above do for a token's
# `.event.calendar` -- there is no such fallback available or wanted on this
# surface; every read below stays strictly group-level.
#
# Window reads (availability-windows / unavailable-windows) are deliberately
# NOT ported to this codeless surface -- see the "Codeless discovery is
# group-aggregated" Guiding Decision. That decision requires every group
# read here to never attribute an interval to a specific member calendar.
# `find_bookable_slots` and `check_group_availability` (used below) already
# satisfy that: both compute their answer as a function of every calendar in
# a slot's pool at once (a slot is "available" only when enough of its pool
# clears TOGETHER), never surfacing one calendar's own window. There is no
# equivalent group-level primitive for a continuous availability/busy
# WINDOW. The calendar-scoped `get_availability_windows_in_range` /
# `get_unavailable_time_windows_in_range` calls the code-gated section above
# reuses are inherently single-calendar, and merging several practitioners'
# windows into one curve is not a query this codebase has anywhere to
# reuse -- shipping one here would mean inventing new aggregation logic
# rather than porting an existing service call (see this phase's working
# method: reuse existing service calls, do not write parallel
# implementations), and getting the merge wrong is actively unsafe rather
# than merely incomplete: unioning "any calendar in the pool is free" would
# offer slots no single combination can actually satisfy (the read and the
# Phase 3 write would then disagree, which this phase's acceptance
# criteria explicitly forbid), while intersecting "every calendar in the
# pool is free" would report the whole group busy solely because ONE
# calendar is busy -- which is itself a leak about that one calendar, the
# exact disclosure this design exists to prevent. Per the plan's own escape
# hatch ("if no coherent non-attributing aggregate exists for a window
# read, ship the other two and say so"), this module ships only the two
# group-aggregated reads below and stops there.


def _resolve_public_group(public_slug: str) -> CalendarGroup:
    """Resolve a codeless discovery read's addressed, public-scheduling group.

    Two-step resolution, mirroring ``BookingCodeGroupEventViewSet.create``'s
    codeless branch (``booking_views.py``) and this phase's stated contract:

    1. Look up ``public_booking_slug`` unscoped -- the organization is not
       yet known; this lookup is what determines it, so
       ``filter_by_organization`` cannot run yet. An unknown slug is a real
       ``404``: the slug is the client's own path input, not a secret the
       way a code-gated read's token-bound scope is, so confirming
       non-existence discloses nothing the caller did not already know.
    2. Require ``accepts_public_scheduling``. A slug that resolves to a
       real, but non-public, group is a real ``403``: the group exists, but
       this route is not open to it.

    Does NOT check ``group.duration`` -- callers that need the search
    duration call ``_resolve_public_group_duration`` next, which folds in
    the fail-closed null-duration rule.
    """
    try:
        group = (
            CalendarGroup.objects.unscoped()
            .select_related("organization")
            .get(public_booking_slug=public_slug)
        )
    except CalendarGroup.DoesNotExist as exc:
        raise NotFound("Calendar group not found.") from exc
    if not group.accepts_public_scheduling:
        raise NotPermittedAPIException("This group does not accept public scheduling.")
    return group


def _resolve_public_group_duration(group: CalendarGroup) -> datetime.timedelta:
    """Resolve the fixed search duration for a codeless discovery read.

    ``duration_seconds`` is not a query parameter on either endpoint below --
    the only length a codeless booking can ever have is the group's own
    pinned duration (see the "Duration -- reads" Guiding Decision), so there
    is nothing a client could legitimately pass to override it. A public
    group with no duration configured is a grandfathered misconfiguration
    (Phase 0's invariant: ``CalendarGroupService.create_group`` /
    ``update_group`` refuse to set ``accepts_public_scheduling=True`` without
    one, going forward -- see the "Public implies length-constrained" Guiding
    Decision) that the write side already refuses to book against, fail
    closed, inside ``CalendarPermissionService``. Fail closed here too,
    rather than guessing a length the write side would then reject -- this
    is what keeps every proposal a codeless read returns actually bookable
    through the Phase 3 codeless write, which is this phase's own
    acceptance criterion.

    Applied uniformly to BOTH endpoints below, including range-availability
    -- that endpoint does not itself consume a duration value (the client
    names its own ranges), but a group the write side would refuse
    regardless of the requested span should not report itself as available
    either; showing slots the write side can never honour would make the
    read and the write disagree, which this phase's acceptance criteria
    explicitly forbid.
    """
    if group.duration is None:
        raise NotPermittedAPIException("This group does not accept public scheduling.")
    return group.duration


@extend_schema(tags=["Booking Codes"])
class PublicCalendarGroupBookableSlotsViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, codeless read of bookable slots for a public calendar group.

    ``GET /public/booking/calendar-groups/<public_slug>/bookable-slots/`` is
    the codeless counterpart of ``BookingCodeCalendarGroupBookableSlotsViewSet``
    above -- no ``X-Booking-Code`` is read or required. The group comes from
    the path's ``public_slug``; see ``_resolve_public_group`` for the
    404/403 contract that gates it.

    Duration is NOT a query parameter here -- see
    ``_resolve_public_group_duration``. The slots this returns are exactly
    the slots the Phase 3 codeless write (``BookingCodeGroupEventViewSet.create``)
    will accept: same group id, same duration, same underlying
    ``find_bookable_slots`` call.
    """

    pagination_class = None  # returns a bare array, not a paginated page
    # No natural model-bound queryset (scope comes from the path's slug, not a
    # URL object keyed by pk) -- set purely to silence drf-spectacular's
    # "Failed to obtain model through view's queryset" schema-generation warning.
    queryset = CalendarGroup.objects.none()

    @extend_schema(
        responses={200: BookableSlotProposalSerializer(many=True)},
        parameters=[
            _SEARCH_WINDOW_START_PARAMETER,
            _SEARCH_WINDOW_END_PARAMETER,
            _SLOT_STEP_SECONDS_PARAMETER,
        ],
        summary="Bookable slot proposals for a public calendar group, no code required",
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        calendar_group_service = self.calendar_group_service
        if calendar_group_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_group_service not configured; check the DI container."
            )

        search_window_start = _parse_datetime_query_param(request, "search_window_start")
        search_window_end = _parse_datetime_query_param(request, "search_window_end")
        # No code sits behind this range check -- there is nothing here for a
        # timing/status oracle to probe -- but the check stays ahead of slug
        # resolution anyway, for the same reason every other read in this
        # module orders it first: a malformed/backwards/too-large range is
        # always a plain 400, never entangled with anything else this action
        # does.
        validate_code_gated_range(search_window_start, search_window_end)

        group = _resolve_public_group(kwargs["public_slug"])
        duration = _resolve_public_group_duration(group)
        slot_step = _resolve_slot_step(request)

        calendar_group_service.initialize(organization=group.organization)
        proposals = calendar_group_service.find_bookable_slots(
            group_id=group.id,
            search_window_start=search_window_start,
            search_window_end=search_window_end,
            duration=duration,
            slot_step=slot_step,
        )

        payload = [{"start_time": p.start_time, "end_time": p.end_time} for p in proposals]
        return Response(BookableSlotProposalSerializer(payload, many=True).data)


@extend_schema(tags=["Booking Codes"])
class PublicCalendarGroupAvailabilityViewSet(BookingCodeViewMixin, GenericViewSet):
    """Unauthenticated, codeless per-range availability for a public calendar group.

    ``POST /public/booking/calendar-groups/<public_slug>/availability/`` is
    the codeless counterpart of ``BookingCodeCalendarGroupAvailabilityViewSet``
    above. ``POST`` for the same reason as that endpoint: it takes a list of
    ranges. The group comes from the path's ``public_slug``; see
    ``_resolve_public_group`` for the 404/403 contract that gates it.

    ``available_calendar_ids`` is retained per slot even though this
    endpoint is otherwise group-aggregated -- group booking's
    ``slot_selections`` genuinely needs those ids to build a valid create
    request against the Phase 3 codeless write. See the "Codeless discovery
    is group-aggregated" Guiding Decision, which calls this retention out
    explicitly.
    """

    serializer_class = CalendarGroupAvailabilityQuerySerializer
    # No natural model-bound queryset (scope comes from the path's slug, not a
    # URL object keyed by pk) -- set purely to silence drf-spectacular's
    # "Failed to obtain model through view's queryset" schema-generation warning.
    queryset = CalendarGroup.objects.none()

    @extend_schema(
        request=CalendarGroupAvailabilityQuerySerializer,
        responses={200: CalendarGroupRangeAvailabilitySerializer(many=True)},
        summary="Per-range availability for a public calendar group, no code required",
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        calendar_group_service = self.calendar_group_service
        if calendar_group_service is None:
            raise CalendarServiceNotInjectedError(
                "calendar_group_service not configured; check the DI container."
            )

        # --- structural + range validation before slug resolution -- same
        # ordering discipline as every other read in this module. Every
        # range is checked, matching
        # BookingCodeCalendarGroupAvailabilityViewSet.create above. ---
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ranges = [(r["start_time"], r["end_time"]) for r in serializer.validated_data["ranges"]]
        for start_time, end_time in ranges:
            validate_code_gated_range(start_time, end_time)

        group = _resolve_public_group(kwargs["public_slug"])
        # check_group_availability does not itself consume a duration value,
        # but a group the write side would refuse regardless of the
        # requested span should not report itself as available either -- see
        # _resolve_public_group_duration's own docstring for why this gate
        # applies uniformly to both endpoints in this section.
        _resolve_public_group_duration(group)

        calendar_group_service.initialize(organization=group.organization)
        result = calendar_group_service.check_group_availability(group_id=group.id, ranges=ranges)

        payload = [
            {
                "start_time": r.start_time,
                "end_time": r.end_time,
                "slots": [
                    {
                        "slot_id": s.slot_id,
                        "available_calendar_ids": s.available_calendar_ids,
                        "required_count": s.required_count,
                    }
                    for s in r.slots
                ],
            }
            for r in result
        ]
        return Response(CalendarGroupRangeAvailabilitySerializer(payload, many=True).data)
