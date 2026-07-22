"""Per-counter semantics for ``EntitlementService.get_current_usage``.

``test_every_limited_resource_has_a_counter`` proves each ``LimitedResource``
member is *registered*. It says nothing about whether the registered counter counts
the right rows — and for the two counters whose semantics were not obvious
(``availability_windows`` and ``public_api_system_users``), getting that wrong is an
over-report, and an over-report is a lockout *below* real usage.

The availability tests deliberately drive the **real** ``AvailabilityService``
rather than hand-building ``AvailableTime`` rows: the whole defect was that editing
a recurring window silently *inserts* rows, which a hand-built fixture would never
reproduce.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.utils import timezone

import pytest

from audit.services import AuditService
from calendar_integration.constants import CalendarProvider
from calendar_integration.models import AvailableTime, Calendar
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.recurrence_manager import RecurrenceManager
from calendar_integration.tests.services.test_availability_service import FakeHost
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import LimitedResource
from payments.services.entitlement_service import EntitlementService
from public_api.models import SystemUser
from users.models import Profile, User


@pytest.fixture
def entitlement_service() -> EntitlementService:
    return EntitlementService()


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Usage Counter Org")


@pytest.fixture
def user(db: Any, organization: Organization) -> User:
    account = User.objects.create_user(email="usage_counters@example.com", password="pass")
    Profile.objects.create(user=account)
    OrganizationMembership.objects.create(
        user=account, organization=organization, role=OrganizationRole.ADMIN
    )
    return account


@pytest.fixture
def audit_service() -> AuditService:
    from di_core.containers import container

    assert container is not None
    return container.audit_service()


@pytest.fixture
def managed_calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Usage Counter Calendar",
        external_id="usage_counter_cal",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
        manage_available_windows=True,
    )


@pytest.fixture
def availability_service(
    organization: Organization, user: User, audit_service: AuditService
) -> AvailabilityService:
    context = CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
        audit_service=audit_service,
    )
    return AvailabilityService(
        context=context,
        recurrence_manager=RecurrenceManager(),
        host=FakeHost(organization=organization),
    )


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
class TestAvailabilityWindowCounter:
    def test_a_plain_window_counts_once(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_a_recurring_window_counts_once_regardless_of_its_occurrences(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """Occurrences are computed in Postgres, not stored, so this is the baseline
        the modified-occurrence case below is measured against."""
        availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_editing_one_occurrence_does_not_add_a_window(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """``create_recurring_available_time_exception`` implements "edit this one
        occurrence" by calling ``create_available_time`` — i.e. by **inserting a
        second row**. Counting every ``AvailableTime`` row therefore reported 2 for
        one window the user created. An organization on a limit of 5 that created 3
        recurring windows and edited 3 occurrences would read as 6 and be blocked
        from creating its 4th, which is a lockout *below* its real usage.
        """
        parent = availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_available_time_exception(
            parent_available_time=parent,
            exception_date=datetime.date(2025, 7, 3),
            modified_start_time=_utc(2025, 7, 3, 14),
            modified_end_time=_utc(2025, 7, 3, 16),
            is_cancelled=False,
        )

        # The extra row genuinely exists -- this is not a test that passes because
        # the edit did nothing.
        assert AvailableTime.objects.filter(organization_id=organization.pk).count() > 1, (
            "Expected the modified occurrence to have inserted a derived row."
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        ), (
            "A window whose occurrence was edited must still count as one window. "
            "The counter is counting recurrence-derived rows."
        )

    def test_cancelling_one_occurrence_does_not_add_a_window(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        parent = availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_available_time_exception(
            parent_available_time=parent,
            exception_date=datetime.date(2025, 7, 3),
            is_cancelled=True,
        )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_splitting_a_series_does_not_add_a_window(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """A bulk modification splits the series and inserts a continuation row,
        linked by ``bulk_modification_parent``. Still one window to the user."""
        parent = availability_service.create_available_time(
            calendar=managed_calendar,
            start_time=_utc(2025, 7, 1, 10),
            end_time=_utc(2025, 7, 1, 12),
            timezone="UTC",
            rrule_string="RRULE:FREQ=DAILY;COUNT=10",
        )

        availability_service.create_recurring_available_time_bulk_modification(
            parent_available_time=parent,
            modification_start_date=_utc(2025, 7, 5, 10),
            modified_start_time_offset=datetime.timedelta(hours=4),
            modified_end_time_offset=datetime.timedelta(hours=4),
        )

        assert AvailableTime.objects.filter(organization_id=organization.pk).count() > 1, (
            "Expected the bulk modification to have inserted a continuation row."
        )
        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 1
        )

    def test_two_independent_windows_count_twice(
        self, entitlement_service, availability_service, managed_calendar, organization
    ):
        """The exclusion must not swallow genuinely separate windows."""
        for hour in (10, 14):
            availability_service.create_available_time(
                calendar=managed_calendar,
                start_time=_utc(2025, 7, 1, hour),
                end_time=_utc(2025, 7, 1, hour + 1),
                timezone="UTC",
            )

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.AVAILABILITY_WINDOWS
            )
            == 2
        )


@pytest.mark.django_db
class TestPublicApiSystemUserCounter:
    def _make_system_user(self, organization, suffix, **kwargs):
        return SystemUser.objects.create(
            organization=organization,
            integration_name=f"integration-{suffix}",
            long_lived_token_hash=f"hash-{suffix}",
            **kwargs,
        )

    def test_counts_only_live_system_users(self, entitlement_service, organization: Organization):
        """Both off-switches free capacity. ``is_active=False`` is a revoked token
        and ``deleted_at`` is the soft delete; a token in either state can no longer
        authenticate, so charging for it would make revoking one pointless."""
        self._make_system_user(organization, "live", is_active=True, deleted_at=None)
        self._make_system_user(organization, "revoked", is_active=False, deleted_at=None)
        self._make_system_user(organization, "deleted", is_active=True, deleted_at=timezone.now())

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
            )
            == 1
        )

    def test_another_organizations_system_users_do_not_leak_in(
        self, entitlement_service, organization: Organization
    ):
        other = Organization.objects.create(name="Someone Else")
        self._make_system_user(organization, "mine")
        self._make_system_user(other, "theirs")

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
            )
            == 1
        )

    def test_an_organizationless_system_user_is_invisible(
        self, entitlement_service, organization: Organization
    ):
        """``SystemUser.organization`` is nullable. Such a token belongs to no
        billing root, so it consumes nobody's capacity — correct for pooling, but it
        does mean it is entirely unmetered. Pinned here so that whoever makes the
        column non-nullable has to revisit this deliberately."""
        self._make_system_user(None, "orphan")

        assert (
            entitlement_service.get_current_usage(
                organization, LimitedResource.PUBLIC_API_SYSTEM_USERS
            )
            == 0
        )
