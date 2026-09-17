"""The pagination bound every list-shaped field on the public API applies.

This lived inside `public_api/queries.py` as `_slice_qs` until the aggregate
root fields needed the same bound. They cannot import it from there --
`queries.py` imports the aggregate fields, so the arrow only points one way --
so the check moved here and `queries.py` imports it back under its old name.
Every existing call site is unchanged, and there is still one definition of
"1 to 100" on this API rather than a second one that can drift.

The two messages are the ones the aggregations plan's error table publishes,
and the ones `public_api.aggregations.errors` repeats for the engine's own
raising path. They are spelled out here rather than imported so this module
stays free of the aggregation package, which pulls in the calendar models.
"""

from django_virtual_models import QuerySet
from graphql import GraphQLError


MIN_LIMIT = 1
MAX_LIMIT = 100

OFFSET_NEGATIVE_MESSAGE = "Offset must be non-negative"
LIMIT_OUT_OF_RANGE_MESSAGE = f"Limit must be between {MIN_LIMIT} and {MAX_LIMIT}"


def validate_pagination(offset: int, limit: int) -> None:
    """Refuse an out-of-range window before any query is built.

    Separated from the slicing below because an aggregate applies the same
    bound but does its own slicing, inside the plan the executor runs.
    """
    if offset < 0:
        raise GraphQLError(OFFSET_NEGATIVE_MESSAGE)
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise GraphQLError(LIMIT_OUT_OF_RANGE_MESSAGE)


def slice_queryset[TQuerySet: QuerySet](qs: TQuerySet, offset: int, limit: int) -> TQuerySet:
    """Apply a validated `offset` / `limit` window to `qs`."""
    validate_pagination(offset, limit)
    return qs[offset : offset + limit]
