"""Unit tests for ``calendar_integration.booking_auth`` and ``booking_exceptions``.

Covers:
- ``X-Booking-Code`` header extraction (present / absent / empty).
- Each service-layer terminal-state exception mapping to the matching
  ``BookingCodeAPIException`` subclass -- status code and ``error_code`` --
  for the write-side resolver.
- The opaque read-side resolver collapsing every failure kind (missing
  header, invalid, expired, used, revoked) into one indistinguishable
  ``OpaqueCodeError`` -- asserted byte-identical across every scenario.
- ``client_ip_from_request``.
- ``validate_code_gated_range`` rejecting a backwards range and a range over
  366 days, and accepting a valid one.
- ``pinned_duration_error`` -- ``None`` when there is no group, an unpinned
  group, or a matching span; a ``NotPermittedAPIException`` naming the
  pinned duration otherwise. Duration pinning lives on ``CalendarGroup``,
  not ``CalendarManagementToken`` -- see that field's help_text.
- ``CalendarPermissionService.resolve_code`` (the real service, not a mock)
  rejecting a malformed token id (non-numeric, oversized) as the same
  ``InvalidTokenError`` a well-formed-but-unknown code raises -- neither may
  leak as an uncaught 500, which would be a distinguishable oracle.
"""

import base64
import datetime

from django.test import RequestFactory

import pytest
from model_bakery import baker

from calendar_integration.booking_auth import (
    BOOKING_CODE_HEADER,
    booking_code_header,
    client_ip_from_request,
    pinned_duration_error,
    resolve_booking_code_from_request,
    resolve_booking_code_opaquely,
    validate_code_gated_range,
)
from calendar_integration.booking_exceptions import (
    AlreadyUsedCodeAPIException,
    BookingCodeRangeError,
    ExpiredCodeAPIException,
    InvalidCodeAPIException,
    OpaqueCodeError,
    RevokedCodeAPIException,
)
from calendar_integration.exceptions import (
    InvalidTokenError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.models import CalendarGroup
from calendar_integration.services.calendar_permission_service import CalendarPermissionService


# ---------------------------------------------------------------------------
# booking_code_header
# ---------------------------------------------------------------------------


def test_booking_code_header_present():
    request = RequestFactory().get("/", headers={"x-booking-code": "abc123"})
    assert booking_code_header(request) == "abc123"


def test_booking_code_header_absent():
    request = RequestFactory().get("/")
    assert booking_code_header(request) is None


def test_booking_code_header_empty():
    request = RequestFactory().get("/", headers={"x-booking-code": ""})
    assert booking_code_header(request) is None


# ---------------------------------------------------------------------------
# resolve_booking_code_from_request — discriminated errors
# ---------------------------------------------------------------------------


class _FakePermissionService:
    """Duck-typed stand-in for CalendarPermissionService.resolve_code."""

    def __init__(self, outcome):
        self._outcome = outcome

    def resolve_code(self, code):  # noqa: ARG002
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _request_with_code(code: str | None):
    if code is None:
        return RequestFactory().get("/")
    return RequestFactory().get("/", headers={"x-booking-code": code})


def test_resolve_booking_code_from_request_missing_header_raises_invalid():
    request = _request_with_code(None)
    with pytest.raises(InvalidCodeAPIException) as exc_info:
        resolve_booking_code_from_request(request, _FakePermissionService("unreachable"))
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error_code"] == "INVALID_CODE"


def test_resolve_booking_code_from_request_success_returns_token():
    sentinel = object()
    request = _request_with_code("some-code")
    result = resolve_booking_code_from_request(request, _FakePermissionService(sentinel))
    assert result is sentinel


@pytest.mark.parametrize(
    ("service_exception", "expected_exception", "expected_status", "expected_error_code"),
    [
        (InvalidTokenError(), InvalidCodeAPIException, 404, "INVALID_CODE"),
        (TokenExpiredError(), ExpiredCodeAPIException, 410, "EXPIRED"),
        (TokenAlreadyUsedError(), AlreadyUsedCodeAPIException, 409, "ALREADY_USED"),
        (TokenRevokedError(), RevokedCodeAPIException, 403, "REVOKED"),
    ],
)
def test_resolve_booking_code_from_request_maps_each_service_exception(
    service_exception, expected_exception, expected_status, expected_error_code
):
    request = _request_with_code("some-code")
    with pytest.raises(expected_exception) as exc_info:
        resolve_booking_code_from_request(request, _FakePermissionService(service_exception))
    assert exc_info.value.status_code == expected_status
    assert exc_info.value.detail["error_code"] == expected_error_code


# ---------------------------------------------------------------------------
# resolve_booking_code_opaquely — one indistinguishable 403 for every failure
# ---------------------------------------------------------------------------


def test_resolve_booking_code_opaquely_success_returns_token():
    sentinel = object()
    request = _request_with_code("some-code")
    result = resolve_booking_code_opaquely(request, _FakePermissionService(sentinel))
    assert result is sentinel


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_header",
        "invalid",
        "expired",
        "used",
        "revoked",
    ],
)
def test_resolve_booking_code_opaquely_every_failure_is_byte_identical(scenario):
    """Every failure kind -- including a missing header -- raises the exact same
    OpaqueCodeError body. This is the security property the read design exists
    for: a client (or attacker) cannot distinguish failure kinds by response
    shape."""
    if scenario == "missing_header":
        request = _request_with_code(None)
        service = _FakePermissionService("unreachable")
    else:
        exceptions_by_scenario = {
            "invalid": InvalidTokenError(),
            "expired": TokenExpiredError(),
            "used": TokenAlreadyUsedError(),
            "revoked": TokenRevokedError(),
        }
        request = _request_with_code("some-code")
        service = _FakePermissionService(exceptions_by_scenario[scenario])

    with pytest.raises(OpaqueCodeError) as exc_info:
        resolve_booking_code_opaquely(request, service)

    assert exc_info.value.status_code == 403
    assert str(exc_info.value.detail) == "Invalid or expired code."


def test_resolve_booking_code_opaquely_bodies_are_identical_across_every_scenario():
    """Direct byte-identical comparison across all five failure scenarios."""
    bodies = []
    for request, service in [
        (_request_with_code(None), _FakePermissionService("unreachable")),
        (_request_with_code("x"), _FakePermissionService(InvalidTokenError())),
        (_request_with_code("x"), _FakePermissionService(TokenExpiredError())),
        (_request_with_code("x"), _FakePermissionService(TokenAlreadyUsedError())),
        (_request_with_code("x"), _FakePermissionService(TokenRevokedError())),
    ]:
        try:
            resolve_booking_code_opaquely(request, service)
        except OpaqueCodeError as exc:
            bodies.append((exc.status_code, str(exc.detail)))

    assert len(bodies) == 5
    assert len(set(bodies)) == 1


# ---------------------------------------------------------------------------
# resolve_code — malformed token id (real service, not a mock)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_resolve_code_non_numeric_token_id_raises_invalid_token_error():
    """A non-numeric token id must not leak an uncaught ValueError as a 500 --
    it has to join every other malformed/unknown code as InvalidTokenError."""
    code = base64.b64encode(b"x:y").decode()

    with pytest.raises(InvalidTokenError):
        CalendarPermissionService().resolve_code(code)


@pytest.mark.django_db
def test_resolve_code_oversized_token_id_raises_invalid_token_error():
    """An oversized integer token id must not leak a distinguishable DB-level
    error as a 500 -- it has to join every other malformed/unknown code as
    InvalidTokenError."""
    code = base64.b64encode(b"99999999999999999999:y").decode()

    with pytest.raises(InvalidTokenError):
        CalendarPermissionService().resolve_code(code)


@pytest.mark.django_db
def test_resolve_booking_code_opaquely_renders_same_error_for_malformed_and_unknown_codes():
    """Both malformed-token-id shapes must render the exact same OpaqueCodeError
    a well-formed-but-unknown code renders on the read path -- no oracle."""
    service = CalendarPermissionService()

    bodies = []
    for payload in (b"x:y", b"99999999999999999999:y", b"0:y"):
        request = _request_with_code(base64.b64encode(payload).decode())
        with pytest.raises(OpaqueCodeError) as exc_info:
            resolve_booking_code_opaquely(request, service)
        bodies.append((exc_info.value.status_code, str(exc_info.value.detail)))

    assert len(set(bodies)) == 1


# ---------------------------------------------------------------------------
# client_ip_from_request
# ---------------------------------------------------------------------------


def test_client_ip_from_request_prefers_forwarded_for():
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="203.0.113.5, 10.0.0.1", REMOTE_ADDR="10.0.0.2"
    )
    assert client_ip_from_request(request) == "203.0.113.5"


def test_client_ip_from_request_falls_back_to_remote_addr():
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.2")
    assert client_ip_from_request(request) == "10.0.0.2"


def test_client_ip_from_request_missing_both_headers_returns_empty_string():
    request = RequestFactory().get("/")
    request.META.pop("REMOTE_ADDR", None)
    assert client_ip_from_request(request) == ""


# ---------------------------------------------------------------------------
# validate_code_gated_range
# ---------------------------------------------------------------------------


def test_validate_code_gated_range_backwards_raises_400():
    start = datetime.datetime(2030, 1, 2, tzinfo=datetime.UTC)
    end = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(BookingCodeRangeError) as exc_info:
        validate_code_gated_range(start, end)
    assert exc_info.value.status_code == 400
    assert str(exc_info.value.detail) == "Invalid time range."


def test_validate_code_gated_range_equal_start_end_raises_400():
    same = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(BookingCodeRangeError):
        validate_code_gated_range(same, same)


def test_validate_code_gated_range_over_366_days_raises_400():
    start = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(days=367)
    with pytest.raises(BookingCodeRangeError) as exc_info:
        validate_code_gated_range(start, end)
    assert exc_info.value.status_code == 400
    assert str(exc_info.value.detail) == "Requested time range is too large."


def test_validate_code_gated_range_within_bound_does_not_raise():
    start = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(days=366)
    validate_code_gated_range(start, end)  # must not raise


# ---------------------------------------------------------------------------
# pinned_duration_error
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pinned_duration_error_none_when_no_group():
    """Single-calendar booking -- no group to check at all (there is no
    ``Calendar.duration``)."""
    start = datetime.datetime(2030, 1, 1, 10, 0, tzinfo=datetime.UTC)

    assert pinned_duration_error(None, start, start + datetime.timedelta(hours=5)) is None


@pytest.mark.django_db
def test_pinned_duration_error_none_when_group_unpinned():
    org = baker.make("organizations.Organization")
    group = baker.make(CalendarGroup, organization=org, duration=None)
    start = datetime.datetime(2030, 1, 1, 10, 0, tzinfo=datetime.UTC)

    assert pinned_duration_error(group, start, start + datetime.timedelta(hours=5)) is None


@pytest.mark.django_db
def test_pinned_duration_error_none_when_span_matches():
    org = baker.make("organizations.Organization")
    group = baker.make(CalendarGroup, organization=org, duration=datetime.timedelta(minutes=30))
    start = datetime.datetime(2030, 1, 1, 10, 0, tzinfo=datetime.UTC)

    result = pinned_duration_error(group, start, start + datetime.timedelta(minutes=30))
    assert result is None


@pytest.mark.django_db
def test_pinned_duration_error_names_pinned_duration_on_mismatch():
    org = baker.make("organizations.Organization")
    group = baker.make(CalendarGroup, organization=org, duration=datetime.timedelta(minutes=30))
    start = datetime.datetime(2030, 1, 1, 10, 0, tzinfo=datetime.UTC)

    result = pinned_duration_error(group, start, start + datetime.timedelta(minutes=45))
    assert result is not None
    assert result.status_code == 403
    assert result.detail["error_code"] == "NOT_PERMITTED"
    assert "30 minute" in result.detail["detail"]


@pytest.mark.django_db
def test_pinned_duration_error_renders_seconds_for_sub_minute_pin():
    """A sub-minute pin (e.g. 45 seconds) must not floor to '0 minute' -- Phase 6
    accepts any positive ``duration_seconds``, including sub-minute spans."""
    org = baker.make("organizations.Organization")
    group = baker.make(CalendarGroup, organization=org, duration=datetime.timedelta(seconds=45))
    start = datetime.datetime(2030, 1, 1, 10, 0, tzinfo=datetime.UTC)

    result = pinned_duration_error(group, start, start + datetime.timedelta(minutes=5))
    assert result is not None
    assert "45 second" in result.detail["detail"]
    assert "minute" not in result.detail["detail"]


@pytest.mark.django_db
def test_pinned_duration_error_renders_seconds_for_non_whole_minute_pin():
    """A 90-second pin must render '90 second', not floor/round to '1 minute'."""
    org = baker.make("organizations.Organization")
    group = baker.make(CalendarGroup, organization=org, duration=datetime.timedelta(seconds=90))
    start = datetime.datetime(2030, 1, 1, 10, 0, tzinfo=datetime.UTC)

    result = pinned_duration_error(group, start, start + datetime.timedelta(minutes=5))
    assert result is not None
    assert "90 second" in result.detail["detail"]


# ---------------------------------------------------------------------------
# Header constant sanity
# ---------------------------------------------------------------------------


def test_booking_code_header_constant():
    assert BOOKING_CODE_HEADER == "X-Booking-Code"
