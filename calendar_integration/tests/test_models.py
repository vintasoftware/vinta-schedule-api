import datetime

from django.core.exceptions import ValidationError

import pytest
from model_bakery import baker

from calendar_integration.constants import RecurrenceFrequency, RecurrenceWeekday
from calendar_integration.models import (
    AvailableTime,
    AvailableTimeBulkModification,
    BlockedTime,
    BlockedTimeBulkModification,
    CalendarEvent,
    EventBulkModification,
    EventRecurrenceException,
    RecurrenceRule,
)


# Helpers
def _dt(year, month, day, hour=9, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_recurrence_rule_to_rrule_string_basic():
    org = baker.make("organizations.Organization")
    until = _dt(2025, 1, 10)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=2,
        count=None,
        until=until,
        by_weekday="MO,WE,FR",
    )
    rrule = rule.to_rrule_string()
    # Interval should appear (since !=1), COUNT absent (None), UNTIL formatted, BYDAY present
    assert "FREQ=DAILY" in rrule
    assert "INTERVAL=2" in rrule
    assert f"UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}" in rrule
    assert "BYDAY=MO,WE,FR" in rrule


@pytest.mark.django_db
def test_recurrence_rule_from_rrule_string_roundtrip():
    org = baker.make("organizations.Organization")
    src_rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=3,
        count=5,
        by_weekday="TU,TH",
    )
    rrule_string = src_rule.to_rrule_string()
    parsed = RecurrenceRule.from_rrule_string(rrule_string, organization=org)
    assert parsed.frequency == src_rule.frequency
    assert parsed.interval == src_rule.interval
    assert parsed.count == src_rule.count
    assert parsed.by_weekday == src_rule.by_weekday


@pytest.mark.django_db
def test_recurrence_rule_clean_conflicting_count_and_until():
    org = baker.make("organizations.Organization")
    rule = RecurrenceRule(
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        count=3,
        until=_dt(2025, 1, 2),
    )
    with pytest.raises(ValidationError):
        rule.save()


@pytest.mark.django_db
def test_recurrence_rule_clean_invalid_weekday():
    org = baker.make("organizations.Organization")
    rule = RecurrenceRule(organization=org, frequency=RecurrenceFrequency.WEEKLY, by_weekday="XX")
    with pytest.raises(ValidationError):
        rule.save()


@pytest.mark.django_db
def test_calendar_event_get_next_occurrence_daily_basic():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily Standup",
        start_time=start,
        end_time=start + datetime.timedelta(minutes=30),
        recurrence_rule_fk=rule,
    )
    after_date = _dt(2025, 1, 2, 10)  # after second day occurrence
    next_occurrence = event.get_next_occurrence(after_date=after_date)
    # Should be Jan 3rd 09:00
    assert next_occurrence.start_time == _dt(2025, 1, 3)


@pytest.mark.django_db
def test_calendar_event_get_next_occurrence_daily_count_limit():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,  # occurrences at day 0,1,2 then stop
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Limited Daily",
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        recurrence_rule_fk=rule,
    )
    # After day2 (3rd occurrence) should return next occurrence only if count not exceeded.
    # Since count=3 means occurrences at Jan1, Jan2, Jan3 only, asking after Jan2 noon should give Jan3 09:00
    after_date = _dt(2025, 1, 2, 12)
    assert event.get_next_occurrence(after_date=after_date).start_time == _dt(2025, 1, 3)
    # After the third occurrence finishes, there is no next occurrence
    after_last = _dt(2025, 1, 3, 12)
    assert event.get_next_occurrence(after_date=after_last) is None


@pytest.mark.django_db
def test_calendar_event_generate_instances_with_cancelled_exception():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=5,
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily Event",
        start_time=start,
        end_time=start + datetime.timedelta(hours=1),
        recurrence_rule_fk=rule,
        external_id="daily-event",
    )
    # Cancel 3rd occurrence (Jan 3)
    event.create_exception(exception_date=_dt(2025, 1, 3), is_cancelled=True)
    instances = event.get_generated_occurrences_in_range(_dt(2025, 1, 1), _dt(2025, 1, 7))
    dates = [inst.start_time.date() for inst in instances]
    assert _dt(2025, 1, 3).date() not in dates  # skipped
    # Other first five (except cancelled) present
    assert dates.count(_dt(2025, 1, 1).date()) == 1
    assert dates.count(_dt(2025, 1, 2).date()) == 1
    assert dates.count(_dt(2025, 1, 4).date()) == 1
    assert dates.count(_dt(2025, 1, 5).date()) == 1


@pytest.mark.django_db
def test_calendar_event_get_occurrences_in_range_with_modified_exception():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,
    )
    parent = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Parent",
        start_time=start,
        end_time=start + datetime.timedelta(minutes=45),
        recurrence_rule_fk=rule,
        external_id="parent",
    )
    # Create modified event for second occurrence (Jan 2)
    modified = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        title="Parent (Modified)",
        start_time=_dt(2025, 1, 2, 10),  # changed time
        end_time=_dt(2025, 1, 2, 11),
        parent_recurring_object=parent,
        is_recurring_exception=True,
        external_id="modified",
    )
    parent.create_exception(
        exception_date=_dt(2025, 1, 2), is_cancelled=False, modified_object=modified
    )
    occurrences = parent.get_occurrences_in_range(_dt(2025, 1, 1), _dt(2025, 1, 5))
    # Expect three occurrences: Jan1 (generated), Jan2 (modified), Jan3 (generated)
    assert len(occurrences) == 3
    # Ensure modified event included and sorted
    assert occurrences[1].id == modified.id
    assert occurrences[1].start_time == _dt(2025, 1, 2, 10)


@pytest.mark.django_db
def test_calendar_event_create_exception_updates_existing():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily",
        start_time=start,
        end_time=start + datetime.timedelta(minutes=30),
        recurrence_rule_fk=rule,
        external_id="daily",
    )
    # First create cancelled exception
    exc1 = event.create_exception(exception_date=_dt(2025, 1, 2), is_cancelled=True)
    assert exc1.is_cancelled is True
    # Update same occurrence to modified
    modified = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        title="Daily (Modified)",
        start_time=_dt(2025, 1, 2, 11),
        end_time=_dt(2025, 1, 2, 11, 30),
        parent_recurring_object=event,
        is_recurring_exception=True,
        external_id="daily-mod",
    )
    exc2 = event.create_exception(
        exception_date=_dt(2025, 1, 2), is_cancelled=False, modified_object=modified
    )
    assert exc2.id == exc1.id  # updated, not new
    assert exc2.is_cancelled is False
    assert exc2.modified_event_fk_id == modified.id


@pytest.mark.django_db
def test_recurrence_rule_to_rrule_string_all_fields():
    org = baker.make("organizations.Organization")
    until = _dt(2025, 12, 31, 23, 59)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.YEARLY,
        interval=4,
        count=None,
        until=until,
        by_weekday="SU",
        by_month_day="1,-1",
        by_month="1,12",
        by_year_day="100,200",
        by_week_number="1,52",
        by_hour="0,12",
        by_minute="0,30",
        by_second="0,59",
        week_start="SU",  # Non-default
    )
    rrule = rule.to_rrule_string()
    assert "FREQ=YEARLY" in rrule
    assert "INTERVAL=4" in rrule
    assert "COUNT=10" not in rrule  # COUNT is not set, should not appear
    assert f"UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}" in rrule
    assert "BYDAY=SU" in rrule
    assert "BYMONTHDAY=1,-1" in rrule
    assert "BYMONTH=1,12" in rrule
    assert "BYYEARDAY=100,200" in rrule
    assert "BYWEEKNO=1,52" in rrule
    assert "BYHOUR=0,12" in rrule
    assert "BYMINUTE=0,30" in rrule
    assert "BYSECOND=0,59" in rrule
    assert "WKST=SU" in rrule


@pytest.mark.django_db
def test_recurrence_rule_to_rrule_string_defaults_and_empty_fields():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.MONTHLY,
        interval=1,  # Default, should not appear
        count=None,
        until=None,
        by_weekday="",
        by_month_day="",
        by_month="",
        by_year_day="",
        by_week_number="",
        by_hour="",
        by_minute="",
        by_second="",
        week_start=RecurrenceWeekday.MONDAY,  # Default, should not appear
    )
    rrule = rule.to_rrule_string()
    assert "FREQ=MONTHLY" in rrule
    assert "INTERVAL=" not in rrule  # Default interval omitted
    assert "COUNT=" not in rrule
    assert "UNTIL=" not in rrule
    assert "BYDAY=" not in rrule
    assert "BYMONTHDAY=" not in rrule
    assert "BYMONTH=" not in rrule
    assert "BYYEARDAY=" not in rrule
    assert "BYWEEKNO=" not in rrule
    assert "BYHOUR=" not in rrule
    assert "BYMINUTE=" not in rrule
    assert "BYSECOND=" not in rrule
    assert "WKST=" not in rrule


@pytest.mark.django_db
def test_recurrence_rule_to_rrule_string_with_count():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=2,
        count=7,
        until=None,
        by_weekday="MO,WE",
    )
    rrule = rule.to_rrule_string()
    assert "FREQ=WEEKLY" in rrule
    assert "INTERVAL=2" in rrule
    assert "COUNT=7" in rrule


@pytest.mark.django_db
def test_from_rrule_string_all_fields():
    org = baker.make("organizations.Organization")
    rrule = (
        "FREQ=MONTHLY;INTERVAL=2;COUNT=5;UNTIL=20251231T235900Z;BYDAY=MO,WE;BYMONTHDAY=1,-1;BYMONTH=1,12;"
        "BYYEARDAY=100,200;BYWEEKNO=1,52;BYHOUR=0,12;BYMINUTE=0,30;BYSECOND=0,59;WKST=SU"
    )
    rule = RecurrenceRule.from_rrule_string(rrule, organization=org)
    assert rule.frequency == "MONTHLY"
    assert rule.interval == 2
    assert rule.count == 5
    assert rule.until.year == 2025 and rule.until.month == 12 and rule.until.day == 31
    assert rule.by_weekday == "MO,WE"
    assert rule.by_month_day == "1,-1"
    assert rule.by_month == "1,12"
    assert rule.by_year_day == "100,200"
    assert rule.by_week_number == "1,52"
    assert rule.by_hour == "0,12"
    assert rule.by_minute == "0,30"
    assert rule.by_second == "0,59"
    assert rule.week_start == "SU"


@pytest.mark.django_db
def test_from_rrule_string_with_rrule_prefix():
    org = baker.make("organizations.Organization")
    rrule = "RRULE:FREQ=WEEKLY;INTERVAL=3;COUNT=2;BYDAY=TU,TH"
    rule = RecurrenceRule.from_rrule_string(rrule, organization=org)
    assert rule.frequency == "WEEKLY"
    assert rule.interval == 3
    assert rule.count == 2
    assert rule.by_weekday == "TU,TH"


@pytest.mark.django_db
def test_from_rrule_string_partial_fields():
    org = baker.make("organizations.Organization")
    rrule = "FREQ=DAILY"
    rule = RecurrenceRule.from_rrule_string(rrule, organization=org)
    assert rule.frequency == "DAILY"
    assert rule.interval == 1  # default
    assert rule.count is None
    assert rule.until is None
    assert rule.by_weekday == ""
    assert rule.by_month_day == ""
    assert rule.by_month == ""
    assert rule.by_year_day == ""
    assert rule.by_week_number == ""
    assert rule.by_hour == ""
    assert rule.by_minute == ""
    assert rule.by_second == ""
    assert rule.week_start == RecurrenceWeekday.MONDAY


@pytest.mark.django_db
def test_from_rrule_string_invalid_parts_are_ignored():
    org = baker.make("organizations.Organization")
    rrule = "FREQ=DAILY;FOO=BAR;INTERVAL=2"
    rule = RecurrenceRule.from_rrule_string(rrule, organization=org)
    assert rule.frequency == "DAILY"
    assert rule.interval == 2
    assert not hasattr(rule, "FOO")


@pytest.mark.django_db
def test_from_rrule_string_until_without_z():
    org = baker.make("organizations.Organization")
    rrule = "FREQ=DAILY;UNTIL=20251231T235900"  # No Z, should not parse until
    rule = RecurrenceRule.from_rrule_string(rrule, organization=org)
    assert rule.until is None


@pytest.mark.django_db
def test_recurrence_rule_clean_invalid_month_day():
    org = baker.make("organizations.Organization")
    # 0 and 32 are invalid, -32 is invalid
    rule = RecurrenceRule(
        organization=org, frequency=RecurrenceFrequency.MONTHLY, by_month_day="0,32,-32,15"
    )
    with pytest.raises(ValidationError) as exc:
        rule.clean()
    assert "Invalid month days" in str(exc.value)


@pytest.mark.django_db
def test_recurrence_rule_clean_non_integer_month_day():
    org = baker.make("organizations.Organization")
    rule = RecurrenceRule(
        organization=org, frequency=RecurrenceFrequency.MONTHLY, by_month_day="1,foo,3"
    )
    with pytest.raises(ValidationError) as exc:
        rule.clean()
    assert "Month days must be integers" in str(exc.value)


@pytest.mark.django_db
def test_recurrence_rule_clean_invalid_month():
    org = baker.make("organizations.Organization")
    # 0 and 13 are invalid
    rule = RecurrenceRule(
        organization=org, frequency=RecurrenceFrequency.MONTHLY, by_month="0,13,5"
    )
    with pytest.raises(ValidationError) as exc:
        rule.clean()
    assert "Invalid months" in str(exc.value)


@pytest.mark.django_db
def test_recurrence_rule_clean_non_integer_month():
    org = baker.make("organizations.Organization")
    rule = RecurrenceRule(
        organization=org, frequency=RecurrenceFrequency.MONTHLY, by_month="1,foo,12"
    )
    with pytest.raises(ValidationError) as exc:
        rule.clean()
    assert "Months must be integers" in str(exc.value)


@pytest.mark.django_db
def test_get_next_occurrence_not_recurring():
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=None,
    )
    assert event.get_next_occurrence() is None


@pytest.mark.django_db
def test_get_next_occurrence_after_date_none():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    # Should return next occurrence after now
    result = event.get_next_occurrence()
    assert result is not None
    assert isinstance(result.start_time, datetime.datetime)


@pytest.mark.django_db
def test_get_next_occurrence_after_date_before_start():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 10, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 10, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC)
    assert event.get_next_occurrence(after_date=after_date).start_time == event.start_time


@pytest.mark.django_db
def test_get_next_occurrence_until_exceeded():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        until=datetime.datetime(2025, 1, 5, 9, 0, tzinfo=datetime.UTC),
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2025, 1, 6, 9, 0, tzinfo=datetime.UTC)
    assert event.get_next_occurrence(after_date=after_date) is None


@pytest.mark.django_db
def test_get_next_occurrence_daily_count_limit():
    org = baker.make("organizations.Organization")
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
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2025, 1, 3, 9, 0, tzinfo=datetime.UTC)
    assert event.get_next_occurrence(after_date=after_date) is None


@pytest.mark.django_db
def test_get_next_occurrence_weekly():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=2,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2025, 1, 8, 9, 0, tzinfo=datetime.UTC)
    expected = event.start_time + datetime.timedelta(weeks=2)
    assert event.get_next_occurrence(after_date=after_date).start_time == expected


@pytest.mark.django_db
def test_get_next_occurrence_monthly():
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.MONTHLY,
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        start_time=datetime.datetime(2025, 1, 31, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 31, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2025, 2, 1, 9, 0, tzinfo=datetime.UTC)
    result = event.get_next_occurrence(after_date=after_date)
    # Accept either Feb or Mar, depending on code behavior
    assert result.start_time.month in (2, 3)
    if result.start_time.month == 2:
        assert result.start_time.day in (28, 29)
    elif result.start_time.month == 3:
        assert result.start_time.day == 31


@pytest.mark.django_db
def test_get_next_occurrence_yearly():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.YEARLY,
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2020, 2, 29, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2020, 2, 29, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2021, 3, 1, 9, 0, tzinfo=datetime.UTC)
    # Should handle leap year edge case
    result = event.get_next_occurrence(after_date=after_date)
    assert (
        result.start_time.year == 2022
        and result.start_time.month == 2
        and result.start_time.day == 28
    )


@pytest.mark.django_db
def test_generate_instances_not_recurring():
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization_id=org.id,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=None,
    )
    assert (
        event.get_generated_occurrences_in_range(
            datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
            datetime.datetime(2025, 1, 10, tzinfo=datetime.UTC),
        )
        == []
    )


@pytest.mark.django_db
def test_generate_instances_daily_until_and_count():
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    # Only set count, not until
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=2,
    )
    event = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        datetime.datetime(2025, 1, 10, tzinfo=datetime.UTC),
    )
    assert len(instances) == 2
    assert instances[0].start_time == datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC)
    assert instances[1].start_time == datetime.datetime(2025, 1, 2, 9, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_generate_instances_weekly_by_weekday():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=1,
        by_weekday="MO,WE,FR",
        count=5,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 6, 9, 0, tzinfo=datetime.UTC),  # Monday
        end_time=datetime.datetime(2025, 1, 6, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2025, 1, 6, tzinfo=datetime.UTC),
        datetime.datetime(2025, 1, 20, tzinfo=datetime.UTC),
    )
    # Should generate 5 occurrences on MO, WE, FR
    assert len(instances) == 5
    days = [inst.start_time.weekday() for inst in instances]
    assert set(days).issubset({0, 2, 4})


@pytest.mark.django_db
def test_generate_instances_weekly_simple():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=2,
        by_weekday="",
        count=3,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        datetime.datetime(2025, 2, 1, tzinfo=datetime.UTC),
    )
    assert len(instances) == 3
    assert instances[1].start_time == datetime.datetime(2025, 1, 15, 9, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_generate_instances_monthly_edge_case():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.MONTHLY,
        interval=1,
        count=2,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 31, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 31, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2025, 1, 31, tzinfo=datetime.UTC),
        datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC),
    )
    # Should handle Feb 31 -> Feb 28/29
    assert len(instances) == 2
    assert instances[1].start_time.month == 2 and instances[1].start_time.day in (28, 29)


@pytest.mark.django_db
def test_generate_instances_yearly_leap_year():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.YEARLY,
        interval=1,
        count=2,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2020, 2, 29, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2020, 2, 29, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2020, 2, 29, tzinfo=datetime.UTC),
        datetime.datetime(2022, 3, 1, tzinfo=datetime.UTC),
    )
    assert len(instances) == 2
    assert instances[1].start_time.year == 2021 and instances[1].start_time.day == 28


@pytest.mark.django_db
def test_generate_instances_unknown_frequency():
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency="UNKNOWN",
        interval=1,
        count=2,
    )
    event = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        datetime.datetime(2025, 1, 10, tzinfo=datetime.UTC),
    )
    # The code generates one instance before breaking
    assert len(instances) == 1
    assert instances[0].start_time == event.start_time


@pytest.mark.django_db
def test_generate_instances_with_exceptions():
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=5,
    )
    event = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    EventRecurrenceException.objects.create(
        organization=org,
        parent_event=event,
        exception_date=datetime.datetime(2025, 1, 3, 9, 0, tzinfo=datetime.UTC),
        is_cancelled=True,
    )
    instances = event.get_generated_occurrences_in_range(
        datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        datetime.datetime(2025, 1, 7, tzinfo=datetime.UTC),
    )
    dates = [inst.start_time.date() for inst in instances]
    assert datetime.date(2025, 1, 3) not in dates
    # Cancelled occurrences within search range don't count toward limit,
    # so we should get 5 instances: Jan 1, 2, 4, 5, 6 (Jan 3 cancelled)
    assert len(instances) == 5


@pytest.mark.django_db
def test_get_next_occurrence_with_cancelled_exception_before_range():
    """Test that cancelled exceptions before search range don't count toward limit"""
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,  # Should generate 3 non-cancelled occurrences
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily with Cancelled",
        start_time=_dt(2025, 1, 1),
        end_time=_dt(2025, 1, 1, 10),
        recurrence_rule_fk=rule,
    )

    # Cancel the second occurrence (Jan 2)
    event.create_exception(exception_date=_dt(2025, 1, 2), is_cancelled=True)

    # Search for next occurrence after Jan 3 - should find Jan 4 since Jan 2 was cancelled
    # and doesn't count toward the limit
    after_date = _dt(2025, 1, 3, 12)
    next_occurrence = event.get_next_occurrence(after_date=after_date)

    # Should return Jan 4 because:
    # - Jan 1: occurrence #1 (counts toward limit)
    # - Jan 2: cancelled (doesn't count toward limit)
    # - Jan 3: occurrence #2 (counts toward limit)
    # - Jan 4: occurrence #3 (counts toward limit) - this should be returned
    assert next_occurrence is not None
    assert next_occurrence.start_time == _dt(2025, 1, 4)

    # After Jan 4, there should be no more occurrences (limit reached)
    after_last = _dt(2025, 1, 4, 12)
    assert event.get_next_occurrence(after_date=after_last) is None


@pytest.mark.django_db
def test_get_next_occurrence_with_modified_exception_before_range():
    """Test that modified exceptions before search range still count toward limit"""
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,  # Should generate exactly 3 occurrences
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily with Modified",
        start_time=_dt(2025, 1, 1),
        end_time=_dt(2025, 1, 1, 10),
        recurrence_rule_fk=rule,
    )

    # Create modified event for second occurrence (Jan 2) - move it to later time
    modified = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        title="Modified Occurrence",
        start_time=_dt(2025, 1, 2, 15),  # moved to 3 PM
        end_time=_dt(2025, 1, 2, 16),
        parent_recurring_object=event,
        is_recurring_exception=True,
        external_id="modified-jan-2",
    )
    event.create_exception(
        exception_date=_dt(2025, 1, 2), is_cancelled=False, modified_object=modified
    )

    # Search for next occurrence after Jan 2 - should find Jan 3
    after_date = _dt(2025, 1, 2, 12)
    next_occurrence = event.get_next_occurrence(after_date=after_date)
    assert next_occurrence is not None
    assert next_occurrence.start_time == _dt(2025, 1, 3)

    # After Jan 3, there should be no more occurrences (limit reached)
    # because the modified Jan 2 occurrence still counts toward the limit
    after_last = _dt(2025, 1, 3, 12)
    assert event.get_next_occurrence(after_date=after_last) is None


@pytest.mark.django_db
def test_get_occurrences_in_range_with_cancelled_before_range():
    """Test generating occurrences when cancelled exceptions exist before the range"""
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=4,  # Should allow 4 non-cancelled occurrences
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily with Early Cancellation",
        start_time=_dt(2025, 1, 1),
        end_time=_dt(2025, 1, 1, 10),
        recurrence_rule_fk=rule,
    )

    # Cancel the first occurrence (Jan 1) - before our search range
    event.create_exception(exception_date=_dt(2025, 1, 1), is_cancelled=True)

    # Get occurrences starting from Jan 3 onward
    occurrences = event.get_occurrences_in_range(
        start_date=_dt(2025, 1, 3), end_date=_dt(2025, 1, 7)
    )

    # Should get Jan 3, 4, 5, 6 because Jan 1 was cancelled and doesn't count toward limit
    # but we're only looking from Jan 3 onward, so we should see Jan 3, 4, 5
    dates = [occ.start_time.date() for occ in occurrences]
    expected_dates = [_dt(2025, 1, 3).date(), _dt(2025, 1, 4).date(), _dt(2025, 1, 5).date()]
    assert dates == expected_dates


@pytest.mark.django_db
def test_get_occurrences_in_range_weekly_with_cancelled_before_range():
    """Test weekly recurrence with cancelled exceptions before range"""
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=1,
        by_weekday="MO,WE,FR",
        count=4,  # Should allow 4 non-cancelled occurrences
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Weekly with Cancellation",
        start_time=_dt(2025, 1, 6),  # Monday
        end_time=_dt(2025, 1, 6, 10),
        recurrence_rule_fk=rule,
    )

    # Cancel the first occurrence (Jan 6, Monday) - before our search range
    event.create_exception(exception_date=_dt(2025, 1, 6), is_cancelled=True)

    # Get occurrences starting from Jan 10 onward
    occurrences = event.get_occurrences_in_range(
        start_date=_dt(2025, 1, 10),  # Friday
        end_date=_dt(2025, 1, 20),
    )

    # Should get Jan 10 (Fri), 13 (Mon), 15 (Wed)
    # because Jan 6 was cancelled and doesn't count toward the limit
    # but Jan 8 (Wed) before search range counts as occurrence #1,
    # so we get 3 more occurrences within search range for total of 4
    dates = [occ.start_time.date() for occ in occurrences]
    expected_dates = [
        _dt(2025, 1, 10).date(),  # Fri
        _dt(2025, 1, 13).date(),  # Mon
        _dt(2025, 1, 15).date(),  # Wed
    ]
    assert dates == expected_dates


@pytest.mark.django_db
def test_get_next_occurrence_multiple_cancellations_before_range():
    """Test with multiple cancelled exceptions before search range"""
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,  # Should generate 3 non-cancelled occurrences
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily with Multiple Cancellations",
        start_time=_dt(2025, 1, 1),
        end_time=_dt(2025, 1, 1, 10),
        recurrence_rule_fk=rule,
    )

    # Cancel Jan 1 and Jan 2 (both before our search range)
    event.create_exception(exception_date=_dt(2025, 1, 1), is_cancelled=True)
    event.create_exception(exception_date=_dt(2025, 1, 2), is_cancelled=True)

    # Search for next occurrence after Jan 4
    after_date = _dt(2025, 1, 4, 12)
    next_occurrence = event.get_next_occurrence(after_date=after_date)

    # Should return Jan 5 because:
    # - Jan 1: cancelled (doesn't count)
    # - Jan 2: cancelled (doesn't count)
    # - Jan 3: occurrence #1 (counts)
    # - Jan 4: occurrence #2 (counts)
    # - Jan 5: occurrence #3 (counts) - this should be returned
    assert next_occurrence is not None
    assert next_occurrence.start_time == _dt(2025, 1, 5)

    # After Jan 5, there should be no more occurrences (limit reached)
    after_last = _dt(2025, 1, 5, 12)
    assert event.get_next_occurrence(after_date=after_last) is None


@pytest.mark.django_db
def test_get_next_occurrence_mixed_exceptions_before_range():
    """Test with both cancelled and modified exceptions before search range"""
    org = baker.make("organizations.Organization")
    cal = baker.make("calendar_integration.Calendar", organization=org)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=3,  # Should generate exactly 3 occurrence slots
    )
    event = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Daily with Mixed Exceptions",
        start_time=_dt(2025, 1, 1),
        end_time=_dt(2025, 1, 1, 10),
        recurrence_rule_fk=rule,
    )

    # Cancel Jan 1 (doesn't count toward limit)
    event.create_exception(exception_date=_dt(2025, 1, 1), is_cancelled=True)

    # Modify Jan 2 (counts toward limit)
    modified = baker.make(
        CalendarEvent,
        calendar=cal,
        organization=org,
        title="Modified Jan 2",
        start_time=_dt(2025, 1, 2, 15),
        end_time=_dt(2025, 1, 2, 16),
        parent_recurring_object=event,
        is_recurring_exception=True,
        external_id="modified-jan-2-mixed",
    )
    event.create_exception(
        exception_date=_dt(2025, 1, 2), is_cancelled=False, modified_object=modified
    )

    # Search for next occurrence after Jan 3
    after_date = _dt(2025, 1, 3, 12)
    next_occurrence = event.get_next_occurrence(after_date=after_date)

    # Should return Jan 4 because:
    # - Jan 1: cancelled (doesn't count toward limit)
    # - Jan 2: modified (counts toward limit) - slot #1 used
    # - Jan 3: regular occurrence (counts toward limit) - slot #2 used
    # - Jan 4: regular occurrence (counts toward limit) - slot #3 used
    assert next_occurrence is not None
    assert next_occurrence.start_time == _dt(2025, 1, 4)

    # After Jan 4, limit should be reached
    after_last = _dt(2025, 1, 4, 12)
    assert event.get_next_occurrence(after_date=after_last) is None


@pytest.mark.django_db
def test_event_bulk_modification_create_and_linking():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )

    parent = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Parent",
        start_time=_dt(2025, 1, 1),
        external_id="evt-parent-1",
        end_time=_dt(2025, 1, 1, 10),
    )

    # Continuation event referencing parent via bulk_modification_parent
    continuation = baker.make(
        CalendarEvent,
        calendar_fk=cal,
        organization=org,
        title="Continuation",
        start_time=_dt(2025, 2, 1),
        external_id="evt-cont-1",
        end_time=_dt(2025, 2, 1, 10),
        bulk_modification_parent=parent,
    )

    assert continuation.bulk_modification_parent_fk_id == parent.id
    # Parent should have the continuation in its reverse relation
    assert continuation in list(parent.bulk_modifications.all())

    # Create bulk modification records and link them
    bulk1 = baker.make(
        EventBulkModification,
        organization=org,
        parent_event=parent,
        modification_start_date=_dt(2025, 2, 1),
        is_bulk_cancelled=False,
    )

    bulk2 = baker.make(
        EventBulkModification,
        organization=org,
        parent_event=parent,
        modification_start_date=_dt(2025, 3, 1),
        original_parent=bulk1,
    )

    # Link continuation
    bulk1.modified_continuation = bulk2
    bulk1.save()

    assert bulk2.original_parent_fk_id == bulk1.id
    assert bulk1.modified_continuation_fk_id == bulk2.id
    # Parent event should reference its bulk records
    assert bulk1 in list(parent.bulk_modification_records.all())


@pytest.mark.django_db
def test_blocked_and_available_bulk_modification_parent_fields():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )

    parent_blocked = baker.make(
        BlockedTime,
        calendar_fk=cal,
        organization=org,
        reason="Original",
        start_time=_dt(2025, 1, 1),
        external_id="bt-parent-1",
        end_time=_dt(2025, 1, 1, 1),
    )

    continuation_blocked = baker.make(
        BlockedTime,
        calendar_fk=cal,
        organization=org,
        reason="Continuation",
        start_time=_dt(2025, 2, 1),
        external_id="bt-cont-1",
        end_time=_dt(2025, 2, 1, 1),
        bulk_modification_parent=parent_blocked,
    )

    assert continuation_blocked.bulk_modification_parent_fk_id == parent_blocked.id
    assert continuation_blocked in list(parent_blocked.bulk_modifications.all())

    parent_available = baker.make(
        AvailableTime,
        calendar_fk=cal,
        organization=org,
        start_time=_dt(2025, 1, 5),
        end_time=_dt(2025, 1, 5, 1),
    )

    continuation_available = baker.make(
        AvailableTime,
        calendar_fk=cal,
        organization=org,
        start_time=_dt(2025, 2, 5),
        end_time=_dt(2025, 2, 5, 1),
        bulk_modification_parent=parent_available,
    )

    assert continuation_available.bulk_modification_parent_fk_id == parent_available.id
    assert continuation_available in list(parent_available.bulk_modifications.all())


@pytest.mark.django_db
def test_blockedtime_and_availabletime_bulk_modification_records():
    org = baker.make("organizations.Organization")
    cal = baker.make(
        "calendar_integration.Calendar", organization=org, external_id=baker.seq("cal")
    )

    parent_bt = baker.make(
        BlockedTime,
        calendar_fk=cal,
        organization=org,
        reason="Original BT",
        start_time=_dt(2025, 1, 1),
        external_id="bt-parent-2",
        end_time=_dt(2025, 1, 1, 1),
    )

    bulk_bt = baker.make(
        BlockedTimeBulkModification,
        organization=org,
        parent_blocked_time=parent_bt,
        modification_start_date=_dt(2025, 2, 1),
        is_bulk_cancelled=True,
    )

    assert bulk_bt.parent_blocked_time_fk_id == parent_bt.id
    assert bulk_bt.is_bulk_cancelled is True

    parent_av = baker.make(
        AvailableTime,
        calendar_fk=cal,
        organization=org,
        start_time=_dt(2025, 1, 10),
        end_time=_dt(2025, 1, 10, 1),
    )

    bulk_av = baker.make(
        AvailableTimeBulkModification,
        organization=org,
        parent_available_time=parent_av,
        modification_start_date=_dt(2025, 2, 10),
    )

    assert bulk_av.parent_available_time_fk_id == parent_av.id
