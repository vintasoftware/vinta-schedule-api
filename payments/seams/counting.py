"""Lets this project's usage counters keep counting in organization ids.

``vinta-django-billing`` 0.8.0 re-keyed usage onto scopes: a counter is handed
``UsageContext.scope_ids`` and must return ``{scope_id: count}``. Every table
this project counts -- memberships, calendars, appointment types, webhook
configurations -- is keyed by ``organization_id`` and knows nothing about
scopes, and the package's own ``count_by_scope`` groups on a ``scope_id``
column none of them have.

The translation is mechanical and identical for all eight counters, so it lives
here as a decorator rather than being open-coded eight times. The counters in
``payments.seams.resources`` are then unchanged from their 0.7 form: they still
read ``context.organization_ids`` and still return ``{organization_id: count}``.
That is the point. The counting logic itself -- which rows count, which are
excluded, why two tables get merged rather than concatenated -- is the delicate
part and the part with the most tests behind it; re-keying is not a good reason
to touch it.

The scope-to-organization direction is a query, not arithmetic, so it happens
once per counter call and both directions reuse the one mapping.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import Any

from django.db.models import Count, QuerySet

from vinta_billing.counting import UsageContext

from payments.seams.scopes import organization_ids_for_scope_ids


@dataclass(frozen=True)
class OrganizationUsageContext:
    """``UsageContext`` with the ids translated back into organization space.

    Carries the same three members a counter is allowed to read, so a counter
    written against the 0.7 ``UsageContext`` sees no difference. ``subscription``
    and ``extra`` pass straight through -- neither is scope-keyed.
    """

    organization_ids: Sequence[int]
    subscription: Any = None
    extra: dict[str, Any] | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Read one key out of ``extra``, tolerating ``extra=None``."""
        if self.extra is None:
            return default
        return self.extra.get(key, default)


def count_by_organization(queryset: QuerySet[Any]) -> dict[int, int]:
    """``{organization_id: row_count}`` for any organization-scoped queryset.

    The 0.7 ``vinta_billing.counting.count_by_organization``, kept here because
    0.8 replaced it with a ``scope_id`` equivalent that none of this project's
    tables can answer. Same implementation, same two load-bearing details:

    ``order_by()`` clears any ordering the caller's queryset carries. Django
    appends ``ORDER BY`` columns to ``GROUP BY``, so an ordered queryset -- a
    ``Meta.ordering`` on the counted model is enough -- would split one
    organization's rows across several groups and the comprehension would keep
    only the last. In a billing engine that is an under-count, which means
    under-charging.

    ``Count("pk")`` rather than ``Count("pk", distinct=True)``: nothing feeding
    this has a row-multiplying join, and on a composite primary key Django
    rejects ``COUNT(DISTINCT)`` outright.
    """
    return {
        row["organization_id"]: row["usage_count"]
        for row in queryset.order_by().values("organization_id").annotate(usage_count=Count("pk"))
    }


def merge_breakdowns(*breakdowns: Mapping[int, int]) -> dict[int, int]:
    """Sum any number of ``{id: count}`` maps key-wise into one.

    For a counter whose "one unit of usage" spans more than one table -- seats
    are memberships *plus* pending invitations -- so that an organization
    holding both kinds is summed rather than double-keyed.
    """
    merged: dict[int, int] = {}
    for breakdown in breakdowns:
        for key, count in breakdown.items():
            merged[key] = merged.get(key, 0) + count
    return merged


def counts_by_organization(
    counter: Callable[[OrganizationUsageContext], dict[int, int]],
) -> Callable[[UsageContext], dict[int, int]]:
    """Adapt an organization-keyed counter to the engine's scope-keyed contract.

    Translates ``scope_ids`` down to organization ids on the way in and the
    resulting breakdown back up to scope ids on the way out, reusing one query's
    mapping for both.

    A scope naming something that is not an organization simply contributes no
    organization id, so it counts as zero rather than raising -- this project
    only ever bills organizations, but a personal scope created by the package's
    own defaults must not take the metering path down with it.
    """

    @wraps(counter)
    def wrapper(context: UsageContext) -> dict[int, int]:
        scope_to_organization = organization_ids_for_scope_ids(context.scope_ids)
        if not scope_to_organization:
            return {}
        organization_to_scope = {
            organization_id: scope_id for scope_id, organization_id in scope_to_organization.items()
        }

        breakdown = counter(
            OrganizationUsageContext(
                organization_ids=list(scope_to_organization.values()),
                subscription=context.subscription,
                extra=context.extra,
            )
        )

        rekeyed: dict[int, int] = {}
        for organization_id, count in breakdown.items():
            scope_id = organization_to_scope.get(organization_id)
            if scope_id is not None:
                rekeyed[scope_id] = rekeyed.get(scope_id, 0) + count
        return rekeyed

    return wrapper
