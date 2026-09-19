"""Unit tests for aggregate filter inputs.

Covers:
- Date range validation (mandatory, non-backwards, within MAX_AGGREGATE_RANGE)
- Filter attribute validation
"""

import datetime

from django.utils import timezone as tz

import pytest
from graphql import GraphQLError

from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.constants import MAX_AGGREGATE_RANGE


@pytest.mark.django_db
class TestTemporalFilterValidation:
    """Tests for mandatory date range validation on temporal entities."""

    def test_calendar_event_filter_rejects_backwards_range(self):
        """end_datetime <= start_datetime raises GraphQLError in apply()."""
        now = tz.now()
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=now,
            end_datetime=now,
        )
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org")

        with pytest.raises(GraphQLError, match="Invalid time range"):
            from calendar_integration.models import CalendarEvent

            qs = CalendarEvent.objects.filter_by_organization(org.id)
            filter_input.apply(qs, org)

    def test_calendar_event_filter_rejects_range_exceeding_max(self):
        """Range exceeding MAX_AGGREGATE_RANGE raises GraphQLError."""
        now = tz.now()
        too_far = now + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=now,
            end_datetime=too_far,
        )
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 2")

        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            from calendar_integration.models import CalendarEvent

            qs = CalendarEvent.objects.filter_by_organization(org.id)
            filter_input.apply(qs, org)

    def test_calendar_event_filter_accepts_valid_range(self):
        """Valid range within bounds is accepted."""
        now = tz.now()
        later = now + datetime.timedelta(days=30)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 3")

        try:
            from calendar_integration.models import CalendarEvent

            qs = CalendarEvent.objects.filter_by_organization(org.id)
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"Valid range raised ValueError: {e}")

    def test_available_time_filter_accepts_valid_range(self):
        """AvailableTime accepts valid temporal range."""
        now = tz.now()
        later = now + datetime.timedelta(days=30)
        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 4")

        try:
            from calendar_integration.models import AvailableTime

            qs = AvailableTime.objects.filter_by_organization(org.id)
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"Valid range raised ValueError: {e}")

    def test_blocked_time_filter_accepts_valid_range(self):
        """BlockedTime accepts valid temporal range."""
        now = tz.now()
        later = now + datetime.timedelta(days=30)
        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 5")

        try:
            from calendar_integration.models import BlockedTime

            qs = BlockedTime.objects.filter_by_organization(org.id)
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"Valid range raised ValueError: {e}")


@pytest.mark.django_db
class TestNonTemporalFilterValidation:
    """Tests for non-temporal filter inputs (no date range required)."""

    def test_appointment_type_filter_accepts_no_arguments(self):
        """AppointmentType filter is valid with no arguments."""
        filter_input = AppointmentTypeAggregateFilterInput()
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 6")

        from calendar_integration.models import AppointmentType

        qs = AppointmentType.objects.filter_by_organization(org.id)
        try:
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"No-argument filter raised ValueError: {e}")

    def test_calendar_filter_accepts_no_arguments(self):
        """Calendar filter is valid with no arguments."""
        filter_input = CalendarAggregateFilterInput()
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 7")

        from calendar_integration.models import Calendar

        qs = Calendar.objects.filter_by_organization(org.id)
        try:
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"No-argument filter raised ValueError: {e}")

    def test_calendar_pool_filter_accepts_no_arguments(self):
        """CalendarPool filter is valid with no arguments."""
        filter_input = CalendarPoolAggregateFilterInput()
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 8")

        from calendar_integration.models import CalendarPool

        qs = CalendarPool.objects.filter_by_organization(org.id)
        try:
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"No-argument filter raised ValueError: {e}")

    def test_appointment_type_filter_with_name_filter(self):
        """AppointmentType filter accepts name predicate."""
        filter_input = AppointmentTypeAggregateFilterInput(name="Meeting")
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 9")

        from calendar_integration.models import AppointmentType

        qs = AppointmentType.objects.filter_by_organization(org.id)
        try:
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"Name-filtered input raised ValueError: {e}")

    def test_calendar_filter_with_name_filter(self):
        """Calendar filter accepts name predicate."""
        filter_input = CalendarAggregateFilterInput(name="Test")
        from organizations.models import Organization

        org = Organization.objects.create(name="Test Org 10")

        from calendar_integration.models import Calendar

        qs = Calendar.objects.filter_by_organization(org.id)
        try:
            result = filter_input.apply(qs, org)
            assert result is not None
        except ValueError as e:
            pytest.fail(f"Name-filtered input raised ValueError: {e}")
