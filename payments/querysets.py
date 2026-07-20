from __future__ import annotations

import datetime
from collections.abc import Sequence

from django.db.models import QuerySet


class ProviderWebhookEventQuerySet(QuerySet):
    """QuerySet for ``ProviderWebhookEvent``, the webhook idempotency ledger."""

    def for_provider(self, provider: str) -> ProviderWebhookEventQuerySet:
        return self.filter(provider=provider)

    def unprocessed(self) -> ProviderWebhookEventQuerySet:
        return self.filter(processed_at__isnull=True)


class MeteredOccurrenceQuerySet(QuerySet):
    """QuerySet for ``MeteredOccurrence``, the post-paid usage ledger.

    **The single definition of "an occurrence that belongs to this billing
    period".** Three callers need that predicate and they must not each write
    their own: the meter (deciding what a sweep has already recorded and how much
    of the allowance is left), the usage counter behind
    ``LimitedResource.EVENT_OCCURRENCES``, and ``reconcile_period``. Two
    hand-written filters that are supposed to agree is the failure mode that has
    produced a defect in every phase of this plan, and here it would surface as
    silent revenue drift rather than as an exception.
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
