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

**Pinned duration** (the two bookable-slots endpoints only): ``duration_seconds``
presence is required regardless of pin state -- a request that omits it
entirely is a ``400`` whether or not the resolved token pins a duration, so
that status alone cannot disclose pin state. Once present, a token that
carries a ``duration`` uses it UNCONDITIONALLY: a wrong or malformed (but
non-empty) ``duration_seconds`` on a pinned code still produces the exact
same response as the correct one. See ``_resolve_duration``.
"""

import datetime

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from calendar_integration.booking_auth import (
    BOOKING_CODE_HEADER,
    resolve_booking_code_opaquely,
    validate_code_gated_range,
)
from calendar_integration.booking_exceptions import OpaqueCodeError
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


def _resolve_duration(token: CalendarManagementToken, request: Request) -> datetime.timedelta:
    """Resolve the search duration for a bookable-slots read.

    ``duration_seconds`` presence is validated identically regardless of pin
    state -- a request that omits it is always a ``400``, so that status
    alone never discloses whether the resolved token pins a duration. Only
    once presence is established does the pin take over: when
    ``token.duration`` is set, it is returned UNCONDITIONALLY and the
    parameter's parsed VALUE is never even inspected, so a wrong or
    malformed (but non-empty) value on a pinned code still produces
    byte-identical output to the correct one (see the "Duration pinning --
    reads" Guiding Decision). Only when the token pins nothing does
    ``duration_seconds`` also have to be a valid positive integer.
    """
    raw = request.query_params.get("duration_seconds")
    if not raw:
        raise ValidationError({"non_field_errors": ["duration_seconds is required."]})

    if token.duration is not None:
        return token.duration

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
    "(a request omitting it is a 400 whether or not the code pins a duration -- "
    "presence alone must never disclose pin state). When the resolved booking code "
    "pins a duration, the pin silently overrides this parameter's VALUE: a "
    "mismatched or malformed value produces the exact same response as the pinned "
    "value, so this endpoint cannot be used to probe whether a code is pinned or "
    "what the pin is. When the code pins no duration, the value must also be a "
    "valid positive integer.",
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

        # --- pinned-duration silent override -- see module docstring / _resolve_duration. ---
        duration = _resolve_duration(token, request)
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
        duration = _resolve_duration(token, request)
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
