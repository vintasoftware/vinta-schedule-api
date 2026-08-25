"""Backend-agnostic definition of what an ``AuditQuery`` *means*.

``AuditQuery`` is the one filter object every ``AuditRepository`` accepts, but a
dataclass of optional fields only names the filters — it does not say what
``search`` matches or how ``has_diff`` treats an empty diff. This module is the
answer, written once in pure Python:

* :func:`record_matches` — does one ``AuditRecord`` satisfy one ``AuditQuery``?
* :func:`normalize_ordering` — validate a caller-supplied ordering against the
  portable allow-list.
* :func:`apply_query` — filter + order + paginate an in-memory sequence, i.e. a
  whole ``AuditRepository.query`` implementation for any backend that can hand
  over records.

A repository that can push the filters down to its store (``DjangoORMAuditRepository``
translates them to SQL) does that instead, but must agree with the semantics
here — ``audit/tests/test_filtering.py`` and the ORM query tests are written
against the same table of meanings. A repository that cannot push them down (an
in-memory one, a flat-file archive, an HTTP-backed store that only offers
list-by-time) gets a working, correct ``query`` for free by calling
:func:`apply_query`.

Adding a field to ``AuditQuery`` means teaching both this module and every
repository that filters natively.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import partial
from typing import TYPE_CHECKING

from audit.types import AuditPage, AuditQuery, AuditRecord


if TYPE_CHECKING:
    from datetime import datetime


#: Ordering fields any repository must support. Deliberately narrow and
#: portable: every entry is a scalar field present on ``AuditRecord`` itself, so
#: a non-ORM backend can honour it without a join. ``uid`` is here because it is
#: the only *total* order that is stable across backends — ``id`` is assigned
#: per repository, so ordering by it would give two copies of the same log a
#: different sequence. Code that needs another field must extend this list AND
#: the ORM repository's own handling; an unknown ordering silently falls back to
#: :data:`DEFAULT_ORDERING` rather than raising, so a hostile or stale value in
#: a URL cannot reach the database.
ALLOWED_ORDERING_FIELDS: frozenset[str] = frozenset(
    [
        "created_at",
        "uid",
        "action",
        "actor_type",
        "subject_type",
    ]
)

#: Newest first — what the admin changelist and every ad-hoc read want.
DEFAULT_ORDERING: tuple[str, ...] = ("-created_at",)

#: Oldest first, with ``uid`` breaking ties. Used by
#: ``AuditRepository.iter_records`` to walk a whole log exactly once: an audit
#: log is append-only, so ascending order puts new rows past the cursor rather
#: than shifting rows the walk already passed, and the ``uid`` tiebreak keeps
#: records written in the same instant from reshuffling between pages (which
#: offset pagination would otherwise turn into a skipped record).
STABLE_ITERATION_ORDERING: tuple[str, ...] = ("created_at", "uid")


def normalize_ordering(
    ordering: str | Sequence[str] | None,
    *,
    default: Sequence[str] = DEFAULT_ORDERING,
) -> tuple[str, ...]:
    """Validate an ordering against :data:`ALLOWED_ORDERING_FIELDS`.

    Accepts a single field or a sequence of them, each optionally prefixed with
    ``-`` for descending. Unknown fields are dropped; if nothing survives, the
    ``default`` is returned. Nothing raises — this runs on user-supplied query
    strings in the admin, where the safe answer to garbage is the default order,
    not a 500.

    Args:
        ordering: A field, a sequence of fields, or None.
        default: What to return when no supplied field is recognized.

    Returns:
        A tuple of validated ordering fields, never empty.
    """
    if ordering is None:
        return tuple(default)
    fields = (ordering,) if isinstance(ordering, str) else tuple(ordering)
    safe = tuple(f for f in fields if f.lstrip("-") in ALLOWED_ORDERING_FIELDS)
    return safe or tuple(default)


def _matches_search(record: AuditRecord, term: str) -> bool:
    """Case-insensitive match of ``term`` across the searchable columns.

    Mirrors the ORM repository's OR group exactly: subject_type, subject_id and
    subject_label by substring, plus actor_id by equality — and only when the
    term is all digits, because actor_id is an integer column and a non-numeric
    comparison is a database type error rather than a non-match.
    """
    lowered = term.lower()
    haystacks = (
        record.subject.subject_type,
        record.subject.subject_id,
        record.subject.subject_label,
    )
    if any(value is not None and lowered in value.lower() for value in haystacks):
        return True
    return bool(term.isdigit()) and record.actor.actor_id == int(term)


def record_matches(record: AuditRecord, q: AuditQuery) -> bool:  # noqa: PLR0911
    """Report whether ``record`` satisfies every active filter on ``q``.

    The reference semantics for ``AuditQuery``, and the rule every repository's
    native filtering must reproduce. Fields AND together; values within a field
    OR together; ``None`` means the filter is inactive and ``[]`` means it is
    active and unsatisfiable (SQL's ``IN ()``).

    Args:
        record: The record to test.
        q: The filters to test it against.

    Returns:
        True when the record matches every active filter.
    """
    if q.organization_id is not None and record.organization_id != q.organization_id:
        return False
    if q.uids is not None and record.uid not in q.uids:
        return False
    if q.actions is not None and record.action not in q.actions:
        return False
    if q.actor_types is not None and record.actor.actor_type not in q.actor_types:
        return False
    if q.actors is not None and record.actor.ref not in q.actors:
        return False
    if q.subject_types is not None and record.subject.subject_type not in q.subject_types:
        return False
    if q.subjects is not None and record.subject.key not in q.subjects:
        return False
    if q.affected_membership_ids is not None and not (
        # A record matches when it affects ANY of the named memberships.
        set(record.affected_membership_ids) & set(q.affected_membership_ids)
    ):
        return False
    # Half-open range: created_after inclusive, created_before exclusive,
    # matching the ORM repository's __gte / __lt pair. That is what makes
    # consecutive sync windows tile without overlapping or dropping a record.
    if q.created_after is not None and record.created_at < q.created_after:
        return False
    if q.created_before is not None and record.created_at >= q.created_before:
        return False
    if q.has_diff is not None and bool(record.diff) is not q.has_diff:
        return False
    return not (q.search is not None and not _matches_search(record, q.search))


def _sort_key(record: AuditRecord, field: str) -> tuple[int, object]:
    """Build a comparable key for one ordering field.

    Returns a ``(is_not_null, value)`` pair so None sorts before any real value
    without ever comparing None to a string — Postgres puts NULLs last on an
    ascending sort, but only ``subject_type``-shaped fields can be null here and
    none of them are in practice; the pair exists so a null cannot crash the
    sort.
    """
    value: datetime | str | object
    if field == "created_at":
        value = record.created_at
    elif field == "uid":
        value = str(record.uid)
    elif field == "action":
        value = record.action
    elif field == "actor_type":
        value = record.actor.actor_type
    else:  # "subject_type" — the only remaining allowed field
        value = record.subject.subject_type
    return (0, "") if value is None else (1, value)


def sort_records(
    records: Iterable[AuditRecord], ordering: Sequence[str] = DEFAULT_ORDERING
) -> list[AuditRecord]:
    """Order records by an already-normalized ordering.

    Applies the fields right-to-left through a stable sort, which is how you
    compose a multi-key ordering with mixed directions out of Python's
    single-key ``sorted``: the last field applied is the most significant.

    Args:
        records: The records to order.
        ordering: Normalized ordering fields (see :func:`normalize_ordering`).

    Returns:
        A new ordered list.
    """
    ordered = list(records)
    for field in reversed(tuple(ordering)):
        descending = field.startswith("-")
        ordered.sort(key=partial(_sort_key, field=field.lstrip("-")), reverse=descending)
    return ordered


def apply_query(
    records: Iterable[AuditRecord],
    q: AuditQuery,
    *,
    offset: int = 0,
    limit: int = 50,
    ordering: str | Sequence[str] = DEFAULT_ORDERING,
) -> AuditPage:
    """Filter, order and paginate an in-memory sequence of records.

    A complete ``AuditRepository.query`` implementation for any backend that
    holds its records in memory or can only hand them over unfiltered.

    ``total`` counts every match *before* pagination, so a caller always learns
    the full size of the result set rather than the size of the page.

    Args:
        records: Every record the backend holds (or a superset of the matches).
        q: The filters to apply.
        offset: Records to skip.
        limit: Maximum records to return.
        ordering: Ordering field(s); validated, so an unknown value falls back.

    Returns:
        The matching page.
    """
    matched = [record for record in records if record_matches(record, q)]
    ordered = sort_records(matched, normalize_ordering(ordering))
    return AuditPage(items=ordered[offset : offset + limit], total=len(matched))
