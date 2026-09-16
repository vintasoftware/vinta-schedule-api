import datetime

import pytest
from graphql import GraphQLError
from model_bakery import baker

from calendar_integration.models import CalendarEvent
from organizations.models import Organization
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
class TestCalendarEventAggregateFilterInput:
    """Test suite for CalendarEventAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def base_datetime(self):
        """Reference datetime for range calculations."""
        return datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)

    def test_valid_range_accepted(self, organization, base_datetime):
        """A date range within MAX_AGGREGATE_RANGE is accepted."""
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = CalendarEvent.objects.none()
        result = filter_input.apply(qs, organization.id)
        assert result is not None

    def test_range_exceeding_max_rejected(self, organization, base_datetime):
        """A date range exceeding MAX_AGGREGATE_RANGE raises GraphQLError."""
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1),
        )
        qs = CalendarEvent.objects.none()
        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(qs, organization.id)

    def test_backward_range_rejected(self, organization, base_datetime):
        """A range with end before start raises GraphQLError."""
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime - datetime.timedelta(days=1),
        )
        qs = CalendarEvent.objects.none()
        with pytest.raises(GraphQLError, match="Invalid time range"):
            filter_input.apply(qs, organization.id)

    def test_equal_start_and_end_rejected(self, organization, base_datetime):
        """A range with equal start and end raises GraphQLError."""
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime,
        )
        qs = CalendarEvent.objects.none()
        with pytest.raises(GraphQLError, match="Invalid time range"):
            filter_input.apply(qs, organization.id)


@pytest.mark.django_db
class TestAvailableTimeAggregateFilterInput:
    """Test suite for AvailableTimeAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def base_datetime(self):
        """Reference datetime for range calculations."""
        return datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)

    def test_valid_range_accepted(self, organization, base_datetime):
        """A date range within MAX_AGGREGATE_RANGE is accepted."""
        from calendar_integration.models import AvailableTime

        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = AvailableTime.objects.none()
        result = filter_input.apply(qs, organization.id)
        assert result is not None

    def test_range_exceeding_max_rejected(self, organization, base_datetime):
        """A date range exceeding MAX_AGGREGATE_RANGE raises GraphQLError."""
        from calendar_integration.models import AvailableTime

        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1),
        )
        qs = AvailableTime.objects.none()
        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(qs, organization.id)


@pytest.mark.django_db
class TestBlockedTimeAggregateFilterInput:
    """Test suite for BlockedTimeAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def base_datetime(self):
        """Reference datetime for range calculations."""
        return datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)

    def test_valid_range_accepted(self, organization, base_datetime):
        """A date range within MAX_AGGREGATE_RANGE is accepted."""
        from calendar_integration.models import BlockedTime

        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = BlockedTime.objects.none()
        result = filter_input.apply(qs, organization.id)
        assert result is not None

    def test_range_exceeding_max_rejected(self, organization, base_datetime):
        """A date range exceeding MAX_AGGREGATE_RANGE raises GraphQLError."""
        from calendar_integration.models import BlockedTime

        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1),
        )
        qs = BlockedTime.objects.none()
        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(qs, organization.id)


@pytest.mark.django_db
class TestAppointmentTypeAggregateFilterInput:
    """Test suite for AppointmentTypeAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    def test_filter_accepts_empty_input(self, organization):
        """An empty filter input is accepted."""
        from calendar_integration.models import AppointmentType

        apt = baker.make(AppointmentType, organization=organization, name="Test Appointment")
        filter_input = AppointmentTypeAggregateFilterInput()
        qs = AppointmentType.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == apt.id

    def test_filter_narrows_by_name(self, organization):
        """Filter narrows appointment types by name."""
        from calendar_integration.models import AppointmentType

        apt1 = baker.make(AppointmentType, organization=organization, name="Consultation")
        baker.make(AppointmentType, organization=organization, name="Follow-up")
        filter_input = AppointmentTypeAggregateFilterInput(name="Consultation")
        qs = AppointmentType.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == apt1.id


@pytest.mark.django_db
class TestCalendarAggregateFilterInput:
    """Test suite for CalendarAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    def test_filter_accepts_empty_input(self, organization):
        """An empty filter input is accepted."""
        from calendar_integration.models import Calendar

        cal = baker.make(
            Calendar, organization=organization, name="Test Calendar", external_id="test-cal"
        )
        filter_input = CalendarAggregateFilterInput()
        qs = Calendar.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == cal.id

    def test_filter_narrows_by_calendar_id(self, organization):
        """Filter narrows calendars by calendar_id."""
        from calendar_integration.models import Calendar

        cal1 = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="cal1"
        )
        baker.make(
            Calendar, organization=organization, name="Calendar 2", external_id="cal2"
        )
        filter_input = CalendarAggregateFilterInput(calendar_id=cal1.id)
        qs = Calendar.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == cal1.id


@pytest.mark.django_db
class TestCalendarPoolAggregateFilterInput:
    """Test suite for CalendarPoolAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    def test_filter_accepts_empty_input(self, organization):
        """An empty filter input is accepted."""
        from calendar_integration.models import CalendarPool

        pool = baker.make(CalendarPool, organization=organization, name="Test Pool")
        filter_input = CalendarPoolAggregateFilterInput()
        qs = CalendarPool.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == pool.id

    def test_filter_narrows_by_name(self, organization):
        """Filter narrows calendar pools by name."""
        from calendar_integration.models import CalendarPool

        pool1 = baker.make(CalendarPool, organization=organization, name="Team A")
        baker.make(CalendarPool, organization=organization, name="Team B")
        filter_input = CalendarPoolAggregateFilterInput(name="Team A")
        qs = CalendarPool.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == pool1.id
