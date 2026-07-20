"""Plain-data return types for the billing/entitlement services.

Kept separate from ``payments/services/dataclasses.py`` (which models the
*payment gateway* wire shapes) because these describe this app's own
limits/entitlements domain and are consumed by non-payments callers — the
organization, calendar, webhooks, and public-API services that ask "may I create
one more of these?".

Deferred from this phase: ``UsageSnapshot``. The plan's data-model section names it
as the periodic materialization of ``EntitlementService.get_current_usage`` (so
dashboards and invoices do not each re-run the subtree aggregate), but nothing
reads a snapshot yet — every consumer in Phases 5-6 wants a point-in-time count,
and a stale snapshot behind a *guard* would be an enforcement bug rather than an
optimization. It belongs with the phase that introduces the first reader.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EffectiveLimit:
    """The ceiling that actually applies to one resource for one billing root.

    ``limit_value`` is the subscription's ``SubscriptionPlanLimit.limit_value``
    plus the quantity of every active ``SubscriptionAddOn`` for the same resource.
    **``None`` means unlimited, never zero** — a resource with no
    ``SubscriptionPlanLimit`` row at all also resolves to ``None``, so a missing
    seed row cannot lock an organization out of a resource it used to be able to
    create (the fail-open rule from the plan's Phase 5 body).

    Deliberately carries no ``current_usage`` field, unlike the sketch in the
    plan's *Type plumbing* section: usage is a subtree-wide aggregate over several
    tables, and folding it in here would make every cheap ceiling lookup pay for a
    count it does not need. ``EntitlementService.get_current_usage`` returns it
    separately, and ``check_limit`` pairs the two.
    """

    resource_key: str
    limit_value: int | None
    kind: str | None
    overage_unit_price: Decimal | None

    @property
    def is_unlimited(self) -> bool:
        return self.limit_value is None


@dataclass(frozen=True)
class LimitCheckResult:
    """Outcome of ``EntitlementService.check_limit``.

    ``ceiling is None`` means unlimited, in which case ``allowed`` is always
    ``True``. ``remedy`` is populated only when ``allowed`` is ``False``; it is one
    of ``LimitRemedy`` and is what the over-limit error body surfaces to clients.

    ``current_usage is None`` means **usage was not counted**, which happens on
    exactly one path: an unlimited ceiling, where the answer cannot depend on it
    and counting would make every guarded create on the ``unlimited`` plan (i.e.
    every organization, for the whole rollout) pay for several queries nobody
    reads. It is ``None`` rather than ``0`` so a caller cannot mistake "not
    measured" for "measured, and it was zero". It is always an ``int`` on the
    branch that matters — ``allowed is False`` — which is the only branch the
    over-limit error body is built from.
    """

    allowed: bool
    resource_key: str
    current_usage: int | None
    ceiling: int | None
    remedy: str | None = None
