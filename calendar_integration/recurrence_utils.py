"""Recurrence utilities: splitting and validating recurrence rules.

This module implements the Phase 2 helpers from the plan: a
`RecurrenceRuleSplitter` and an `OccurrenceValidator`.

Notes:
- Functions return new, unsaved ``RecurrenceRule`` model instances when
  producing modified rules.
"""

import copy
import datetime

from django.utils import timezone

from dateutil.rrule import rrulestr

from calendar_integration.models import RecurrenceRule


class RecurrenceRuleSplitter:
    """Helpers to split or truncate RecurrenceRule objects."""

    @staticmethod
    def _build_rrule(rrule: RecurrenceRule, dtstart: datetime.datetime):
        if not rrule:
            raise ValueError("No recurrence rule provided")
        return rrulestr("RRULE:" + rrule.to_rrule_string(), dtstart=dtstart)

    @staticmethod
    def truncate_rule_until_date(
        rule: RecurrenceRule, until_date: datetime.datetime
    ) -> RecurrenceRule | None:
        """Return a copy of ``rule`` truncated with UNTIL set to ``until_date``.

        The returned rule is an unsaved copy. Returns ``None`` if ``rule`` is
        falsy.
        """
        if not rule:
            return None

        if until_date.tzinfo is None:
            until_date = timezone.make_aware(until_date, timezone.get_current_timezone())

        truncated = copy.deepcopy(rule)
        truncated.count = None
        truncated.until = until_date
        return truncated

    @staticmethod
    def create_continuation_rule(
        original_rule: RecurrenceRule,
        new_start_date: datetime.datetime,
        original_start: datetime.datetime | None = None,
    ) -> RecurrenceRule | None:
        """Create a continuation rule starting at ``new_start_date``.

        If the original rule had a COUNT the method computes the remaining
        count. Returns ``None`` if there are no remaining occurrences.
        """
        if not original_rule:
            return None

        if new_start_date.tzinfo is None:
            new_start_date = timezone.make_aware(new_start_date, timezone.get_current_timezone())

        dtstart_for_count = original_start or new_start_date
        full_rrule = RecurrenceRuleSplitter._build_rrule(original_rule, dtstart_for_count)

        remaining: int | None = None
        if original_rule.count:
            # Count occurrences strictly before new_start_date, include the dtstart
            used = 0
            for occ in full_rrule:
                if occ >= new_start_date:
                    break
                used += 1
            remaining = original_rule.count - used
            if remaining <= 0:
                return None

        continuation = copy.deepcopy(original_rule)
        continuation.count = remaining if original_rule.count else None

        # If the UNTIL boundary is before or equal to the new start the
        # continuation would produce nothing.
        if continuation.until and continuation.until <= new_start_date:
            return None

        return continuation

    @staticmethod
    def split_at_date(
        original_rule: RecurrenceRule,
        split_date: datetime.datetime,
        original_start: datetime.datetime,
    ) -> tuple[RecurrenceRule | None, RecurrenceRule | None]:
        """Split ``original_rule`` at ``split_date`` into (truncated, continuation).

        ``truncated`` has UNTIL set to the last occurrence before ``split_date``.
        ``continuation`` is a rule suitable to generate occurrences from
        ``split_date`` forwards (may be ``None`` if nothing remains).
        """
        if not original_rule:
            return None, None

        if split_date.tzinfo is None:
            split_date = timezone.make_aware(split_date, timezone.get_current_timezone())
        if original_start.tzinfo is None:
            original_start = timezone.make_aware(original_start, timezone.get_current_timezone())

        r = RecurrenceRuleSplitter._build_rrule(original_rule, original_start)

        prev_occurrence = r.before(split_date, inc=False)
        truncated_rule: RecurrenceRule | None
        if prev_occurrence is None:
            truncated_rule = None
        else:
            truncated_rule = RecurrenceRuleSplitter.truncate_rule_until_date(
                original_rule, prev_occurrence
            )

        continuation_rule = RecurrenceRuleSplitter.create_continuation_rule(
            original_rule, split_date, original_start=original_start
        )

        return truncated_rule, continuation_rule


class OccurrenceValidator:
    """Helpers to validate and normalize modification dates against a recurrence."""

    @staticmethod
    def validate_modification_date(recurring_object, target_date: datetime.datetime) -> bool:
        """Return True if ``target_date`` is an occurrence of ``recurring_object``."""
        rule = getattr(recurring_object, "recurrence_rule", None)
        if not rule:
            return False

        dtstart = recurring_object.start_time
        if target_date.tzinfo is None:
            target_date = timezone.make_aware(target_date, timezone.get_current_timezone())

        r = RecurrenceRuleSplitter._build_rrule(rule, dtstart)
        occ = r.before(target_date, inc=True)
        return occ == target_date
