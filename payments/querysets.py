from __future__ import annotations

from django.db.models import QuerySet


class ProviderWebhookEventQuerySet(QuerySet):
    """QuerySet for ``ProviderWebhookEvent``, the webhook idempotency ledger."""

    def for_provider(self, provider: str) -> ProviderWebhookEventQuerySet:
        return self.filter(provider=provider)

    def unprocessed(self) -> ProviderWebhookEventQuerySet:
        return self.filter(processed_at__isnull=True)
