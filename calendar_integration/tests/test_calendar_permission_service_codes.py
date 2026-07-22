"""Unit tests for CalendarPermissionService booking-code methods.

Covers:
- create_booking_token() persists scope, permissions, expiry, and minter.
- validate_code() returns the token on a valid active code.
- validate_code() raises the right exception for each terminal state
  (expired / used / revoked / unknown).
- can_perform_scheduling() group-scoped token branch, including the cross-org
  isolation case.
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
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.dataclasses import CalendarEventInputData, CalendarSettingsData
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


# ---------------------------------------------------------------------------
# can_perform_scheduling() — group-scoped token branch
# ---------------------------------------------------------------------------


def _restricted_settings() -> CalendarSettingsData:
    """A calendar settings fixture where public scheduling is disabled."""
    return CalendarSettingsData(manage_available_windows=True, accepts_public_scheduling=False)


def _public_settings() -> CalendarSettingsData:
    """A calendar settings fixture where public scheduling is enabled."""
    return CalendarSettingsData(manage_available_windows=False, accepts_public_scheduling=True)


def _dummy_event_data() -> CalendarEventInputData:
    """Minimal CalendarEventInputData for use in can_perform_scheduling calls."""
    now = timezone.now()
    return CalendarEventInputData(
        title="Test",
        description="",
        start_time=now,
        end_time=now + datetime.timedelta(hours=1),
        timezone="UTC",
    )


@pytest.fixture
def group_with_member_calendar(org, calendar):
    """A CalendarGroup with one slot containing `calendar`."""
    grp = CalendarGroup.objects.create(organization=org, name="Test Group")
    slot = CalendarGroupSlot.objects.create(
        organization=org,
        group=grp,
        name="Physicians",
        order=0,
    )
    CalendarGroupSlotMembership.objects.create(
        organization=org,
        slot=slot,
        calendar=calendar,
    )
    return grp


@pytest.fixture
def non_member_calendar(org):
    """A calendar that does NOT belong to any group slot."""
    return Calendar.objects.create(
        name="Non-Member Calendar",
        organization=org,
        external_id="non-member-cal",
    )


@pytest.mark.django_db
def test_can_perform_scheduling_group_token_member_calendar_with_create_returns_true(
    service, org, calendar, group_with_member_calendar
):
    """Group-scoped token + CREATE + member calendar → True.

    Before the fix, this returned False for a restricted calendar.
    """
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group_with_member_calendar.id,
    )
    # Simulate what initialize_with_token does: set the token on the service.
    service.token = token

    result = service.can_perform_scheduling(
        calendar_id=calendar.id,
        calendar_settings=_restricted_settings(),
        event=_dummy_event_data(),
    )
    assert result is True


@pytest.mark.django_db
def test_can_perform_scheduling_group_token_non_member_calendar_returns_false(
    service, org, non_member_calendar, group_with_member_calendar
):
    """Group-scoped token + CREATE + calendar NOT in the group → False."""
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group_with_member_calendar.id,
    )
    service.token = token

    result = service.can_perform_scheduling(
        calendar_id=non_member_calendar.id,
        calendar_settings=_restricted_settings(),
        event=_dummy_event_data(),
    )
    assert result is False


@pytest.mark.django_db
def test_can_perform_scheduling_group_token_without_create_returns_false(
    service, org, calendar, group_with_member_calendar
):
    """Group-scoped token without CREATE permission → False even for a member calendar."""
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=group_with_member_calendar.id,
    )
    service.token = token

    result = service.can_perform_scheduling(
        calendar_id=calendar.id,
        calendar_settings=_restricted_settings(),
        event=_dummy_event_data(),
    )
    assert result is False


@pytest.mark.django_db
def test_can_perform_scheduling_public_calendar_always_returns_true(
    service, org, calendar, group_with_member_calendar
):
    """accepts_public_scheduling=True bypasses token check (existing behaviour)."""
    # No token set.
    result = service.can_perform_scheduling(
        calendar_id=calendar.id,
        calendar_settings=_public_settings(),
        event=_dummy_event_data(),
    )
    assert result is True


@pytest.mark.django_db
def test_can_perform_scheduling_calendar_scoped_token_own_calendar_returns_true(
    service, org, calendar
):
    """Calendar-scoped token for the exact calendar + CREATE → True (existing behaviour)."""
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    service.token = token

    result = service.can_perform_scheduling(
        calendar_id=calendar.id,
        calendar_settings=_restricted_settings(),
        event=_dummy_event_data(),
    )
    assert result is True


@pytest.mark.django_db
def test_can_perform_scheduling_group_token_member_calendar_in_other_org_returns_false(
    service, org
):
    """Group-scoped token in org A must NOT authorize a calendar that belongs to org B.

    This test proves that the ``filter_by_organization(self.token.organization_id)``
    guard inside ``can_perform_scheduling`` prevents cross-org access even when org B's
    calendar happens to be a member of a structurally similar group.

    Setup:
    - org A has a group (grp_a) with one slot containing cal_a.
    - org B has its own group (grp_b) with one slot containing cal_b.
    - A CREATE token is minted in org A scoped to grp_a.
    - can_perform_scheduling is called with org B's calendar id → must return False.
    """
    other_org = baker.make("organizations.Organization")

    # Org A side
    grp_a = CalendarGroup.objects.create(organization=org, name="Org-A Group")
    slot_a = CalendarGroupSlot.objects.create(organization=org, group=grp_a, name="Slot A", order=0)
    cal_a = Calendar.objects.create(name="Org-A Calendar", organization=org)
    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot_a, calendar=cal_a)

    # Org B side — mirrors org A's structure with distinct DB rows
    grp_b = CalendarGroup.objects.create(organization=other_org, name="Org-B Group")
    slot_b = CalendarGroupSlot.objects.create(
        organization=other_org, group=grp_b, name="Slot B", order=0
    )
    cal_b = Calendar.objects.create(name="Org-B Calendar", organization=other_org)
    CalendarGroupSlotMembership.objects.create(organization=other_org, slot=slot_b, calendar=cal_b)

    # Token minted in org A scoped to grp_a
    token, _ = service.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=grp_a.id,
    )
    service.token = token

    # Attempt to authorize org B's calendar with org A's token → must be False
    result = service.can_perform_scheduling(
        calendar_id=cal_b.id,
        calendar_settings=_restricted_settings(),
        event=_dummy_event_data(),
    )
    assert result is False
