"""Exception vocabulary for the unauthenticated booking-code REST surface.

Two distinct error contracts, per the plan's "Error contract (writes)" /
"Error contract (reads)" Guiding Decisions:

- **Writes** (Phases 1-4) render a real HTTP status plus
  ``{"error_code": ..., "detail": ...}``, reusing the same
  :class:`~calendar_integration.graphql.BookingCodeErrorCode` values GraphQL
  already returns so a client can branch identically on either surface.
  :class:`BookingCodeAPIException` is the shared base; one subclass per
  ``BookingCodeErrorCode`` member carries its status code from the plan's
  **API Design** status map. DRF's default exception handler renders a dict
  ``exc.detail`` verbatim (``data = exc.detail`` when it is a ``dict``), so
  ``common/exception_handlers.py`` needs **no** change for these -- that
  module's own docstring is explicit that adding a case there can alter
  transactional semantics, which nothing here needs.
- **Reads** (Phase 5) collapse every code failure -- invalid, expired, used,
  revoked, or wrong-scope -- into one indistinguishable
  ``403 {"detail": "Invalid or expired code."}`` via :class:`OpaqueCodeError`.
  Discriminating error codes there would turn the reads into a free oracle
  for probing a code's state; see the "Error contract (reads)" row.

:class:`BookingCodeRangeError` is the shared ``400`` for the range-validation
guard (``validate_code_gated_range`` in ``booking_auth.py``), which every
code-gated read runs **before** resolving the code at all -- see the "Range
validation ordering" Guiding Decision.
"""

from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied

from calendar_integration.graphql import BookingCodeErrorCode


class BookingCodeAPIException(APIException):
    """Base for booking-code write failures.

    Subclasses set ``status_code`` (a real HTTP status, per the plan's status
    map) and ``error_code`` (a :class:`BookingCodeErrorCode` member).
    ``detail`` always renders as ``{"error_code": <value>, "detail": <message>}``
    -- a ``dict``, which DRF's exception handler passes through unmodified.
    """

    error_code: BookingCodeErrorCode
    default_detail = "This booking code cannot be used."

    def __init__(self, detail: str | None = None) -> None:
        message = detail if detail is not None else self.default_detail
        super().__init__(detail={"error_code": self.error_code.value, "detail": message})


class InvalidCodeAPIException(BookingCodeAPIException):
    """``404`` -- unknown, malformed, or wrong-organization code."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = BookingCodeErrorCode.INVALID_CODE
    default_detail = "Invalid or unknown booking code."


class NotPermittedAPIException(BookingCodeAPIException):
    """``403`` -- the code is live but lacks the permission, or is scoped wrong."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = BookingCodeErrorCode.NOT_PERMITTED
    default_detail = "This code does not permit this operation."


class RevokedCodeAPIException(BookingCodeAPIException):
    """``403`` -- explicitly revoked by the minting organization."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = BookingCodeErrorCode.REVOKED
    default_detail = "This booking code has been revoked."


class ExpiredCodeAPIException(BookingCodeAPIException):
    """``410`` -- ``expires_at`` has passed."""

    status_code = status.HTTP_410_GONE
    error_code = BookingCodeErrorCode.EXPIRED
    default_detail = "This booking code has expired."


class AlreadyUsedCodeAPIException(BookingCodeAPIException):
    """``409`` -- consumed by a prior successful write."""

    status_code = status.HTTP_409_CONFLICT
    error_code = BookingCodeErrorCode.ALREADY_USED
    default_detail = "This booking code has already been used."


class SlotUnavailableAPIException(BookingCodeAPIException):
    """``409`` -- slot taken or policy-violating; the code is NOT consumed."""

    status_code = status.HTTP_409_CONFLICT
    error_code = BookingCodeErrorCode.SLOT_UNAVAILABLE
    default_detail = "The requested time slot is not available."


class OpaqueCodeError(PermissionDenied):
    """Uniform ``403`` for every code-gated *read* failure.

    Deliberately discloses nothing about which failure occurred -- invalid,
    expired, used, revoked, and wrong-scope all raise this same exception
    with the same message, so a client (or an attacker) cannot distinguish
    them by response shape. See the "Error contract (reads)" Guiding
    Decision.
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Invalid or expired code."
    default_code = "invalid_or_expired_code"


class BookingCodeRangeError(APIException):
    """``400`` for a code-gated read's range-validation failure.

    Raised by ``validate_code_gated_range`` in ``booking_auth.py``, which
    every code-gated read calls BEFORE resolving the code -- a bad range must
    be rejectable without a valid code, or response timing/status become a
    second oracle for probing code state (see the "Range validation
    ordering" Guiding Decision). Deliberately a plain ``APIException`` (not
    DRF's ``ValidationError``, which wraps a string ``detail`` in a list) so
    the body matches the plan's exact shape: ``{"detail": "<message>"}``.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Invalid time range."
