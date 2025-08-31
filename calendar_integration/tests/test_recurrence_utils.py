import datetime

import pytest
from model_bakery import baker

from calendar_integration.constants import RecurrenceFrequency
from calendar_integration.models import CalendarEvent, RecurrenceRule
from calendar_integration.recurrence_utils import OccurrenceValidator, RecurrenceRuleSplitter


# Helpers
def _dt(year, month, day, hour=9, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_split_at_date_truncates_and_continues():
    org = baker.make("organizations.Organization")
    start = _dt(2025, 1, 1)
    # daily rule with no count/until
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=None,
        until=None,
    )

    split_date = _dt(2025, 1, 5)
    truncated, continuation = RecurrenceRuleSplitter.split_at_date(rule, split_date, start)

    # Previous occurrence should be Jan 4 09:00 and truncated.until set to that
    assert truncated is not None
    assert truncated.until == _dt(2025, 1, 4)
    assert continuation is not None


@pytest.mark.django_db
def test_split_with_count_creates_continuation_with_remaining_count():
    org = baker.make("organizations.Organization")
    start = _dt(2025, 1, 1)
    # count=5 occurrences Jan1..Jan5
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=5,
    )

    # split at Jan 4 -> used occurrences before Jan4 are Jan1,Jan2,Jan3 => 3 used
    truncated, continuation = RecurrenceRuleSplitter.split_at_date(rule, _dt(2025, 1, 4), start)

    assert truncated is not None
    # truncated should have UNTIL at Jan3
    assert truncated.until == _dt(2025, 1, 3)

    assert continuation is not None
    # remaining count should be 2 (5 - 3)
    assert continuation.count == 2


@pytest.mark.django_db
def test_create_continuation_returns_none_if_until_before_start():
    org = baker.make("organizations.Organization")
    start = _dt(2025, 1, 1)
    # rule that ends Jan 5
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        until=_dt(2025, 1, 5),
    )

    # Try to continue from Jan 6 -> nothing should remain
    cont = RecurrenceRuleSplitter.create_continuation_rule(
        rule, _dt(2025, 1, 6), original_start=start
    )
    assert cont is None


@pytest.mark.django_db
def test_occurrence_validator_normalize_and_validate():
    org = baker.make("organizations.Organization")
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,
    )

    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        recurrence_rule_fk=rule,
    )

    # approximate date slightly after occurrence time -> should normalize to Jan2 09:00
    approx = _dt(2025, 1, 2, 10)
    normalized = OccurrenceValidator.normalize_modification_date(event, approx)
    assert normalized == _dt(2025, 1, 2)

    # validate exact occurrence
    assert OccurrenceValidator.validate_modification_date(event, _dt(2025, 1, 2)) is True

    # previous occurrence before Jan3 should be Jan2
    prev = OccurrenceValidator.get_previous_occurrence_date(event, _dt(2025, 1, 3))
    assert prev == _dt(2025, 1, 2)


@pytest.mark.django_db
def test_truncate_clears_count_and_sets_until():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=10,
    )

    until = _dt(2025, 2, 1)
    truncated = RecurrenceRuleSplitter.truncate_rule_until_date(rule, until)
    assert truncated is not None
    # truncated should clear COUNT and set UNTIL
    assert truncated.count is None
    assert truncated.until == until


@pytest.mark.django_db
def test_split_monthly_edge_case_with_jan31():
    org = baker.make("organizations.Organization")
    start = _dt(2025, 1, 31)
    # monthly rule with count=3 -> Jan31, Feb28/29, Mar31
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.MONTHLY,
        interval=1,
        count=3,
    )

    # split at Feb 15 -> previous occurrence should be Jan31
    truncated, continuation = RecurrenceRuleSplitter.split_at_date(rule, _dt(2025, 2, 15), start)
    assert truncated is not None
    assert truncated.until == _dt(2025, 1, 31)
    assert continuation is not None
    # used occurrences before Feb15 is 1 (Jan31) so remaining should be 2
    assert continuation.count == 2


@pytest.mark.django_db
def test_dst_transition_normalization_and_validation():
    # Use a US zone with DST transition
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        pytest.skip("ZoneInfo not available in this environment")

    tz = ZoneInfo("America/New_York")
    org = baker.make("organizations.Organization")
    # Start on the day before DST starts (2021-03-13) at 09:00 local
    start = datetime.datetime(2021, 3, 13, 9, 0, tzinfo=tz)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,
    )

    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        recurrence_rule_fk=rule,
    )

    # The next day (DST boundary) should still normalize to the occurrence in that zone
    approx = datetime.datetime(2021, 3, 14, 9, 0, tzinfo=tz)
    normalized = OccurrenceValidator.normalize_modification_date(event, approx)
    assert normalized is not None
    assert normalized.tzinfo == tz
    assert normalized.date() == approx.date()

    # validation of exact occurrence should be True
    assert OccurrenceValidator.validate_modification_date(event, approx) is True
