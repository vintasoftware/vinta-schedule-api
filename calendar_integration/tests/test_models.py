import datetime

from django.core.exceptions import ValidationError

import pytest
from model_bakery import baker

from calendar_integration.constants import RecurrenceFrequency, RecurrenceWeekday
from calendar_integration.models import CalendarEvent, RecurrenceException, RecurrenceRule


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
    assert next_occurrence == _dt(2025, 1, 3)


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
    assert event.get_next_occurrence(after_date=after_date) == _dt(2025, 1, 3)
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
    instances = event.generate_instances(_dt(2025, 1, 1), _dt(2025, 1, 7))
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
        parent_event=parent,
        is_recurring_exception=True,
        external_id="modified",
    )
    parent.create_exception(
        exception_date=_dt(2025, 1, 2), is_cancelled=False, modified_event=modified
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
        parent_event=event,
        is_recurring_exception=True,
        external_id="daily-mod",
    )
    exc2 = event.create_exception(
        exception_date=_dt(2025, 1, 2), is_cancelled=False, modified_event=modified
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
    assert isinstance(result, datetime.datetime)


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
    assert event.get_next_occurrence(after_date=after_date) == event.start_time


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
    assert event.get_next_occurrence(after_date=after_date) == expected


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
    assert result.month in (2, 3)
    if result.month == 2:
        assert result.day in (28, 29)
    elif result.month == 3:
        assert result.day == 31


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
    assert result.year == 2022 and result.month == 2 and result.day == 28


@pytest.mark.django_db
def test_get_next_occurrence_unknown_frequency():
    org = baker.make("organizations.Organization")
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency="UNKNOWN",
        interval=1,
    )
    event = baker.make(
        CalendarEvent,
        organization=org,
        start_time=datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
        recurrence_rule=rule,
    )
    after_date = datetime.datetime(2025, 1, 2, 9, 0, tzinfo=datetime.UTC)
    assert event.get_next_occurrence(after_date=after_date) is None


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
        event.generate_instances(
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
    instances = event.generate_instances(
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
    instances = event.generate_instances(
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
    instances = event.generate_instances(
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
    instances = event.generate_instances(
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
    instances = event.generate_instances(
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
    instances = event.generate_instances(
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
    RecurrenceException.objects.create(
        organization=org,
        parent_event=event,
        exception_date=datetime.datetime(2025, 1, 3, 9, 0, tzinfo=datetime.UTC),
        is_cancelled=True,
    )
    instances = event.generate_instances(
        datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        datetime.datetime(2025, 1, 7, tzinfo=datetime.UTC),
    )
    dates = [inst.start_time.date() for inst in instances]
    assert datetime.date(2025, 1, 3) not in dates
    assert len(instances) == 4
