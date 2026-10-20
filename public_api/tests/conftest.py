"""Fixtures shared by the public API tests.

A test module that defines a fixture with the same name uses its own instead.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.tests.helpers import GraphQLPost, make_token_post


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization, name="Public API Org")


@pytest.fixture
def booking_calendar(organization: Organization) -> Calendar:
    """An internal personal calendar open on 2030-06-01 from 09:00 to 17:00 UTC."""
    calendar = baker.make(
        Calendar,
        organization=organization,
        name="Provider Calendar",
        external_id=f"booking-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )
    return calendar


@pytest.fixture
def appointment_type_with_slot(
    organization: Organization, booking_calendar: Calendar
) -> tuple[AppointmentType, AppointmentTypeSlot]:
    """A private appointment type whose one slot books ``booking_calendar``."""
    appointment_type = baker.make(
        AppointmentType,
        organization=organization,
        name="Private Visit",
        accepts_public_scheduling=False,
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Provider",
        order=0,
        required_count=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=booking_calendar
    )
    return appointment_type, slot


@pytest.fixture
def org_wide_post(organization: Organization) -> GraphQLPost:
    """post() for an organization-wide token holding APPOINTMENT_TYPE and CALENDAR_EVENT."""
    return make_token_post(
        organization, [PublicAPIResources.APPOINTMENT_TYPE, PublicAPIResources.CALENDAR_EVENT]
    )
