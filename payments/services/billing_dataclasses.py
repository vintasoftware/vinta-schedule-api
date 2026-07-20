"""Plain-data return types for the billing/entitlement services.

Kept separate from ``payments/services/dataclasses.py`` (which models the
*payment gateway* wire shapes) because these describe this app's own
limits/entitlements domain and are consumed by non-payments callers — the
organization, calendar, webhooks, and public-API services that ask "may I create
one more of these?".
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
    """

    allowed: bool
    resource_key: str
    current_usage: int
    ceiling: int | None
    remedy: str | None = None
