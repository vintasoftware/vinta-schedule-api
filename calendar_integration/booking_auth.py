"""Shared code-resolution helpers for the unauthenticated booking-code REST surface.

Booking codes travel as the ``X-Booking-Code`` request header on every
``public/booking/`` endpoint (reads and writes alike) -- see the "Code
transport" Guiding Decision. This module owns:

- Header extraction and code resolution, in two flavours: discriminated
  (``resolve_booking_code_from_request``, for writes) and opaque
  (``resolve_booking_code_opaquely``, for reads).
- ``client_ip_from_request`` for consume auditing, mirroring
  ``calendar_integration.mutations._client_ip_from_request``.
- ``MAX_CODE_GATED_RANGE``, the one home for the window bound --
  ``public_api.queries`` imports the constant from here rather than
  declaring its own literal, so REST and GraphQL cannot independently drift
  on it. ``validate_code_gated_range`` applies that shared bound for the
  REST surface; GraphQL's own ``_validate_code_gated_range`` applies the
  same imported constant independently.
- ``pinned_duration_error``, a view-layer helper that produces a message
  naming a CalendarGroup's pinned duration. It exists purely for that
  message: the authorization guarantee itself lives in
  ``CalendarPermissionService.can_perform_group_scheduling`` (and
  ``can_perform_update`` for a grouped reschedule), which every surface that
  honours group booking already calls. This helper must never drift from
  that check -- it re-reads the same ``group.duration`` / span comparison,
  not a separate rule. History note: an earlier draft of this design pinned
  duration on ``CalendarManagementToken`` instead, and this helper took a
  token; duration pinning now lives on ``CalendarGroup`` (see that field's
  help_text), so single-calendar codes carry no pin at all and this helper
  takes a group instead of a token.
"""

import datetime

from django.http import HttpRequest

from rest_framework.request import Request

from calendar_integration.booking_exceptions import (
    AlreadyUsedCodeAPIException,
    BookingCodeRangeError,
    ExpiredCodeAPIException,
    InvalidCodeAPIException,
    NotPermittedAPIException,
    OpaqueCodeError,
    RevokedCodeAPIException,
)
from calendar_integration.exceptions import (
    InvalidTokenError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.models import CalendarGroup, CalendarManagementToken
from calendar_integration.services.calendar_permission_service import CalendarPermissionService


BOOKING_CODE_HEADER = "X-Booking-Code"

# The one home for this bound -- ``public_api.queries`` imports it from here
# rather than declaring its own literal, so REST and GraphQL cannot drift
# apart on the window size.
MAX_CODE_GATED_RANGE = datetime.timedelta(days=366)


def booking_code_header(request: HttpRequest | Request) -> str | None:
    """Return the ``X-Booking-Code`` header value, or ``None`` when absent/empty.

    Absence means "codeless" -- see the "Code transport" Guiding Decision.
    Callers that require a code (every write except the Phase 3 codeless
    group-booking branch, and every read) reject ``None`` explicitly; callers
    that support a codeless path branch on it instead.
    """
    value = request.headers.get(BOOKING_CODE_HEADER)
    return value or None


def resolve_booking_code_from_request(
    request: HttpRequest | Request, permission_service: CalendarPermissionService
) -> CalendarManagementToken:
    """Resolve ``X-Booking-Code`` for a write endpoint, with discriminated errors.

    Raises the matching :class:`~calendar_integration.booking_exceptions.BookingCodeAPIException`
    subclass -- a distinct HTTP status and ``error_code`` per failure kind, per
    the plan's "Error contract (writes)" Guiding Decision.
    """
    code = booking_code_header(request)
    if code is None:
        raise InvalidCodeAPIException("Missing X-Booking-Code header.")

    try:
        return permission_service.resolve_code(code)
    except InvalidTokenError:
        raise InvalidCodeAPIException() from None
    except TokenExpiredError:
        raise ExpiredCodeAPIException() from None
    except TokenAlreadyUsedError:
        raise AlreadyUsedCodeAPIException() from None
    except TokenRevokedError:
        raise RevokedCodeAPIException() from None


def resolve_booking_code_opaquely(
    request: HttpRequest | Request, permission_service: CalendarPermissionService
) -> CalendarManagementToken:
    """Resolve ``X-Booking-Code`` for a read endpoint, with one uniform error.

    Every failure -- missing header, invalid, expired, used, revoked -- raises
    the same :class:`~calendar_integration.booking_exceptions.OpaqueCodeError`,
    per the plan's "Error contract (reads)" Guiding Decision. Wrong-scope
    (a code that resolves to neither a calendar nor a group the endpoint
    accepts) is NOT handled here -- callers raise ``OpaqueCodeError`` themselves
    once they inspect the resolved token's scope.
    """
    code = booking_code_header(request)
    if code is None:
        raise OpaqueCodeError() from None

    try:
        return permission_service.resolve_code(code)
    except (InvalidTokenError, TokenExpiredError, TokenAlreadyUsedError, TokenRevokedError):
        raise OpaqueCodeError() from None


def client_ip_from_request(request: HttpRequest | Request) -> str:
    """Extract the client IP address from a request for consume auditing.

    Mirrors ``calendar_integration.mutations._client_ip_from_request``.
    Prefers the first entry of ``X-Forwarded-For`` (set by load balancers /
    proxies); falls back to ``REMOTE_ADDR``.
    """
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def validate_code_gated_range(start: datetime.datetime, end: datetime.datetime) -> None:
    """Validate a client-supplied datetime range for a code-gated read.

    Raises :class:`~calendar_integration.booking_exceptions.BookingCodeRangeError`
    (``400``) if the range is backwards or exceeds ``MAX_CODE_GATED_RANGE``.
    Callers MUST call this BEFORE resolving the code -- see the "Range
    validation ordering" Guiding Decision: a client must be able to get a
    ``400`` for a bad range without presenting a valid code, or response
    status becomes a second oracle for probing code state. Mirrors
    ``public_api.queries._validate_code_gated_range``.
    """
    if end <= start:
        raise BookingCodeRangeError("Invalid time range.")
    if (end - start) > MAX_CODE_GATED_RANGE:
        raise BookingCodeRangeError("Requested time range is too large.")


def pinned_duration_error(
    group: CalendarGroup | None,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
) -> NotPermittedAPIException | None:
    """Return a ``403 NOT_PERMITTED`` naming a CalendarGroup's pinned duration, or ``None``.

    ``None`` means either there is no group to check (single-calendar
    booking -- no ``Calendar.duration`` exists, so nothing to name), the
    group pins no duration (``group.duration is None``), or the requested
    span matches it exactly. This exists purely so the write endpoints can
    name the pinned duration in the error message -- the actual guarantee is
    enforced independently by
    ``CalendarPermissionService.can_perform_group_scheduling`` (and
    ``can_perform_update`` for a grouped reschedule), which every surface
    that honours group booking already calls. Callers MUST still rely on
    that service check; this helper is a better error message layered on
    top of it, not a substitute for it.

    Deliberately does NOT attempt to name the fail-closed case (a public
    group with no duration at all) -- there is no pinned duration to name
    there, only a misconfiguration; that case still returns ``None`` here
    and the caller falls through to the service check's generic
    ``NOT_PERMITTED``.
    """
    if group is None or group.duration is None:
        return None
    if (end_time - start_time) == group.duration:
        return None

    pinned_total_seconds = group.duration.total_seconds()
    if pinned_total_seconds % 60 == 0:
        pinned_minutes = int(pinned_total_seconds // 60)
        return NotPermittedAPIException(f"This code is fixed to a {pinned_minutes} minute booking.")

    pinned_seconds = int(pinned_total_seconds)
    return NotPermittedAPIException(f"This code is fixed to a {pinned_seconds} second booking.")
