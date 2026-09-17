"""The four aggregate output types every entity shares, plus the bucketing enum.

The plan's central schema decision lives here: *type-appropriate operations are
enforced by the schema, not the resolver*. A model field becomes a GraphQL
field whose type is one of these four, so ``title { sum }`` fails GraphQL
validation before a resolver runs or a database connection is taken. The
registry (:mod:`public_api.aggregations.registry`) is what decides which of the
four a given model field gets.

Nothing here is attached to the schema yet -- the root fields that expose these
types arrive in a later phase of the plan. Until then these are unreferenced
type definitions and the published SDL is unchanged.
"""

import datetime
import enum

import strawberry


@strawberry.enum(description="Bucket size for a temporal group-by dimension.")
class TemporalGranularity(enum.Enum):
    """How wide a bucket a temporal dimension is truncated to.

    The bucket boundary is computed in the caller-supplied IANA timezone, not
    in the row's own timezone, so one result set reads on one wall clock.
    ``WEEK`` starts on Monday, matching Django's ``TruncWeek``.
    """

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


@strawberry.type(description="Aggregates available over a numeric field.")
class NumericAggregate:
    """``sum`` / ``avg`` / ``min`` / ``max`` over an integer, float, decimal or
    duration field.

    Every field is nullable: an aggregate over a group whose rows all hold
    ``NULL`` is ``NULL``, which is SQL's answer and not an error.
    """

    sum: float | None = None
    avg: float | None = None
    min: float | None = None
    max: float | None = None


@strawberry.type(description="Aggregates available over a string field.")
class StringAggregate:
    """``concat`` / ``min`` / ``max`` over a char or text field.

    ``sum`` and ``avg`` are absent by construction rather than by a runtime
    check -- that absence is the whole point of having four separate types.
    """

    min: str | None = None
    max: str | None = None

    # Held privately and returned by the ``concat`` resolver below. The
    # separator and distinctness are query *arguments*, so they change the SQL
    # rather than the presentation: the resolver that builds the query plan
    # reads them off the selection and asks the database for exactly this
    # string. By the time the field resolves, the work is done.
    concat_value: strawberry.Private[str | None] = None

    @strawberry.field(
        description=(
            "Postgres string_agg over the group. Ordered by the aggregated value "
            "so repeated runs of the same query return the same string."
        )
    )
    def concat(self, separator: str = ",", distinct: bool = False) -> str | None:
        """Return the concatenation the query plan already asked the database for.

        The arguments are declared here because they belong to this field in
        the published schema, and they are consumed when the plan is built.
        """
        return self.concat_value


@strawberry.type(description="Aggregates available over a date or datetime field.")
class DateTimeAggregate:
    """``min`` / ``max`` over a temporal field.

    There is no ``sum`` or ``avg``: neither means anything over instants, and
    leaving them off the type is what stops a caller asking for them.
    """

    min: datetime.datetime | None = None
    max: datetime.datetime | None = None


@strawberry.type(description="Counts over a boolean field.")
class BooleanAggregate:
    """How many rows in the group hold ``True`` and how many hold ``False``.

    A nullable boolean's ``NULL`` rows are in neither count, so the two do not
    necessarily add up to the group's ``count``.
    """

    true_count: int = 0
    false_count: int = 0
