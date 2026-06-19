"""Audit diff computation helper."""

from __future__ import annotations


def compute_diff(before: dict, after: dict) -> dict | None:
    """Compute the diff between two state dicts.

    Returns a dict mapping changed field names to their before/after states, in the
    shape {field: {"old": before_value, "new": after_value}}.

    Keys that appear only in 'after' are treated as additions: {"old": None, "new": value}.
    Keys that appear only in 'before' are treated as removals: {"old": value, "new": None}.

    Keys with identical values (compared via ==) are omitted from the result.
    Nested dicts and lists are compared by equality, not recursively inspected.

    Returns None (not an empty dict) when there are no differences. This upholds the
    locked diff invariant: diff is always None or a NON-EMPTY dict. The None return
    value ensures that has_diff filters (using diff__isnull) stay meaningful.

    Args:
        before: The "old" state dict.
        after: The "new" state dict.

    Returns:
        A diff dict in {field: {"old": ..., "new": ...}} shape if there are changes,
        or None if before and after are identical.
    """
    result = {}

    # Check all keys in before: changed or removed.
    for key, before_value in before.items():
        after_value = after.get(key)
        if before_value != after_value:
            result[key] = {"old": before_value, "new": after_value}

    # Check all keys in after: added (keys not in before).
    for key, after_value in after.items():
        if key not in before:
            result[key] = {"old": None, "new": after_value}

    return result if result else None
