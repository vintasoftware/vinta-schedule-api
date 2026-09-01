import pytest
from model_bakery import baker

from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import Calendar, CalendarPool, CalendarPoolMembership


@pytest.mark.django_db
class TestCalendarPoolOnlyMemberOf:
    """Tests for CalendarPoolQuerySet.only_member_of, the pool analogue of
    CalendarGroupQuerySet.only_member_of."""

    def setup_method(self):
        self.organization = baker.make("organizations.Organization")
        self.user = baker.make("users.User")
        self.other_user = baker.make("users.User")

        self.owned_calendar = baker.make(
            Calendar, organization=self.organization, external_id="cal-owned"
        )
        self.other_calendar = baker.make(
            Calendar, organization=self.organization, external_id="cal-other"
        )
        self.unowned_calendar = baker.make(
            Calendar, organization=self.organization, external_id="cal-unowned"
        )

        create_calendar_ownership(calendar=self.owned_calendar, user=self.user)
        create_calendar_ownership(calendar=self.other_calendar, user=self.user)
        create_calendar_ownership(calendar=self.unowned_calendar, user=self.other_user)

    def _only_member_of(self, user):
        return list(
            CalendarPool.objects.filter_by_organization(self.organization.id).only_member_of(
                user.id
            )
        )

    def test_returns_pool_with_owned_calendar(self):
        pool = CalendarPool.objects.create(organization=self.organization, name="Nurses")
        CalendarPoolMembership.objects.create(
            organization=self.organization, pool=pool, calendar=self.owned_calendar
        )

        assert self._only_member_of(self.user) == [pool]

    def test_excludes_pool_without_owned_calendar(self):
        pool = CalendarPool.objects.create(organization=self.organization, name="Rooms")
        CalendarPoolMembership.objects.create(
            organization=self.organization, pool=pool, calendar=self.unowned_calendar
        )

        assert self._only_member_of(self.user) == []

    def test_dedupes_when_user_owns_several_calendars_in_one_pool(self):
        pool = CalendarPool.objects.create(organization=self.organization, name="Nurses")
        CalendarPoolMembership.objects.create(
            organization=self.organization, pool=pool, calendar=self.owned_calendar
        )
        CalendarPoolMembership.objects.create(
            organization=self.organization, pool=pool, calendar=self.other_calendar
        )

        result = self._only_member_of(self.user)
        assert result == [pool]
        assert len(result) == 1

    def test_pool_with_no_calendars_excluded(self):
        CalendarPool.objects.create(organization=self.organization, name="Empty")

        assert self._only_member_of(self.user) == []

    def test_other_users_calendar_does_not_leak_pool(self):
        pool = CalendarPool.objects.create(organization=self.organization, name="Rooms")
        CalendarPoolMembership.objects.create(
            organization=self.organization, pool=pool, calendar=self.unowned_calendar
        )

        assert self._only_member_of(self.other_user) == [pool]
        assert self._only_member_of(self.user) == []
