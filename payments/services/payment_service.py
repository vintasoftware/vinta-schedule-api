import datetime
import logging
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated

from django.db import transaction

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from payments.billing_constants import BillingInterval, ProviderWebhookRoute
from payments.constants import (
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from payments.exceptions import (
    BillingProfileContactEmailMissingError,
    MissingBillingProfileError,
    PaymentProviderNotConfiguredError,
    UnknownPaymentProviderError,
)
from payments.models import BillingAddress as BillingAddressModel
from payments.models import BillingPlan as BillingPlanModel
from payments.models import BillingProfile as BillingProfileModel
from payments.models import Payment as PaymentModel
from payments.models import PaymentStatusUpdate as PaymentStatusUpdateModel
from payments.models import ProviderWebhookEvent as ProviderWebhookEventModel
from payments.models import Refund as RefundModel
from payments.models import RefundStatusUpdate, SubscriptionStatusUpdate
from payments.models import Subscription as SubscriptionModel
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    Payment,
    PaymentStatusUpdate,
    Plan,
    Refund,
    Subscription,
)
from payments.services.payment_adapters.base import BasePaymentAdapter
from payments.services.payment_provider_resolver import PaymentProviderResolver
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter
from payments.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory


logger = logging.getLogger(__name__)


class PaymentService[
    PaymentAdapter: BasePaymentAdapter,
    SubscriptionAdapter: BaseSubscriptionAdapter,
    SubscriptionPlanFactory: BaseSubscriptionPlanFactory,
]:
    @inject
    def __init__(
        self,
        subscription_plan_factory: SubscriptionPlanFactory,
        payment_provider_resolver: Annotated[
            PaymentProviderResolver, Provide["payment_provider_resolver"]
        ],
        payment_provider_registry: Annotated[
            dict[str, PaymentAdapter], Provide["payment_provider_registry"]
        ],
        subscription_provider_registry: Annotated[
            dict[str, SubscriptionAdapter], Provide["subscription_provider_registry"]
        ],
    ):
        self.subscription_plan_factory = subscription_plan_factory
        self.payment_provider_resolver = payment_provider_resolver
        self.payment_provider_registry = payment_provider_registry
        self.subscription_provider_registry = subscription_provider_registry

    # ------------------------------------------------------------------
    # Adapter resolution -- two variants, deliberately.
    #
    # `get_*_adapter` is **registry-only**. `get_configured_*_adapter` is
    # registry + a credential assertion. The split is load-bearing and must not
    # be collapsed back into one method:
    #
    # * The **inbound webhook** path (`verify_*_webhook_signature`,
    #   `handle_*_webhook`, `receive_*_update`) is *receiving* a notification the
    #   provider pushed at us. It authenticates the delivery with a webhook
    #   secret, not with the outbound API credential, and it must keep working in
    #   a deployment that has no outbound credential for that provider at all --
    #   otherwise every delivery 500s, the provider retries forever, and payment
    #   confirmations / `record_payment_method` / add-on activation / dunning
    #   resolution all silently stop. So it resolves through the registry alone
    #   and the only error it can raise is `UnknownPaymentProviderError`, which
    #   `payments.views.PaymentsViewSet` already renders as a 404.
    # * The **outbound** path (charges, refunds, status polls, subscription
    #   operations) is about to authenticate a real API call with the provider's
    #   secret. An empty credential there is a deployment error worth failing
    #   loudly and early on, before a `Payment`/`Subscription` row exists -- so it
    #   goes through `get_configured_*_adapter`.
    # * Registry membership is also all a *validation* caller wants (e.g.
    #   `SubscriptionService.set_payment_provider`'s staff repoint, which is
    #   legitimately allowed to point an organization at a provider whose
    #   credentials this environment has not been given yet).
    # ------------------------------------------------------------------

    def get_payment_adapter(self, provider: str) -> PaymentAdapter:
        """Resolve the payment adapter registered for *provider* -- a URL kwarg slug,
        an existing row's ``payment_provider``, or an organization's resolved provider.

        Registry lookup **only**: this answers "is ``provider`` a provider this
        deployment knows how to build an adapter for?", never "does this
        deployment hold the credential to drive it?". Callers about to make an
        outbound provider call want ``get_configured_payment_adapter`` instead --
        see the comment block above for why the two are separate.

        :raises UnknownPaymentProviderError: ``provider`` does not match any adapter
            this deployment has registered at all (bad data in a pin/URL kwarg --
            a routing/data error, not a deployment one). The only exception this
            can raise, which is what keeps the inbound webhook views' existing
            ``except UnknownPaymentProviderError`` handling exhaustive.
        """
        try:
            return self.payment_provider_registry[provider]
        except KeyError as e:
            raise UnknownPaymentProviderError(provider) from e

    def get_subscription_adapter(self, provider: str) -> SubscriptionAdapter:
        """Resolve the subscription adapter registered for *provider*.

        Same contract as ``get_payment_adapter`` -- registry lookup only. See its
        docstring and the comment block above.
        """
        try:
            return self.subscription_provider_registry[provider]
        except KeyError as e:
            raise UnknownPaymentProviderError(provider) from e

    def get_configured_payment_adapter(self, provider: str) -> PaymentAdapter:
        """``get_payment_adapter``, plus an assertion that this deployment actually
        holds the **outbound** credential the adapter authenticates provider API
        calls with. For every call site that is about to spend (or move, or poll)
        real money.

        "Configured" is asked of the adapter itself (``BasePaymentAdapter.is_configured``)
        rather than derived from a settings table here: the adapter is where the
        credential lives, so the answer cannot drift from what an outbound call
        would actually do, and a newly registered provider cannot forget to declare
        it (the conformance suite enforces the override).

        Explicitly **not** derived from
        ``payments.services.provider_credentials.resolve_public_credentials``. That
        module reads the *browser-safe publishable* key, which is never sent on an
        outbound call -- gating charges on it both refuses a provider whose secret
        key works and green-lights one whose secret key is empty. It also
        deliberately collapses "unknown slug" and "unconfigured" into a single
        error for its own read-only purpose (Phase 3 tracking decision #3), a
        collapse that must not reach adapter resolution, where the
        Unknown-vs-NotConfigured distinction is what tells "bad data in the pin
        column" apart from "this environment has no credentials for that provider".

        :raises UnknownPaymentProviderError: see ``get_payment_adapter``.
        :raises PaymentProviderNotConfiguredError: ``provider`` is a real, registered
            provider whose outbound credential is empty in this deployment.
        """
        adapter = self.get_payment_adapter(provider)
        if not adapter.is_configured:
            raise PaymentProviderNotConfiguredError(provider)
        return adapter

    def get_configured_subscription_adapter(self, provider: str) -> SubscriptionAdapter:
        """``get_subscription_adapter``, plus the same outbound-credential assertion
        ``get_configured_payment_adapter`` makes -- see its docstring.
        """
        adapter = self.get_subscription_adapter(provider)
        if not adapter.is_configured:
            raise PaymentProviderNotConfiguredError(provider)
        return adapter

    def assert_subscription_provider_configured(self, provider: str) -> None:
        """Raise unless *provider* is a registered subscription provider this
        deployment holds the outbound credential for.

        An explicit assertion rather than a discarded
        ``get_configured_subscription_adapter(...)`` return value, so a call site
        that only wants the check (``create_subscription``, which must fail before
        it writes a row it could never drive) does not read like a mistake.

        :raises UnknownPaymentProviderError: see ``get_subscription_adapter``.
        :raises PaymentProviderNotConfiguredError: see
            ``get_configured_subscription_adapter``.
        """
        self.get_configured_subscription_adapter(provider)

    def create_payment(
        self,
        organization: Organization,
        currency: str,
        amount: Decimal,
        description: str,
        payment_method: str,
        payment_token: str,
        idempotency_key: str = "",
    ) -> PaymentModel:
        try:
            billing_profile = organization.billing_profile
        except BillingProfileModel.DoesNotExist as e:
            raise ValueError("Organization does not have a billing profile") from e

        # New row: resolve from the organization -- its pin when set, the system
        # default otherwise -- never from any existing row. Resolved (and the
        # adapter looked up) *before* the `Payment` row is created, so an org
        # pinned to an unknown/unconfigured provider fails loudly with no row
        # left behind, instead of a half-created `Payment` nothing can ever drive.
        provider = self.payment_provider_resolver.resolve_for_organization(organization)
        adapter = self.get_configured_payment_adapter(provider)

        payment = PaymentModel.objects.create(
            billing_profile=billing_profile,
            currency=currency,
            value=amount,
            description=description,
            payment_method=payment_method,
            status=PaymentStatuses.PENDING_SEND,
            payment_provider=provider,
        )
        # `idempotency_key` is threaded through to the provider (see
        # `BasePaymentAdapter.process`) so a retried charge after a rolled-back
        # transaction resolves to the same provider-side charge rather than a
        # second one -- the local `Payment` row above is re-created on retry and
        # so cannot itself dedupe across the rollback.
        external_id = adapter.process(
            payment=self._serialize_payment(payment),
            payment_token=payment_token,
            idempotency_key=idempotency_key,
        )

        payment.external_id = external_id
        payment.save(update_fields=["external_id"])
        return payment

    def _serialize_billing_address(self, billing_address: BillingAddressModel) -> BillingAddress:
        return BillingAddress(
            id=billing_address.id,
            street_name=billing_address.street_name,
            street_number=billing_address.street_number,
            neighborhood=billing_address.neighborhood,
            city=billing_address.city,
            state=billing_address.state,
            country=billing_address.country,
            zip_code=billing_address.zip_code,
            address_line_2=billing_address.address_line_2,
        )

    def _serialize_billing_profile(self, billing_profile: BillingProfileModel) -> BillingProfile:
        # Billing is owned by the organization, not a person, but the gateway still
        # requires a payer identity (MercadoPago hard-400s on a null payer email).
        # `contact_*` on BillingProfile is the organization's designated billing
        # contact, sourced explicitly rather than left null.
        if not billing_profile.contact_email:
            raise BillingProfileContactEmailMissingError
        return BillingProfile(
            pk=billing_profile.pk,
            first_name=billing_profile.contact_first_name,
            last_name=billing_profile.contact_last_name or None,
            email=billing_profile.contact_email,
            phone=billing_profile.contact_phone or None,
            document_type=billing_profile.document_type,
            document_number=billing_profile.document_number,
            billing_address=self._serialize_billing_address(billing_profile.billing_address),
        )

    def _serialize_payment(self, payment: PaymentModel) -> Payment:
        return Payment(
            id=payment.id,
            value=payment.value,
            description=payment.description,
            payment_method=payment.payment_method,
            billing_profile=self._serialize_billing_profile(payment.billing_profile),
            currency=payment.currency,
            external_id=payment.external_id,
            status=payment.status,
            payment_provider=payment.payment_provider,
            status_updates=[
                PaymentStatusUpdate(
                    id=status_update.id,
                    status=status_update.status,
                    description=status_update.description,
                    update_external_id=status_update.external_id,
                )
                for status_update in payment.status_updates.all()
            ],
        )

    def process_payment(self, payment: PaymentModel, card_token: str) -> PaymentModel:
        # Existing row: resolve from `payment`'s own stored provider, never from
        # the organization's current pin -- a charge made through one provider
        # must be driven through that same provider for the rest of its life.
        adapter = self.get_configured_payment_adapter(payment.payment_provider)
        external_payment_id = adapter.process(
            self._serialize_payment(payment),
            card_token,
        )

        payment.external_id = external_payment_id
        payment.save()

        return payment

    def create_refund(
        self,
        payment_id: int,
        value: Decimal,
        currency: str,
    ) -> RefundModel:
        # Existing row: resolve from the payment being refunded's own stored
        # provider, never the organization's current pin -- and resolve (and look
        # up the adapter for) it *before* any `Refund` row exists, so an
        # unknown/unconfigured provider fails loudly with nothing left behind,
        # matching `create_payment`'s no-stray-row behavior.
        payment = PaymentModel.objects.get(pk=payment_id)
        adapter = self.get_configured_payment_adapter(payment.payment_provider)
        # Serialized *before* the `Refund` row exists, and before the `try` below,
        # for the same reason the adapter is resolved up here: `_serialize_payment`
        # raises `BillingProfileContactEmailMissingError` on a profile with no
        # billing contact, which is a local data problem, not a provider decline.
        # Inside the `try`, the broad `except Exception` would swallow it and
        # record a FAILED refund status update reading "Failed to process refund"
        # -- mislabelling a never-attempted refund as a provider-rejected one.
        serialized_payment = self._serialize_payment(payment)

        refund = RefundModel.objects.create(
            payment_id=payment_id,
            value=value,
            currency=currency,
            status=RefundStatuses.PENDING_SEND,
        )
        RefundStatusUpdate.objects.create(
            refund=refund,
            status=RefundStatuses.PENDING_SEND,
            description="Refund created in the database, will send to payment gateway",
        )
        try:
            refund_result = adapter.refund(
                Refund(
                    id=refund.id,
                    value=refund.value,
                    currency=refund.currency,
                    payment=serialized_payment,
                )
            )
            refund.external_id = refund_result.external_id
            refund.status = refund_result.status
            RefundStatusUpdate.objects.create(
                refund=refund,
                status=refund_result.status,
                # The status comes straight off the provider's create-refund
                # response (see `RefundResult`), not a subsequent
                # `check_refund_status` poll — both MercadoPago and Stripe return
                # it synchronously alongside the new refund's id.
                description=f"Refund created in the payment gateway with status {refund_result.status}",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(e)
            RefundStatusUpdate.objects.create(
                refund=refund,
                status=RefundStatuses.FAILED,
                description="Failed to process refund",
            )
            refund.status = RefundStatuses.FAILED
            pass

        refund.save()
        return refund

    def check_payment_status(self, payment: PaymentModel) -> PaymentStatusUpdate:
        # Existing row: same reasoning as `process_payment`. Outbound provider
        # call, so the credential-asserting variant.
        adapter = self.get_configured_payment_adapter(payment.payment_provider)
        return adapter.check_status(payment.external_id)

    def check_refund_status(self, refund: RefundModel) -> None:
        # Existing row: resolve from the refunded payment's own stored provider,
        # never the organization's current pin.
        adapter = self.get_configured_payment_adapter(refund.payment.payment_provider)
        refund.status = adapter.check_refund_status(
            Refund(
                id=refund.id,
                value=refund.value,
                currency=refund.currency,
                payment=self._serialize_payment(refund.payment),
                external_id=refund.external_id,
            )
        )
        refund.save()

    def get_payment_by_external_id(self, external_id: str) -> PaymentModel | None:
        return PaymentModel.objects.filter(external_id=external_id).first()

    def receive_payment_update(
        self, update_payload: dict, provider: str
    ) -> PaymentStatusUpdateModel | None:
        adapter = self.get_payment_adapter(provider)
        update_data = adapter.receive_update(update_payload)
        if not update_data:
            return None
        payment_external_id, payment_status_update_data = update_data

        payment = self.get_payment_by_external_id(payment_external_id)
        if not payment:
            return None

        return PaymentStatusUpdateModel.objects.create(
            status=payment_status_update_data.status,
            description=payment_status_update_data.description or "",
            external_id=payment_status_update_data.update_external_id or "",
            payment=payment,
        )

    def get_subscription_by_external_id(self, external_id: str) -> SubscriptionModel | None:
        return SubscriptionModel.objects.filter(external_id=external_id).first()

    def receive_subscription_payment_update(
        self, update_payload: dict, provider: str
    ) -> PaymentStatusUpdateModel | None:
        adapter = self.get_subscription_adapter(provider)
        update_data = adapter.receive_payment_update(update_payload)

        if not update_data:
            return None

        subscription_payment_data, payment_status_update_data = update_data

        subscription_external_id = subscription_payment_data.subscription_external_id
        subscription = self.get_subscription_by_external_id(subscription_external_id)
        if not subscription:
            return None

        payment_external_id = subscription_payment_data.external_id
        payment = self.get_payment_by_external_id(payment_external_id)
        if not payment:
            billing_profile = BillingProfileModel.objects.filter(
                organization=subscription.organization
            ).first()
            if billing_profile is None:
                logger.warning(
                    "Cannot create payment for subscription %s: organization %s has no "
                    "billing profile.",
                    subscription.id,
                    subscription.organization_id,
                )
                return None
            payment = PaymentModel.objects.create(
                external_id=payment_external_id,
                billing_profile=billing_profile,
                value=subscription_payment_data.value,
                currency=subscription_payment_data.currency,
                status=subscription_payment_data.status,
                description=subscription_payment_data.description,
                payment_method=subscription_payment_data.payment_method,
                subscription=subscription,
                # Every recurring subscription charge lands a `Payment` row here.
                # It must carry a provider or it is unroutable for the rest of its
                # life: `check_payment_status`/`create_refund` resolve their
                # adapter from this column (Rule A), and an empty one raises
                # `UnknownPaymentProviderError`. Both subscription adapters stamp
                # `payment_provider` onto the `SubscriptionPayment` dataclass they
                # return; `provider` (the delivering provider, off the webhook's
                # own URL kwarg) is the fallback for an adapter that ever leaves
                # it blank -- the two can never legitimately disagree, since a
                # provider only notifies about charges it made itself.
                payment_provider=subscription_payment_data.payment_provider or provider,
            )

        return PaymentStatusUpdateModel.objects.create(
            status=payment_status_update_data.status,
            description=payment_status_update_data.description or "",
            external_id=payment_status_update_data.update_external_id or "",
            payment=payment,
        )

    def verify_payment_webhook_signature(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str]
    ) -> bool:
        """Verify an inbound ``payment-update`` webhook against *raw_body*.

        ``raw_body`` must be the literal bytes the provider sent (see
        ``BasePaymentAdapter.verify_signature``) — callers must capture
        ``request.body`` before touching ``request.data``.
        """
        return self.get_payment_adapter(provider).verify_signature(raw_body, headers)

    def verify_subscription_webhook_signature(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str]
    ) -> bool:
        """Verify an inbound ``subscription-payment-update`` webhook against *raw_body*.

        ``raw_body`` must be the literal bytes the provider sent (see
        ``BaseSubscriptionAdapter.verify_signature``) — callers must capture
        ``request.body`` before touching ``request.data``.
        """
        return self.get_subscription_adapter(provider).verify_signature(raw_body, headers)

    def handle_payment_webhook(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str], payload: dict
    ) -> PaymentStatusUpdateModel | None:
        """Idempotently process an inbound ``payment-update`` webhook notification.

        Callers must call ``verify_payment_webhook_signature`` first — this method
        does not re-verify authenticity, only idempotency + dispatch. Safe to call
        more than once with the same provider event: a redelivery of an
        already-processed event is a no-op.

        ``raw_body``/``headers`` must be the same values already verified by
        ``verify_payment_webhook_signature`` — the idempotency ledger key is
        derived from them (signed material), never from ``payload`` alone, which
        may contain unsigned fields an attacker can vary across replays.

        ``mark_processed`` only runs when ``receive_payment_update`` actually
        returns a result. A ``None`` result — whether because the event is
        irrelevant or because something failed to resolve (e.g. a since-fixed
        adapter bug) — leaves the ledger row unprocessed, per
        ``ProviderWebhookEventManager.get_or_create_pending``'s contract: an
        unprocessed row is exactly what allows a provider redelivery of the same
        event to be retried instead of being permanently burned.
        """
        adapter = self.get_payment_adapter(provider)
        event_id = adapter.get_event_id(raw_body, headers, payload)
        with transaction.atomic():
            event, is_new_delivery = ProviderWebhookEventModel.objects.get_or_create_pending(
                provider=provider,
                route=ProviderWebhookRoute.PAYMENT_UPDATE,
                external_event_id=event_id,
                payload=payload,
            )
            if not is_new_delivery:
                return None

            result = self.receive_payment_update(payload, provider=provider)
            if result is not None:
                ProviderWebhookEventModel.objects.mark_processed(event)
        return result

    def handle_subscription_payment_webhook(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str], payload: dict
    ) -> PaymentStatusUpdateModel | None:
        """Idempotently process an inbound ``subscription-payment-update`` webhook.

        Callers must call ``verify_subscription_webhook_signature`` first — this
        method does not re-verify authenticity, only idempotency + dispatch. Safe to
        call more than once with the same provider event: a redelivery of an
        already-processed event is a no-op.

        ``raw_body``/``headers`` must be the same values already verified by
        ``verify_subscription_webhook_signature`` — the idempotency ledger key is
        derived from them (signed material), never from ``payload`` alone, which
        may contain unsigned fields an attacker can vary across replays.

        ``mark_processed`` only runs when ``receive_subscription_payment_update``
        actually returns a result — see ``handle_payment_webhook``'s docstring for
        why a ``None`` result must not permanently burn the delivery.
        """
        adapter = self.get_subscription_adapter(provider)
        event_id = adapter.get_event_id(raw_body, headers, payload)
        with transaction.atomic():
            event, is_new_delivery = ProviderWebhookEventModel.objects.get_or_create_pending(
                provider=provider,
                route=ProviderWebhookRoute.SUBSCRIPTION_PAYMENT_UPDATE,
                external_event_id=event_id,
                payload=payload,
            )
            if not is_new_delivery:
                return None

            result = self.receive_subscription_payment_update(payload, provider=provider)
            if result is not None:
                ProviderWebhookEventModel.objects.mark_processed(event)
        return result

    def _serialize_subscription(self, subscription: SubscriptionModel) -> Subscription:
        organization_billing_profile = BillingProfileModel.objects.filter(
            organization=subscription.organization
        ).first()
        if organization_billing_profile is None:
            logger.warning(
                "Cannot serialize subscription %s: organization %s has no billing profile.",
                subscription.id,
                subscription.organization_id,
            )
            raise MissingBillingProfileError
        return Subscription(
            id=subscription.id,
            plan=self.subscription_plan_factory.make_plan_from_subscription(subscription),
            status=subscription.status,
            external_id=subscription.external_id,
            billing_profile=self._serialize_billing_profile(organization_billing_profile),
            start_date=subscription.current_period_start.strftime("%Y-%m-%d"),
            end_date=subscription.current_period_end.strftime("%Y-%m-%d"),
        )

    def create_subscription_plan(self, plan: Plan, provider: str) -> CreatedPlan:
        """Create *plan*'s provider-side plan/price object at *provider*.

        ``provider`` is explicit rather than resolved here: this method takes a
        bare ``Plan`` dataclass with no organization or subscription attached, so
        it has nothing of its own to resolve a provider from. Every caller
        already has a ``Subscription`` in hand and passes its own
        ``payment_provider`` -- an existing row's stored provider, the same rule
        every other existing-row operation in this class follows.
        """
        adapter = self.get_configured_subscription_adapter(provider)
        external_id = adapter.create_subscription_plan(plan)
        return CreatedPlan(external_id=external_id, **asdict(plan))

    def update_subscription_plan(
        self, external_id: str, new_plan_data: Plan, provider: str
    ) -> CreatedPlan:
        """Update the provider-side plan/price object at *provider*.

        See ``create_subscription_plan`` -- same reasoning for the explicit
        ``provider`` parameter.

        **No production caller.** ``_ensure_provider_plan`` deliberately creates a
        fresh provider-side plan every time rather than updating one (see its
        docstring), so this exists only as the adapter-level counterpart of
        ``create_subscription_plan`` and is exercised by tests. Left in place
        rather than deleted because both adapters implement
        ``update_subscription_plan`` and the conformance suite requires the
        interface to stay symmetric; do not build a new flow on it without
        revisiting the per-catalog-plan caching question ``_ensure_provider_plan``
        raises.
        """
        adapter = self.get_configured_subscription_adapter(provider)
        external_id = adapter.update_subscription_plan(external_id, new_plan_data)
        return CreatedPlan(external_id=external_id, **asdict(new_plan_data))

    def create_subscription(
        self,
        organization: Organization,
        plan: BillingPlanModel,
        current_period_start: datetime.datetime,
        current_period_end: datetime.datetime,
        billing_interval: str = BillingInterval.MONTHLY,
    ) -> SubscriptionModel:
        # NOTE: this is an unconditional ``SubscriptionModel.objects.create`` against
        # a ``OneToOneField`` to ``organization``. Every billing-root organization
        # already has a ``Subscription`` (see
        # ``SubscriptionService.create_subscription_for_organization``), so calling
        # this against one raises ``IntegrityError``, and it does not create
        # ``SubscriptionPlanLimit`` / ``SubscriptionEntitlement`` rows even when it
        # succeeds. Currently exercised only by tests. Do not build new
        # subscription-creation flows on this path — use ``SubscriptionService``
        # instead; this needs reconciling with the "no plan-less state" rule before
        # it is used for real.
        if not BillingProfileModel.objects.filter(organization=organization).exists():
            raise MissingBillingProfileError

        # New row: resolve from the organization, exactly like `create_payment` --
        # and, same as there, resolve (and look up the adapter for) it *before*
        # the `Subscription` row is created, so an org pinned to an
        # unknown/unconfigured provider fails loudly with no row left behind.
        provider = self.payment_provider_resolver.resolve_for_organization(organization)
        self.assert_subscription_provider_configured(provider)

        subscription = SubscriptionModel.objects.create(
            organization=organization,
            plan=plan,
            billing_interval=billing_interval,
            current_period_start=current_period_start,
            current_period_end=current_period_end,
            status=SubscriptionStatuses.PENDING_SEND,
            payment_provider=provider,
        )

        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.PENDING_SEND,
            description="Subscription created in the database, will send to subscription gateway",
        )

        return subscription

    def process_subscription(
        self,
        subscription: SubscriptionModel,
        payment_token: str,
        idempotency_key: str = "",
    ) -> SubscriptionModel:
        # Existing row: resolve from `subscription`'s own stored provider.
        adapter = self.get_configured_subscription_adapter(subscription.payment_provider)
        subscription.external_id = adapter.create_subscription(
            subscription=self._serialize_subscription(subscription),
            payment_token=payment_token,
            idempotency_key=idempotency_key,
        )
        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.PENDING,
            description="Subscription created in subscription gateway, waiting for payment",
        )
        subscription.status = SubscriptionStatuses.PENDING
        subscription.save(update_fields=["external_id", "status"])
        return subscription

    def change_subscription_plan(
        self, subscription: SubscriptionModel, new_plan: CreatedPlan, idempotency_key: str = ""
    ) -> None:
        """Move `subscription`'s provider-side subscription onto `new_plan`.

        Thin wrapper over the adapter -- see
        `BaseSubscriptionAdapter.change_subscription_plan` for the proration
        contract. Writes nothing locally: the outcome arrives later through the
        subscription-payment webhook. `idempotency_key` is forwarded so a retried
        drive prorates at most once.

        Existing row: resolves from `subscription`'s own stored provider.
        """
        adapter = self.get_configured_subscription_adapter(subscription.payment_provider)
        adapter.change_subscription_plan(
            self._serialize_subscription(subscription), new_plan, idempotency_key=idempotency_key
        )

    def cancel_subscription(self, subscription: SubscriptionModel) -> None:
        # Existing row: resolves from `subscription`'s own stored provider.
        adapter = self.get_configured_subscription_adapter(subscription.payment_provider)
        adapter.cancel_subscription(self._serialize_subscription(subscription))
        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.CANCELLED,
            description="Subscription cancelled",
        )
        subscription.status = SubscriptionStatuses.CANCELLED
        subscription.save()
