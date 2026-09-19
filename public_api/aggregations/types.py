"""Strawberry output types shared by every aggregate field.

The whole point of these four types is that the GraphQL schema, not a resolver,
decides which operations a field offers. ``title`` is a ``StringAggregate``, so
``title { sum }`` fails GraphQL validation before any resolver runs and before a
database connection is taken. A single ``[{op, field}]`` metric list could not
express that at all.

The types are plain carriers: the executor computes the values in SQL and a
resolver maps a row dict onto them. Nothing here reads the database, and nothing
here aggregates in Python.
"""

import dataclasses
import datetime
import enum
from collections.abc import Mapping

import strawberry


@strawberry.enum
class TemporalGranularity(enum.Enum):
    """Bucket width for a temporal group-by dimension.

    Buckets are computed in the caller-supplied IANA timezone, so ``DAY`` means
    a local wall-clock day rather than a UTC one. ``WEEK`` starts on Monday,
    matching Django's ``TruncWeek``.
    """

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@strawberry.type(description="Aggregates available over a numeric field.")
class NumericAggregate:
    """``sum`` / ``avg`` / ``min`` / ``max`` over a numeric column or expression.

    Every field is nullable: an aggregate the caller did not select is never
    computed, and ``SUM`` over a group whose values are all NULL is NULL.
    """

    sum: float | None = None
    avg: float | None = None
    min: float | None = None
    max: float | None = None


@strawberry.type(description="Aggregates available over a string field.")
class StringAggregate:
    """``concat`` / ``min`` / ``max`` over a text column.

    ``concat`` carries arguments, which means its value depends on how the
    caller selected it. The executor computes one value per distinct
    ``(separator, distinct)`` pair the document asked for and hands them over in
    ``concat_values``; the resolver below is a lookup, not a computation.
    """

    min: str | None = None
    max: str | None = None
    concat_values: strawberry.Private[Mapping[tuple[str, bool], str | None]] = dataclasses.field(
        default_factory=dict
    )

    @strawberry.field(
        description=(
            "Postgres string_agg. Ordered by the aggregated field so repeated "
            "runs of the same query return the same string."
        )
    )
    def concat(self, separator: str = ",", distinct: bool = False) -> str | None:
        return self.concat_values.get((separator, distinct))


@strawberry.type(description="Aggregates available over a date-time field.")
class DateTimeAggregate:
    """``min`` / ``max`` over a timestamp column.

    No ``sum`` or ``avg``: neither means anything over instants, and leaving
    them off the type is what stops a caller asking for one.
    """

    min: datetime.datetime | None = None
    max: datetime.datetime | None = None


@strawberry.type(description="Aggregates available over a boolean field.")
class BooleanAggregate:
    """Row counts either side of a boolean column.

    Both counts are non-null: a group exists because it has rows, so the two
    counts are always computable and always sum to the group's ``count``.
    """

    true_count: int = 0
    false_count: int = 0
