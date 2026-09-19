"""The resolved shape of one aggregate request.

An ``AggregateQueryPlan`` is built from a GraphQL selection *before* any ORM
call and is the only thing the executor reads. That seam is deliberate: the
audit hook records a plan, so auditing stays independent of how the plan is
executed, and two phases can work on the two halves without meeting in the
middle of a queryset.

Every dataclass here is frozen and every collection on one is a tuple or a
read-only mapping, so a plan that was audited is the plan that ran.
"""

import datetime
import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from public_api.aggregations.errors import (
    DUPLICATE_ALIAS_MESSAGE,
    EMPTY_ALIAS_MESSAGE,
    NO_DIMENSIONS_MESSAGE,
    InvalidAggregatePlanError,
)
from public_api.aggregations.types import TemporalGranularity


class AggregatableEntity(enum.StrEnum):
    """The entity families an aggregate field can group.

    A closed set by design: the plan's non-goals rule out aggregating over
    memberships, webhook events, change requests, booking policies and billing
    models, so adding a member here is a deliberate decision rather than a
    convenience.
    """

    CALENDAR_EVENT = "CALENDAR_EVENT"
    AVAILABLE_TIME = "AVAILABLE_TIME"
    BLOCKED_TIME = "BLOCKED_TIME"
    APPOINTMENT_TYPE = "APPOINTMENT_TYPE"
    CALENDAR = "CALENDAR"
    CALENDAR_POOL = "CALENDAR_POOL"


class AggregateOp(enum.StrEnum):
    """One aggregate operation.

    Which of these a field offers is decided by the field's kind in
    ``registry.py``, never by a resolver: ``CONCAT`` is unreachable on a numeric
    field and ``SUM`` is unreachable on a string one.
    """

    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"
    COUNT = "COUNT"
    CONCAT = "CONCAT"
    TRUE_COUNT = "TRUE_COUNT"
    FALSE_COUNT = "FALSE_COUNT"


class ComparisonOperator(enum.StrEnum):
    """Comparison used by a HAVING condition."""

    EQ = "EQ"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"


class WindowFrameType(enum.StrEnum):
    """Frame unit of a window function's ``ROWS`` / ``RANGE`` clause."""

    ROWS = "ROWS"
    RANGE = "RANGE"


class WindowBound(enum.StrEnum):
    """One end of a window frame."""

    UNBOUNDED_PRECEDING = "UNBOUNDED_PRECEDING"
    PRECEDING = "PRECEDING"
    CURRENT_ROW = "CURRENT_ROW"
    FOLLOWING = "FOLLOWING"
    UNBOUNDED_FOLLOWING = "UNBOUNDED_FOLLOWING"


def _frozen_mapping(source: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only copy, so a frozen spec is frozen all the way down."""
    return MappingProxyType(dict(source))


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One requested aggregate: the field, the operation, and the alias the row
    dict will carry it under.

    ``field_path`` is the *registry* name for the field, which for a plain
    column is also its ORM path (``title``) and for a derived or relation-backed
    metric is a logical name the registry knows how to build an expression for
    (``duration_minutes``, ``attendances``).

    ``options`` carries operation-specific settings — ``separator`` and
    ``distinct`` for ``CONCAT``. It is normalised to a read-only mapping so two
    plans built from the same selection compare equal.
    """

    alias: str
    field_path: str
    op: AggregateOp
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _frozen_mapping(self.options))


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """One GROUP BY dimension.

    ``granularity`` and ``tzinfo`` are set only for a temporal dimension, and
    only together: bucketing by local day is meaningless without the timezone
    whose day is meant. Phase 2 is what turns them into ``TruncDay`` and
    friends; a plan built without them groups on the raw column.
    """

    alias: str
    field_path: str
    granularity: TemporalGranularity | None = None
    tzinfo: ZoneInfo | None = None


@dataclass(frozen=True, slots=True)
class FilterBounds:
    """What the caller's filter narrowed the queryset to.

    Recorded on the plan rather than derived from the queryset because the audit
    hook needs it and must not have to read SQL to find it. ``predicates`` holds
    opaque record ids only — never free text, never a PHI-bearing value — since
    everything on a plan is eligible to be written to the audit trail.
    """

    start: datetime.datetime | None = None
    end: datetime.datetime | None = None
    predicates: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "predicates", _frozen_mapping(self.predicates))


@dataclass(frozen=True, slots=True)
class OrderSpec:
    """Order the grouped rows by one alias — a dimension's or a metric's."""

    alias: str
    descending: bool = True


@dataclass(frozen=True, slots=True)
class HavingCondition:
    """One comparison against an aggregated value, applied after grouping."""

    alias: str
    operator: ComparisonOperator
    value: float


@dataclass(frozen=True, slots=True)
class HavingSpec:
    """A tree of post-aggregation conditions.

    ``conditions`` and ``all_of`` are ANDed together; ``any_of`` is ORed and
    then ANDed with the rest. That is what the GraphQL input's ``and`` / ``or``
    fields resolve to.
    """

    conditions: tuple[HavingCondition, ...] = ()
    all_of: tuple["HavingSpec", ...] = ()
    any_of: tuple["HavingSpec", ...] = ()


@dataclass(frozen=True, slots=True)
class WindowFrameSpec:
    """The frame a window function reads over, e.g. ``ROWS BETWEEN 2 PRECEDING AND CURRENT ROW``."""

    frame_type: WindowFrameType = WindowFrameType.ROWS
    start: WindowBound = WindowBound.UNBOUNDED_PRECEDING
    start_offset: int | None = None
    end: WindowBound = WindowBound.CURRENT_ROW
    end_offset: int | None = None


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """Partition, ordering and frame for window metrics over the grouped result.

    Carried on the plan from the start so the audit hook can record that a query
    asked for windows. Phase 6 is what executes it.
    """

    partition_by: tuple[str, ...] = ()
    order_by: tuple[OrderSpec, ...] = ()
    frame: WindowFrameSpec | None = None


@dataclass(frozen=True, slots=True)
class AggregateQueryPlan:
    """The fully resolved request, built from the GraphQL selection before any
    ORM call.

    This is what the audit hook records and what the executor turns into a
    queryset — the seam that keeps audit independent of execution.
    """

    entity: AggregatableEntity
    dimensions: tuple[DimensionSpec, ...]
    metrics: tuple[MetricSpec, ...]
    filter_bounds: FilterBounds = field(default_factory=FilterBounds)
    having: HavingSpec | None = None
    order_by: tuple[OrderSpec, ...] = ()
    window: WindowSpec | None = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise InvalidAggregatePlanError(NO_DIMENSIONS_MESSAGE)

        aliases = [spec.alias for spec in self.dimensions] + [spec.alias for spec in self.metrics]
        if any(not alias for alias in aliases):
            raise InvalidAggregatePlanError(EMPTY_ALIAS_MESSAGE)
        if len(set(aliases)) != len(aliases):
            # A dimension and a metric sharing an alias would silently overwrite
            # one another in the row dict, and the loser would be whichever the
            # ORM happened to emit second.
            raise InvalidAggregatePlanError(DUPLICATE_ALIAS_MESSAGE)

    @property
    def dimension_aliases(self) -> tuple[str, ...]:
        return tuple(spec.alias for spec in self.dimensions)

    @property
    def metric_aliases(self) -> tuple[str, ...]:
        return tuple(spec.alias for spec in self.metrics)
