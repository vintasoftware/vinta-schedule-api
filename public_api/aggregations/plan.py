"""The resolved shape of one aggregate request.

An ``AggregateQueryPlan`` is built from a GraphQL selection before any ORM
call is made, and it is the seam the rest of the feature hangs off: the
executor turns it into a queryset, and the audit hook records it without
knowing anything about querysets. Keeping those two on opposite sides of a
plain dataclass is why an audit change cannot break execution and vice
versa.

Everything here is frozen and holds only tuples and strings -- no querysets,
no model instances, no Django expressions. A plan is safe to build, compare,
log the shape of, and hand around.
"""

import datetime
import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from public_api.aggregations.errors import (
    OFFSET_NEGATIVE_MESSAGE,
    WINDOW_FRAME_END_BOUND_MESSAGE,
    WINDOW_FRAME_OFFSET_REQUIRED_MESSAGE,
    WINDOW_FRAME_OFFSET_UNEXPECTED_MESSAGE,
    WINDOW_FRAME_ORDER_MESSAGE,
    WINDOW_FRAME_RANGE_OFFSET_MESSAGE,
    WINDOW_FRAME_START_BOUND_MESSAGE,
    WINDOW_NEEDS_ORDER_BY_MESSAGE,
    AliasCollisionError,
    InvalidPlanError,
    limit_out_of_range_message,
)
from public_api.aggregations.types import TemporalGranularity


#: A ``COUNT`` metric over the grouped rows themselves counts primary keys --
#: see "Counts are ``Count('id')``" in the plan's Guiding Decisions. Spelled
#: as a constant so a plan builder does not have to know the column name.
ROW_COUNT_FIELD_PATH = "id"

#: The alias every row's mandatory count lands under -- see
#: ``public_api.aggregations.fields``'s ``_COUNT_METRIC``, which every row
#: carries under this name regardless of what else a query selected. HAVING
#: and ORDER BY resolution reuse it so a caller can filter or sort on
#: ``count`` without a second name for the same thing.
ROW_COUNT_ALIAS = "count"

#: The band ``limit`` must fall in. Matches every other list field on this
#: API; a warehouse consumer that needs more wants a paged export, not a
#: bigger live query.
MIN_AGGREGATE_LIMIT = 1
MAX_AGGREGATE_LIMIT = 100


class AggregateOp(enum.Enum):
    """One aggregate operation.

    Which of these a given field may be asked for is decided by the field's
    kind in ``public_api.aggregations.registry``, not here -- this enum is
    the full vocabulary, not a per-field menu.
    """

    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"
    COUNT = "COUNT"
    CONCAT = "CONCAT"
    TRUE_COUNT = "TRUE_COUNT"
    FALSE_COUNT = "FALSE_COUNT"


class AggregatableEntity(enum.Enum):
    """The entities this engine can group.

    Closed on purpose. Adding a member without a registry entry raises at
    execution time rather than aggregating nothing, and adding one at all is
    a decision about what a partner token can roll up.
    """

    CALENDAR_EVENT = "calendar_event"
    AVAILABLE_TIME = "available_time"
    BLOCKED_TIME = "blocked_time"
    APPOINTMENT_TYPE = "appointment_type"
    CALENDAR = "calendar"
    CALENDAR_POOL = "calendar_pool"


class OrderDirection(enum.Enum):
    """Sort direction for one ordering term."""

    ASC = "ASC"
    DESC = "DESC"


class ComparisonOp(enum.Enum):
    """One comparison a HAVING leaf can make against a metric or dimension alias."""

    EQ = "EQ"
    NE = "NE"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"


def default_metric_alias(field_path: str, op: AggregateOp) -> str:
    """The alias a metric gets when nothing else names one for it.

    ``public_api.aggregations.fields`` uses this for every metric it builds
    from a GraphQL row selection, and HAVING / ORDER BY resolution reuse it
    so a caller can filter or sort on a value it did not select for
    output -- the alias lines up with one the row selection would have
    produced, so referencing an unselected metric this way still lands on
    exactly one annotation rather than a second, differently-named one for
    the same aggregate.
    """
    return f"{field_path}__{op.value.lower()}"


@dataclass(frozen=True)
class MetricSpec:
    """One requested aggregate: the model field, the operation, and the alias
    the row dict will carry it under."""

    alias: str
    field_path: str
    op: AggregateOp
    #: Operation-specific settings -- ``separator`` and ``distinct`` for
    #: ``CONCAT``, ``distinct`` for ``COUNT``. Frozen in ``__post_init__`` so
    #: the dataclass is immutable all the way down.
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.alias:
            raise InvalidPlanError("A metric needs a non-empty alias")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    def __hash__(self) -> int:
        # The generated ``__hash__`` would hash ``options``, and a mapping
        # proxy is unhashable however immutable it is. Everything else about
        # this object says "value", so it hashes like one, over the same
        # fields the generated ``__eq__`` compares.
        return hash((self.alias, self.field_path, self.op, tuple(sorted(self.options.items()))))


@dataclass(frozen=True)
class DimensionSpec:
    """One GROUP BY dimension. `granularity` is set only for temporal fields."""

    alias: str
    field_path: str
    granularity: TemporalGranularity | None = None
    tzinfo: ZoneInfo | None = None

    def __post_init__(self) -> None:
        if not self.alias:
            raise InvalidPlanError("A dimension needs a non-empty alias")
        # A granularity without a timezone would bucket in whatever Django's
        # current timezone happens to be, which is the silent wall-clock mix
        # the caller-supplied IANA name exists to prevent.
        if (self.granularity is None) != (self.tzinfo is None):
            raise InvalidPlanError(
                "A temporal dimension needs both a granularity and a timezone, or neither"
            )


@dataclass(frozen=True)
class FilterBounds:
    """What the aggregate was narrowed to, in a form that is safe to audit.

    ``predicates`` holds opaque record ids only -- calendar ids, appointment
    type ids. Never free text, never a field value: this object is handed
    straight to the audit trail, and the audit trail is not allowed to become
    a second PHI store.
    """

    start: datetime.datetime | None = None
    end: datetime.datetime | None = None
    predicates: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "predicates",
            MappingProxyType({key: tuple(value) for key, value in self.predicates.items()}),
        )

    def __hash__(self) -> int:
        # Same reason as :meth:`MetricSpec.__hash__`: the mapping proxy the
        # generated hash would reach for is unhashable.
        return hash((self.start, self.end, tuple(sorted(self.predicates.items()))))


@dataclass(frozen=True)
class OrderSpec:
    """One ordering term, naming a dimension alias or a metric alias."""

    alias: str
    direction: OrderDirection = OrderDirection.DESC


@dataclass(frozen=True)
class HavingComparison:
    """One leaf predicate: ``<alias> <comparison> <value>``.

    ``alias`` names a dimension or metric alias the same way
    :class:`OrderSpec` does -- see
    ``AggregateQueryPlan._validate_having``, which checks it against the
    same known-alias set ``_validate_order_by`` already checks
    ``OrderSpec.alias`` against. HAVING can filter on a metric the caller
    never asked to see in the row selection; the resolver layer
    (``public_api.aggregations.having``) is what makes sure such a metric is
    still in ``AggregateQueryPlan.metrics`` under this same alias, so the
    executor never has to treat this leaf specially.
    """

    alias: str
    comparison: ComparisonOp
    value: float | int


@dataclass(frozen=True)
class HavingSpec:
    """Post-aggregation predicate tree. Exactly one of the three slots is set:
    ``comparison`` for a leaf, ``all_of`` / ``any_of`` to combine child specs
    with AND / OR.

    ``AggregateQueryPlan`` is what Phase 5's audit hook reads, so giving this
    a real shape (rather than the empty placeholder earlier phases carried)
    is what lets the audit record describe a HAVING clause's shape -- aliases
    and comparisons, never the values a metric held for one group.
    """

    comparison: HavingComparison | None = None
    all_of: tuple["HavingSpec", ...] = ()
    any_of: tuple["HavingSpec", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "all_of", tuple(self.all_of))
        object.__setattr__(self, "any_of", tuple(self.any_of))
        set_slots = sum([self.comparison is not None, bool(self.all_of), bool(self.any_of)])
        if set_slots != 1:
            raise InvalidPlanError(
                "A having node must set exactly one of comparison, all_of, or any_of"
            )

    def referenced_aliases(self) -> tuple[str, ...]:
        """Every alias this node or its children compare against.

        Used by ``AggregateQueryPlan._validate_having`` to confirm each one
        is a dimension or metric the plan actually carries.
        """
        if self.comparison is not None:
            return (self.comparison.alias,)
        return tuple(
            alias for child in (*self.all_of, *self.any_of) for alias in child.referenced_aliases()
        )


class WindowFunctionKind(enum.Enum):
    """One window function computed over the grouped rows.

    Every member reads the same column -- :attr:`WindowSpec.metric_alias` --
    and differs only in what it does with it, so a caller picks the metric
    once and selects as many of these as it wants.
    """

    RUNNING_TOTAL = "RUNNING_TOTAL"
    MOVING_AVERAGE = "MOVING_AVERAGE"
    RANK = "RANK"
    PERCENT_OF_TOTAL = "PERCENT_OF_TOTAL"


class WindowFrameType(enum.Enum):
    """The unit a frame's bounds are counted in.

    ``ROWS`` counts rows. ``RANGE`` counts *peers* -- every row sharing the
    window's ordering value counts as one step -- so a three-step ``RANGE``
    frame over a day bucket with ties reads more rows than a three-row
    ``ROWS`` frame does.
    """

    ROWS = "ROWS"
    RANGE = "RANGE"


class WindowBound(enum.Enum):
    """One end of a window frame."""

    UNBOUNDED_PRECEDING = "UNBOUNDED_PRECEDING"
    PRECEDING = "PRECEDING"
    CURRENT_ROW = "CURRENT_ROW"
    FOLLOWING = "FOLLOWING"
    UNBOUNDED_FOLLOWING = "UNBOUNDED_FOLLOWING"


#: The bounds that may open a frame. ``UNBOUNDED_FOLLOWING`` is absent: a
#: frame starting after every row of its partition contains nothing.
_START_BOUNDS = frozenset(
    {
        WindowBound.UNBOUNDED_PRECEDING,
        WindowBound.PRECEDING,
        WindowBound.CURRENT_ROW,
        WindowBound.FOLLOWING,
    }
)

#: The bounds that may close a frame, by the mirror image of the same rule.
_END_BOUNDS = frozenset(
    {
        WindowBound.PRECEDING,
        WindowBound.CURRENT_ROW,
        WindowBound.FOLLOWING,
        WindowBound.UNBOUNDED_FOLLOWING,
    }
)

#: The two bounds that measure a distance, and so need an offset. The other
#: three name a fixed position and must not carry one.
_OFFSET_BOUNDS = frozenset({WindowBound.PRECEDING, WindowBound.FOLLOWING})


@dataclass(frozen=True)
class WindowFrameSpec:
    """Which rows around the current one a window function reads.

    The defaults are what makes a running total run: every row of the
    partition from its start up to and including this one.

    :meth:`bounds` returns the pair Django's ``RowRange`` / ``ValueRange``
    take -- ``None`` for unbounded, ``0`` for the current row, a negative
    number for *preceding* and a positive one for *following*. That encoding
    is Django's, not this module's, which is why the offsets a caller
    supplies are always positive and the sign is applied here.
    """

    frame_type: WindowFrameType = WindowFrameType.ROWS
    start: WindowBound = WindowBound.UNBOUNDED_PRECEDING
    start_offset: int | None = None
    end: WindowBound = WindowBound.CURRENT_ROW
    end_offset: int | None = None

    def __post_init__(self) -> None:
        if self.start not in _START_BOUNDS:
            raise InvalidPlanError(WINDOW_FRAME_START_BOUND_MESSAGE)
        if self.end not in _END_BOUNDS:
            raise InvalidPlanError(WINDOW_FRAME_END_BOUND_MESSAGE)
        self._validate_offset(self.start, self.start_offset)
        self._validate_offset(self.end, self.end_offset)
        # Postgres accepts ``RANGE <n> PRECEDING`` only over exactly one
        # ordering column whose type an integer can offset. A window here
        # routinely orders by a bucketed timestamp, or by two terms, and both
        # are refused by the database rather than by the schema -- so the
        # combination is refused here, in this engine's own words.
        if self.frame_type is WindowFrameType.RANGE and (
            self.start in _OFFSET_BOUNDS or self.end in _OFFSET_BOUNDS
        ):
            raise InvalidPlanError(WINDOW_FRAME_RANGE_OFFSET_MESSAGE)
        start, end = self.bounds()
        # Only when both ends are finite: an unbounded end is an infinity on
        # the correct side of the other by construction.
        if start is not None and end is not None and start > end:
            raise InvalidPlanError(WINDOW_FRAME_ORDER_MESSAGE)

    @staticmethod
    def _validate_offset(bound: WindowBound, offset: int | None) -> None:
        """Refuse an offset on a bound that has no distance to measure, and a
        missing or non-positive one on a bound that does.

        Zero is refused rather than read as the current row: ``0 PRECEDING``
        and ``CURRENT ROW`` are the same frame said two ways, and accepting
        both would mean two spellings of one thing.
        """
        if bound in _OFFSET_BOUNDS:
            if offset is None or offset < 1:
                raise InvalidPlanError(WINDOW_FRAME_OFFSET_REQUIRED_MESSAGE)
            return
        if offset is not None:
            raise InvalidPlanError(WINDOW_FRAME_OFFSET_UNEXPECTED_MESSAGE)

    def bounds(self) -> tuple[int | None, int | None]:
        """``(start, end)`` in the encoding Django's window frames take."""
        return _bound_value(self.start, self.start_offset), _bound_value(self.end, self.end_offset)


def _bound_value(bound: WindowBound, offset: int | None) -> int | None:
    """One bound as the number Django's ``RowRange`` / ``ValueRange`` want."""
    match bound:
        case WindowBound.UNBOUNDED_PRECEDING | WindowBound.UNBOUNDED_FOLLOWING:
            return None
        case WindowBound.CURRENT_ROW:
            return 0
        case WindowBound.PRECEDING:
            # ``offset`` is not None here: ``_validate_offset`` ran first.
            return -int(offset or 0)
        case WindowBound.FOLLOWING:
            return int(offset or 0)
    raise InvalidPlanError(WINDOW_FRAME_START_BOUND_MESSAGE)  # pragma: no cover


#: Prefix every window column's alias carries in the row dict. Nothing stops
#: a caller aliasing a metric this way, but Django refuses a duplicate
#: annotation name outright, so a collision is a loud error rather than a
#: wrong number -- the same reasoning as the executor's relation-count prefix.
WINDOW_ALIAS_PREFIX = "_window_"


def window_alias(kind: WindowFunctionKind) -> str:
    """The row-dict key one window function's value lands under."""
    return WINDOW_ALIAS_PREFIX + kind.value.lower()


@dataclass(frozen=True)
class WindowSpec:
    """Window-function clause applied over the grouped result.

    One metric, one partitioning, one ordering, one frame, and the set of
    functions to compute over them. The functions share everything else
    because they answer the same question about the same column -- asking for
    a running total and a moving average of two different metrics is two
    queries, not one window.

    ``order_by`` is the *window's* ordering, which is not the result's: the
    field's own ``orderBy`` decides what order the caller reads rows in, this
    decides what order a running total accumulates in, and a query that wants
    "the ten busiest days, each carrying its running total in date order"
    needs them to differ. It is mandatory -- an unordered running total is not
    a loose definition, it is no definition at all, and Postgres would pick an
    order and return numbers that move between runs.
    """

    metric_alias: str
    order_by: tuple[OrderSpec, ...]
    functions: tuple[WindowFunctionKind, ...] = ()
    partition_by: tuple[str, ...] = ()
    frame: WindowFrameSpec | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_by", tuple(self.order_by))
        object.__setattr__(self, "functions", tuple(self.functions))
        object.__setattr__(self, "partition_by", tuple(self.partition_by))
        if not self.metric_alias:
            raise InvalidPlanError("A window needs a metric alias to read")
        if not self.order_by:
            raise InvalidPlanError(WINDOW_NEEDS_ORDER_BY_MESSAGE)

    def referenced_aliases(self) -> tuple[str, ...]:
        """Every alias this window reads -- its metric, its partitioning and
        its ordering. Used by ``AggregateQueryPlan._validate_window`` to
        confirm each one is something the plan actually carries.
        """
        return (
            self.metric_alias,
            *self.partition_by,
            *(order.alias for order in self.order_by),
        )


@dataclass(frozen=True)
class AggregateQueryPlan:
    """The fully resolved request, built from the GraphQL selection before any
    ORM call. This is what the audit hook records and what the executor turns
    into a queryset — the seam that keeps audit independent of execution."""

    entity: AggregatableEntity
    dimensions: tuple[DimensionSpec, ...]
    metrics: tuple[MetricSpec, ...]
    filter_bounds: FilterBounds = field(default_factory=FilterBounds)
    having: HavingSpec | None = None
    order_by: tuple[OrderSpec, ...] = ()
    window: WindowSpec | None = None
    limit: int = MAX_AGGREGATE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        self._validate_dimensions()
        self._validate_metrics()
        self._validate_aliases()
        self._validate_order_by()
        self._validate_having()
        self._validate_window()
        self._validate_slice()

    # -- validation ------------------------------------------------------
    #
    # The first two rules are the same rule seen from either end: Django only
    # emits a ``GROUP BY`` when ``.values()`` names something *and*
    # ``.annotate()`` adds an aggregate over it. Drop either half and the
    # query still runs, still returns rows, and is no longer grouped -- one
    # row per source row, with the group keys repeating. Nothing about the
    # result's shape says so, which is why both halves are refused here
    # rather than discovered downstream.

    def _validate_dimensions(self) -> None:
        if not self.dimensions:
            raise InvalidPlanError("An aggregate plan needs at least one group-by dimension")

    def _validate_metrics(self) -> None:
        if not self.metrics:
            raise InvalidPlanError("An aggregate plan needs at least one metric")

    def _all_aliases(self) -> tuple[str, ...]:
        """Every alias the row dict will carry, dimensions before metrics."""
        return (
            *(dimension.alias for dimension in self.dimensions),
            *(metric.alias for metric in self.metrics),
        )

    def _validate_aliases(self) -> None:
        seen: set[str] = set()
        for alias in self._all_aliases():
            if alias in seen:
                raise AliasCollisionError(
                    f"Alias {alias!r} is used by more than one dimension or metric"
                )
            seen.add(alias)

    def _validate_order_by(self) -> None:
        known = set(self._all_aliases())
        for order in self.order_by:
            if order.alias not in known:
                raise InvalidPlanError(
                    f"Cannot order by {order.alias!r}: no dimension or metric carries that alias"
                )

    def _validate_having(self) -> None:
        if self.having is None:
            return
        known = set(self._all_aliases())
        for alias in self.having.referenced_aliases():
            if alias not in known:
                raise InvalidPlanError(
                    f"Cannot filter on {alias!r}: no dimension or metric carries that alias"
                )

    def _validate_window(self) -> None:
        """Refuse a window that reads something the plan does not carry.

        ``partition_by`` is held to the dimensions alone, not to every alias:
        partitioning on a metric would mean a partition per distinct value of
        a number that the window is meant to be reading *across*, and there
        is no ``GROUP BY`` column for it either. Ordering, by contrast, may
        name a metric -- "running total in descending count order" is a
        reasonable thing to ask for.
        """
        if self.window is None:
            return
        if self.window.metric_alias not in set(self.metric_aliases):
            raise InvalidPlanError(
                f"Cannot window over {self.window.metric_alias!r}: no metric carries that alias"
            )
        dimension_aliases = set(self.dimension_aliases)
        for alias in self.window.partition_by:
            if alias not in dimension_aliases:
                raise InvalidPlanError(
                    f"Cannot partition by {alias!r}: no dimension carries that alias"
                )
        known = set(self._all_aliases())
        for order in self.window.order_by:
            if order.alias not in known:
                raise InvalidPlanError(
                    f"Cannot order a window by {order.alias!r}: "
                    f"no dimension or metric carries that alias"
                )

    def _validate_slice(self) -> None:
        # Wording matches ``public_api.queries._slice_qs`` (and this engine's
        # own caller-visible ``LimitOutOfRangeError`` / ``OffsetOutOfRangeError``)
        # exactly. A resolver is expected to validate ``limit``/``offset`` before
        # ever building a plan -- see ``public_api.aggregations.fields`` -- so
        # this branch is a defensive invariant that should not normally fire.
        # Still, if it ever does, it must not invent a third wording for the
        # same refusal.
        if not MIN_AGGREGATE_LIMIT <= self.limit <= MAX_AGGREGATE_LIMIT:
            raise InvalidPlanError(
                limit_out_of_range_message(MIN_AGGREGATE_LIMIT, MAX_AGGREGATE_LIMIT)
            )
        if self.offset < 0:
            raise InvalidPlanError(OFFSET_NEGATIVE_MESSAGE)

    # -- read helpers ----------------------------------------------------

    @property
    def dimension_aliases(self) -> tuple[str, ...]:
        """The group key aliases, in the order they were requested."""
        return tuple(spec.alias for spec in self.dimensions)

    @property
    def metric_aliases(self) -> tuple[str, ...]:
        """The metric aliases, in the order they were requested."""
        return tuple(spec.alias for spec in self.metrics)
