"""Integration tests for aggregate filter scoping.

Verifies that filters correctly apply organization and owner-scope filtering,
and that a scoped system user's filter never returns rows outside their
calendar scope.
"""

import datetime

from django.utils import timezone as tz

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from public_api.aggregations import (
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
)
from public_api.models import SystemUser


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.mark.django_db
class TestCalendarEventFilterOrganizationScoping:
    """Tests that CalendarEvent filters respect organization boundaries."""

    def test_filter_excludes_events_from_other_organizations(self, organization):
        """Filter applied to one org never returns events from another org."""
        org1 = organization
        org2 = baker.make("organizations.Organization")

        start = tz.now()
        end = start + datetime.timedelta(days=30)
        event_start = start + datetime.timedelta(hours=1)
        event_end = event_start + datetime.timedelta(hours=1)

        # Create calendar and event in org1
        calendar1 = baker.make("calendar_integration.Calendar", organization=org1, external_id="cal1")
        event1 = baker.make(
            "calendar_integration.CalendarEvent",
            calendar_fk=calendar1,
            organization=org1,
            external_id="event1",
            title="Event in org1",
            start_time_tz_unaware=event_start,
            end_time_tz_unaware=event_end,
            timezone="UTC",
        )

        # Create calendar and event in org2
        calendar2 = baker.make("calendar_integration.Calendar", organization=org2, external_id="cal2")
        event2 = baker.make(
            "calendar_integration.CalendarEvent",
            calendar_fk=calendar2,
            organization=org2,
            external_id="event2",
            title="Event in org2",
            start_time_tz_unaware=event_start,
            end_time_tz_unaware=event_end,
            timezone="UTC",
        )

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
        )
        qs = filter_input.apply(None, org1)

        event_ids = set(qs.values_list("id", flat=True))
        assert event1.id in event_ids
        assert event2.id not in event_ids

    def test_filter_respects_calendar_filter_across_orgs(self, organization):
        """Calendar ID filter is scoped by organization, preventing cross-org access."""
        org1 = organization
        org2 = baker.make("organizations.Organization")

        start = tz.now()
        end = start + datetime.timedelta(days=30)
        event_start = start + datetime.timedelta(hours=1)
        event_end = event_start + datetime.timedelta(hours=1)

        calendar1 = baker.make("calendar_integration.Calendar", organization=org1, external_id="cal1")
        calendar2 = baker.make("calendar_integration.Calendar", organization=org2, external_id="cal2")

        # Create events in both calendars
        event1 = baker.make(
            "calendar_integration.CalendarEvent",
            calendar_fk=calendar1,
            organization=org1,
            external_id="event1",
            title="Event in org1",
            start_time_tz_unaware=event_start,
            end_time_tz_unaware=event_end,
            timezone="UTC",
        )
        event2 = baker.make(
            "calendar_integration.CalendarEvent",
            calendar_fk=calendar2,
            organization=org2,
            external_id="event2",
            title="Event in org2",
            start_time_tz_unaware=event_start,
            end_time_tz_unaware=event_end,
            timezone="UTC",
        )

        # Filter for org1 with calendar1's ID - should get event1
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
            calendar_id=calendar1.id,
        )
        qs = filter_input.apply(None, org1)
        event_ids = set(qs.values_list("id", flat=True))
        assert event1.id in event_ids
        assert event2.id not in event_ids

        # Filter for org1 with calendar2's ID (from org2) should return nothing
        # because the organization filter will exclude it
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start,
            end_datetime=end,
            calendar_id=calendar2.id,
        )
        qs = filter_input.apply(None, org1)
        assert not qs.exists()


@pytest.mark.django_db
class TestCalendarFilterOwnerScoping:
    """Tests that Calendar filters respect owner-scope for scoped system users."""

    def test_org_wide_token_sees_all_calendars(self, organization):
        """An org-wide system user sees all calendars in the organization."""
        org = organization

        # Create multiple calendars
        calendar1 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal1")
        calendar2 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal2")

        # Create an org-wide system user (no scoping)
        system_user = baker.make(
            SystemUser,
            organization=org,
            scoped_to_membership_user_id=None,  # org-wide
        )

        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(system_user, org)

        calendar_ids = set(qs.values_list("id", flat=True))
        assert calendar1.id in calendar_ids
        assert calendar2.id in calendar_ids

    def test_scoped_token_sees_only_owned_calendars(self, organization):
        """A scoped system user sees only calendars they own."""
        org = organization

        # Create two users with different memberships
        user1 = baker.make("users.User")
        user2 = baker.make("users.User")

        membership1 = baker.make(
            OrganizationMembership,
            user=user1,
            organization=org,
            is_active=True,
        )
        membership2 = baker.make(
            OrganizationMembership,
            user=user2,
            organization=org,
            is_active=True,
        )

        # Create calendars owned by different users
        calendar1 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal1")
        calendar1.ownerships.create(membership=membership1, organization=org)

        calendar2 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal2")
        calendar2.ownerships.create(membership=membership2, organization=org)

        # Create a system user scoped to user1
        system_user = baker.make(
            SystemUser,
            organization=org,
            scoped_to_membership_user_id=user1.id,
        )

        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(system_user, org)

        calendar_ids = set(qs.values_list("id", flat=True))
        assert calendar1.id in calendar_ids
        assert calendar2.id not in calendar_ids

    def test_scoped_token_with_inactive_membership_sees_nothing(self, organization):
        """A scoped token whose membership is inactive sees no calendars."""
        org = organization

        user1 = baker.make("users.User")

        # Create an inactive membership
        membership1 = baker.make(
            OrganizationMembership,
            user=user1,
            organization=org,
            is_active=False,
        )

        calendar1 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal1")
        calendar1.ownerships.create(membership=membership1, organization=org)

        # Create a system user scoped to user1
        system_user = baker.make(
            SystemUser,
            organization=org,
            scoped_to_membership_user_id=user1.id,
        )

        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(system_user, org)

        # Should be empty because the membership is inactive
        assert not qs.exists()
