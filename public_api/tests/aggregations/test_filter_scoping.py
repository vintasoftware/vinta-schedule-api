import datetime

import pytest
from freezegun import freeze_time
from model_bakery import baker

from public_api.aggregations.filters import (
    AppointmentTypeAggregateFilterInput,
    AvailableTimeAggregateFilterInput,
    BlockedTimeAggregateFilterInput,
    CalendarAggregateFilterInput,
    CalendarEventAggregateFilterInput,
    CalendarPoolAggregateFilterInput,
)


@pytest.mark.django_db
class TestFilterScoping:
    """Test that aggregate filters respect organization and owner-scope boundaries."""

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_filter_respects_organization_scope(self):
        """Calendar filter respects organization boundaries."""
        org1 = baker.make("organizations.Organization")
        org2 = baker.make("organizations.Organization")

        cal1 = baker.make("calendar_integration.Calendar", organization=org1, external_id="cal1")
        cal2 = baker.make("calendar_integration.Calendar", organization=org2, external_id="cal2")

        # Create a system user for org1
        system_user = baker.make("public_api.SystemUser", organization=org1)

        # Apply filter with system_user scoped to org1
        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(system_user=system_user, organization=org1)

        # Should only return org1's calendar
        assert cal1 in list(qs)
        assert cal2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_filter_with_scoped_token_limits_to_owner_calendars(self):
        """Calendar filter with scoped token only shows owned calendars."""
        org = baker.make("organizations.Organization")

        user1 = baker.make("users.User")
        user2 = baker.make("users.User")

        membership1 = baker.make(
            "organizations.OrganizationMembership",
            organization=org,
            user=user1,
            is_active=True,
        )
        membership2 = baker.make(
            "organizations.OrganizationMembership",
            organization=org,
            user=user2,
            is_active=True,
        )

        cal1 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal1")
        cal2 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal2")

        baker.make(
            "calendar_integration.CalendarOwnership",
            calendar=cal1,
            membership=membership1,
        )
        baker.make(
            "calendar_integration.CalendarOwnership",
            calendar=cal2,
            membership=membership2,
        )

        # Create a scoped system user limited to user1
        system_user = baker.make(
            "public_api.SystemUser",
            organization=org,
            scoped_to_membership_user_id=user1.id,
        )

        # Apply filter with scoped system user
        filter_input = CalendarAggregateFilterInput()
        qs = filter_input.apply(system_user=system_user, organization=org)

        # Should only return cal1
        assert cal1 in list(qs)
        assert cal2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_event_filter_respects_organization_scope(self):
        """CalendarEvent filter applies organization scoping through querysets."""
        org1 = baker.make("organizations.Organization")
        org2 = baker.make("organizations.Organization")

        # Create calendars in both organizations
        cal1 = baker.make("calendar_integration.Calendar", organization=org1)
        cal2 = baker.make("calendar_integration.Calendar", organization=org2)

        # Create events in both organizations with unique external_ids
        event1 = baker.make(
            "calendar_integration.CalendarEvent",
            organization=org1,
            calendar=cal1,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
            external_id="event1",
        )
        event2 = baker.make(
            "calendar_integration.CalendarEvent",
            organization=org2,
            calendar=cal2,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
            external_id="event2",
        )

        # Create a scoped system user for org1
        system_user = baker.make("public_api.SystemUser", organization=org1)

        # Apply filter with system_user scoped to org1
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(start_datetime=start, end_datetime=end)
        qs = filter_input.apply(system_user=system_user, organization=org1)

        # The queryset should be organization-scoped
        # Verify by checking the model and organization filtering
        assert qs.model.__name__ == "CalendarEvent"
        # Ensure that only org1's events are returned, org2's are excluded
        assert event1 in list(qs)
        assert event2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_event_filter_with_scoped_token_and_specific_calendar(self):
        """CalendarEvent filter respects calendar ownership scope."""
        org = baker.make("organizations.Organization")

        user1 = baker.make("users.User")
        user2 = baker.make("users.User")

        membership1 = baker.make(
            "organizations.OrganizationMembership",
            organization=org,
            user=user1,
            is_active=True,
        )
        membership2 = baker.make(
            "organizations.OrganizationMembership",
            organization=org,
            user=user2,
            is_active=True,
        )

        cal1 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal1")
        cal2 = baker.make("calendar_integration.Calendar", organization=org, external_id="cal2")

        baker.make(
            "calendar_integration.CalendarOwnership",
            calendar=cal1,
            membership=membership1,
        )
        baker.make(
            "calendar_integration.CalendarOwnership",
            calendar=cal2,
            membership=membership2,
        )

        # Create events in both calendars within the time window
        event1 = baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=cal1,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
            external_id="event1",
        )
        event2 = baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=cal2,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
            external_id="event2",
        )

        # Create a scoped system user limited to user1
        system_user = baker.make(
            "public_api.SystemUser",
            organization=org,
            scoped_to_membership_user_id=user1.id,
        )

        # Try to access cal2 (not in scope) explicitly - should fail closed
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC)
        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=start, end_datetime=end, calendar_id=cal2.id
        )
        qs = filter_input.apply(system_user=system_user, organization=org)
        assert list(qs) == []

        # Unfiltered scoped request should return only cal1's event
        filter_input = CalendarEventAggregateFilterInput(start_datetime=start, end_datetime=end)
        qs = filter_input.apply(system_user=system_user, organization=org)
        assert event1 in list(qs)
        assert event2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_available_time_filter_respects_organization_scope(self):
        """AvailableTime filter respects organization and temporal boundaries."""
        org1 = baker.make("organizations.Organization")
        org2 = baker.make("organizations.Organization")

        cal1 = baker.make("calendar_integration.Calendar", organization=org1)
        cal2 = baker.make("calendar_integration.Calendar", organization=org2)

        available1 = baker.make(
            "calendar_integration.AvailableTime",
            organization=org1,
            calendar=cal1,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
        )
        available2 = baker.make(
            "calendar_integration.AvailableTime",
            organization=org2,
            calendar=cal2,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
        )

        system_user = baker.make("public_api.SystemUser", organization=org1)
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC)
        filter_input = AvailableTimeAggregateFilterInput(start_datetime=start, end_datetime=end)
        qs = filter_input.apply(system_user=system_user, organization=org1)

        assert available1 in list(qs)
        assert available2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_blocked_time_filter_respects_organization_scope(self):
        """BlockedTime filter respects organization and temporal boundaries."""
        org1 = baker.make("organizations.Organization")
        org2 = baker.make("organizations.Organization")

        cal1 = baker.make("calendar_integration.Calendar", organization=org1)
        cal2 = baker.make("calendar_integration.Calendar", organization=org2)

        blocked1 = baker.make(
            "calendar_integration.BlockedTime",
            organization=org1,
            calendar=cal1,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
        )
        blocked2 = baker.make(
            "calendar_integration.BlockedTime",
            organization=org2,
            calendar=cal2,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5),
            timezone="UTC",
        )

        system_user = baker.make("public_api.SystemUser", organization=org1)
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC)
        filter_input = BlockedTimeAggregateFilterInput(start_datetime=start, end_datetime=end)
        qs = filter_input.apply(system_user=system_user, organization=org1)

        assert blocked1 in list(qs)
        assert blocked2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_appointment_type_filter_respects_organization_scope(self):
        """AppointmentType filter respects organization scope."""
        org1 = baker.make("organizations.Organization")
        org2 = baker.make("organizations.Organization")

        apt1 = baker.make(
            "calendar_integration.AppointmentType",
            organization=org1,
            accepts_public_scheduling=True,
        )
        apt2 = baker.make(
            "calendar_integration.AppointmentType",
            organization=org2,
            accepts_public_scheduling=True,
        )

        system_user = baker.make("public_api.SystemUser", organization=org1)
        filter_input = AppointmentTypeAggregateFilterInput(accepts_public_scheduling=True)
        qs = filter_input.apply(system_user=system_user, organization=org1)

        assert apt1 in list(qs)
        assert apt2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_appointment_type_filter_by_public_scheduling(self):
        """AppointmentType filter narrows by accepts_public_scheduling."""
        org = baker.make("organizations.Organization")

        apt_public = baker.make(
            "calendar_integration.AppointmentType",
            organization=org,
            accepts_public_scheduling=True,
        )
        apt_private = baker.make(
            "calendar_integration.AppointmentType",
            organization=org,
            accepts_public_scheduling=False,
        )

        system_user = baker.make("public_api.SystemUser", organization=org)

        # Filter for public appointment types
        filter_input = AppointmentTypeAggregateFilterInput(accepts_public_scheduling=True)
        qs = filter_input.apply(system_user=system_user, organization=org)
        assert apt_public in list(qs)
        assert apt_private not in list(qs)

        # Filter for private appointment types
        filter_input = AppointmentTypeAggregateFilterInput(accepts_public_scheduling=False)
        qs = filter_input.apply(system_user=system_user, organization=org)
        assert apt_public not in list(qs)
        assert apt_private in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_pool_filter_respects_organization_scope(self):
        """CalendarPool filter respects organization scope."""
        org1 = baker.make("organizations.Organization")
        org2 = baker.make("organizations.Organization")

        pool1 = baker.make("calendar_integration.CalendarPool", organization=org1, name="Pool1")
        pool2 = baker.make("calendar_integration.CalendarPool", organization=org2, name="Pool2")

        system_user = baker.make("public_api.SystemUser", organization=org1)
        filter_input = CalendarPoolAggregateFilterInput()
        qs = filter_input.apply(system_user=system_user, organization=org1)

        assert pool1 in list(qs)
        assert pool2 not in list(qs)

    @freeze_time("2026-01-15 12:00:00 UTC")
    def test_calendar_pool_filter_by_name(self):
        """CalendarPool filter narrows by name substring."""
        org = baker.make("organizations.Organization")

        pool_team = baker.make("calendar_integration.CalendarPool", organization=org, name="Team A")
        pool_room = baker.make(
            "calendar_integration.CalendarPool", organization=org, name="Conference Room B"
        )
        pool_other = baker.make("calendar_integration.CalendarPool", organization=org, name="Other")

        system_user = baker.make("public_api.SystemUser", organization=org)

        # Filter for pools with "Room" in name
        filter_input = CalendarPoolAggregateFilterInput(name_contains="Room")
        qs = filter_input.apply(system_user=system_user, organization=org)
        assert pool_team not in list(qs)
        assert pool_room in list(qs)
        assert pool_other not in list(qs)
