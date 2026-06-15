"""Tests for calendar_service_utils module-level helpers.

Covers:
- timezone conversion equivalence with the original CalendarService method.
- event-serialization equivalence (serialize_event / serialize_event_internal_attendee /
  serialize_event_external_attendee).
- Regression test for the multi-tenant lru_cache bug in the calendar lookups:
  a single shared cache must never return org A's Calendar when queried for org B.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, CalendarEvent, EventAttendance
from calendar_integration.services.calendar_service_utils import (
    convert_naive_utc_datetime_to_timezone,
    get_calendar_by_external_id,
    get_calendar_by_id,
    serialize_event_data_input,
    serialize_event_internal_attendee,
)
from calendar_integration.services.dataclasses import (
    CalendarEventData,
    CalendarEventInputData,
    ResourceAllocationInputData,
)
from organizations.models import Organization
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    """Primary test organization."""
    return Organization.objects.create(name="Utils Test Org A")


@pytest.fixture
def organization_b(db):
    """Secondary test organization for multi-tenant checks."""
    return Organization.objects.create(name="Utils Test Org B")


@pytest.fixture
def calendar_a(db, organization):
    """A calendar belonging to org A with a specific external_id."""
    return Calendar.objects.create(
        name="Org A Calendar",
        external_id="ext_cal_org_a",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def calendar_b(db, organization_b):
    """A calendar belonging to org B — same external_id as calendar_a."""
    return Calendar.objects.create(
        name="Org B Calendar",
        external_id="ext_cal_org_b",
        provider=CalendarProvider.GOOGLE,
        organization=organization_b,
    )


@pytest.fixture
def calendar_same_ext_id_org_b(db, organization, organization_b):
    """Org B calendar that shares the same external_id as calendar_a — the key collision case."""
    return Calendar.objects.create(
        name="Org B Calendar (same ext_id)",
        external_id="ext_cal_org_a",  # intentionally same as calendar_a
        provider=CalendarProvider.GOOGLE,
        organization=organization_b,
    )


@pytest.fixture
def mock_adapter():
    """A minimal CalendarAdapter mock with provider=GOOGLE."""
    adapter = MagicMock()
    adapter.provider = CalendarProvider.GOOGLE
    return adapter


# ---------------------------------------------------------------------------
# Timezone conversion
# ---------------------------------------------------------------------------


class TestConvertNaiveUtcDatetimeToTimezone:
    """Tests for convert_naive_utc_datetime_to_timezone module function.

    The function must behave identically to the original CalendarService method
    it replaces.
    """

    def test_utc_stays_utc(self):
        dt = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        result = convert_naive_utc_datetime_to_timezone(dt, "UTC")
        # 12:00Z in UTC -> 12:00 local (naive), then re-tagged to UTC
        assert result.hour == 12
        assert result.tzinfo == datetime.UTC

    def test_converts_to_negative_offset_timezone(self):
        """12:00Z in America/Recife (UTC-3) should yield 09:00."""
        dt = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        result = convert_naive_utc_datetime_to_timezone(dt, "America/Recife")
        assert result.hour == 9
        assert result.tzinfo == datetime.UTC

    def test_converts_to_positive_offset_timezone(self):
        """12:00Z in Europe/Berlin (UTC+2 in summer) should yield 14:00."""
        dt = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        result = convert_naive_utc_datetime_to_timezone(dt, "Europe/Berlin")
        assert result.hour == 14
        assert result.tzinfo == datetime.UTC

    def test_naive_input_treated_as_utc(self):
        """A naive datetime must be treated as UTC, not shifted."""
        dt = datetime.datetime(2025, 6, 15, 12, 0, 0)  # no tzinfo
        result = convert_naive_utc_datetime_to_timezone(dt, "America/Recife")
        assert result.hour == 9  # same as the aware UTC version

    def test_invalid_iana_tz_raises_value_error(self):
        dt = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        with pytest.raises(ValueError, match="Invalid IANA timezone"):
            convert_naive_utc_datetime_to_timezone(dt, "Not/A/Timezone")


# ---------------------------------------------------------------------------
# Calendar lookup — multi-tenant regression test (the lru_cache bug)
# ---------------------------------------------------------------------------


class TestGetCalendarByExternalIdMultiTenant:
    """Regression test for the lru_cache multi-tenant bug.

    With @lru_cache(maxsize=128) on the instance method, the cache key was only
    the positional args (calendar_external_id).  When a service instance was
    reused across two organizations, the first org's Calendar was returned for
    the second org's query with the same external_id.

    The fix uses a dict keyed on (organization_id, external_id).  This test
    asserts the correct Calendar is returned for each org even when both have the
    same external_id value.
    """

    def test_same_external_id_different_orgs_return_correct_calendar(
        self, db, calendar_a, calendar_same_ext_id_org_b, organization, organization_b, mock_adapter
    ):
        """A shared cache must never return org A's Calendar for org B."""
        shared_cache: dict[tuple[int, str | int], Calendar] = {}

        # First lookup — org A
        result_a = get_calendar_by_external_id(
            shared_cache,
            calendar_external_id="ext_cal_org_a",
            organization=organization,
            calendar_adapter=mock_adapter,
        )
        assert result_a.id == calendar_a.id
        assert result_a.organization_id == organization.id

        # Second lookup — org B, same external_id as org A's calendar
        result_b = get_calendar_by_external_id(
            shared_cache,
            calendar_external_id="ext_cal_org_a",
            organization=organization_b,
            calendar_adapter=mock_adapter,
        )
        assert result_b.id == calendar_same_ext_id_org_b.id
        assert result_b.organization_id == organization_b.id

        # The two results must differ — this is the bug being prevented
        assert result_a.id != result_b.id

    def test_cache_is_populated_on_first_lookup(self, db, calendar_a, organization, mock_adapter):
        """After a lookup the cache entry must be present so the next call avoids a DB query."""
        cache: dict[tuple[int, str | int], Calendar] = {}
        assert len(cache) == 0

        get_calendar_by_external_id(
            cache,
            calendar_external_id="ext_cal_org_a",
            organization=organization,
            calendar_adapter=mock_adapter,
        )
        assert (organization.id, "ext_cal_org_a") in cache

    def test_cached_result_is_returned_on_second_lookup(
        self, db, calendar_a, organization, mock_adapter
    ):
        """The second call with the same (org, external_id) must return the cached object."""
        cache: dict[tuple[int, str | int], Calendar] = {}
        first = get_calendar_by_external_id(cache, "ext_cal_org_a", organization, mock_adapter)
        second = get_calendar_by_external_id(cache, "ext_cal_org_a", organization, mock_adapter)
        assert first is second  # identity — same object from cache


class TestGetCalendarByIdMultiTenant:
    """Regression test: get_calendar_by_id must key cache on (org_id, calendar_id)."""

    def test_same_calendar_id_different_orgs(self, db, organization, organization_b):
        """Two calendars with the same DB pk in different orgs (hypothetically) are distinct."""
        # In practice the same PK can't exist twice in the same table, but we can verify
        # that the cache key includes org_id by using two _different_ calendars and
        # confirming each is returned for its respective org.
        cal_a = Calendar.objects.create(
            name="Org A Cal",
            external_id="ext_a",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        )
        cal_b = Calendar.objects.create(
            name="Org B Cal",
            external_id="ext_b",
            provider=CalendarProvider.GOOGLE,
            organization=organization_b,
        )

        cache: dict[tuple[int, str | int], Calendar] = {}
        result_a = get_calendar_by_id(cache, cal_a.id, organization)
        result_b = get_calendar_by_id(cache, cal_b.id, organization_b)

        assert result_a.id == cal_a.id
        assert result_b.id == cal_b.id
        # Both cache entries coexist keyed by their respective (org_id, cal_id) tuples
        assert (organization.id, cal_a.id) in cache
        assert (organization_b.id, cal_b.id) in cache

    def test_org_scoped_lookup_excludes_other_org(self, db, organization, organization_b):
        """Querying a calendar from another org raises DoesNotExist (not a cross-org leak)."""
        cal_a = Calendar.objects.create(
            name="Org A Cal",
            external_id="ext_a_only",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        )
        cache: dict[tuple[int, str | int], Calendar] = {}

        with pytest.raises(Calendar.DoesNotExist):
            # cal_a belongs to org A — lookup against org B must fail
            get_calendar_by_id(cache, cal_a.id, organization_b)


# ---------------------------------------------------------------------------
# Event serialization equivalence
# ---------------------------------------------------------------------------


class TestSerializeEventInternalAttendee:
    """serialize_event_internal_attendee should match the former method's output."""

    def test_basic_fields(self, db, organization):
        """Check that user fields are correctly mapped to EventInternalAttendeeData."""
        user = User.objects.create_user(email="attendee@example.com", password="pw")
        Profile.objects.update_or_create(
            user=user, defaults={"first_name": "Alice", "last_name": "Smith"}
        )

        calendar = Calendar.objects.create(
            name="Test Cal",
            external_id="ext_01",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        )
        event = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=organization,
            title="Test Event",
            start_time_tz_unaware=datetime.datetime(2025, 6, 20, 10, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 6, 20, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        attendance = EventAttendance.objects.create(
            event=event,
            user=user,
            organization=organization,
            status="accepted",
        )

        result = serialize_event_internal_attendee(attendance)

        assert result.user_id == user.id
        assert result.email == "attendee@example.com"
        assert result.name == "Alice Smith"
        assert result.status == "accepted"


class TestSerializeEventDataInputResourceAllocations:
    """Pin the behavior of serialize_event_data_input on the resource-allocation path.

    NOTE: This test documents and pins a *known pre-existing latent bug* that the
    Phase 0 refactor deliberately preserves byte-for-byte. The resources comprehension
    iterates ``Calendar`` objects (the ``Calendar.objects.filter(...)`` result) but then
    accesses ``resource_allocation.calendar.name`` / ``.status`` as if each item were a
    ``ResourceAllocation``. A ``Calendar`` has no ``.calendar`` attribute, so when the
    resource_allocations match a RESOURCE calendar in the org, evaluating the resources
    list raises ``AttributeError``. We assert that here so the preserved behavior is
    explicit and any future "fix" is a deliberate, reviewed change rather than an
    accidental Phase 0 deviation.
    """

    def test_matching_resource_allocation_raises_attribute_error(self, db, organization):
        """A non-empty resource_allocations path that matches a RESOURCE Calendar raises."""
        resource_calendar = Calendar.objects.create(
            name="Conference Room A",
            email="room-a@example.com",
            external_id="ext_resource_a",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            organization=organization,
        )
        owning_calendar = Calendar.objects.create(
            name="Owning Cal",
            external_id="ext_owning",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        )
        event = CalendarEvent.objects.create(
            calendar_fk=owning_calendar,
            organization=organization,
            title="Event With Resource",
            start_time_tz_unaware=datetime.datetime(2025, 6, 20, 10, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 6, 20, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        event_data = CalendarEventInputData(
            title="Event With Resource",
            description="",
            start_time=datetime.datetime(2025, 6, 20, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 6, 20, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            resource_allocations=[ResourceAllocationInputData(resource_id=resource_calendar.id)],
        )

        # The (restored) original comprehension iterates Calendar objects but accesses
        # ``.calendar`` on them — Calendar has no such attribute -> AttributeError.
        with pytest.raises(AttributeError):
            serialize_event_data_input(event, event_data, organization)


class TestSerializeEventDelegation:
    """The facade CalendarService._serialize_event must forward to the util function."""

    def test_serialize_event_delegates_to_util(self, monkeypatch):
        """CalendarService._serialize_event forwards to the module-level util and returns it."""
        from calendar_integration.services import calendar_service as calendar_service_module
        from calendar_integration.services.calendar_service import CalendarService

        sentinel_event = object()
        sentinel_result = MagicMock(spec=CalendarEventData)

        forwarded = MagicMock(return_value=sentinel_result)
        monkeypatch.setattr(calendar_service_module, "_serialize_event_util", forwarded)

        service = CalendarService()
        result = service._serialize_event(sentinel_event)  # type: ignore[arg-type]

        forwarded.assert_called_once_with(sentinel_event)
        assert result is sentinel_result
