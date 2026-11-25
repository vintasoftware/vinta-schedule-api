"""Rescheduling an appointment-type event checks every crew calendar's availability.

Booking (``create_appointment_type_event``) rejects a time when any selected
calendar is busy or outside its availability. Rescheduling must apply the same
check at the new time to the whole crew: the primary calendar and every
non-primary calendar selected for the event.

The event being moved must not count against itself. Its own ``CalendarEvent``
on the primary calendar and its own ``BlockedTime`` rows on the other calendars
still sit at the old time while the check runs, so a move that overlaps the old
slot would otherwise always be rejected.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import CalendarProvider, CalendarSyncStatus, CalendarType
from calendar_integration.exceptions import AppointmentTypeValidationError, RoomUnavailableError
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    CalendarSync,
    ResourceAllocation,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AppointmentTypeEventInputData,
    AppointmentTypeSlotSelectionInputData,
    CalendarEventAdapterOutputData,
    ResourceAllocationInputData,
)
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService
from users.models import Profile, User


def _utc(hour: int, minute: int = 0, day: int = 2) -> datetime.datetime:
    # 2025-09-02 is a Tuesday. Every calendar below is available Monday to Friday.
    return datetime.datetime(2025, 9, day, hour, minute, tzinfo=datetime.UTC)


def _naive(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=None)


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Crew Reschedule Org", should_sync_rooms=False)


def _make_calendar(
    organization: Organization,
    name: str,
    *,
    managed: bool,
    provider: CalendarProvider = CalendarProvider.INTERNAL,
) -> Calendar:
    calendar = Calendar.objects.create(
        organization=organization,
        name=name,
        external_id=name.lower().replace(" ", "_"),
        provider=provider,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=managed,
        accepts_public_scheduling=True,
    )
    if managed:
        # Monday 2025-09-01 to Saturday 2025-09-06: the whole working week.
        AvailableTime.objects.create(
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2025, 9, 1),
            end_time_tz_unaware=datetime.datetime(2025, 9, 6),
            timezone="UTC",
        )
    return calendar


def _appointment_type_service(
    organization: Organization, user_or_token: SystemUser | None
) -> AppointmentTypeService:
    from di_core.containers import container

    assert container is not None
    calendar_service = CalendarService()
    calendar_service.initialize_without_provider(
        user_or_token=user_or_token, organization=organization
    )
    svc = AppointmentTypeService(
        calendar_service=calendar_service,
        calendar_permission_service=CalendarPermissionService(),
        audit_service=container.audit_service(),
    )
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def service(organization: Organization) -> AppointmentTypeService:
    """The service as the public API drives it: acting as an org-wide API token."""
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="crew-reschedule", organization=organization
    )
    ResourceAccess.objects.create(
        system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    return _appointment_type_service(organization, system_user)


def _book_crew(
    organization: Organization,
    lead: Calendar,
    assistant: Calendar,
    *,
    room: Calendar | None = None,
) -> CalendarEvent:
    """Book a 10:00-10:30 Tuesday appointment with ``lead`` as primary and
    ``assistant`` as the second crew member, through the real public booking path."""
    appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Surgery",
        accepts_public_scheduling=True,
        duration=datetime.timedelta(minutes=30),
    )
    lead_slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Lead", order=0
    )
    assistant_slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Assistant", order=1
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=lead_slot, calendar=lead
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=assistant_slot, calendar=assistant
    )
    booking_service = _appointment_type_service(organization, None)
    return booking_service.create_appointment_type_event(
        AppointmentTypeEventInputData(
            title="Surgery",
            description="",
            start_time=_utc(10),
            end_time=_utc(10, 30),
            timezone="UTC",
            appointment_type_id=appointment_type.id,
            slot_selections=[
                AppointmentTypeSlotSelectionInputData(slot_id=lead_slot.id, calendar_ids=[lead.id]),
                AppointmentTypeSlotSelectionInputData(
                    slot_id=assistant_slot.id, calendar_ids=[assistant.id]
                ),
            ],
            resource_allocations=(
                [ResourceAllocationInputData(resource_id=room.id)] if room is not None else []
            ),
        )
    )


def _assert_not_moved(organization: Organization, event: CalendarEvent) -> None:
    stored = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event.id)
    assert _naive(stored.start_time_tz_unaware) == _naive(_utc(10))
    own_blocks = BlockedTime.objects.filter_by_organization(organization.id).filter(
        external_id__startswith=f"appointment-type-event-{event.id}-cal-"
    )
    assert own_blocks.exists()
    for block in own_blocks:
        assert _naive(block.start_time_tz_unaware) == _naive(_utc(10))


@pytest.mark.django_db
@pytest.mark.parametrize("managed", [True, False], ids=["managed", "unmanaged"])
def test_reschedule_rejects_when_non_primary_crew_calendar_has_conflicting_event(
    service: AppointmentTypeService, organization: Organization, managed: bool
) -> None:
    lead = _make_calendar(organization, "Lead", managed=managed)
    assistant = _make_calendar(organization, "Assistant", managed=managed)
    event = _book_crew(organization, lead, assistant)
    CalendarEvent.objects.create(
        organization=organization,
        calendar=assistant,
        title="Other surgery",
        start_time_tz_unaware=_naive(_utc(14)),
        end_time_tz_unaware=_naive(_utc(15)),
        timezone="UTC",
        external_id="assistant-other-surgery",
    )

    with pytest.raises(AppointmentTypeValidationError, match=str(assistant.id)):
        service.reschedule_appointment_type_event(
            event_id=event.id, start_time=_utc(14), end_time=_utc(14, 30), tz="UTC"
        )

    _assert_not_moved(organization, event)


@pytest.mark.django_db
@pytest.mark.parametrize("managed", [True, False], ids=["managed", "unmanaged"])
def test_reschedule_rejects_when_primary_crew_calendar_has_blocked_time(
    service: AppointmentTypeService, organization: Organization, managed: bool
) -> None:
    lead = _make_calendar(organization, "Lead", managed=managed)
    assistant = _make_calendar(organization, "Assistant", managed=managed)
    event = _book_crew(organization, lead, assistant)
    BlockedTime.objects.create(
        organization=organization,
        calendar=lead,
        start_time_tz_unaware=_naive(_utc(14)),
        end_time_tz_unaware=_naive(_utc(15)),
        timezone="UTC",
        reason="Lunch",
    )

    with pytest.raises(AppointmentTypeValidationError, match=str(lead.id)):
        service.reschedule_appointment_type_event(
            event_id=event.id, start_time=_utc(14), end_time=_utc(14, 30), tz="UTC"
        )

    _assert_not_moved(organization, event)


@pytest.mark.django_db
def test_reschedule_rejects_when_non_primary_crew_calendar_has_no_availability_window(
    service: AppointmentTypeService, organization: Organization
) -> None:
    lead = _make_calendar(organization, "Lead", managed=True)
    assistant = _make_calendar(organization, "Assistant", managed=True)
    event = _book_crew(organization, lead, assistant)
    # The lead is also available on Saturday; the assistant is not.
    AvailableTime.objects.create(
        organization=organization,
        calendar=lead,
        start_time_tz_unaware=datetime.datetime(2025, 9, 6),
        end_time_tz_unaware=datetime.datetime(2025, 9, 7),
        timezone="UTC",
    )

    with pytest.raises(AppointmentTypeValidationError, match=str(assistant.id)):
        service.reschedule_appointment_type_event(
            event_id=event.id,
            start_time=_utc(10, day=6),
            end_time=_utc(10, 30, day=6),
            tz="UTC",
        )

    _assert_not_moved(organization, event)


@pytest.mark.django_db
@pytest.mark.parametrize("managed", [True, False], ids=["managed", "unmanaged"])
def test_reschedule_overlapping_its_own_old_slot_is_not_a_conflict(
    service: AppointmentTypeService, organization: Organization, managed: bool
) -> None:
    lead = _make_calendar(organization, "Lead", managed=managed)
    assistant = _make_calendar(organization, "Assistant", managed=managed)
    event = _book_crew(organization, lead, assistant)

    service.reschedule_appointment_type_event(
        event_id=event.id, start_time=_utc(10, 15), end_time=_utc(10, 45), tz="UTC"
    )

    stored = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event.id)
    assert _naive(stored.start_time_tz_unaware) == _naive(_utc(10, 15))
    block = BlockedTime.objects.filter_by_organization(organization.id).get(
        external_id=f"appointment-type-event-{event.id}-cal-{assistant.id}"
    )
    assert _naive(block.start_time_tz_unaware) == _naive(_utc(10, 15))


@pytest.mark.django_db
def test_reschedule_still_sees_other_bookings_next_to_its_own_old_slot(
    service: AppointmentTypeService, organization: Organization
) -> None:
    """Only the moved event's own rows are ignored, not everything near the old slot."""
    lead = _make_calendar(organization, "Lead", managed=True)
    assistant = _make_calendar(organization, "Assistant", managed=True)
    event = _book_crew(organization, lead, assistant)
    BlockedTime.objects.create(
        organization=organization,
        calendar=assistant,
        start_time_tz_unaware=_naive(_utc(10, 30)),
        end_time_tz_unaware=_naive(_utc(11)),
        timezone="UTC",
        reason="Break",
    )

    with pytest.raises(AppointmentTypeValidationError, match=str(assistant.id)):
        service.reschedule_appointment_type_event(
            event_id=event.id, start_time=_utc(10, 15), end_time=_utc(10, 45), tz="UTC"
        )

    _assert_not_moved(organization, event)


def _own(organization: Organization, calendar: Calendar, email: str) -> None:
    owner = User.objects.create_user(email=email, password="pw")  # noqa: S106
    Profile.objects.create(user=owner)
    OrganizationMembership.objects.create(user=owner, organization=organization)
    CalendarOwnership.objects.create(
        organization=organization, calendar=calendar, membership_user_id=owner.id
    )


_PROVIDER_ADAPTER_CLASSES = {
    CalendarProvider.GOOGLE: (
        "calendar_integration.services.calendar_adapters.google_calendar_adapter."
        "GoogleCalendarAdapter"
    ),
    CalendarProvider.MICROSOFT: (
        "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter."
        "MSOutlookCalendarAdapter"
    ),
}


def _adapter_event(
    calendar_external_id: str, external_id: str, start: datetime.datetime
) -> CalendarEventAdapterOutputData:
    return CalendarEventAdapterOutputData(
        calendar_external_id=calendar_external_id,
        external_id=external_id,
        ical_uid="surgery-uid@provider",
        title="Surgery",
        description="",
        start_time=start,
        end_time=start + datetime.timedelta(minutes=30),
        timezone="UTC",
        attendees=[],
    )


def _sync_from_provider(
    organization: Organization,
    calendar: Calendar,
    provider: CalendarProvider,
    events: list[CalendarEventAdapterOutputData],
) -> None:
    """Run the real calendar sync for ``calendar``, with the provider's adapter
    returning ``events``."""
    account_user = User.objects.create_user(email=f"sync-{provider}@example.com", password="pw")  # noqa: S106
    Profile.objects.create(user=account_user)
    account = SocialAccount.objects.create(user=account_user, provider=provider, uid="sync")
    SocialToken.objects.create(
        account=account,
        token="token",  # noqa: S106
        token_secret="secret",  # noqa: S106
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    with patch(_PROVIDER_ADAPTER_CLASSES[provider]) as adapter_class:
        adapter = Mock()
        adapter.provider = provider
        del adapter.resolve_expression
        del adapter.get_source_expressions
        adapter_class.return_value = adapter
        adapter.get_events.return_value = {"events": events, "next_sync_token": "token"}
        sync_service = CalendarService()
        sync_service.authenticate(account=account_user, organization=organization)
        calendar_sync = CalendarSync.objects.create(
            organization=organization,
            calendar=calendar,
            start_datetime=_utc(0),
            end_datetime=_utc(23, 59),
            should_update_events=True,
        )
        sync_service.sync_events(calendar_sync)
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.SUCCESS


@pytest.mark.django_db
@pytest.mark.parametrize("managed", [True, False], ids=["managed", "unmanaged"])
@pytest.mark.parametrize(
    ("provider", "assistant_copy_id"),
    [
        # Google gives every attendee's copy the organizer's event id.
        (CalendarProvider.GOOGLE, "lead-event-id"),
        # Outlook gives each mailbox's copy its own id.
        (CalendarProvider.MICROSOFT, "assistant-copy-id"),
    ],
    ids=["google", "outlook"],
)
def test_reschedule_of_a_same_provider_crew_ignores_the_synced_copy_of_its_own_invite(
    service: AppointmentTypeService,
    organization: Organization,
    managed: bool,
    provider: CalendarProvider,
    assistant_copy_id: str,
    write_adapter: Mock,
) -> None:
    """Both crew calendars are owned calendars on one provider, so booking writes no
    linked BlockedTime on the assistant's calendar: the provider's invite puts the event
    there, and the calendar sync imports that copy as a BlockedTime. The copy carries the
    event's iCalendar UID, so it is recognised as the event itself and moving into the
    old slot is not a conflict."""
    lead = _make_calendar(organization, "Lead", managed=managed, provider=provider)
    assistant = _make_calendar(organization, "Assistant", managed=managed, provider=provider)
    _own(organization, lead, "lead@example.com")
    _own(organization, assistant, "assistant@example.com")
    write_adapter.create_event.return_value = _adapter_event("lead", "lead-event-id", _utc(10))
    event = _book_crew(organization, lead, assistant)
    assert event.ical_uid == "surgery-uid@provider"
    assert not BlockedTime.objects.filter_by_organization(organization.id).filter(
        external_id__startswith=f"appointment-type-event-{event.id}-cal-"
    )

    _sync_from_provider(
        organization,
        assistant,
        provider,
        [_adapter_event("assistant", assistant_copy_id, _utc(10))],
    )
    copy = BlockedTime.objects.filter_by_organization(organization.id).get(
        calendar=assistant, external_id=assistant_copy_id
    )
    assert copy.ical_uid == "surgery-uid@provider"

    write_adapter.update_event.return_value = _adapter_event("lead", "lead-event-id", _utc(10, 15))
    service.reschedule_appointment_type_event(
        event_id=event.id, start_time=_utc(10, 15), end_time=_utc(10, 45), tz="UTC"
    )

    stored = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event.id)
    assert _naive(stored.start_time_tz_unaware) == _naive(_utc(10, 15))


@pytest.mark.django_db
def test_reschedule_rejects_when_the_room_the_event_holds_is_taken(
    service: AppointmentTypeService, organization: Organization
) -> None:
    lead = _make_calendar(organization, "Lead", managed=False)
    assistant = _make_calendar(organization, "Assistant", managed=False)
    room = Calendar.objects.create(
        organization=organization,
        name="Theatre 1",
        external_id="theatre_1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
    )
    event = _book_crew(organization, lead, assistant, room=room)
    other_host = _make_calendar(organization, "Other host", managed=False)
    other = CalendarEvent.objects.create(
        organization=organization,
        calendar=other_host,
        title="Other surgery",
        start_time_tz_unaware=_naive(_utc(14)),
        end_time_tz_unaware=_naive(_utc(15)),
        timezone="UTC",
        external_id="other-surgery-in-theatre-1",
    )
    ResourceAllocation.objects.create(organization=organization, event=other, calendar=room)

    with pytest.raises(RoomUnavailableError):
        service.reschedule_appointment_type_event(
            event_id=event.id, start_time=_utc(14), end_time=_utc(14, 30), tz="UTC"
        )

    _assert_not_moved(organization, event)
