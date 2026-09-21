"""The four aggregate output types, plus the temporal granularity enum.

These are the types that make "strings are not summable" a *schema* fact
rather than a resolver check: a model field is exposed as exactly one of
them, so ``title { sum }`` fails GraphQL validation before a resolver runs.
``public_api.aggregations.registry`` is the one place that decides which
field gets which.

Nothing here computes anything. Every value is read out of the row dict the
database already produced -- see the note on ``StringAggregate.concat``,
which is the only field that looks like it might be doing work.

These types are not attached to the schema yet. Phase 3 builds the per-entity
row types that carry them.
"""

import datetime
import enum

import strawberry


@strawberry.enum(description="Bucket width for a temporal group-by dimension.")
class TemporalGranularity(enum.Enum):
    """How wide a bucket a temporal dimension is truncated to.

    Buckets are sparse: a day with no matching rows produces no row. Zero
    filling is the client's job, because the alternative inside this plan's
    constraints is either a date-dimension table or a Python fill loop.

    ``WEEK`` follows Django's ``TruncWeek``, which starts weeks on Monday.
    """

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@strawberry.type(description="Aggregates available over a numeric field.")
class NumericAggregate:
    """``SUM`` / ``AVG`` / ``MIN`` / ``MAX`` over one numeric column.

    Every field is nullable: an aggregate over a group whose rows all hold
    ``NULL`` is ``NULL``, and saying so is more honest than reporting a zero
    the data does not contain.
    """

    sum: float | None = None
    avg: float | None = None
    min: float | None = None
    max: float | None = None


@strawberry.type(description="Aggregates available over a string field.")
class StringAggregate:
    """``MIN`` / ``MAX`` / ``string_agg`` over one text column."""

    min: str | None = None
    max: str | None = None

    #: The ``string_agg`` result the database returned for this group, or
    #: ``None`` when the selection did not ask for one. Private because the
    #: public reader is the ``concat`` field below, which carries the
    #: arguments that shaped it.
    concat_value: strawberry.Private[str | None] = None

    @strawberry.field(
        description=(
            "Postgres string_agg over the group. Ordered by the group's ordering for determinism."
        )
    )
    def concat(self, separator: str = ",", distinct: bool = False) -> str | None:
        """Return the concatenation the database computed for this group.

        The arguments are declared so they appear in the schema and so the
        field factory can read them off the selection when it builds the
        plan -- by the time this resolver runs, ``separator`` and ``distinct``
        have already been baked into the ``string_agg`` call that produced
        ``concat_value``. This resolver joins nothing; doing the work here
        would be the Python-side aggregation the plan rules out.
        """
        return self.concat_value


@strawberry.type(description="Aggregates available over a datetime field.")
class DateTimeAggregate:
    """``MIN`` / ``MAX`` over one datetime column.

    No ``SUM`` or ``AVG``: neither means anything over instants, and leaving
    them off the type is what stops a caller asking.
    """

    min: datetime.datetime | None = None
    max: datetime.datetime | None = None


@strawberry.type(description="Aggregates available over a boolean field.")
class BooleanAggregate:
    """How the group splits on one boolean column.

    Both counts are non-null and both are filtered ``COUNT``s rather than a
    total and a subtraction, so a ``NULL`` in a nullable boolean column is
    counted in neither -- which is the truthful answer for a row that is
    neither true nor false.
    """

    true_count: int = strawberry.field(
        default=0, description="Count of rows in the group where the field is true."
    )
    false_count: int = strawberry.field(
        default=0, description="Count of rows in the group where the field is false."
    )
