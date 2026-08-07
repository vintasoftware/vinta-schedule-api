import datetime

import pytest
from model_bakery import baker

from calendar_integration.constants import RecurrenceFrequency
from calendar_integration.models import RecurrenceRule
from calendar_integration.recurrence_utils import RecurrenceRuleSplitter


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
def test_split_returns_rules_that_do_not_alias_the_original_row():
    """Both halves of a split must be new rows, not aliases of the input rule.

    ``copy.deepcopy`` of a saved model preserves its ``pk``. If either half comes
    back carrying the original pk, saving it issues an ``UPDATE`` against the
    original rule instead of an ``INSERT`` -- which is how a bulk modification
    used to erase the parent's truncation and duplicate the series.
    """
    org = baker.make("organizations.Organization")
    start = _dt(2025, 1, 1)
    rule = baker.make(
        RecurrenceRule,
        organization=org,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        count=None,
        until=None,
    )

    truncated, continuation = RecurrenceRuleSplitter.split_at_date(rule, _dt(2025, 1, 5), start)

    assert truncated is not None
    assert continuation is not None
    assert truncated.pk is None
    assert continuation.pk is None

    # Saving either half must leave the original row untouched.
    truncated.save()
    continuation.save()
    rule.refresh_from_db()
    assert rule.count is None
    assert rule.until is None
    assert {truncated.pk, continuation.pk}.isdisjoint({rule.pk})


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
