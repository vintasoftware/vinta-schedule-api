from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Manager
from django.utils import timezone

from payments.querysets import ProviderWebhookEventQuerySet


if TYPE_CHECKING:
    from payments.models import ProviderWebhookEvent


class ProviderWebhookEventManager(Manager):
    """Manager for the webhook-delivery idempotency ledger.

    ``ProviderWebhookEvent`` is not tenant-scoped — a webhook notification arrives
    before we know which organization it resolves to (see the billing plans and
    limits plan's Data Model Changes) — so this is a plain ``Manager`` rather than
    the tenant-aware ``OrganizationManager``.

    Uses an explicit ``get_queryset()`` override (instead of
    ``Manager.from_queryset(...)`` as a base class) because inlining
    ``from_queryset`` as a dynamic base class is unsupported by the django-stubs
    mypy plugin — it only recognizes the ``Foo = Manager.from_queryset(Bar)``
    module-level assignment form, which doesn't allow adding further methods.
    """

    def get_queryset(self) -> ProviderWebhookEventQuerySet:
        return ProviderWebhookEventQuerySet(self.model, using=self._db)

    def for_provider(self, provider: str) -> ProviderWebhookEventQuerySet:
        return self.get_queryset().for_provider(provider)

    def unprocessed(self) -> ProviderWebhookEventQuerySet:
        return self.get_queryset().unprocessed()

    def get_or_create_pending(
        self, *, provider: str, route: str, external_event_id: str, payload: dict
    ) -> tuple[ProviderWebhookEvent, bool]:
        """Idempotency entry point for an inbound provider webhook delivery.

        Returns ``(event, is_new_delivery)``. ``is_new_delivery`` is ``False`` only
        when a row for this ``(provider, route, external_event_id)`` already exists
        **and** was already marked processed — the caller must short-circuit and do
        nothing else, because the provider delivered the same event more than once
        (at-least-once delivery / provider-side retries on timeout). It is ``True``
        both for a brand-new row and for an existing-but-unprocessed row (a previous
        delivery that crashed mid-processing), so a retry after a partial failure is
        allowed to run the handler again instead of being silently dropped forever.

        Must be called inside ``transaction.atomic()``: the row is locked with
        ``select_for_update()`` before ``is_new_delivery`` is decided, so a
        concurrent redelivery of the same event blocks on the lock instead of both
        deliveries racing past the check and double-processing.
        """
        event: ProviderWebhookEvent
        event, _created = self.get_queryset().get_or_create(
            provider=provider,
            route=route,
            external_event_id=external_event_id,
            defaults={"payload": payload},
        )
        event = self.get_queryset().select_for_update().get(pk=event.pk)
        is_new_delivery = event.processed_at is None
        return event, is_new_delivery

    def mark_processed(self, event: ProviderWebhookEvent) -> None:
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at", "modified"])
