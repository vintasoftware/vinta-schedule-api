"""Integration tests for aggregate filter scoping.

Covers:
- A scoped system user's filter never returns rows outside its calendar scope
- Filters maintain organization isolation (multi-tenant verification)
"""

import datetime

from django.utils import timezone as tz

import pytest
from model_bakery import baker

from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
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


@pytest.fixture
def auth_service():
    from di_core.containers import container

    return container.public_api_auth_service()


@pytest.fixture
def organization():
    return Organization.objects.create(name="Primary Org")


@pytest.fixture
def other_organization():
    return Organization.objects.create(name="Other Org")


@pytest.fixture
def primary_calendar(organization):
    return baker.make(
        Calendar, organization=organization, name="Primary Calendar", external_id="primary-cal"
    )


@pytest.fixture
def other_calendar(other_organization):
    return baker.make(
        Calendar, organization=other_organization, name="Other Calendar", external_id="other-cal"
    )


@pytest.fixture
def primary_appointment_type(organization):
    return baker.make(AppointmentType, organization=organization, name="Primary AT")


@pytest.fixture
def other_appointment_type(other_organization):
    return baker.make(AppointmentType, organization=other_organization, name="Other AT")


@pytest.fixture
def primary_calendar_pool(organization):
    return baker.make(CalendarPool, organization=organization, name="Primary Pool")


@pytest.fixture
def other_calendar_pool(other_organization):
    return baker.make(CalendarPool, organization=other_organization, name="Other Pool")


@pytest.mark.django_db
class TestCalendarEventFilterScoping:
    """Tests for CalendarEvent filter organization isolation."""

    def test_filter_excludes_other_org_events(
        self, organization, other_organization, primary_calendar, other_calendar
    ):
        """A filter on primary org never returns events from other org."""
        now = tz.now()
        later = now + datetime.timedelta(days=10)

        baker.make(
            CalendarEvent,
            calendar=primary_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
            external_id="primary-event",
        )
        other_event = baker.make(
            CalendarEvent,
            calendar=other_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
            external_id="other-event",
        )

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )

        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert other_event not in filtered_qs
        assert filtered_qs.count() == 1

    def test_filter_includes_matching_primary_org_events(self, organization, primary_calendar):
        """A filter returns matching events from its own organization."""
        now = tz.now()
        later = now + datetime.timedelta(days=10)

        event = baker.make(
            CalendarEvent,
            calendar=primary_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
            external_id="test-event",
        )

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )

        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert event in filtered_qs
        assert filtered_qs.count() == 1


@pytest.mark.django_db
class TestAvailableTimeFilterScoping:
    """Tests for AvailableTime filter organization isolation."""

    def test_filter_excludes_other_org_available_times(
        self, organization, other_organization, primary_calendar, other_calendar
    ):
        """A filter on primary org never returns AvailableTime from other org."""
        now = tz.now()
        later = now + datetime.timedelta(days=10)

        baker.make(
            AvailableTime,
            calendar=primary_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
        )
        other_at = baker.make(
            AvailableTime,
            calendar=other_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
        )

        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )

        qs = AvailableTime.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert other_at not in filtered_qs
        assert filtered_qs.count() == 1


@pytest.mark.django_db
class TestBlockedTimeFilterScoping:
    """Tests for BlockedTime filter organization isolation."""

    def test_filter_excludes_other_org_blocked_times(
        self, organization, other_organization, primary_calendar, other_calendar
    ):
        """A filter on primary org never returns BlockedTime from other org."""
        now = tz.now()
        later = now + datetime.timedelta(days=10)

        baker.make(
            BlockedTime,
            calendar=primary_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
        )
        other_bt = baker.make(
            BlockedTime,
            calendar=other_calendar,
            start_time_tz_unaware=now + datetime.timedelta(days=2),
            timezone="UTC",
        )

        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )

        qs = BlockedTime.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert other_bt not in filtered_qs
        assert filtered_qs.count() == 1


@pytest.mark.django_db
class TestAppointmentTypeFilterScoping:
    """Tests for AppointmentType filter organization isolation."""

    def test_filter_excludes_other_org_appointment_types(
        self, organization, other_organization, primary_appointment_type, other_appointment_type
    ):
        """A filter on primary org never returns AppointmentType from other org."""
        filter_input = AppointmentTypeAggregateFilterInput()

        qs = AppointmentType.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert other_appointment_type not in filtered_qs
        assert primary_appointment_type in filtered_qs


@pytest.mark.django_db
class TestCalendarFilterScoping:
    """Tests for Calendar filter organization isolation."""

    def test_filter_excludes_other_org_calendars(
        self, organization, other_organization, primary_calendar, other_calendar
    ):
        """A filter on primary org never returns Calendar from other org."""
        filter_input = CalendarAggregateFilterInput()

        qs = Calendar.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert other_calendar not in filtered_qs
        assert primary_calendar in filtered_qs


@pytest.mark.django_db
class TestCalendarPoolFilterScoping:
    """Tests for CalendarPool filter organization isolation."""

    def test_filter_excludes_other_org_pools(
        self, organization, other_organization, primary_calendar_pool, other_calendar_pool
    ):
        """A filter on primary org never returns CalendarPool from other org."""
        filter_input = CalendarPoolAggregateFilterInput()

        qs = CalendarPool.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization)

        assert other_calendar_pool not in filtered_qs
        assert primary_calendar_pool in filtered_qs


@pytest.mark.django_db
class TestScopedSystemUserFiltering:
    """Tests for scoped system user token filtering."""

    def test_scoped_calendar_filter_respects_calendar_ownership(
        self, auth_service, organization, primary_calendar
    ):
        """A scoped token can only see calendars it owns."""
        from calendar_integration.models import CalendarOwnership
        from users.models import User

        user = baker.make(User)
        membership = OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            is_active=True,
        )

        other_calendar = baker.make(
            Calendar, organization=organization, name="Other", external_id="other-scoped-cal"
        )

        CalendarOwnership.objects.create(
            calendar=primary_calendar,
            membership=membership,
        )

        system_user, _ = auth_service.create_system_user(
            integration_name="scoped-test",
            organization=organization,
            scoped_to_membership=membership,
            bypass_limits=True,
        )

        filter_input = CalendarAggregateFilterInput()
        qs = Calendar.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization, system_user)

        assert primary_calendar in filtered_qs
        assert other_calendar not in filtered_qs

    def test_scoped_calendar_event_filter_respects_calendar_ownership(
        self, auth_service, organization, primary_calendar
    ):
        """A scoped token's CalendarEvent filter only sees owned calendars."""
        from calendar_integration.models import CalendarOwnership
        from users.models import User

        now = tz.now()
        later = now + datetime.timedelta(days=10)
        event_time = now + datetime.timedelta(days=2)

        user = baker.make(User)
        membership = OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            is_active=True,
        )

        other_calendar = baker.make(
            Calendar, organization=organization, name="Other", external_id="other-scoped-cal"
        )

        owned_event = baker.make(
            CalendarEvent,
            calendar=primary_calendar,
            start_time_tz_unaware=event_time,
            timezone="UTC",
            external_id="owned-event",
        )
        unowned_event = baker.make(
            CalendarEvent,
            calendar=other_calendar,
            start_time_tz_unaware=event_time,
            timezone="UTC",
            external_id="unowned-event",
        )

        CalendarOwnership.objects.create(
            calendar=primary_calendar,
            membership=membership,
        )

        system_user, _ = auth_service.create_system_user(
            integration_name="scoped-test",
            organization=organization,
            scoped_to_membership=membership,
            bypass_limits=True,
        )

        filter_input = CalendarEventAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )
        qs = CalendarEvent.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization, system_user)

        assert owned_event in filtered_qs
        assert unowned_event not in filtered_qs

    def test_scoped_available_time_filter_respects_calendar_ownership(
        self, auth_service, organization, primary_calendar
    ):
        """A scoped token's AvailableTime filter only sees owned calendars."""
        from calendar_integration.models import CalendarOwnership
        from users.models import User

        now = tz.now()
        later = now + datetime.timedelta(days=10)
        event_time = now + datetime.timedelta(days=2)

        user = baker.make(User)
        membership = OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            is_active=True,
        )

        other_calendar = baker.make(
            Calendar, organization=organization, name="Other", external_id="other-scoped-cal"
        )

        owned_at = baker.make(
            AvailableTime,
            calendar=primary_calendar,
            start_time_tz_unaware=event_time,
            timezone="UTC",
        )
        unowned_at = baker.make(
            AvailableTime,
            calendar=other_calendar,
            start_time_tz_unaware=event_time,
            timezone="UTC",
        )

        CalendarOwnership.objects.create(
            calendar=primary_calendar,
            membership=membership,
        )

        system_user, _ = auth_service.create_system_user(
            integration_name="scoped-test",
            organization=organization,
            scoped_to_membership=membership,
            bypass_limits=True,
        )

        filter_input = AvailableTimeAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )
        qs = AvailableTime.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization, system_user)

        assert owned_at in filtered_qs
        assert unowned_at not in filtered_qs

    def test_scoped_blocked_time_filter_respects_calendar_ownership(
        self, auth_service, organization, primary_calendar
    ):
        """A scoped token's BlockedTime filter only sees owned calendars."""
        from calendar_integration.models import CalendarOwnership
        from users.models import User

        now = tz.now()
        later = now + datetime.timedelta(days=10)
        event_time = now + datetime.timedelta(days=2)

        user = baker.make(User)
        membership = OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            is_active=True,
        )

        other_calendar = baker.make(
            Calendar, organization=organization, name="Other", external_id="other-scoped-cal"
        )

        owned_bt = baker.make(
            BlockedTime,
            calendar=primary_calendar,
            start_time_tz_unaware=event_time,
            timezone="UTC",
        )
        unowned_bt = baker.make(
            BlockedTime,
            calendar=other_calendar,
            start_time_tz_unaware=event_time,
            timezone="UTC",
        )

        CalendarOwnership.objects.create(
            calendar=primary_calendar,
            membership=membership,
        )

        system_user, _ = auth_service.create_system_user(
            integration_name="scoped-test",
            organization=organization,
            scoped_to_membership=membership,
            bypass_limits=True,
        )

        filter_input = BlockedTimeAggregateFilterInput(
            start_datetime=now,
            end_datetime=later,
        )
        qs = BlockedTime.objects.filter_by_organization(organization.id)
        filtered_qs = filter_input.apply(qs, organization, system_user)

        assert owned_bt in filtered_qs
        assert unowned_bt not in filtered_qs
