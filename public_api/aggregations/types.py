"""Shared GraphQL output types for aggregates.

One type per field kind, so the *schema* decides which operations a field
offers. ``title { sum }`` is a validation error against ``StringAggregate``
before a resolver runs or a database connection is taken -- which is the whole
reason the aggregate types are split by kind rather than being one
``Aggregate`` type with every operation nullable on it.

These types carry values only. Nothing here runs a query: the executor fills a
row dict, and the per-entity resolvers added in later phases map that dict onto
these types.
"""

import datetime
import enum

import strawberry


@strawberry.enum(description="Bucket width for a temporal group-by dimension.")
class TemporalGranularity(enum.Enum):
    """How wide a temporal dimension's buckets are.

    Applied by the executor as a ``Trunc`` in the caller-supplied IANA
    timezone, so one consistent wall clock is used across a multi-region
    organization.
    """

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@strawberry.type(description="Aggregates available over a numeric field.")
class NumericAggregate:
    """``SUM`` / ``AVG`` / ``MIN`` / ``MAX`` over a numeric field.

    Every operation is nullable: a group whose rows all hold ``NULL`` for the
    field aggregates to ``NULL``, and that is reported rather than coerced to
    zero.
    """

    sum: float | None = None
    avg: float | None = None
    min: float | None = None
    max: float | None = None


@strawberry.type(description="Aggregates available over a string field.")
class StringAggregate:
    """``concat`` / ``MIN`` / ``MAX`` over a string field.

    ``concat`` is exposed as a field with arguments because the separator and
    the distinct flag change the generated SQL: the plan builder reads them off
    the selection before the query runs, and ``concat_value`` is the result the
    executor already computed with exactly those arguments. The resolver
    therefore returns a stored value rather than recomputing one -- no
    Python-side aggregation happens here.
    """

    min: str | None = None
    max: str | None = None

    #: Filled by the resolver from the executor's row dict. Not part of the schema.
    concat_value: strawberry.Private[str | None] = None

    @strawberry.field(
        description=(
            "Postgres string_agg over the group. Ordered by the aggregated value "
            "itself so the result is deterministic, including under distinct."
        )
    )
    def concat(self, separator: str = ",", distinct: bool = False) -> str | None:
        """Return the concatenation the executor computed for this group.

        The arguments are declared so they appear in the schema and in the
        GraphQL selection the plan builder reads; they are not applied here.
        """
        return self.concat_value


@strawberry.type(description="Aggregates available over a date/time field.")
class DateTimeAggregate:
    """``MIN`` / ``MAX`` over a temporal field.

    No ``SUM`` or ``AVG``: neither is meaningful over instants, and leaving
    them off the type is what makes asking for one a validation error.
    """

    min: datetime.datetime | None = None
    max: datetime.datetime | None = None


@strawberry.type(description="Aggregates available over a boolean field.")
class BooleanAggregate:
    """Row counts split by a boolean field's value.

    Rows where the field is ``NULL`` fall into neither count, so the two need
    not add up to the group's ``count``.
    """

    true_count: int = strawberry.field(
        default=0, description="Count of rows in the group where the field is true."
    )
    false_count: int = strawberry.field(
        default=0, description="Count of rows in the group where the field is false."
    )
