"""Unit tests for audit.filtering — the shared meaning of an AuditQuery.

The filter *semantics* are proved end-to-end against real backends in
test_repository_conformance.py. What is tested here is the module itself: the
pieces a non-ORM backend leans on directly (ordering validation, multi-key
sorting, total-before-pagination) and the edge cases that are awkward to reach
through a repository.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from audit.filtering import (
    ALLOWED_ORDERING_FIELDS,
    DEFAULT_ORDERING,
    STABLE_ITERATION_ORDERING,
    apply_query,
    normalize_ordering,
    record_matches,
    sort_records,
)
from audit.types import ActorSnapshot, AuditQuery, AuditRecord, SubjectRef


def make_record(
    *,
    record_id: int = 1,
    created_at: dt.datetime | None = None,
    organization_id: int = 1,
    action: str = "create",
    actor_type: str = "system",
    actor_id: int | None = None,
    subject_type: str = "app.Model",
    subject_id: str = "1",
    subject_label: str | None = None,
    affected_membership_ids: list[int] | None = None,
    diff: dict | None = None,
    uid: uuid.UUID | None = None,
) -> AuditRecord:
    return AuditRecord(
        id=record_id,
        uid=uid or uuid.uuid7(),
        created_at=created_at or dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC),
        organization_id=organization_id,
        action=action,
        actor=ActorSnapshot(actor_type=actor_type, actor_id=actor_id),
        subject=SubjectRef(
            subject_type=subject_type, subject_id=subject_id, subject_label=subject_label
        ),
        affected_membership_ids=affected_membership_ids or [],
        diff=diff,
    )


class TestNormalizeOrdering:
    def test_none_gives_the_default(self):
        assert normalize_ordering(None) == DEFAULT_ORDERING

    def test_a_bare_string_is_accepted(self):
        assert normalize_ordering("created_at") == ("created_at",)

    def test_a_descending_prefix_is_kept(self):
        assert normalize_ordering("-created_at") == ("-created_at",)

    def test_a_sequence_is_kept_in_order(self):
        assert normalize_ordering(["created_at", "uid"]) == ("created_at", "uid")

    def test_unknown_fields_are_dropped(self):
        assert normalize_ordering(["created_at", "password"]) == ("created_at",)

    def test_an_ordering_with_nothing_left_falls_back(self):
        """A hostile or stale value in a URL must not reach the backend."""
        assert normalize_ordering("; DROP TABLE audit_audit; --") == DEFAULT_ORDERING

    def test_an_empty_sequence_falls_back(self):
        assert normalize_ordering([]) == DEFAULT_ORDERING

    def test_a_caller_can_supply_its_own_fallback(self):
        assert normalize_ordering("nope", default=("uid",)) == ("uid",)

    @pytest.mark.parametrize("field", sorted(ALLOWED_ORDERING_FIELDS))
    def test_every_allowed_field_survives_in_both_directions(self, field):
        assert normalize_ordering(field) == (field,)
        assert normalize_ordering(f"-{field}") == (f"-{field}",)

    def test_the_stable_iteration_ordering_is_itself_valid(self):
        """iter_records depends on this ordering surviving validation intact."""
        assert normalize_ordering(STABLE_ITERATION_ORDERING) == STABLE_ITERATION_ORDERING


class TestSortRecords:
    def test_descending_created_at_is_newest_first(self):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        records = [
            make_record(record_id=i, created_at=base + dt.timedelta(days=i)) for i in range(3)
        ]
        assert [r.id for r in sort_records(records, ("-created_at",))] == [2, 1, 0]

    def test_ascending_created_at_is_oldest_first(self):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        records = [
            make_record(record_id=i, created_at=base + dt.timedelta(days=i)) for i in range(3)
        ]
        assert [r.id for r in sort_records(records, ("created_at",))] == [0, 1, 2]

    def test_a_second_field_breaks_ties(self):
        """The tiebreak iter_records relies on to page a log without losing rows."""
        same_instant = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        uids = sorted((uuid.uuid7() for _ in range(3)), key=str)
        records = [
            make_record(record_id=i, created_at=same_instant, uid=uid)
            for i, uid in enumerate(reversed(uids))
        ]
        ordered = sort_records(records, STABLE_ITERATION_ORDERING)
        assert [r.uid for r in ordered] == uids

    def test_mixed_directions_compose(self):
        records = [
            make_record(record_id=1, action="b", subject_type="x"),
            make_record(record_id=2, action="a", subject_type="y"),
            make_record(record_id=3, action="a", subject_type="x"),
        ]
        ordered = sort_records(records, ("action", "-subject_type"))
        assert [r.id for r in ordered] == [2, 3, 1]

    def test_sorting_does_not_mutate_the_input(self):
        records = [make_record(record_id=i) for i in range(3)]
        sort_records(records, ("-created_at",))
        assert [r.id for r in records] == [0, 1, 2]


class TestRecordMatches:
    def test_an_empty_query_matches_everything(self):
        assert record_matches(make_record(), AuditQuery())

    def test_fields_and_together(self):
        record = make_record(action="create", actor_type="system")
        assert record_matches(record, AuditQuery(actions=["create"], actor_types=["system"]))
        assert not record_matches(
            record, AuditQuery(actions=["create"], actor_types=["membership"])
        )

    def test_values_within_a_field_or_together(self):
        record = make_record(action="create")
        assert record_matches(record, AuditQuery(actions=["create", "update"]))

    def test_an_empty_list_matches_nothing(self):
        assert not record_matches(make_record(), AuditQuery(actions=[]))

    def test_a_label_does_not_participate_in_subject_identity(self):
        """SubjectKey deliberately drops the label — it is a snapshot, not identity."""
        record = make_record(subject_id="42", subject_label="Renamed Since")
        assert record_matches(record, AuditQuery(subjects=[record.subject.key]))

    def test_search_skips_the_integer_column_for_a_non_numeric_term(self):
        """Matching actor_id against a non-numeric term is a type error in SQL."""
        record = make_record(actor_id=42, subject_label=None)
        assert not record_matches(record, AuditQuery(search="not-a-number"))

    def test_search_matches_a_numeric_term_against_actor_id(self):
        assert record_matches(make_record(actor_id=42), AuditQuery(search="42"))

    def test_search_ignores_a_null_label_without_raising(self):
        assert not record_matches(make_record(subject_label=None), AuditQuery(search="zzz"))

    def test_has_diff_treats_an_empty_dict_as_no_diff(self):
        assert record_matches(make_record(diff={}), AuditQuery(has_diff=False))
        assert not record_matches(make_record(diff={}), AuditQuery(has_diff=True))


class TestApplyQuery:
    def test_total_counts_matches_before_pagination(self):
        """A caller must learn the size of the result set, not of the page."""
        records = [make_record(record_id=i) for i in range(10)]
        page = apply_query(records, AuditQuery(), offset=0, limit=3)
        assert len(page.items) == 3
        assert page.total == 10

    def test_offset_and_limit_slice_the_ordered_result(self):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        records = [
            make_record(record_id=i, created_at=base + dt.timedelta(days=i)) for i in range(5)
        ]
        page = apply_query(records, AuditQuery(), offset=1, limit=2, ordering="created_at")
        assert [r.id for r in page.items] == [1, 2]

    def test_a_zero_limit_still_totals_correctly(self):
        """This is how the interface's count() is answered."""
        records = [make_record(record_id=i) for i in range(4)]
        page = apply_query(records, AuditQuery(), limit=0)
        assert page.items == []
        assert page.total == 4

    def test_an_offset_past_the_end_yields_an_empty_page(self):
        page = apply_query([make_record()], AuditQuery(), offset=10, limit=5)
        assert page.items == []
        assert page.total == 1

    def test_an_unknown_ordering_falls_back_rather_than_raising(self):
        records = [make_record(record_id=i) for i in range(3)]
        assert apply_query(records, AuditQuery(), ordering="nope").total == 3
