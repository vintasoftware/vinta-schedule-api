"""Unit tests for aggregate filter inputs.

Verifies that each filter input:
- Validates temporal bounds against MAX_AGGREGATE_RANGE
- Raises documented errors on validation failure
- Returns correct queryset type from apply()
"""

import datetime

from django.utils import timezone as tz

import pytest
from model_bakery import baker

from calendar_integration.models import (
    AppointmentType,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
from organizations.models import Organization
from public_api.aggregations import (
    AggregateFilterValidationError,
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.constants import MAX_AGGREGATE_RANGE


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.mark.django_db
class TestCalendarEventAggregateFilterInput:
    """Tests for CalendarEventAggregateFilterInput."""

    def test_apply_returns_calendar_event_queryset(self, organization):
        """apply() returns a CalendarEvent queryset."""
        start = tz.now()
        end = start + datetime.timedelta(days=30)

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )
        qs = filter_input.apply(None, organization)

        assert qs.model == CalendarEvent

    def test_apply_raises_on_range_exceeding_max(self, organization):
        """A range exceeding MAX_AGGREGATE_RANGE raises the documented error."""
        start = tz.now()
        end = start + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1)

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )

        with pytest.raises(AggregateFilterValidationError) as exc:
            filter_input.apply(None, organization)

        assert "exceeds maximum" in str(exc.value)
        assert str(MAX_AGGREGATE_RANGE.days) in str(exc.value)

    def test_apply_range_at_boundary_succeeds(self, organization):
        """A range exactly at MAX_AGGREGATE_RANGE succeeds."""
        start = tz.now()
        end = start + MAX_AGGREGATE_RANGE

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )
        qs = filter_input.apply(None, organization)

        assert qs.model == CalendarEvent


@pytest.mark.django_db
class TestBlockedTimeAggregateFilterInput:
    """Tests for BlockedTimeAggregateFilterInput."""

    def test_apply_returns_blocked_time_queryset(self, organization):
        """apply() returns a BlockedTime queryset."""
        start = tz.now()
        end = start + datetime.timedelta(days=30)

        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )
        qs = filter_input.apply(None, organization)

        assert qs.model == BlockedTime

    def test_apply_raises_on_range_exceeding_max(self, organization):
        """A range exceeding MAX_AGGREGATE_RANGE raises the documented error."""
        start = tz.now()
        end = start + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1)

        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )

        with pytest.raises(AggregateFilterValidationError) as exc:
            filter_input.apply(None, organization)

        assert "exceeds maximum" in str(exc.value)
        assert str(MAX_AGGREGATE_RANGE.days) in str(exc.value)


@pytest.mark.django_db
class TestAvailableTimeAggregateFilterInput:
    """Tests for AvailableTimeAggregateFilterInput."""

    def test_apply_returns_available_time_queryset(self, organization):
        """apply() returns an AvailableTime queryset."""
        start = tz.now()
        end = start + datetime.timedelta(days=30)

        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )
        qs = filter_input.apply(None, organization)

        # AvailableTime is imported inside apply(), so we check the model name
        assert qs.model.__name__ == "AvailableTime"

    def test_apply_raises_on_range_exceeding_max(self, organization):
        """A range exceeding MAX_AGGREGATE_RANGE raises the documented error."""
        start = tz.now()
        end = start + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1)

        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )

        with pytest.raises(AggregateFilterValidationError) as exc:
            filter_input.apply(None, organization)

        assert "exceeds maximum" in str(exc.value)
        assert str(MAX_AGGREGATE_RANGE.days) in str(exc.value)


@pytest.mark.django_db
class TestAppointmentTypeAggregateFilterInput:
    """Tests for AppointmentTypeAggregateFilterInput."""

    def test_apply_returns_appointment_type_queryset(self, organization):
        """apply() returns an AppointmentType queryset."""
        filter_input = AppointmentTypeAggregateFilterInput()
        qs = filter_input.apply(None, organization)

        assert qs.model == AppointmentType


@pytest.mark.django_db
class TestCalendarAggregateFilterInput:
    """Tests for CalendarAggregateFilterInput."""

    def test_apply_returns_calendar_queryset(self, organization):
        """apply() returns a Calendar queryset."""
        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(None, organization)

        assert qs.model == Calendar


@pytest.mark.django_db
class TestCalendarPoolAggregateFilterInput:
    """Tests for CalendarPoolAggregateFilterInput."""

    def test_apply_returns_calendar_pool_queryset(self, organization):
        """apply() returns a CalendarPool queryset."""
        filter_input = CalendarPoolAggregateFilterInput()
        qs = filter_input.apply(None, organization)

        assert qs.model == CalendarPool
