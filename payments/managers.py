from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import TYPE_CHECKING

from django.db.models import Manager
from django.utils import timezone

from payments.querysets import MeteredOccurrenceQuerySet, ProviderWebhookEventQuerySet


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


class MeteredOccurrenceManager(Manager):
    """Manager for the post-paid occurrence ledger.

    A plain ``Manager`` for the same reason as ``ProviderWebhookEventManager``:
    ``MeteredOccurrence`` is not an ``OrganizationModel``, because billing reads
    legitimately cross organizations (summing a reseller subtree's usage, sweeping
    every subscription at cycle close). See the model docstring.
    """

    def get_queryset(self) -> MeteredOccurrenceQuerySet:
        return MeteredOccurrenceQuerySet(self.model, using=self._db)

    def for_billing_period(
        self, subscription_id: int, billing_period_start: datetime.datetime
    ) -> MeteredOccurrenceQuerySet:
        return self.get_queryset().for_billing_period(subscription_id, billing_period_start)

    def for_organizations(self, organization_ids: Sequence[int]) -> MeteredOccurrenceQuerySet:
        return self.get_queryset().for_organizations(organization_ids)


class LimitWarningNotificationManager(Manager):
    """Manager for the approaching-limit / limit-reached debounce ledger.
    A plain ``Manager`` -- no custom queryset, since the only query this model
    needs is the idempotent claim below; see the model docstring
    for why a durable row, not an in-memory flag, is what makes
    ``check_approaching_limits`` safe to re-run every beat tick.
    """

    def mark_if_new(
        self,
        *,
        subscription_id: int,
        resource_key: str,
        billing_period_start: datetime.datetime,
        level: str,
    ) -> bool:
        """Atomically claim the ``(subscription, resource_key,
        billing_period_start, level)`` marker.

        Returns ``True`` the first time this exact marker is claimed within the
        cycle -- the caller should send the notification only then. Returns
        ``False`` on every subsequent call for the same marker (already
        notified this cycle), including a redelivered/re-ticked beat run.

        ``get_or_create`` rather than a separate locked read-then-write: the
        unique constraint is what makes a concurrent double-claim resolve to
        one row either way (Django retries the ``create`` once on
        ``IntegrityError`` from a losing race), and two beat ticks racing to
        send the *same* warning is a low-stakes, low-probability event this
        does not need a row lock to close out completely.
        """
        _row, created = self.get_or_create(
            subscription_id=subscription_id,
            resource_key=resource_key,
            billing_period_start=billing_period_start,
            level=level,
        )
        return created
