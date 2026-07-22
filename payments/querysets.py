from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal

from django.db.models import QuerySet, Sum


class ProviderWebhookEventQuerySet(QuerySet):
    """QuerySet for ``ProviderWebhookEvent``, the webhook idempotency ledger."""

    def for_provider(self, provider: str) -> ProviderWebhookEventQuerySet:
        return self.filter(provider=provider)

    def unprocessed(self) -> ProviderWebhookEventQuerySet:
        return self.filter(processed_at__isnull=True)


class MeteredOccurrenceQuerySet(QuerySet):
    """QuerySet for ``MeteredOccurrence``, the post-paid usage ledger.

    **The single definition of "an occurrence that belongs to this billing
    period".** Three callers need that rule and they must not each write their
    own: the meter (deciding what a sweep has already recorded and how much of
    the allowance is left), the usage counter behind
    ``LimitedResource.EVENT_OCCURRENCES``, and ``reconcile_period``. Two
    hand-written filters that are supposed to agree is a recurring failure mode,
    and here it would show up as silent revenue drift rather than as an exception.
    """

    def for_billing_period(
        self, subscription_id: int, billing_period_start: datetime.datetime
    ) -> MeteredOccurrenceQuerySet:
        """Rows this subscription accrued in the cycle starting at ``billing_period_start``.

        Keyed on the stamped ``billing_period_start`` rather than on an
        ``occurrence_start`` range, so the answer cannot drift when a subscription's
        period boundaries are later moved (a plan change, an interval change). What
        was billed to a cycle stays billed to that cycle.
        """
        return self.filter(
            subscription_id=subscription_id, billing_period_start=billing_period_start
        )

    def for_organizations(self, organization_ids: Sequence[int]) -> MeteredOccurrenceQuerySet:
        """Restrict to a pooled billing subtree.

        ``MeteredOccurrence`` is not an ``OrganizationModel`` (see the model
        docstring), so this is an ordinary filter — but every usage read still has
        to be organization-scoped, and going through a named method keeps that
        visible at the call site.
        """
        return self.filter(organization_id__in=organization_ids)

    def overage(self) -> MeteredOccurrenceQuerySet:
        """Only the rows that fell **outside** the included allowance.

        ``is_within_allowance`` is stamped at meter time against the effective
        limit in force then, so this reads the meter's own decision
        rather than recomputing the allowance boundary at close time — a later
        limit change must not retroactively reprice an already-metered period.
        """
        return self.filter(is_within_allowance=False)

    def overage_total(self) -> Decimal:
        """The money owed for this queryset's overage: the sum of the ``unit_price``
        columns of every row outside the allowance.

        **The single derivation of "how much overage does this period owe".** Cycle
        close charges exactly this (``CycleCloseService``), and it is a sum over the
        very same ``for_billing_period`` rows ``reconcile_period`` recomputes its
        identity set from — so what gets charged and what reconciliation audits can
        never be two different numbers. The total is ``sum(unit_price) where not
        is_within_allowance``, never ``count * current_price``: the price is the one
        stamped when each occurrence was metered, so re-pricing the plan after the
        fact cannot change a closed period's bill.
        """
        return self.overage().aggregate(total=Sum("unit_price"))["total"] or Decimal("0")
