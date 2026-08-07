"""Tests for the untruncated-recurring-parent repair script.

The corrupt state is built directly rather than by running the (now fixed) buggy
code path: a series, a bulk-modification record, a continuation, and a parent rule
with its ``UNTIL`` missing. That is byte-for-byte what the defect left in
production, and building it explicitly keeps the test meaningful after the upstream
fix rather than depending on code that no longer misbehaves.
"""

import datetime
import importlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import CalendarProvider, CalendarType, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    EventBulkModification,
    RecurrenceRule,
)
from organizations.models import Organization
from payments.models import MeteredOccurrence, Subscription
from users.models import Profile, User


_SCRIPT_PATH = Path(__file__).parent / "script.py"
_spec = importlib.util.spec_from_file_location("repair_untruncated_script", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
script_module = importlib.util.module_from_spec(_spec)
# Register before exec: the dataclasses in script.py resolve their annotations
# through sys.modules[cls.__module__], which is None for an unregistered module.
sys.modules[_spec.name] = script_module
_spec.loader.exec_module(script_module)

RepairUntruncatedRecurringParents = script_module.RepairUntruncatedRecurringParents
ScriptConfig = script_module.ScriptConfig
RULE_TABLE = script_module.RULE_TABLE
METERED_TABLE = script_module.METERED_TABLE

_base = importlib.import_module("scripts.one_off._base")


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
FIRST_MONDAY = datetime.datetime(2025, 6, 2, 10, 0, tzinfo=datetime.UTC)
ALL_MONDAYS = [FIRST_MONDAY + datetime.timedelta(weeks=week) for week in range(5)]
HALF_HOUR = datetime.timedelta(minutes=30)


class _TestRuntime(_base.Runtime):
    """In-memory runtime: tmp paths, no signals, no lease, no upload."""

    def __init__(self, config: ScriptConfig) -> None:
        super().__init__(config)
        self.lines: list[str] = []
        self._processed: set[str] = set()
        self._stop = False

    def acquire_lease(self) -> None:
        return None

    def release_lease(self) -> None:
        return None

    def install_stop_handler(self, on_stop: Callable[[str], None]) -> None:
        self._on_stop = on_stop

    def should_stop(self) -> bool:
        return self._stop

    def trigger_stop(self) -> None:
        """Simulate an operator interrupt mid-run."""
        self._stop = True

    def log(self, level: str, message: str) -> None:
        self.lines.append(f"{level} {message}")

    def fsync_log(self) -> None:
        return None

    def load_processed_ids(self) -> set[str]:
        return set(self._processed)

    def mark_processed(self, item_id: str) -> None:
        self._processed.add(item_id)

    def list_run_artifacts(self) -> list[Path]:
        return [p for p in self.run_dir.iterdir() if p.is_file()]

    def upload_run_artifacts(self) -> None:
        return None


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Repair Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    return subscription


@pytest.fixture
def social_account(db) -> SocialAccount:
    user = User.objects.create_user(email="repair@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="88888")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    calendar = Calendar.objects.create(
        name="Repair Calendar",
        description="",
        external_id="repair_cal_1",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.VIRTUAL,
        organization=organization,
        manage_available_windows=True,
    )
    AvailableTime.objects.create(
        calendar_fk=calendar,
        organization=organization,
        start_time_tz_unaware=PERIOD_START - datetime.timedelta(days=30),
        end_time_tz_unaware=PERIOD_END + datetime.timedelta(days=30),
        timezone="UTC",
    )
    return calendar


@pytest.fixture
def runtime(tmp_path: Path) -> _TestRuntime:
    return _TestRuntime(_config(tmp_path))


def _config(tmp_path: Path) -> ScriptConfig:
    return ScriptConfig(name="test-repair-untruncated", log_dir=tmp_path / "runs", batch_size=100)


def _build_script(runtime: _TestRuntime, dry_run: bool) -> RepairUntruncatedRecurringParents:
    return RepairUntruncatedRecurringParents(
        config=runtime.config, runtime=runtime, dry_run=dry_run
    )


def _corrupt_open_ended_series(
    calendar: Calendar, *, offset: datetime.timedelta = HALF_HOUR
) -> tuple[CalendarEvent, CalendarEvent]:
    """Build the exact state the defect left: an unbounded parent plus a continuation.

    ``offset`` shifts the continuation's start times. Pass ``timedelta(0)`` for the
    vacuous title-only split, where parent and continuation occupy the same slots.
    """
    parent = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Open ended standup",
        description="",
        start_time=FIRST_MONDAY,
        end_time=FIRST_MONDAY + datetime.timedelta(hours=1),
        frequency=RecurrenceFrequency.WEEKLY,
        by_weekday="MO",
        external_id="repair_parent",
    )
    continuation = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Open ended standup (moved)",
        description="",
        start_time=ALL_MONDAYS[1] + offset,
        end_time=ALL_MONDAYS[1] + offset + datetime.timedelta(hours=1),
        frequency=RecurrenceFrequency.WEEKLY,
        by_weekday="MO",
        external_id="repair_continuation",
    )
    continuation.bulk_modification_parent_fk = parent
    continuation.save()

    EventBulkModification.objects.create(
        organization=calendar.organization,
        parent_event=parent,
        modification_start_date=ALL_MONDAYS[1],
        modified_continuation=None,
        is_bulk_cancelled=False,
    )

    # The damage: the parent's rule never received its UNTIL.
    rule = parent.recurrence_rule
    rule.count = None
    rule.until = None
    rule.save(update_fields=["count", "until"])
    return parent, continuation


def _rule_of(event: CalendarEvent) -> RecurrenceRule:
    return CalendarEvent.objects.get(organization=event.organization, pk=event.pk).recurrence_rule


# ---------------------------------------------------------------------------
# Contract: dry-run / apply / idempotency / restore
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dry_run_writes_nothing(runtime: _TestRuntime, calendar: Calendar, subscription):
    parent, _ = _corrupt_open_ended_series(calendar)

    _build_script(runtime, dry_run=True).execute()

    rule = _rule_of(parent)
    assert rule.until is None, "dry-run must not write"
    assert rule.count is None
    assert any("[dry-run] would process item" in line for line in runtime.lines)


@pytest.mark.django_db
def test_apply_truncates_the_parent_at_the_split(
    runtime: _TestRuntime, calendar: Calendar, subscription
):
    parent, _ = _corrupt_open_ended_series(calendar)

    assert _build_script(runtime, dry_run=False).execute() == 0

    rule = _rule_of(parent)
    assert rule.until == ALL_MONDAYS[0], "parent must stop at the last occurrence before the split"
    assert rule.count is None


@pytest.mark.django_db
def test_a_second_apply_is_a_no_op(runtime: _TestRuntime, calendar: Calendar, subscription):
    parent, _ = _corrupt_open_ended_series(calendar)

    _build_script(runtime, dry_run=False).execute()
    first_pass = _rule_of(parent)

    second = _build_script(runtime, dry_run=False)
    assert second.execute() == 0

    rule = _rule_of(parent)
    assert (rule.until, rule.count) == (first_pass.until, first_pass.count)
    assert second.reported.get("already-correct") == 1


@pytest.mark.django_db
def test_restore_puts_the_rule_back(runtime: _TestRuntime, calendar: Calendar, subscription):
    parent, _ = _corrupt_open_ended_series(calendar)

    script = _build_script(runtime, dry_run=False)
    script.execute()
    assert _rule_of(parent).until == ALL_MONDAYS[0]

    backups = list(runtime.run_dir.glob(f"{RULE_TABLE}.*.csv"))
    assert backups, "a destructive run must leave a rule backup"

    _build_script(runtime, dry_run=False).restore_from_backup(runtime.run_dir)

    rule = _rule_of(parent)
    assert rule.until is None, "restore must return the row to its pre-run state"
    assert rule.count is None


@pytest.mark.django_db
def test_an_interrupt_leaves_a_consistent_state(
    runtime: _TestRuntime, calendar: Calendar, subscription
):
    """A stop before the first item writes nothing and still flushes cleanly."""
    parent, _ = _corrupt_open_ended_series(calendar)
    runtime.trigger_stop()

    assert _build_script(runtime, dry_run=False).execute() == 0

    assert _rule_of(parent).until is None
    assert any("stop flag set" in line for line in runtime.lines)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_parent_with_an_unexpected_until_is_skipped_and_reported(
    runtime: _TestRuntime, calendar: Calendar, subscription
):
    """A rule bounded somewhere else may be a legitimate later edit -- never overwrite it."""
    parent, _ = _corrupt_open_ended_series(calendar)
    rule = parent.recurrence_rule
    rule.until = ALL_MONDAYS[3]
    rule.save(update_fields=["until"])

    script = _build_script(runtime, dry_run=False)
    script.execute()

    assert _rule_of(parent).until == ALL_MONDAYS[3], "an ambiguous parent must be left alone"
    assert script.reported.get("ambiguous-until-mismatch") == 1
    assert any("needs review [ambiguous-until-mismatch]" in line for line in runtime.lines)


@pytest.mark.django_db
def test_the_earliest_split_wins_when_a_series_was_modified_twice(
    runtime: _TestRuntime, calendar: Calendar, subscription
):
    parent, _ = _corrupt_open_ended_series(calendar)
    EventBulkModification.objects.create(
        organization=calendar.organization,
        parent_event=parent,
        modification_start_date=ALL_MONDAYS[3],
        modified_continuation=None,
        is_bulk_cancelled=False,
    )

    _build_script(runtime, dry_run=False).execute()

    assert _rule_of(parent).until == ALL_MONDAYS[0], (
        "the parent must stop at the earliest split, not the latest"
    )


# ---------------------------------------------------------------------------
# Metering cleanup
# ---------------------------------------------------------------------------


def _meter(subscription: Subscription, event_id: int, starts: list[datetime.datetime]) -> None:
    for start in starts:
        MeteredOccurrence.objects.create(
            organization_id=subscription.organization_id,
            subscription=subscription,
            event_id=event_id,
            occurrence_start=start,
            billing_period_start=PERIOD_START,
            is_within_allowance=True,
            unit_price="0.0000",
        )


@pytest.mark.django_db
def test_phantom_metered_rows_are_deleted(
    runtime: _TestRuntime, calendar: Calendar, subscription: Subscription
):
    """The inflated ledger is trimmed to the occurrences that really happened."""
    parent, _ = _corrupt_open_ended_series(calendar)
    # What the un-truncated parent caused the meter to record: every Monday at
    # 10:00 from the parent, plus the continuation's Mondays at 10:30.
    _meter(subscription, parent.pk, ALL_MONDAYS + [m + HALF_HOUR for m in ALL_MONDAYS[1:]])
    assert MeteredOccurrence.objects.count() == 9

    script = _build_script(runtime, dry_run=False)
    script.execute()

    remaining = sorted(MeteredOccurrence.objects.values_list("occurrence_start", flat=True))
    assert remaining == [ALL_MONDAYS[0], *[m + HALF_HOUR for m in ALL_MONDAYS[1:]]]
    assert script._metered_deleted == 4


@pytest.mark.django_db
def test_a_vacuous_split_deletes_no_metered_rows(
    runtime: _TestRuntime, calendar: Calendar, subscription: Subscription
):
    """A title-only split reuses the parent's slots, so nothing is phantom."""
    parent, _ = _corrupt_open_ended_series(calendar, offset=datetime.timedelta(0))
    _meter(subscription, parent.pk, ALL_MONDAYS)

    script = _build_script(runtime, dry_run=False)
    script.execute()

    assert MeteredOccurrence.objects.count() == 5
    assert script._metered_deleted == 0


@pytest.mark.django_db
def test_dry_run_deletes_no_metered_rows(
    runtime: _TestRuntime, calendar: Calendar, subscription: Subscription
):
    parent, _ = _corrupt_open_ended_series(calendar)
    _meter(subscription, parent.pk, ALL_MONDAYS + [m + HALF_HOUR for m in ALL_MONDAYS[1:]])

    _build_script(runtime, dry_run=True).execute()

    assert MeteredOccurrence.objects.count() == 9


@pytest.mark.django_db
def test_deleted_metered_rows_can_be_restored(
    runtime: _TestRuntime, calendar: Calendar, subscription: Subscription
):
    parent, _ = _corrupt_open_ended_series(calendar)
    _meter(subscription, parent.pk, ALL_MONDAYS + [m + HALF_HOUR for m in ALL_MONDAYS[1:]])
    before = set(MeteredOccurrence.objects.values_list("occurrence_start", flat=True))

    script = _build_script(runtime, dry_run=False)
    script.execute()
    assert MeteredOccurrence.objects.count() == 5

    _build_script(runtime, dry_run=False).restore_from_backup(runtime.run_dir)

    assert set(MeteredOccurrence.objects.values_list("occurrence_start", flat=True)) == before
