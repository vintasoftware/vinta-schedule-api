import datetime

import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarEvent, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from public_api.aggregations.filters import CalendarEventAggregateFilterInput
from public_api.models import SystemUser
from users.models import User


@pytest.mark.django_db
class TestFilterScopeSecurity:
    """Test that scoped filters enforce organization isolation."""

    @pytest.fixture
    def org1(self):
        """Create first test organization."""
        return baker.make(Organization, name="Org1")

    @pytest.fixture
    def org2(self):
        """Create second test organization."""
        return baker.make(Organization, name="Org2")

    @pytest.fixture
    def user1(self):
        """Create user for org1."""
        return baker.make(User, email="user1@example.com")

    @pytest.fixture
    def user2(self):
        """Create user for org2."""
        return baker.make(User, email="user2@example.com")

    @pytest.fixture
    def membership1(self, org1, user1):
        """Create membership for user1 in org1."""
        return baker.make(
            OrganizationMembership, user=user1, organization=org1, is_active=True
        )

    @pytest.fixture
    def membership2(self, org2, user2):
        """Create membership for user2 in org2."""
        return baker.make(
            OrganizationMembership, user=user2, organization=org2, is_active=True
        )

    @pytest.fixture
    def calendar1(self, org1, user1):
        """Create calendar in org1 owned by user1."""
        calendar = baker.make(Calendar, organization=org1, name="Org1 Calendar")
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user1.id,
            organization=org1,
        )
        return calendar

    @pytest.fixture
    def calendar2(self, org2, user2):
        """Create calendar in org2 owned by user2."""
        calendar = baker.make(Calendar, organization=org2, name="Org2 Calendar")
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user2.id,
            organization=org2,
        )
        return calendar

    @pytest.fixture
    def event1(self, org1, calendar1):
        """Create event in org1."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        return baker.make(
            CalendarEvent,
            organization=org1,
            calendar_fk=calendar1,
            start_time_tz_unaware=base_dt,
            end_time_tz_unaware=base_dt + datetime.timedelta(hours=1),
            timezone="UTC",
            title="Org1 Event",
            external_id="event-org1",
        )

    @pytest.fixture
    def event2(self, org2, calendar2):
        """Create event in org2."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        return baker.make(
            CalendarEvent,
            organization=org2,
            calendar_fk=calendar2,
            start_time_tz_unaware=base_dt,
            end_time_tz_unaware=base_dt + datetime.timedelta(hours=1),
            timezone="UTC",
            title="Org2 Event",
            external_id="event-org2",
        )

    @pytest.fixture
    def system_user1(self, membership1):
        """Create scoped system user for org1."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership1.user_id,
            organization=membership1.organization,
        )

    def test_scoped_filter_excludes_other_org_events(
        self, org1, org2, event1, event2, system_user1
    ):
        """A scoped user's filter returns only events from their org's calendars."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_dt,
            end_datetime=base_dt + datetime.timedelta(days=30),
        )

        qs = CalendarEvent.objects.filter_by_organization(org1.id)
        filtered_qs = filter_input.apply(qs, org1.id, system_user1)

        events = list(filtered_qs)
        assert len(events) == 1
        assert events[0].id == event1.id
        assert events[0].organization_id == org1.id

    def test_unscoped_filter_still_respects_org_boundary(
        self, org1, org2, event1, event2
    ):
        """Filter always applies organization filter, even without a system user."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_dt,
            end_datetime=base_dt + datetime.timedelta(days=30),
        )

        qs = CalendarEvent.objects.filter_by_organization(org1.id)
        filtered_qs = filter_input.apply(qs, org1.id, system_user=None)

        org1_events = list(filtered_qs)
        assert len(org1_events) == 1
        assert org1_events[0].id == event1.id
        assert org1_events[0].organization_id == org1.id
