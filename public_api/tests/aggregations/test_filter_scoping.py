import datetime

import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarEvent, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from public_api.aggregations.filters import CalendarEventAggregateFilterInput
from users.models import User


@pytest.mark.django_db
class TestFilterScopeSecurity:
    """Test that scoped filters enforce organization isolation."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization, name="Test Org")

    @pytest.fixture
    def user(self, organization):
        """Create a user in the organization."""
        user = baker.make(User, email="user@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization, is_active=True)
        return user

    @pytest.fixture
    def calendar(self, organization, user):
        """Create a calendar owned by the user."""
        calendar = baker.make(Calendar, organization=organization, name="Test Calendar")
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user.id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def event(self, organization, calendar):
        """Create an event in the calendar."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        return baker.make(
            CalendarEvent,
            organization=organization,
            calendar_fk=calendar,
            start_time_tz_unaware=base_dt,
            end_time_tz_unaware=base_dt + datetime.timedelta(hours=1),
            timezone="UTC",
            title="Test Event",
            external_id="event-test",
        )

    def test_filter_applies_org_scoping(self, organization, event):
        """Filter applies organization_id filter correctly."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_dt,
            end_datetime=base_dt + datetime.timedelta(days=30),
        )

        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization.id, system_user=None)

        events = list(filtered_qs)
        assert len(events) == 1
        assert events[0].id == event.id

    def test_filter_applies_calendar_id_constraint(self, organization, calendar, event):
        """Filter applies calendar_id constraint when provided."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(
            calendar_id=calendar.id,
            start_datetime=base_dt,
            end_datetime=base_dt + datetime.timedelta(days=30),
        )

        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization.id, system_user=None)

        events = list(filtered_qs)
        assert len(events) == 1
        assert events[0].calendar_fk_id == calendar.id
