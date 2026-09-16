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
        return baker.make(OrganizationMembership, user=user1, organization=org1, is_active=True)

    @pytest.fixture
    def membership2(self, org2, user2):
        """Create membership for user2 in org2."""
        return baker.make(OrganizationMembership, user=user2, organization=org2, is_active=True)

    @pytest.fixture
    def calendar1(self, org1, membership1):
        """Create calendar in org1 owned by user1."""
        calendar = baker.make(
            Calendar, organization=org1, name="Org1 Calendar", external_id="org1-cal1"
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=membership1.user_id,
            organization=org1,
        )
        return calendar

    @pytest.fixture
    def calendar2(self, org2, membership2):
        """Create calendar in org2 owned by user2."""
        calendar = baker.make(
            Calendar, organization=org2, name="Org2 Calendar", external_id="org2-cal2"
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=membership2.user_id,
            organization=org2,
        )
        return calendar

    @pytest.fixture
    def calendar1_unowned(self, org1, membership1):
        """Create second calendar in org1 not owned by user1."""
        calendar = baker.make(
            Calendar,
            organization=org1,
            name="Org1 Calendar Unowned",
            external_id="org1-cal1-unowned",
        )
        other_user = baker.make(User, email="other@example.com")
        other_membership = baker.make(
            OrganizationMembership, user=other_user, organization=org1, is_active=True
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=other_membership.user_id,
            organization=org1,
        )
        return calendar

    @pytest.fixture
    def event1(self, org1, calendar1):
        """Create event in org1 on owned calendar."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        return baker.make(
            CalendarEvent,
            organization=org1,
            calendar_fk=calendar1,
            start_time_tz_unaware=base_dt,
            end_time_tz_unaware=base_dt + datetime.timedelta(hours=1),
            timezone="UTC",
            title="Org1 Event",
            external_id="event-org1-owned",
        )

    @pytest.fixture
    def event1_unowned(self, org1, calendar1_unowned):
        """Create event in org1 on unowned calendar."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        return baker.make(
            CalendarEvent,
            organization=org1,
            calendar_fk=calendar1_unowned,
            start_time_tz_unaware=base_dt,
            end_time_tz_unaware=base_dt + datetime.timedelta(hours=1),
            timezone="UTC",
            title="Org1 Unowned Event",
            external_id="event-org1-unowned-scoped",
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
            external_id="event-org2-scoped",
        )

    @pytest.fixture
    def system_user1(self, membership1):
        """Create scoped system user for org1."""
        return baker.make(
            SystemUser,
            scoped_to_membership_user_id=membership1.user_id,
            organization=membership1.organization,
        )

    def test_scoped_filter_excludes_unowned_calendars(
        self, org1, event1, event1_unowned, system_user1
    ):
        """A scoped user's filter excludes events from calendars they don't own."""
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
        assert event1_unowned.id not in [e.id for e in events]

    def test_unscoped_filter_includes_all_org_calendars(self, org1, event1, event1_unowned):
        """Filter without system user includes events from all calendars in the org."""
        base_dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=base_dt,
            end_datetime=base_dt + datetime.timedelta(days=30),
        )

        qs = CalendarEvent.objects.filter_by_organization(org1.id)
        filtered_qs = filter_input.apply(qs, org1.id, system_user=None)

        events = sorted(list(filtered_qs), key=lambda e: e.id)
        assert len(events) == 2
        assert {events[0].id, events[1].id} == {event1.id, event1_unowned.id}
