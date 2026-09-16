import datetime

import pytest
from graphql import GraphQLError
from model_bakery import baker

from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    CalendarPool,
)
from organizations.models import Organization, OrganizationMembership
from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)
from public_api.constants import MAX_AGGREGATE_RANGE
from public_api.models import SystemUser
from users.models import User


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

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="test@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create organization membership."""
        return baker.make(
            OrganizationMembership, organization=organization, user=user, is_active=True
        )

    @pytest.fixture
    def calendar1(self, organization, membership):
        """Create first calendar."""
        calendar = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="cal1-event"
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=membership.user_id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def calendar2(self, organization):
        """Create second calendar."""
        return baker.make(
            Calendar, organization=organization, name="Calendar 2", external_id="cal2-event"
        )

    @pytest.fixture
    def event1(self, organization, calendar1, base_datetime):
        """Create event on first calendar."""
        return baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=calendar1,
            start_time_tz_unaware=base_datetime,
            end_time_tz_unaware=base_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="event1-narrow",
        )

    @pytest.fixture
    def event2(self, organization, calendar2, base_datetime):
        """Create event on second calendar."""
        return baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=calendar2,
            start_time_tz_unaware=base_datetime,
            end_time_tz_unaware=base_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="event2-narrow",
        )

    @pytest.fixture
    def system_user(self, membership):
        """Create scoped system user."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership.user_id,
            organization=membership.organization,
        )

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

    def test_calendar_id_narrows_events(self, organization, event1, event2, base_datetime):
        """Filter narrows events by calendar_id."""
        filter_input = CalendarEventAggregateFilterInput(
            calendar_id=event1.calendar_fk_id,
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == event1.id

    def test_system_user_scope_excludes_unowned_calendars(
        self, organization, event1, event2, base_datetime, system_user
    ):
        """Filter with system user excludes events from unowned calendars."""
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id, system_user))
        assert len(result) == 1
        assert result[0].id == event1.id


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

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="available@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create organization membership."""
        return baker.make(
            OrganizationMembership, organization=organization, user=user, is_active=True
        )

    @pytest.fixture
    def calendar1(self, organization, membership):
        """Create first calendar."""
        calendar = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="cal1-avail"
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=membership.user_id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def calendar2(self, organization):
        """Create second calendar."""
        return baker.make(
            Calendar, organization=organization, name="Calendar 2", external_id="cal2-avail"
        )

    @pytest.fixture
    def available1(self, organization, calendar1, base_datetime):
        """Create available time on first calendar."""
        return baker.make(
            AvailableTime,
            organization=organization,
            calendar_fk=calendar1,
            start_time_tz_unaware=base_datetime,
            end_time_tz_unaware=base_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
        )

    @pytest.fixture
    def available2(self, organization, calendar2, base_datetime):
        """Create available time on second calendar."""
        return baker.make(
            AvailableTime,
            organization=organization,
            calendar_fk=calendar2,
            start_time_tz_unaware=base_datetime,
            end_time_tz_unaware=base_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
        )

    @pytest.fixture
    def system_user(self, membership):
        """Create scoped system user."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership.user_id,
            organization=membership.organization,
        )

    def test_valid_range_accepted(self, organization, base_datetime):
        """A date range within MAX_AGGREGATE_RANGE is accepted."""
        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = AvailableTime.objects.none()
        result = filter_input.apply(qs, organization.id)
        assert result is not None

    def test_range_exceeding_max_rejected(self, organization, base_datetime):
        """A date range exceeding MAX_AGGREGATE_RANGE raises GraphQLError."""
        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1),
        )
        qs = AvailableTime.objects.none()
        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(qs, organization.id)

    def test_calendar_id_narrows_availability(
        self, organization, available1, available2, base_datetime
    ):
        """Filter narrows available times by calendar_id."""
        filter_input = AvailableTimeAggregateFilterInput(
            calendar_id=available1.calendar_fk_id,
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = AvailableTime.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == available1.id

    def test_system_user_scope_excludes_unowned_calendars(
        self, organization, available1, available2, base_datetime, system_user
    ):
        """Filter with system user excludes availability from unowned calendars."""
        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = AvailableTime.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id, system_user))
        assert len(result) == 1
        assert result[0].id == available1.id


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

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="blocked@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create organization membership."""
        return baker.make(
            OrganizationMembership, organization=organization, user=user, is_active=True
        )

    @pytest.fixture
    def calendar1(self, organization, membership):
        """Create first calendar."""
        calendar = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="cal1-blocked"
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=membership.user_id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def calendar2(self, organization):
        """Create second calendar."""
        return baker.make(
            Calendar, organization=organization, name="Calendar 2", external_id="cal2-blocked"
        )

    @pytest.fixture
    def blocked1(self, organization, calendar1, base_datetime):
        """Create blocked time on first calendar."""
        return baker.make(
            BlockedTime,
            organization=organization,
            calendar_fk=calendar1,
            start_time_tz_unaware=base_datetime,
            end_time_tz_unaware=base_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
        )

    @pytest.fixture
    def blocked2(self, organization, calendar2, base_datetime):
        """Create blocked time on second calendar."""
        return baker.make(
            BlockedTime,
            organization=organization,
            calendar_fk=calendar2,
            start_time_tz_unaware=base_datetime,
            end_time_tz_unaware=base_datetime + datetime.timedelta(hours=1),
            timezone="UTC",
        )

    @pytest.fixture
    def system_user(self, membership):
        """Create scoped system user."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership.user_id,
            organization=membership.organization,
        )

    def test_valid_range_accepted(self, organization, base_datetime):
        """A date range within MAX_AGGREGATE_RANGE is accepted."""
        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = BlockedTime.objects.none()
        result = filter_input.apply(qs, organization.id)
        assert result is not None

    def test_range_exceeding_max_rejected(self, organization, base_datetime):
        """A date range exceeding MAX_AGGREGATE_RANGE raises GraphQLError."""
        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + MAX_AGGREGATE_RANGE + datetime.timedelta(days=1),
        )
        qs = BlockedTime.objects.none()
        with pytest.raises(GraphQLError, match="Requested time range is too large"):
            filter_input.apply(qs, organization.id)

    def test_calendar_id_narrows_blocked_times(
        self, organization, blocked1, blocked2, base_datetime
    ):
        """Filter narrows blocked times by calendar_id."""
        filter_input = BlockedTimeAggregateFilterInput(
            calendar_id=blocked1.calendar_fk_id,
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = BlockedTime.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == blocked1.id

    def test_system_user_scope_excludes_unowned_calendars(
        self, organization, blocked1, blocked2, base_datetime, system_user
    ):
        """Filter with system user excludes blocked times from unowned calendars."""
        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=base_datetime,
            end_datetime=base_datetime + datetime.timedelta(days=30),
        )
        qs = BlockedTime.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id, system_user))
        assert len(result) == 1
        assert result[0].id == blocked1.id


@pytest.mark.django_db
class TestAppointmentTypeAggregateFilterInput:
    """Test suite for AppointmentTypeAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="appt@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create organization membership."""
        return baker.make(
            OrganizationMembership, organization=organization, user=user, is_active=True
        )

    @pytest.fixture
    def system_user(self, membership):
        """Create scoped system user."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership.user_id,
            organization=membership.organization,
        )

    def test_filter_accepts_empty_input(self, organization):
        """An empty filter input is accepted."""
        apt = baker.make(AppointmentType, organization=organization, name="Test Appointment")
        filter_input = AppointmentTypeAggregateFilterInput()
        qs = AppointmentType.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == apt.id

    def test_filter_narrows_by_name(self, organization):
        """Filter narrows appointment types by name."""
        apt1 = baker.make(AppointmentType, organization=organization, name="Consultation")
        baker.make(AppointmentType, organization=organization, name="Follow-up")
        filter_input = AppointmentTypeAggregateFilterInput(name="Consultation")
        qs = AppointmentType.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == apt1.id

    def test_system_user_scope_applies(self, organization, system_user):
        """Filter with system user applies scoped appointment type filtering."""
        baker.make(AppointmentType, organization=organization, name="Scoped Appointment")
        baker.make(AppointmentType, organization=organization, name="Other Appointment")
        filter_input = AppointmentTypeAggregateFilterInput()
        qs = AppointmentType.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id, system_user))
        assert result is not None


@pytest.mark.django_db
class TestCalendarAggregateFilterInput:
    """Test suite for CalendarAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="cal@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create organization membership."""
        return baker.make(
            OrganizationMembership, organization=organization, user=user, is_active=True
        )

    @pytest.fixture
    def calendar1(self, organization, membership):
        """Create owned calendar."""
        calendar = baker.make(
            Calendar, organization=organization, name="Owned Calendar", external_id="cal-owned"
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=membership.user_id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def calendar2(self, organization):
        """Create unowned calendar."""
        return baker.make(
            Calendar, organization=organization, name="Unowned Calendar", external_id="cal-unowned"
        )

    @pytest.fixture
    def system_user(self, membership):
        """Create scoped system user."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership.user_id,
            organization=membership.organization,
        )

    def test_filter_accepts_empty_input(self, organization):
        """An empty filter input is accepted."""
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
        cal1 = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="cal1"
        )
        baker.make(Calendar, organization=organization, name="Calendar 2", external_id="cal2")
        filter_input = CalendarAggregateFilterInput(calendar_id=cal1.id)
        qs = Calendar.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == cal1.id

    def test_system_user_scope_excludes_unowned_calendars(
        self, organization, calendar1, calendar2, system_user
    ):
        """Filter with system user excludes unowned calendars."""
        filter_input = CalendarAggregateFilterInput()
        qs = Calendar.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id, system_user))
        assert len(result) == 1
        assert result[0].id == calendar1.id


@pytest.mark.django_db
class TestCalendarPoolAggregateFilterInput:
    """Test suite for CalendarPoolAggregateFilterInput."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="pool@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create organization membership."""
        return baker.make(
            OrganizationMembership, organization=organization, user=user, is_active=True
        )

    @pytest.fixture
    def system_user(self, membership):
        """Create scoped system user."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership.user_id,
            organization=membership.organization,
        )

    def test_filter_accepts_empty_input(self, organization):
        """An empty filter input is accepted."""
        pool = baker.make(CalendarPool, organization=organization, name="Test Pool")
        filter_input = CalendarPoolAggregateFilterInput()
        qs = CalendarPool.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == pool.id

    def test_filter_narrows_by_name(self, organization):
        """Filter narrows calendar pools by name."""
        pool1 = baker.make(CalendarPool, organization=organization, name="Team A")
        baker.make(CalendarPool, organization=organization, name="Team B")
        filter_input = CalendarPoolAggregateFilterInput(name="Team A")
        qs = CalendarPool.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id))
        assert len(result) == 1
        assert result[0].id == pool1.id

    def test_system_user_scope_applies(self, organization, system_user):
        """Filter with system user applies scoped calendar pool filtering."""
        baker.make(CalendarPool, organization=organization, name="Pool A")
        baker.make(CalendarPool, organization=organization, name="Pool B")
        filter_input = CalendarPoolAggregateFilterInput()
        qs = CalendarPool.objects.filter_by_organization(organization.id)
        result = list(filter_input.apply(qs, organization.id, system_user))
        assert result is not None
