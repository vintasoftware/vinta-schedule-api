import datetime

import pytest
from freezegun import freeze_time
from graphql import GraphQLError
from model_bakery import baker

from calendar_integration.models import (
    AppointmentType,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
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
    """Test date range validation for temporal aggregate filters."""

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_event_filter_valid_range(self):
        """A valid date range within MAX_AGGREGATE_RANGE passes validation."""
        organization = baker.make("organizations.Organization")
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)

        filter_input = CalendarEventAggregateFilterInput(start_datetime=start, end_datetime=end)
        qs = filter_input.apply(system_user=None, organization=organization)

        # Should not raise and should return a valid queryset
        assert qs.model == CalendarEvent

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_event_filter_exceeds_max_range(self):
        """A date range exceeding MAX_AGGREGATE_RANGE raises the documented error."""
        organization = baker.make("organizations.Organization")
        start = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)

        filter_input = CalendarEventAggregateFilterInput(start_datetime=start, end_datetime=end)

        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(system_user=None, organization=organization)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_event_filter_backwards_range(self):
        """A backwards date range (end <= start) raises an error."""
        organization = baker.make("organizations.Organization")
        start = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        filter_input = CalendarEventAggregateFilterInput(start_datetime=start, end_datetime=end)

        with pytest.raises(GraphQLError, match="Invalid time range"):
            filter_input.apply(system_user=None, organization=organization)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_available_time_filter_exceeds_max_range(self):
        """AvailableTime filter also enforces MAX_AGGREGATE_RANGE."""
        organization = baker.make("organizations.Organization")
        start = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)

        filter_input = AvailableTimeAggregateFilterInput(start_datetime=start, end_datetime=end)

        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(system_user=None, organization=organization)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_blocked_time_filter_exceeds_max_range(self):
        """BlockedTime filter also enforces MAX_AGGREGATE_RANGE."""
        organization = baker.make("organizations.Organization")
        start = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)

        filter_input = BlockedTimeAggregateFilterInput(start_datetime=start, end_datetime=end)

        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(system_user=None, organization=organization)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_temporal_filter_at_boundary(self):
        """A date range exactly at MAX_AGGREGATE_RANGE boundary is accepted."""
        organization = baker.make("organizations.Organization")
        start = datetime.datetime(2025, 1, 15, tzinfo=datetime.UTC)
        end = start + MAX_AGGREGATE_RANGE

        filter_input = CalendarEventAggregateFilterInput(start_datetime=start, end_datetime=end)
        qs = filter_input.apply(system_user=None, organization=organization)

        # Should not raise
        assert qs.model == CalendarEvent


@pytest.mark.django_db
class TestNonTemporalFilterValidation:
    """Test non-temporal aggregate filters without date range requirements."""

    def test_appointment_type_filter_no_date_range(self):
        """AppointmentType filter requires no date range."""
        organization = baker.make("organizations.Organization")
        filter_input = AppointmentTypeAggregateFilterInput()
        qs = filter_input.apply(system_user=None, organization=organization)

        assert qs.model == AppointmentType

    def test_calendar_filter_no_date_range(self):
        """Calendar filter requires no date range."""
        organization = baker.make("organizations.Organization")
        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(system_user=None, organization=organization)

        assert qs.model == Calendar

    def test_calendar_pool_filter_no_date_range(self):
        """CalendarPool filter requires no date range."""
        organization = baker.make("organizations.Organization")
        filter_input = CalendarPoolAggregateFilterInput()
        qs = filter_input.apply(system_user=None, organization=organization)

        assert qs.model == CalendarPool


class TestFilterTypeLevelValidation:
    """Test type-level constraints on aggregate filter inputs."""

    def test_calendar_event_filter_requires_start_datetime(self):
        """CalendarEvent filter requires non-null start_datetime at the type level."""
        # Check that the class has start_datetime and end_datetime as required fields
        annotations = CalendarEventAggregateFilterInput.__annotations__
        assert "start_datetime" in annotations
        # The annotation should be datetime.datetime (not Optional)
        assert annotations["start_datetime"] == datetime.datetime

    def test_calendar_event_filter_requires_end_datetime(self):
        """CalendarEvent filter requires non-null end_datetime at the type level."""
        annotations = CalendarEventAggregateFilterInput.__annotations__
        assert "end_datetime" in annotations
        assert annotations["end_datetime"] == datetime.datetime

    def test_available_time_filter_requires_bounds(self):
        """AvailableTime filter requires non-null start/end datetime at the type level."""
        annotations = AvailableTimeAggregateFilterInput.__annotations__
        assert "start_datetime" in annotations
        assert "end_datetime" in annotations
        assert annotations["start_datetime"] == datetime.datetime
        assert annotations["end_datetime"] == datetime.datetime

    def test_blocked_time_filter_requires_bounds(self):
        """BlockedTime filter requires non-null start/end datetime at the type level."""
        annotations = BlockedTimeAggregateFilterInput.__annotations__
        assert "start_datetime" in annotations
        assert "end_datetime" in annotations
        assert annotations["start_datetime"] == datetime.datetime
        assert annotations["end_datetime"] == datetime.datetime

    def test_non_temporal_filters_have_no_required_datetime(self):
        """Non-temporal filters do not require datetime fields."""
        apt_annotations = AppointmentTypeAggregateFilterInput.__annotations__
        assert "start_datetime" not in apt_annotations
        assert "end_datetime" not in apt_annotations

        cal_annotations = CalendarAggregateFilterInput.__annotations__
        assert "start_datetime" not in cal_annotations
        assert "end_datetime" not in cal_annotations

        pool_annotations = CalendarPoolAggregateFilterInput.__annotations__
        assert "start_datetime" not in pool_annotations
        assert "end_datetime" not in pool_annotations
