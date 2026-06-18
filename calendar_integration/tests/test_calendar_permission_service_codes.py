"""Unit tests for CalendarPermissionService booking-code methods (Phase 0).

Covers:
- create_booking_token() persists scope, permissions, expiry, and minter.
- validate_code() returns the token on a valid active code.
- validate_code() raises the right exception for each terminal state
  (expired / used / revoked / unknown).
"""

import base64
import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import EventManagementPermissions
from calendar_integration.exceptions import (
    InvalidTokenError,
    NoPermissionsSpecifiedError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization
from public_api.models import SystemUser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org(db) -> Organization:
    return baker.make("organizations.Organization")


@pytest.fixture
def calendar(org) -> Calendar:
    return Calendar.objects.create(name="Test Calendar", organization=org)


@pytest.fixture
def calendar_group(org) -> CalendarGroup:
    return CalendarGroup.objects.create(name="Test Group", organization=org)


@pytest.fixture
def event(org, calendar) -> CalendarEvent:
    now = timezone.now()
    return CalendarEvent.objects.create(
        organization=org,
        calendar_fk=calendar,
        title="Test Event",
        start_time_tz_unaware=now,
        end_time_tz_unaware=now + datetime.timedelta(hours=1),
        timezone="UTC",
    )


@pytest.fixture
def system_user(org) -> SystemUser:
    return SystemUser.objects.create(
        organization=org,
        integration_name="test-integration",
        long_lived_token_hash="deadbeef",
    )


@pytest.fixture
def service() -> CalendarPermissionService:
    return CalendarPermissionService()


# ---------------------------------------------------------------------------
# create_booking_token()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_booking_token_calendar_scope(service, org, calendar):
    """create_booking_token scoped to a calendar persists the FK and permission."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    assert token.pk is not None
    assert token.organization_id == org.id
    assert token.calendar_fk_id == calendar.id
    assert token.event_fk_id is None
    assert token.calendar_group_fk_id is None
    assert code  # non-empty string
    # Permissions persisted
    perms = list(token.permissions.values_list("permission", flat=True))
    assert EventManagementPermissions.CREATE in perms


@pytest.mark.django_db
def test_create_booking_token_calendar_group_scope(service, org, calendar_group):
    """create_booking_token scoped to a calendar_group persists the FK."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=calendar_group.id,
    )

    assert token.calendar_group_fk_id == calendar_group.id
    assert token.calendar_fk_id is None
    assert code


@pytest.mark.django_db
def test_create_booking_token_event_scope(service, org, calendar, event):
    """create_booking_token scoped to an event (reschedule/cancel) persists both FKs."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=event.id,
    )

    assert token.event_fk_id == event.id
    assert token.calendar_fk_id == calendar.id
    assert code


@pytest.mark.django_db
def test_create_booking_token_with_expiry(service, org, calendar):
    """create_booking_token stores the provided expires_at."""
    future = timezone.now() + datetime.timedelta(days=7)
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
        expires_at=future,
    )

    token.refresh_from_db()
    assert token.expires_at is not None
    # Allow a small tolerance for rounding.
    assert abs((token.expires_at - future).total_seconds()) < 1


@pytest.mark.django_db
def test_create_booking_token_with_minter(service, org, calendar, system_user):
    """create_booking_token stores the minted_by_system_user FK."""
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
        minted_by=system_user,
    )

    token.refresh_from_db()
    assert token.minted_by_system_user_id == system_user.id


@pytest.mark.django_db
def test_create_booking_token_no_permissions_raises(service, org, calendar):
    """create_booking_token raises NoPermissionsSpecifiedError for empty perms."""
    with pytest.raises(NoPermissionsSpecifiedError):
        service.create_booking_token(
            organization_id=org.id,
            permissions=[],
            calendar_id=calendar.id,
        )


@pytest.mark.django_db
def test_create_booking_token_code_is_decodable(service, org, calendar):
    """The plaintext code must be base64-decodable and contain <id>:<raw>."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    decoded = base64.b64decode(code).decode("utf-8")
    parts = decoded.split(":")
    assert len(parts) == 2
    assert int(parts[0]) == token.pk


@pytest.mark.django_db
def test_create_booking_token_multiple_permissions(service, org, calendar):
    """create_booking_token stores all provided permissions."""
    perms = [EventManagementPermissions.RESCHEDULE, EventManagementPermissions.CANCEL]
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=perms,
        calendar_id=calendar.id,
    )

    stored = set(
        CalendarManagementTokenPermission.objects.filter(
            organization_id=org.id,
            token_fk=token,
        ).values_list("permission", flat=True)
    )
    assert EventManagementPermissions.RESCHEDULE in stored
    assert EventManagementPermissions.CANCEL in stored


# ---------------------------------------------------------------------------
# validate_code()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_validate_code_returns_active_token(service, org, calendar):
    """validate_code returns the token for a fresh, valid code."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    result = service.validate_code(code, org.id)
    assert result.pk == token.pk


@pytest.mark.django_db
def test_validate_code_raises_invalid_for_bad_format(service, org):
    """validate_code raises InvalidTokenError for a garbage code string."""
    with pytest.raises(InvalidTokenError):
        service.validate_code("not-a-valid-base64-token", org.id)


@pytest.mark.django_db
def test_validate_code_raises_invalid_for_wrong_org(service, org, calendar):
    """validate_code raises InvalidTokenError when org_id does not match."""
    _, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    other_org = baker.make("organizations.Organization")
    with pytest.raises(InvalidTokenError):
        service.validate_code(code, other_org.id)


@pytest.mark.django_db
def test_validate_code_raises_invalid_for_unknown_id(service, org):
    """validate_code raises InvalidTokenError when the token id does not exist."""
    # Manufacture a code whose id does not exist in the DB.
    fake_code = base64.b64encode(b"999999:sometoken").decode()
    with pytest.raises(InvalidTokenError):
        service.validate_code(fake_code, org.id)


@pytest.mark.django_db
def test_validate_code_raises_expired(service, org, calendar):
    """validate_code raises TokenExpiredError for a past-expiry token."""
    past = timezone.now() - datetime.timedelta(hours=1)
    _token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
        expires_at=past,
    )

    with pytest.raises(TokenExpiredError):
        service.validate_code(code, org.id)


@pytest.mark.django_db
def test_validate_code_raises_already_used(service, org, calendar):
    """validate_code raises TokenAlreadyUsedError for a consumed token."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    # Mark as used.
    CalendarManagementToken.objects.filter(pk=token.pk).update(used_at=timezone.now())

    with pytest.raises(TokenAlreadyUsedError):
        service.validate_code(code, org.id)


@pytest.mark.django_db
def test_validate_code_raises_revoked(service, org, calendar):
    """validate_code raises TokenRevokedError for a revoked token."""
    token, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )

    # Revoke the token.
    CalendarManagementToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())

    with pytest.raises(TokenRevokedError):
        service.validate_code(code, org.id)


@pytest.mark.django_db
def test_validate_code_future_expiry_is_valid(service, org, calendar):
    """validate_code accepts a token whose expires_at is in the future."""
    future = timezone.now() + datetime.timedelta(hours=48)
    _, code = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
        expires_at=future,
    )

    result = service.validate_code(code, org.id)
    assert result is not None
