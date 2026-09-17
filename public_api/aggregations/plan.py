"""The immutable description of one aggregate request.

An :class:`AggregateQueryPlan` is built from a GraphQL selection *before* any
ORM call, and is the seam the rest of the feature hangs off: the executor turns
one into a queryset, and the audit hook records one without ever touching the
executor. That separation is deliberate -- audit logs the *shape* of a query,
so it must be able to describe a query it did not run and whose results it
never sees.

Everything here is a frozen dataclass and everything here is pure: this module
imports no models, no registry and no queryset. Which field paths an entity
actually allows is the registry's job (:mod:`public_api.aggregations.registry`),
and the checks that need it run in
:func:`public_api.aggregations.registry.validate_plan`. What a plan validates
for itself is only what it can see from the inside -- that it asks for
something, and that no two selections claim the same row key.
"""

import datetime
import enum
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from django.db.models import Q

from public_api.aggregations.errors import (
    AliasCollisionError,
    EmptyAggregatePlanError,
    LimitOutOfRangeError,
    MisplacedFrameBoundError,
    MissingBucketTimezoneError,
    OffsetNegativeError,
    UnknownOrderAliasError,
    WindowFrameOffsetError,
    WindowFrameOrderError,
    WindowOrderingRequiredError,
    WindowSourceMissingError,
)
from public_api.aggregations.types import TemporalGranularity


# The same bound every list field on this API applies, reused so an aggregate
# cannot be the one field that returns more rows than the rest.
MIN_LIMIT = 1
MAX_LIMIT = 100


class AggregateOp(enum.Enum):
    """An aggregate operation, independent of which field it runs over.

    Which of these a given field accepts is decided once, in the registry, from
    the field's type. ``TRUE_COUNT`` / ``FALSE_COUNT`` are the two halves of
    ``BooleanAggregate``; they are counts rather than a fifth aggregate type
    because that is what they compile to (``COUNT(id) FILTER (WHERE ...)``).
    """

    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    CONCAT = "concat"
    TRUE_COUNT = "true_count"
    FALSE_COUNT = "false_count"


class AggregatableEntity(enum.Enum):
    """The entity families this API aggregates over.

    Closed by design. The plan's non-goals rule out aggregating memberships,
    webhook events, change requests, booking policies and billing models, so a
    new member here is a product decision rather than a wiring detail.
    """

    CALENDAR_EVENT = "calendar_event"
    AVAILABLE_TIME = "available_time"
    BLOCKED_TIME = "blocked_time"
    APPOINTMENT_TYPE = "appointment_type"
    CALENDAR = "calendar"
    CALENDAR_POOL = "calendar_pool"


def _freeze_options(options: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only copy so a frozen spec is frozen all the way down."""
    return MappingProxyType(dict(options))


@dataclass(frozen=True)
class MetricSpec:
    """One requested aggregate: the model field, the operation, and the alias
    the row dict will carry it under.

    ``field_path`` names a field the registry registered as aggregatable. For
    a plain column that name *is* the ORM path (``"title"``); for a derived
    metric it is the registered name the registry knows an expression for
    (``"duration_minutes"``). Under ``COUNT`` it is either ``"id"``, the
    group's row count, or the name of a registered relation count
    (``"event_count"``).

    ``options`` carries the operation's own arguments: ``separator`` and
    ``distinct`` for ``CONCAT``, ``distinct`` for ``COUNT``.
    """

    alias: str
    field_path: str
    op: AggregateOp
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _freeze_options(self.options))

    @classmethod
    def row_count(cls, alias: str = "count", *, distinct: bool = False) -> "MetricSpec":
        """The group's row count -- ``Count("id")``, as the plan prescribes."""
        return cls(
            alias=alias,
            field_path="id",
            op=AggregateOp.COUNT,
            options={"distinct": distinct},
        )

    def as_audit_dict(self) -> dict[str, Any]:
        """A stable, value-free description of this metric.

        Names the alias, the field path and the operation, and nothing that was
        aggregated. The audit trail records query shape; recording a value here
        would turn the trail itself into a store of the data it is auditing.
        """
        return {
            "alias": self.alias,
            "field_path": self.field_path,
            "op": self.op.value,
            "options": {key: self.options[key] for key in sorted(self.options)},
        }


@dataclass(frozen=True)
class DimensionSpec:
    """One GROUP BY dimension. ``granularity`` is set only for temporal fields.

    ``field_path`` names a field the registry registered as groupable; the
    registry translates that name to the column or expression to group on
    (``"calendar_id"`` reads the concrete ``calendar_fk`` column).

    ``tzinfo`` is the caller-supplied IANA zone the bucket boundary is computed
    in, and is **required** whenever ``granularity`` is set. It is a resolved
    :class:`zoneinfo.ZoneInfo` rather than a string because the name has
    already been validated by the time a plan exists -- an unknown name is
    refused before this dataclass is built.
    """

    alias: str
    field_path: str
    granularity: TemporalGranularity | None = None
    tzinfo: zoneinfo.ZoneInfo | None = None

    def __post_init__(self) -> None:
        if self.granularity is not None and self.tzinfo is None:
            raise MissingBucketTimezoneError(self.field_path)

    def as_audit_dict(self) -> dict[str, Any]:
        """A stable, value-free description of this dimension."""
        return {
            "alias": self.alias,
            "field_path": self.field_path,
            "granularity": self.granularity.value if self.granularity else None,
            "timezone": str(self.tzinfo) if self.tzinfo else None,
        }


@dataclass(frozen=True)
class FilterBounds:
    """What the caller narrowed to, recorded for the audit trail.

    The date range is the mandatory bounded window every aggregate field
    carries. ``predicates`` holds the scalar id predicates alongside it --
    record ids only, never free text, because this object is serialized into an
    audit record.
    """

    start: datetime.datetime | None = None
    end: datetime.datetime | None = None
    predicates: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "predicates", _freeze_options(self.predicates))

    def as_audit_dict(self) -> dict[str, Any]:
        """A stable description of the bounds, ids included, text excluded."""
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "predicates": {key: self.predicates[key] for key in sorted(self.predicates)},
        }


@dataclass(frozen=True)
class HavingSpec:
    """A predicate applied *after* aggregation, which Django renders as HAVING.

    Held as a ``Q`` over annotation aliases so the executor stays out of the
    business of interpreting comparison inputs: the layer that owns the typed
    ``having`` GraphQL input builds the ``Q``, and the executor only has to
    know where in the chain to apply it.
    """

    predicate: Q


@dataclass(frozen=True)
class OrderSpec:
    """Order the grouped rows by one alias -- a dimension or a metric.

    The alias must be one the plan already produces; ordering by anything else
    would add a column to the GROUP BY and silently change the result.
    """

    alias: str
    descending: bool = False

    def as_order_by(self) -> str:
        """The string ``QuerySet.order_by`` wants."""
        return f"-{self.alias}" if self.descending else self.alias


class WindowFunction(enum.Enum):
    """What a window metric computes over its frame.

    Each one compiles to a real ``OVER`` clause. ``RANK`` takes no source
    metric -- it ranks rows by the window's own ``ORDER BY`` -- while the other
    three window over an aggregate the plan already computes.
    """

    RUNNING_SUM = "running_sum"
    MOVING_AVG = "moving_avg"
    RANK = "rank"
    PERCENT_OF_TOTAL = "percent_of_total"


# ``running_*`` is cumulative *by definition*, so it fixes its own frame and
# ignores the caller's; a "running total" over a three-row frame is not a
# running total. The caller's frame is what ``moving_avg`` reads, which is the
# metric a frame is actually for. ``rank`` and ``percent_of_total`` take no
# frame at all -- one is a position, the other is over the whole partition.
FRAMED_WINDOW_FUNCTIONS = frozenset({WindowFunction.MOVING_AVG})


@dataclass(frozen=True)
class WindowMetricSpec:
    """One window metric: what to compute, and over which grouped metric.

    ``source_alias`` names a metric this same plan annotates, rather than
    repeating its definition, so the running total is guaranteed to be over
    exactly the number the row displays. It is empty for :data:`WindowFunction.RANK`,
    which has no source.
    """

    alias: str
    function: WindowFunction
    source_alias: str = ""

    def __post_init__(self) -> None:
        if self.function is WindowFunction.RANK:
            return
        if not self.source_alias:
            raise WindowSourceMissingError(self.alias, self.function.value)

    def as_audit_dict(self) -> dict[str, Any]:
        """A stable, value-free description of this window metric."""
        return {
            "alias": self.alias,
            "function": self.function.value,
            "source_alias": self.source_alias,
        }


@dataclass(frozen=True)
class WindowSpec:
    """A window function applied over the grouped result.

    ``partition_by`` and ``order_by`` hold *row aliases* -- the same keys the
    grouped rows come back under -- because that is what a window over a
    grouped result can address. Which of those aliases are legal is the
    registry's and the plan's business, not this dataclass's; what it enforces
    is the one rule that is true of every window regardless of entity: an
    unordered running total is meaningless rather than merely wrong, so a
    window with no ordering is refused rather than executed.
    """

    order_by: tuple[OrderSpec, ...] = ()
    partition_by: tuple[str, ...] = ()
    metrics: tuple[WindowMetricSpec, ...] = ()
    frame_type: str = "ROWS"
    frame_start: str = "UNBOUNDED_PRECEDING"
    frame_end: str = "CURRENT_ROW"
    frame_start_offset: int | None = None
    frame_end_offset: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_by", tuple(self.order_by))
        object.__setattr__(self, "partition_by", tuple(self.partition_by))
        object.__setattr__(self, "metrics", tuple(self.metrics))

        if not self.order_by:
            raise WindowOrderingRequiredError

        for bound, offset in (
            (self.frame_start, self.frame_start_offset),
            (self.frame_end, self.frame_end_offset),
        ):
            if bound in _OFFSET_BOUNDS and offset is None:
                raise WindowFrameOffsetError(bound)
            if bound in _OFFSET_BOUNDS and offset is not None and offset < 0:
                raise WindowFrameOffsetError(bound)

        # An unbounded bound reaches the database as "no limit in this
        # direction", and which direction that is comes from *which side of the
        # frame it sits on* rather than from its name. Putting one on the wrong
        # side is therefore not refused downstream -- it is silently read as
        # the opposite edge, and the frame ends up spanning the whole
        # partition. Refused here, where the name is still available to say so.
        if self.frame_start == "UNBOUNDED_FOLLOWING":
            raise MisplacedFrameBoundError(self.frame_start, "start")
        if self.frame_end == "UNBOUNDED_PRECEDING":
            raise MisplacedFrameBoundError(self.frame_end, "end")

        # A frame whose start sorts after its end describes no rows. Django's
        # backend catches the both-integers case, but as a bare `ValueError`
        # while compiling the SQL, which surfaces as an internal error.
        if _frame_position(self.frame_start, self.frame_start_offset) > _frame_position(
            self.frame_end, self.frame_end_offset
        ):
            raise WindowFrameOrderError

    def as_audit_dict(self) -> dict[str, Any]:
        """A stable description of the window's shape."""
        return {
            "partition_by": list(self.partition_by),
            "order_by": [order.as_order_by() for order in self.order_by],
            "metrics": [metric.as_audit_dict() for metric in self.metrics],
            "frame_type": self.frame_type,
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "frame_start_offset": self.frame_start_offset,
            "frame_end_offset": self.frame_end_offset,
        }


# The two bounds that mean nothing without a row count attached.
_OFFSET_BOUNDS = frozenset({"PRECEDING", "FOLLOWING"})


def _frame_position(bound: str, offset: int | None) -> float:
    """Where a frame bound sits, as a signed row offset from the current row.

    Negative is behind, positive is ahead, and the unbounded pair are the
    infinities -- the same reading the executor applies when it hands these to
    Django, so ordering them here orders the frame that actually gets built.
    """
    if bound == "CURRENT_ROW":
        return 0.0
    if bound == "PRECEDING":
        return -float(offset or 0)
    if bound == "FOLLOWING":
        return float(offset or 0)
    if bound == "UNBOUNDED_FOLLOWING":
        return float("inf")
    return float("-inf")


@dataclass(frozen=True)
class AggregateQueryPlan:
    """The fully resolved request, built from the GraphQL selection before any
    ORM call. This is what the audit hook records and what the executor turns
    into a queryset -- the seam that keeps audit independent of execution.
    """

    entity: AggregatableEntity
    dimensions: tuple[DimensionSpec, ...]
    metrics: tuple[MetricSpec, ...]
    filter_bounds: FilterBounds = field(default_factory=FilterBounds)
    having: HavingSpec | None = None
    order_by: tuple[OrderSpec, ...] = ()
    window: WindowSpec | None = None
    limit: int = MAX_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        # Sequences arrive as tuples from every caller in this repo, but a list
        # would make two otherwise-identical plans compare unequal and would
        # let a caller mutate a "frozen" plan after the fact.
        object.__setattr__(self, "dimensions", tuple(self.dimensions))
        object.__setattr__(self, "metrics", tuple(self.metrics))
        object.__setattr__(self, "order_by", tuple(self.order_by))

        if not self.dimensions:
            raise EmptyAggregatePlanError(
                "An aggregate plan needs at least one dimension: `.values()` with no "
                "arguments groups by every column, which dumps rows instead of "
                "aggregating them."
            )
        if not self.metrics:
            raise EmptyAggregatePlanError("An aggregate plan needs at least one metric.")

        self._reject_duplicate_aliases()

        if self.offset < 0:
            raise OffsetNegativeError
        if not MIN_LIMIT <= self.limit <= MAX_LIMIT:
            raise LimitOutOfRangeError

        self._reject_unknown_order_aliases(set(self.aliases))

    def _reject_duplicate_aliases(self) -> None:
        """One alias, one row key. A clash would overwrite, not merge."""
        seen: set[str] = set()
        for alias in [spec.alias for spec in self.dimensions] + [
            spec.alias for spec in self.metrics
        ]:
            if alias in seen:
                raise AliasCollisionError(alias)
            seen.add(alias)

    def _reject_unknown_order_aliases(self, known: set[str]) -> None:
        """Ordering by an alias the plan does not produce is not a no-op.

        Django would resolve the name against the model instead, which adds a
        column to the GROUP BY and changes the grouping the caller asked for.
        """
        for order in self.order_by:
            if order.alias not in known:
                raise UnknownOrderAliasError(order.alias)

    @property
    def dimension_aliases(self) -> tuple[str, ...]:
        """Row keys holding the group key, in GROUP BY order."""
        return tuple(spec.alias for spec in self.dimensions)

    @property
    def metric_aliases(self) -> tuple[str, ...]:
        """Row keys holding an aggregated value."""
        return tuple(spec.alias for spec in self.metrics)

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every row key this plan produces."""
        return self.dimension_aliases + self.metric_aliases

    def as_audit_dict(self) -> dict[str, Any]:
        """A stable, value-free description of the whole request.

        Everything an access trail needs to answer "who asked what of which
        rows" and nothing that would answer "and what did it say".
        """
        return {
            "entity": self.entity.value,
            "dimensions": [spec.as_audit_dict() for spec in self.dimensions],
            "metrics": [spec.as_audit_dict() for spec in self.metrics],
            "filter_bounds": self.filter_bounds.as_audit_dict(),
            "has_having": self.having is not None,
            "order_by": [order.as_order_by() for order in self.order_by],
            "window": self.window.as_audit_dict() if self.window else None,
            "limit": self.limit,
            "offset": self.offset,
        }
