"""Tests that codeless public appointment type booking checks AppointmentType.accepts_public_scheduling.

Tests cover:
- Public appointment type (accepts_public_scheduling=True): codeless appointment type booking succeeds.
- Private appointment type (default False): codeless appointment type booking is rejected (PermissionDenied).
- Private appointment type + valid appointment-type-scoped token: booking succeeds (existing token path intact).
- Backward-compat: single-Calendar and bundle booking behavior is unchanged.

Implementation notes:
  The appointment-type-level check lives in AppointmentTypeService.create_appointment_type_event via
  CalendarPermissionService.can_perform_appointment_type_scheduling.  The ``appointment_type_authorized=True``
  field on CalendarEventInputData bypasses the per-member-calendar
  accepts_public_scheduling check so private member calendars don't independently
  block an appointment type booking the appointment type itself permits.
"""

import base64
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.utils import timezone

import pytest

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    EventManagementPermissions,
)
from calendar_integration.exceptions import AppointmentTypeValidationError
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
    CalendarManagementToken,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AppointmentTypeEventInputData,
    AppointmentTypeSlotSelectionInputData,
    CalendarEventInputData,
    CalendarSettingsData,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Gate Test Org", should_sync_rooms=False)


@pytest.fixture
def calendar_service(organization):
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    return cs


@pytest.fixture
def internal_calendars(organization):
    """INTERNAL provider calendars; accepts_public_scheduling=False (private) by design.

    The primary calendar being private is intentional for these tests: we want to
    verify that the appointment-type-level authorization (appointment_type_authorized=True) bypasses
    the per-calendar gate for private member calendars when the appointment type itself is public.
    """
    calendars = {}
    for name, ext in (("Dr. A", "phys_a"), ("Room 1", "room_1")):
        calendars[ext] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=ext,
            provider=CalendarProvider.INTERNAL,
            calendar_type=(
                CalendarType.PERSONAL if ext.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
            accepts_public_scheduling=False,  # private member calendars
        )
    return calendars


def _make_available(calendar: Calendar, start, end) -> None:
    AvailableTime.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _make_appointment_type(
    organization,
    calendars,
    accepts_public_scheduling: bool,
    duration: timedelta | None = None,
) -> AppointmentType:
    """Create an AppointmentType with one slot covering all ``calendars``.

    Constructs the appointment type directly (not via service) to avoid the service gate
    firing during setup. ``duration`` is required (non-None) by callers that
    both request ``accepts_public_scheduling=True`` AND drive an actual
    booking through ``create_appointment_type_event`` with real times -- a public
    appointment type with ``duration=None`` fails closed in
    ``CalendarPermissionService.can_perform_appointment_type_scheduling`` (see that
    method's docstring), which would otherwise break these tests for a
    reason unrelated to what they're actually testing.
    """
    appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Test AppointmentType",
        accepts_public_scheduling=accepts_public_scheduling,
        duration=duration,
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Physicians",
        required_count=1,
        order=0,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=slot,
        calendar=calendars["phys_a"],
    )
    AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Rooms",
        required_count=1,
        order=1,
    )
    room_slot = appointment_type.slots.get(name="Rooms")
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=room_slot,
        calendar=calendars["room_1"],
    )
    return appointment_type


def _make_appointment_type_service(
    organization: Organization,
    calendar_service: CalendarService,
    permission_service: CalendarPermissionService | None = None,
) -> AppointmentTypeService:
    svc = AppointmentTypeService(
        calendar_service=calendar_service,
        calendar_permission_service=permission_service,
    )
    svc.initialize(organization=organization)
    return svc


def _build_event_input(
    appointment_type, internal_calendars, start, end
) -> AppointmentTypeEventInputData:
    physicians_slot = appointment_type.slots.get(name="Physicians")
    rooms_slot = appointment_type.slots.get(name="Rooms")
    return AppointmentTypeEventInputData(
        title="Test Booking",
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        appointment_type_id=appointment_type.id,
        slot_selections=[
            AppointmentTypeSlotSelectionInputData(
                slot_id=physicians_slot.id,
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
            AppointmentTypeSlotSelectionInputData(
                slot_id=rooms_slot.id,
                calendar_ids=[internal_calendars["room_1"].id],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Core appointment type authorization tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_public_appointment_type_with_private_member_calendars_codeless_booking_succeeds(
    organization, calendar_service, internal_calendars
):
    """Public appointment type (accepts_public_scheduling=True) with private member calendars:
    codeless booking must succeed.

    This is the key regression check: the appointment-type-level authorization
    (appointment_type_authorized=True) must bypass each member calendar's own private check.
    """
    appointment_type = _make_appointment_type(
        organization,
        internal_calendars,
        accepts_public_scheduling=True,
        duration=timedelta(hours=1),
    )

    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        _make_available(cal, start, end)

    # Explicit uninitialized permission service (no token) — codeless path.
    perm_svc = CalendarPermissionService()
    svc = _make_appointment_type_service(
        organization, calendar_service, permission_service=perm_svc
    )

    event = svc.create_appointment_type_event(
        _build_event_input(appointment_type, internal_calendars, start, end)
    )

    assert event is not None
    assert event.calendar_fk_id == internal_calendars["phys_a"].id
    assert event.appointment_type_fk_id == appointment_type.id


@pytest.mark.django_db
def test_private_appointment_type_codeless_booking_raises_permission_denied(
    organization, calendar_service, internal_calendars
):
    """Private appointment type (accepts_public_scheduling=False, the default): codeless booking
    must be rejected with PermissionDenied.
    """
    # Member calendars public — so the per-calendar gate would PASS — but the
    # appointment type gate must block before we even get there.
    for cal in internal_calendars.values():
        cal.accepts_public_scheduling = True
        cal.save(update_fields=["accepts_public_scheduling"])

    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=False
    )

    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        _make_available(cal, start, end)

    perm_svc = CalendarPermissionService()
    svc = _make_appointment_type_service(
        organization, calendar_service, permission_service=perm_svc
    )

    with pytest.raises(PermissionDenied):
        svc.create_appointment_type_event(
            _build_event_input(appointment_type, internal_calendars, start, end)
        )


@pytest.mark.django_db
def test_private_appointment_type_with_appointment_type_scoped_token_booking_succeeds(
    organization, calendar_service, internal_calendars
):
    """Private appointment type + valid appointment-type-scoped token: booking must succeed.

    This verifies the existing token path (can_perform_appointment_type_scheduling case 2)
    is preserved after the appointment-type-level check is added.
    """
    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=False
    )

    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        _make_available(cal, start, end)

    # Mint an appointment-type-scoped token with CREATE permission.
    raw_token = generate_long_lived_token()
    token = CalendarManagementToken.objects.create(
        organization=organization,
        token_hash=hash_long_lived_token(raw_token),
        appointment_type_fk=appointment_type,
    )
    token.permissions.create(
        permission=EventManagementPermissions.CREATE,
        organization_id=organization.id,
    )

    # Initialize the permission service with the token string.
    encoded = base64.b64encode(f"{token.pk}:{raw_token}".encode()).decode()
    perm_svc = CalendarPermissionService()
    perm_svc.initialize_with_token(encoded, organization_id=organization.id)

    # Also wire the same permission service into the calendar service so that
    # create_event's per-calendar gate (which uses the permission service from
    # its context) can also authorize via the appointment-type-scoped token.
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    cs.calendar_permission_service = perm_svc
    cs._context = cs._build_context_snapshot()

    svc = _make_appointment_type_service(organization, cs, permission_service=perm_svc)

    event = svc.create_appointment_type_event(
        _build_event_input(appointment_type, internal_calendars, start, end)
    )

    assert event is not None
    assert event.appointment_type_fk_id == appointment_type.id


@pytest.mark.django_db
def test_can_perform_appointment_type_scheduling_public_appointment_type_no_token(
    organization, internal_calendars
):
    """Unit test: can_perform_appointment_type_scheduling returns True for public appointment type + no token."""
    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=True
    )
    perm_svc = CalendarPermissionService()  # no token
    assert (
        perm_svc.can_perform_appointment_type_scheduling(appointment_type=appointment_type) is True
    )


@pytest.mark.django_db
def test_can_perform_appointment_type_scheduling_private_appointment_type_no_token(
    organization, internal_calendars
):
    """Unit test: can_perform_appointment_type_scheduling returns False for private appointment type + no token."""
    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=False
    )
    perm_svc = CalendarPermissionService()  # no token
    assert (
        perm_svc.can_perform_appointment_type_scheduling(appointment_type=appointment_type) is False
    )


@pytest.mark.django_db
def test_can_perform_appointment_type_scheduling_private_type_with_type_scoped_token(
    organization, internal_calendars
):
    """Unit test: can_perform_appointment_type_scheduling returns True for private appointment type + appointment type token."""
    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=False
    )

    raw_token = generate_long_lived_token()
    token = CalendarManagementToken.objects.create(
        organization=organization,
        token_hash=hash_long_lived_token(raw_token),
        appointment_type_fk=appointment_type,
    )
    token.permissions.create(
        permission=EventManagementPermissions.CREATE,
        organization_id=organization.id,
    )

    encoded = base64.b64encode(f"{token.pk}:{raw_token}".encode()).decode()
    perm_svc = CalendarPermissionService()
    perm_svc.initialize_with_token(encoded, organization_id=organization.id)

    assert (
        perm_svc.can_perform_appointment_type_scheduling(appointment_type=appointment_type) is True
    )


@pytest.mark.django_db
def test_can_perform_appointment_type_scheduling_private_appointment_type_calendar_scoped_token_rejected(
    organization, internal_calendars
):
    """Unit test: a calendar-scoped token (not appointment-type-scoped) must NOT grant appointment type scheduling."""
    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=False
    )

    raw_token = generate_long_lived_token()
    token = CalendarManagementToken.objects.create(
        organization=organization,
        token_hash=hash_long_lived_token(raw_token),
        calendar_fk=internal_calendars["phys_a"],  # calendar-scoped, not appointment-type-scoped
    )
    token.permissions.create(
        permission=EventManagementPermissions.CREATE,
        organization_id=organization.id,
    )

    encoded = base64.b64encode(f"{token.pk}:{raw_token}".encode()).decode()
    perm_svc = CalendarPermissionService()
    perm_svc.initialize_with_token(encoded, organization_id=organization.id)

    # Calendar-scoped token cannot authorize appointment-type-level scheduling.
    assert (
        perm_svc.can_perform_appointment_type_scheduling(appointment_type=appointment_type) is False
    )


# ---------------------------------------------------------------------------
# Backward-compat: single-Calendar gate unchanged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_single_calendar_public_codeless_booking_succeeds_unchanged(organization):
    """Single-calendar public booking: gate still keys on Calendar.accepts_public_scheduling.

    The per-calendar check for direct single-calendar bookings is unchanged;
    only the APPOINTMENT_TYPE path changes.
    """
    public_cal = Calendar.objects.create(
        organization=organization,
        name="Public Cal",
        external_id="pub_cal",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    AvailableTime.objects.create(
        organization=organization,
        calendar=public_cal,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    perm_svc = CalendarPermissionService()  # no token
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    cs.calendar_permission_service = perm_svc
    cs._context = cs._build_context_snapshot()

    event = cs.create_event(
        public_cal.id,
        CalendarEventInputData(
            title="Single cal",
            description="",
            start_time=start,
            end_time=end,
            timezone="UTC",
        ),
    )
    assert event is not None
    assert event.calendar_fk_id == public_cal.id


@pytest.mark.django_db
def test_single_calendar_private_codeless_booking_still_rejected(organization):
    """Single-calendar private booking: still rejected by Calendar.accepts_public_scheduling.

    The per-calendar check for direct bookings is not softened.
    """
    private_cal = Calendar.objects.create(
        organization=organization,
        name="Private Cal",
        external_id="priv_cal",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    AvailableTime.objects.create(
        organization=organization,
        calendar=private_cal,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    perm_svc = CalendarPermissionService()  # no token
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    cs.calendar_permission_service = perm_svc
    cs._context = cs._build_context_snapshot()

    with pytest.raises(PermissionDenied):
        cs.create_event(
            private_cal.id,
            CalendarEventInputData(
                title="Should fail",
                description="",
                start_time=start,
                end_time=end,
                timezone="UTC",
            ),
        )


@pytest.mark.django_db
def test_appointment_type_authorized_flag_bypasses_private_member_calendar_gate(organization):
    """appointment_type_authorized=True on CalendarEventInputData bypasses the per-calendar gate.

    This unit-tests the threading mechanism: create_event with appointment_type_authorized=True
    must succeed even when the calendar is private and the permission service has no token.
    """
    private_cal = Calendar.objects.create(
        organization=organization,
        name="Private Member",
        external_id="priv_member",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    AvailableTime.objects.create(
        organization=organization,
        calendar=private_cal,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    perm_svc = CalendarPermissionService()  # no token
    cs = CalendarService()
    cs.initialize_without_provider(organization=organization)
    cs.calendar_permission_service = perm_svc
    cs._context = cs._build_context_snapshot()

    # appointment_type_authorized=True: the appointment type service has already authorized this booking.
    event = cs.create_event(
        private_cal.id,
        CalendarEventInputData(
            title="AppointmentType-authorized bypass",
            description="",
            start_time=start,
            end_time=end,
            timezone="UTC",
            appointment_type_authorized=True,
        ),
    )
    assert event is not None
    assert event.calendar_fk_id == private_cal.id


@pytest.mark.django_db
def test_can_perform_scheduling_still_gates_single_calendar_bookings(organization):
    """can_perform_scheduling still gates single-calendar bookings exactly as before.

    The appointment-type-scheduling gate added around it must not disturb the
    CalendarPermissionService.can_perform_scheduling signature or its behavior
    for single-calendar booking cases.
    """
    cal = Calendar.objects.create(
        organization=organization,
        name="Test Cal",
        external_id="test_cal",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
    )
    perm_svc = CalendarPermissionService()  # no token
    event = CalendarEventInputData(
        title="x",
        description="",
        start_time=timezone.now(),
        end_time=timezone.now() + timedelta(hours=1),
        timezone="UTC",
    )

    # Public calendar, no token → True (case 1: accepts_public_scheduling).
    assert (
        perm_svc.can_perform_scheduling(
            calendar_id=cal.id,
            calendar_settings=CalendarSettingsData(
                manage_available_windows=False,
                accepts_public_scheduling=True,
            ),
            event=event,
        )
        is True
    )

    # Private calendar, no token → False.
    assert (
        perm_svc.can_perform_scheduling(
            calendar_id=cal.id,
            calendar_settings=CalendarSettingsData(
                manage_available_windows=False,
                accepts_public_scheduling=False,
            ),
            event=event,
        )
        is False
    )


# ---------------------------------------------------------------------------
# Fail-closed gate — None permission service raises PermissionDenied
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_appointment_type_event_fail_closed_with_none_permission_service(
    organization, calendar_service, internal_calendars
):
    """create_appointment_type_event on a PRIVATE appointment type with calendar_permission_service=None
    (non-authenticated caller) must raise PermissionDenied — the gate must fail closed.

    Previously the gate was ``if not caller_is_authenticated_user and
    self.calendar_permission_service is not None:``, which let the booking through
    when the permission service was None.  Now it is
    ``if self.calendar_permission_service is None or not ...: raise``.
    """
    appointment_type = _make_appointment_type(
        organization, internal_calendars, accepts_public_scheduling=False
    )

    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        _make_available(cal, start, end)

    # Explicitly pass calendar_permission_service=None to simulate a misconfigured
    # non-authenticated call path where DI failed to inject the permission service.
    svc = _make_appointment_type_service(organization, calendar_service, permission_service=None)

    with pytest.raises(PermissionDenied):
        svc.create_appointment_type_event(
            _build_event_input(appointment_type, internal_calendars, start, end)
        )


# ---------------------------------------------------------------------------
# Bundle backward-compat — appointment_type_authorized does NOT leak into bundle gate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bundle_can_perform_scheduling_still_keys_on_accepts_public_scheduling(organization):
    """Bundle (Calendar type=BUNDLE) scheduling authorization still keys on the
    bundle Calendar's accepts_public_scheduling flag.  The appointment_type_authorized field on
    CalendarEventInputData bypasses the per-calendar PERMISSION gate (token check),
    but the can_perform_scheduling logic itself is unchanged: a private bundle with no
    token returns False; a public bundle returns True.

    This is a backward-compat unit test: CalendarPermissionService.can_perform_scheduling
    is unchanged for bundle calendars.
    """
    bundle_cal = Calendar.objects.create(
        organization=organization,
        name="Bundle",
        external_id="bundle_cal",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.BUNDLE,
    )

    perm_svc = CalendarPermissionService()  # no token

    event_input = CalendarEventInputData(
        title="Bundle booking",
        description="",
        start_time=timezone.now(),
        end_time=timezone.now() + timedelta(hours=1),
        timezone="UTC",
    )

    # Public bundle (accepts_public_scheduling=True) → True even without a token.
    assert (
        perm_svc.can_perform_scheduling(
            calendar_id=bundle_cal.id,
            calendar_settings=CalendarSettingsData(
                manage_available_windows=False,
                accepts_public_scheduling=True,
            ),
            event=event_input,
        )
        is True
    )

    # Private bundle (accepts_public_scheduling=False), no token → False.
    assert (
        perm_svc.can_perform_scheduling(
            calendar_id=bundle_cal.id,
            calendar_settings=CalendarSettingsData(
                manage_available_windows=False,
                accepts_public_scheduling=False,
            ),
            event=event_input,
        )
        is False
    )


@pytest.mark.django_db
def test_bundle_calendar_in_appointment_type_slot_is_rejected_by_create_appointment_type_event(
    organization, calendar_service, internal_calendars
):
    """A BUNDLE calendar selected in an appointment type slot is rejected by create_appointment_type_event.

    The appointment-type-event flow creates BlockedTimes for non-primary selected calendars;
    bundle calendars are not a valid selection target and must raise
    AppointmentTypeValidationError (not silently create a BlockedTime).
    This test locks in the existing _create_non_primary_blocked_times guard.
    """
    bundle_cal = Calendar.objects.create(
        organization=organization,
        name="Bundle",
        external_id="bundle_cal2",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.BUNDLE,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )

    # Build an appointment type with two slots: primary (phys_a) + bundle slot.
    # duration=1h matches the booking span below -- a public appointment type with no
    # duration fails closed in can_perform_appointment_type_scheduling before this test
    # ever reaches the bundle-calendar validation it's actually exercising.
    appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Bundle Slot AppointmentType",
        accepts_public_scheduling=True,
        duration=timedelta(hours=1),
    )
    physician_slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Physicians",
        required_count=1,
        order=0,
    )
    bundle_slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Bundles",
        required_count=1,
        order=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=physician_slot, calendar=internal_calendars["phys_a"]
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=bundle_slot, calendar=bundle_cal
    )

    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    _make_available(internal_calendars["phys_a"], start, end)
    _make_available(bundle_cal, start, end)

    perm_svc = CalendarPermissionService()
    svc = _make_appointment_type_service(
        organization, calendar_service, permission_service=perm_svc
    )

    with pytest.raises(AppointmentTypeValidationError, match="Bundle calendars cannot be selected"):
        svc.create_appointment_type_event(
            AppointmentTypeEventInputData(
                title="Should fail",
                description="",
                start_time=start,
                end_time=end,
                timezone="UTC",
                appointment_type_id=appointment_type.id,
                slot_selections=[
                    AppointmentTypeSlotSelectionInputData(
                        slot_id=physician_slot.id,
                        calendar_ids=[internal_calendars["phys_a"].id],
                    ),
                    AppointmentTypeSlotSelectionInputData(
                        slot_id=bundle_slot.id,
                        calendar_ids=[bundle_cal.id],
                    ),
                ],
            )
        )
