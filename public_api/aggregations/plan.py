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

from public_api.aggregations.errors import AliasCollisionError, InvalidPlanError
from public_api.aggregations.types import TemporalGranularity


#: A ``COUNT`` metric over the grouped rows themselves counts primary keys --
#: see "Counts are ``Count('id')``" in the plan's Guiding Decisions. Spelled
#: as a constant so a plan builder does not have to know the column name.
ROW_COUNT_FIELD_PATH = "id"

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


@dataclass(frozen=True)
class OrderSpec:
    """One ordering term, naming a dimension alias or a metric alias."""

    alias: str
    direction: OrderDirection = OrderDirection.DESC


@dataclass(frozen=True)
class HavingSpec:
    """Post-aggregation predicate tree.

    Phase 4 of the plan owns the shape of this and the executor's handling of
    it. It is declared here, empty, because ``AggregateQueryPlan`` is what
    Phase 5's audit hook reads: giving the slot a name now means the audit
    record does not change shape when HAVING lands. Until then the executor
    refuses a plan that sets it rather than dropping it silently.
    """


@dataclass(frozen=True)
class WindowSpec:
    """Window-function clause applied over the grouped result.

    Declared here for the same reason as :class:`HavingSpec`; Phase 6 owns
    the shape and the window-over-subquery construction.
    """


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
        self._validate_aliases()
        self._validate_order_by()
        self._validate_slice()

    # -- validation ------------------------------------------------------

    def _validate_dimensions(self) -> None:
        # ``.values()`` with no arguments followed by ``.annotate()`` does not
        # group at all -- it annotates every row. That failure is invisible in
        # the result shape and expensive in the database, so a plan with no
        # dimension is refused instead.
        if not self.dimensions:
            raise InvalidPlanError("An aggregate plan needs at least one group-by dimension")

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

    def _validate_slice(self) -> None:
        if not MIN_AGGREGATE_LIMIT <= self.limit <= MAX_AGGREGATE_LIMIT:
            raise InvalidPlanError(
                f"limit must be between {MIN_AGGREGATE_LIMIT} and {MAX_AGGREGATE_LIMIT}"
            )
        if self.offset < 0:
            raise InvalidPlanError("offset cannot be negative")

    # -- read helpers ----------------------------------------------------

    @property
    def dimension_aliases(self) -> tuple[str, ...]:
        """The group key aliases, in the order they were requested."""
        return tuple(spec.alias for spec in self.dimensions)

    @property
    def metric_aliases(self) -> tuple[str, ...]:
        """The metric aliases, in the order they were requested."""
        return tuple(spec.alias for spec in self.metrics)
