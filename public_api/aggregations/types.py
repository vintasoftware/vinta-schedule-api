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

from public_api.aggregations.errors import ConcatArgumentsMismatchError


# The default ``concat`` separator, declared once. It is the GraphQL argument's
# default, the registry's default metric option, and the executor's fallback --
# three places that have to agree for a row key to describe the string it holds.
DEFAULT_CONCAT_SEPARATOR = ","


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

    # Held privately and returned by the ``concat`` resolver below, alongside
    # the arguments the query was actually built for. ``separator`` and
    # ``distinct`` are query *arguments*: they change the SQL rather than the
    # presentation, so the resolver that builds the query plan reads them off
    # the selection and asks the database for exactly this string. By the time
    # the field resolves, the work is done -- and the two fields below are what
    # let it prove that the work done matches the work asked for.
    concat_value: strawberry.Private[str | None] = None
    concat_separator: strawberry.Private[str] = DEFAULT_CONCAT_SEPARATOR
    concat_distinct: strawberry.Private[bool] = False

    @strawberry.field(
        description=(
            "Postgres string_agg over the group. Ordered by the aggregated value "
            "so repeated runs of the same query return the same string."
        )
    )
    def concat(
        self, separator: str = DEFAULT_CONCAT_SEPARATOR, distinct: bool = False
    ) -> str | None:
        """Return the concatenation the query plan already asked the database for.

        The arguments are checked rather than ignored. They were consumed when
        the plan was built, so a mismatch here means the plan builder did not
        read them off the selection -- in which case this string joins on a
        separator the caller did not ask for, and returning it would be wrong
        in a way nothing downstream could detect.
        """
        if separator != self.concat_separator or distinct != self.concat_distinct:
            raise ConcatArgumentsMismatchError
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
