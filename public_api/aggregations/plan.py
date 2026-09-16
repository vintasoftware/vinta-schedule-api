"""The resolved shape of one aggregate request.

An ``AggregateQueryPlan`` is built from a GraphQL selection *before* any ORM
call and is the only thing the executor reads. That seam is deliberate: the
audit hook added in a later phase records the plan -- entity, dimensions,
metric names, filter bounds -- without ever touching the executor, so auditing
what was asked stays independent of how it is run, and no field *value* is
anywhere near the record.

Everything here is frozen. A plan that could be mutated after it was audited
would make the audit trail a description of a query that never ran.
"""

import datetime
import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from public_api.aggregations import errors
from public_api.aggregations.types import TemporalGranularity


#: Prefix every dimension alias carries in the row dict.
#:
#: Aliases are prefixed because Django refuses an annotation whose name equals a
#: concrete column ("The annotation 'start_time' conflicts with a field on the
#: model"), and a day-bucketed ``start_time`` is exactly the dimension a caller
#: asks for first. Prefixing at the engine level means no entity surface has to
#: discover that rule for itself.
DIMENSION_ALIAS_PREFIX = "dim_"

#: Prefix every metric alias carries in the row dict. Same reason as above.
METRIC_ALIAS_PREFIX = "m_"

#: Alias of the per-group row count, which every aggregate row carries.
ROW_COUNT_ALIAS = f"{METRIC_ALIAS_PREFIX}count"


class AggregateOp(enum.StrEnum):
    """One aggregate operation.

    Which of these a given field may be asked for is decided by the field's
    kind in :mod:`public_api.aggregations.registry`, never here.
    """

    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    #: ``Count("id")`` over the group's own rows.
    COUNT = "count"
    #: ``Count`` over a related collection, executed as a correlated subquery
    #: so that two of them in one query cannot fan the join out.
    RELATION_COUNT = "relation_count"
    #: Postgres ``string_agg``.
    CONCAT = "concat"
    TRUE_COUNT = "true_count"
    FALSE_COUNT = "false_count"


class AggregatableEntity(enum.StrEnum):
    """The entity families that expose an aggregate field.

    Closed on purpose: adding a member without a registration in
    :mod:`public_api.aggregations.registry` raises rather than producing an
    entity whose aggregates silently return nothing.
    """

    CALENDAR_EVENT = "calendar_event"
    AVAILABLE_TIME = "available_time"
    BLOCKED_TIME = "blocked_time"
    APPOINTMENT_TYPE = "appointment_type"
    CALENDAR = "calendar"
    CALENDAR_POOL = "calendar_pool"


class OrderDirection(enum.StrEnum):
    """Sort direction for an ``orderBy`` entry."""

    ASC = "asc"
    DESC = "desc"


class ComparisonOp(enum.StrEnum):
    """Comparison used by a post-aggregation (``HAVING``) predicate."""

    EQ = "eq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


def dimension_alias(name: str) -> str:
    """Return the row-dict key a dimension called `name` is read under."""
    return f"{DIMENSION_ALIAS_PREFIX}{name}"


def metric_alias(name: str, op: AggregateOp) -> str:
    """Return the row-dict key the `op` aggregate of `name` is read under.

    Deterministic and collision-free by construction: two different (field,
    operation) pairs cannot produce the same alias, so a row carrying both
    ``sum`` and ``avg`` of one field keeps them apart.
    """
    return f"{METRIC_ALIAS_PREFIX}{name}_{op.value}"


@dataclass(frozen=True)
class MetricSpec:
    """One requested aggregate: the model field, the operation, and the alias
    the row dict will carry it under.

    ``field_path`` names the field *as the registry knows it* for the plan's
    entity -- the registry resolves it to an ORM path, and to a derived
    expression for the fields (``duration_minutes``) that are computed rather
    than stored. For ``COUNT`` it is empty; for ``RELATION_COUNT`` it names a
    countable relation.
    """

    alias: str
    field_path: str
    op: AggregateOp
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze the options too. A frozen dataclass wrapping a live dict is
        # only half frozen, and this object is what the audit trail records.
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    @classmethod
    def row_count(cls, alias: str = ROW_COUNT_ALIAS, *, distinct: bool = False) -> "MetricSpec":
        """Build the per-group row count that every aggregate row carries."""
        return cls(alias=alias, field_path="", op=AggregateOp.COUNT, options={"distinct": distinct})

    @classmethod
    def of(cls, name: str, op: AggregateOp, /, **options: Any) -> "MetricSpec":
        """Build a metric over the registered field `name`, aliased canonically.

        ``options`` are operation-specific: ``separator`` / ``distinct`` for
        ``CONCAT``, ``distinct`` for the counts.
        """
        return cls(alias=metric_alias(name, op), field_path=name, op=op, options=options)


@dataclass(frozen=True)
class DimensionSpec:
    """One GROUP BY dimension. `granularity` is set only for temporal fields.

    ``tzinfo`` is the caller-supplied IANA zone the bucket is cut in. It is
    required whenever ``granularity`` is set, because "per day" is genuinely
    ambiguous for a row that stores a naive local wall clock plus its own
    timezone column.
    """

    alias: str
    field_path: str
    granularity: TemporalGranularity | None = None
    tzinfo: ZoneInfo | None = None

    def __post_init__(self) -> None:
        if self.granularity is not None and self.tzinfo is None:
            raise errors.InvalidPlanError(
                f"Dimension {self.alias!r} sets a granularity but no timezone"
            )

    @classmethod
    def of(
        cls,
        name: str,
        *,
        granularity: TemporalGranularity | None = None,
        tzinfo: ZoneInfo | None = None,
    ) -> "DimensionSpec":
        """Build a dimension over the registered field `name`, aliased canonically."""
        return cls(
            alias=dimension_alias(name),
            field_path=name,
            granularity=granularity,
            tzinfo=tzinfo,
        )


@dataclass(frozen=True)
class FilterBounds:
    """The mandatory bounded date range every aggregate field carries.

    One of the four independent cost bounds in this plan; the other three are
    the ``limit`` cap, the per-query statement timeout and the nested-batching
    guarantee. A range alone does not bound group cardinality, which is why it
    is not the only one.
    """

    field_path: str
    start: datetime.datetime
    end: datetime.datetime

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise errors.invalid_date_range()

    @property
    def span_days(self) -> float:
        """Width of the range in days, for the maximum-span check."""
        return (self.end - self.start).total_seconds() / 86400.0


@dataclass(frozen=True)
class MetricComparison:
    """One post-aggregation comparison against a metric alias."""

    alias: str
    op: ComparisonOp
    value: float


@dataclass(frozen=True)
class HavingSpec:
    """Post-aggregation predicate tree, applied as SQL ``HAVING``.

    Phase 4 owns the translation into ``.filter()`` over the annotations; this
    shape exists now so the plan dataclass is complete and the audit hook can
    record that a request carried a ``HAVING`` at all. The executor refuses a
    plan carrying one until then, rather than dropping the clause.
    """

    comparisons: tuple[MetricComparison, ...] = ()
    and_: tuple["HavingSpec", ...] = ()
    or_: tuple["HavingSpec", ...] = ()

    def aliases(self) -> frozenset[str]:
        """Every metric alias this predicate tree references."""
        referenced = {comparison.alias for comparison in self.comparisons}
        for branch in (*self.and_, *self.or_):
            referenced |= branch.aliases()
        return frozenset(referenced)


@dataclass(frozen=True)
class OrderSpec:
    """One ``ORDER BY`` entry, naming a dimension or metric alias."""

    alias: str
    direction: OrderDirection = OrderDirection.DESC

    def as_order_by(self) -> str:
        """Render the entry as a Django ``order_by`` term."""
        return self.alias if self.direction is OrderDirection.ASC else f"-{self.alias}"


@dataclass(frozen=True)
class WindowSpec:
    """A window computed over the grouped result.

    Phase 6 owns the construction -- Django computes ``Window`` expressions
    after grouping and refuses to filter on one, so the grouped queryset gets
    wrapped and the window applied on the outside. The shape lives here because
    the plan is what the audit hook reads, and "this request asked for a
    running total" is part of the shape of the request.
    """

    partition_by: tuple[str, ...] = ()
    order_by: tuple[OrderSpec, ...] = ()
    frame_start: str | None = None
    frame_end: str | None = None


@dataclass(frozen=True)
class AggregateQueryPlan:
    """The fully resolved request, built from the GraphQL selection before any
    ORM call. This is what the audit hook records and what the executor turns
    into a queryset -- the seam that keeps audit independent of execution.

    At least one dimension is required. A dimensionless aggregate is a grand
    total over the whole filtered set, which this engine does not produce: it
    would have to be a different SQL shape (``.aggregate()``, one row, no
    ``GROUP BY``), and the GraphQL surface never asks for one -- every entity
    field takes a non-empty ``groupBy``.
    """

    entity: AggregatableEntity
    dimensions: tuple[DimensionSpec, ...]
    metrics: tuple[MetricSpec, ...]
    filter_bounds: FilterBounds
    having: HavingSpec | None = None
    order_by: tuple[OrderSpec, ...] = ()
    window: WindowSpec | None = None
    limit: int = errors.MAX_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise errors.no_dimensions()
        if self.limit <= 0 or self.limit > errors.MAX_LIMIT:
            raise errors.limit_out_of_range()
        if self.offset < 0:
            raise errors.offset_negative()

        self._assert_aliases_are_unique()
        self._assert_references_are_resolvable()

    def _assert_aliases_are_unique(self) -> None:
        seen: set[str] = set()
        for alias in (*self.dimension_aliases, *self.metric_aliases):
            if alias in seen:
                raise errors.AliasCollisionError(
                    f"Alias {alias!r} is produced by more than one dimension or metric"
                )
            seen.add(alias)

    def _assert_references_are_resolvable(self) -> None:
        produced = frozenset(self.aliases)
        for order in self.order_by:
            if order.alias not in produced:
                raise errors.InvalidPlanError(
                    f"orderBy names {order.alias!r}, which no dimension or metric produces"
                )
        referenced = self.having.aliases() if self.having is not None else frozenset()
        if self.window is not None:
            referenced |= frozenset(self.window.partition_by)
            referenced |= {order.alias for order in self.window.order_by}
        unknown = referenced - produced
        if unknown:
            raise errors.InvalidPlanError(
                f"Plan references {sorted(unknown)!r}, which no dimension or metric produces"
            )

    @property
    def dimension_aliases(self) -> tuple[str, ...]:
        """Row-dict keys the dimensions are read under, in plan order."""
        return tuple(dimension.alias for dimension in self.dimensions)

    @property
    def metric_aliases(self) -> tuple[str, ...]:
        """Row-dict keys the metrics are read under, in plan order."""
        return tuple(metric.alias for metric in self.metrics)

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every row-dict key this plan produces, dimensions first."""
        return (*self.dimension_aliases, *self.metric_aliases)

    def with_limits(self, *, limit: int, offset: int) -> "AggregateQueryPlan":
        """Return a copy of this plan with a different slice.

        The plan is frozen, so paging is a new plan rather than a mutation --
        which keeps a plan already handed to the audit hook describing the
        query that actually ran.
        """
        return replace(self, limit=limit, offset=offset)
