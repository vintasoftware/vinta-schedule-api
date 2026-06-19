"""Tests for audit.diff module."""

from __future__ import annotations

import datetime as dt

from audit.diff import compute_diff


class TestComputeDiff:
    """Tests for compute_diff helper."""

    def test_single_changed_key(self):
        """A single key with a different value produces a diff."""
        diff = compute_diff({"k": "v1"}, {"k": "v2"})
        assert diff == {"k": {"old": "v1", "new": "v2"}}

    def test_multiple_changed_keys(self):
        """Multiple keys with different values are all included."""
        diff = compute_diff(
            {"a": 1, "b": 2, "c": 3},
            {"a": 1, "b": 20, "c": 30},
        )
        assert diff == {"b": {"old": 2, "new": 20}, "c": {"old": 3, "new": 30}}

    def test_added_key(self):
        """A key present only in 'after' is included with old: None."""
        diff = compute_diff({"a": 1}, {"a": 1, "b": 2})
        assert diff == {"b": {"old": None, "new": 2}}

    def test_removed_key(self):
        """A key present only in 'before' is included with new: None."""
        diff = compute_diff({"a": 1, "b": 2}, {"a": 1})
        assert diff == {"b": {"old": 2, "new": None}}

    def test_no_changes(self):
        """Identical dicts return None, not an empty dict."""
        diff = compute_diff({"a": 1, "b": 2}, {"a": 1, "b": 2})
        assert diff is None

    def test_empty_dicts(self):
        """Two empty dicts return None."""
        diff = compute_diff({}, {})
        assert diff is None

    def test_mixed_additions_and_removals(self):
        """Mix of changed, added, and removed keys."""
        diff = compute_diff(
            {"a": 1, "b": 2, "c": 3},
            {"a": 10, "b": 2, "d": 4},
        )
        assert diff == {
            "a": {"old": 1, "new": 10},
            "c": {"old": 3, "new": None},
            "d": {"old": None, "new": 4},
        }

    def test_nested_dict_equality(self):
        """Nested dicts are compared by value equality, not recursively inspected."""
        before = {"meta": {"x": 1, "y": 2}}
        after = {"meta": {"x": 1, "y": 2}}
        diff = compute_diff(before, after)
        assert diff is None

    def test_nested_dict_changed(self):
        """A nested dict that changes produces a single entry with the entire dict."""
        before = {"meta": {"x": 1}}
        after = {"meta": {"x": 2}}
        diff = compute_diff(before, after)
        assert diff == {"meta": {"old": {"x": 1}, "new": {"x": 2}}}

    def test_nested_list_equality(self):
        """Lists are compared by value equality."""
        before = {"items": [1, 2, 3]}
        after = {"items": [1, 2, 3]}
        diff = compute_diff(before, after)
        assert diff is None

    def test_nested_list_changed(self):
        """A list that changes is recorded as a single diff entry."""
        before = {"items": [1, 2]}
        after = {"items": [1, 2, 3]}
        diff = compute_diff(before, after)
        assert diff == {"items": {"old": [1, 2], "new": [1, 2, 3]}}

    def test_null_values(self):
        """None values are handled like any other value."""
        diff = compute_diff({"a": None}, {"a": 1})
        assert diff == {"a": {"old": None, "new": 1}}

    def test_bool_values(self):
        """Boolean values are compared correctly."""
        diff = compute_diff({"flag": True}, {"flag": False})
        assert diff == {"flag": {"old": True, "new": False}}

    def test_zero_values(self):
        """Zero is distinguished from None/falsy values."""
        diff = compute_diff({"count": 0}, {"count": 1})
        assert diff == {"count": {"old": 0, "new": 1}}

    def test_empty_string(self):
        """Empty string is distinguished from None."""
        diff = compute_diff({"text": ""}, {"text": "hello"})
        assert diff == {"text": {"old": "", "new": "hello"}}

    def test_before_empty_after_non_empty(self):
        """Empty before dict; all after keys are additions."""
        diff = compute_diff({}, {"a": 1, "b": 2})
        assert diff == {
            "a": {"old": None, "new": 1},
            "b": {"old": None, "new": 2},
        }

    def test_before_non_empty_after_empty(self):
        """Non-empty before; empty after means all keys are removed."""
        diff = compute_diff({"a": 1, "b": 2}, {})
        assert diff == {
            "a": {"old": 1, "new": None},
            "b": {"old": 2, "new": None},
        }

    def test_single_value_type_change(self):
        """Value type changes are recorded as differences."""
        diff = compute_diff({"val": 123}, {"val": "123"})
        assert diff == {"val": {"old": 123, "new": "123"}}

    def test_large_nested_structures(self):
        """Large nested structures (dicts, lists) are compared as units, not recursively."""
        before = {
            "config": {
                "deep": {
                    "nested": {"value": [1, 2, 3, {"a": 1}]},
                }
            }
        }
        after = {
            "config": {
                "deep": {
                    "nested": {"value": [1, 2, 3, {"a": 2}]},
                }
            }
        }
        diff = compute_diff(before, after)
        # The nested structure is different, so the top key changed
        assert diff == {
            "config": {
                "old": {"deep": {"nested": {"value": [1, 2, 3, {"a": 1}]}}},
                "new": {"deep": {"nested": {"value": [1, 2, 3, {"a": 2}]}}},
            }
        }

    def test_added_key_with_none_value(self):
        """A key with explicit None value in 'after' is recorded as an addition."""
        diff = compute_diff({}, {"k": None})
        assert diff == {"k": {"old": None, "new": None}}

    def test_non_serializable_values_preserved(self):
        """Non-serializable values like datetime objects are preserved as-is in diff."""
        before = {"created_at": dt.datetime(2020, 1, 1)}
        after = {"created_at": dt.datetime(2021, 1, 1)}
        diff = compute_diff(before, after)
        assert diff == {
            "created_at": {
                "old": dt.datetime(2020, 1, 1),
                "new": dt.datetime(2021, 1, 1),
            }
        }
