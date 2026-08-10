import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Literal

import stripe

from payments.billing_constants import BillingInterval
from payments.constants import PaymentProviders, PaymentStatuses
from payments.exceptions import (
    NoOutstandingBalanceError,
    PaymentAdapterError,
    ProviderWebhookEventIdMissingError,
)
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    PaymentStatusUpdate,
    Plan,
    Subscription,
    SubscriptionPayment,
)
from payments.services.payment_adapters.stripe_payment_adapter import (
    PAYMENT_INTENT_STATUS_MAPPING,
    ZERO_DECIMAL_CURRENCIES,
    to_stripe_amount,
)
from payments.services.stripe_signature import verify_stripe_event
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter


logger = logging.getLogger(__name__)


#: Stripe webhook event types this adapter's `is_payment_update` acts on.
RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES = frozenset({"invoice.paid", "invoice.payment_failed"})
#: Event types whose `data.object` *is* the subscription itself, as opposed to an
#: invoice that merely references one via its own `subscription` field.
SUBSCRIPTION_EVENT_TYPE_PREFIX = "customer.subscription."
INVOICE_EVENT_TYPE_PREFIX = "invoice."


def _stripe_recurring_interval(billing_interval: str) -> Literal["month", "year"]:
    """Map our internal cadence onto Stripe's `Price.recurring.interval`.

    Stripe only accepts `"day"`, `"week"`, `"month"`, or `"year"` — there is no
    concept of an explicit "every N months" annual plan the way MercadoPago's
    `frequency`/`frequency_type` pair allows, so annual maps directly onto
    `"year"` rather than 12 months.
    """
    if billing_interval == BillingInterval.ANNUAL:
        return "year"
    return "month"


def _expandable_id(value: "str | stripe.Product | stripe.Customer") -> str:
    """Stripe's expandable relations (e.g. `Price.product`, `Subscription.customer`)
    are typed as either the bare id string or the expanded resource, depending
    on whether the original call requested expansion. None of this adapter's
    retrievals ask for it, but narrowing explicitly here is cheaper than
    threading an `expand=` list through every call site that only ever wants
    the id.
    """
    return value if isinstance(value, str) else value.id


def _billing_profile_from_payment_intent_payload(payment_payload: dict) -> BillingProfile:
    """Best-effort `BillingProfile` reconstruction from a Stripe `PaymentIntent`.

    `PaymentService.receive_subscription_payment_update` never actually reads
    `SubscriptionPayment.billing_profile` — it sources the persisted payment's
    billing profile from the subscription's own organization instead — so this
    exists purely to satisfy the dataclass's shape, not because anything
    downstream depends on its accuracy.

    `PaymentIntent.charges` (the field this used to reconstruct billing details
    from) was removed from the API; the replacement, `latest_charge`, is an
    expandable id/`Charge` union that `get_payment_payload` never asks to be
    expanded purely to populate a value nothing downstream reads. Rather than
    add an expand solely for that, this returns an explicitly empty profile.
    """
    return BillingProfile(
        pk=None,
        first_name=None,
        last_name=None,
        email=None,
        phone=None,
        document_type=None,
        document_number=None,
        billing_address=BillingAddress(
            id=None,
            street_name="",
            street_number="",
            neighborhood=None,
            address_line_2="",
            city="",
            state="",
            country="",
            zip_code="",
        ),
    )


def _select_payment_intent_id(payments: object) -> str | None:
    """Pick the PaymentIntent id off a Stripe `payments` list (an
    `InvoicePayment` collection) -- never blindly `data[0]`. Stripe does not
    document an ordering guarantee for this list, and a dunning-recovered
    invoice carries *both* the dead card's failed attempt and the new card's
    successful one: `data[0]` picked the failed attempt's PaymentIntent in
    this phase's reproduction, whose status (`"pending"` in the reviewer's
    repro) matches neither `APPROVED` nor any `FAILED_SUBSCRIPTION_PAYMENT
    _STATUSES` member, so nothing happened even though the balance was
    genuinely collected (Billing API Contract Hardening, Phase 4 reviewer
    finding BLOCKER 2).

    Prefers the entry whose own `status` (`InvoicePayment.status`, one of
    `"open"`, `"paid"`, `"canceled"`) is `"paid"` -- the one that actually
    collected money. When none is `"paid"` (e.g. every attempt on this invoice
    is still failing), falls back to the most recently created entry --
    `created` is a real, orderable `InvoicePayment` field, unlike list
    position.
    """
    payment_entries = payments.get("data") or [] if isinstance(payments, dict) else []
    if not payment_entries:
        return None
    paid_entries = [entry for entry in payment_entries if entry.get("status") == "paid"]
    candidates = paid_entries or payment_entries
    selected = max(candidates, key=lambda entry: entry.get("created") or 0)
    payment = selected.get("payment") or {}
    payment_intent = payment.get("payment_intent")
    if isinstance(payment_intent, dict):
        return payment_intent.get("id")
    return payment_intent


class StripeSubscriptionAdapter(BaseSubscriptionAdapter):
    provider = PaymentProviders.STRIPE
    #: Stripe's `Stripe-Signature` header signs `{timestamp}.{raw_body}` — the
    #: entire body — unlike MercadoPago's narrower manifest. See
    #: `payments.services.stripe_signature.verify_stripe_event`.
    verifies_full_body = True

    def __init__(self, api_key: str, webhook_secret: str = ""):
        self.api_key = api_key
        self.webhook_secret = webhook_secret

    @property
    def is_configured(self) -> bool:
        """See ``BaseSubscriptionAdapter.is_configured``. ``STRIPE_SECRET_KEY`` is
        the credential every outbound Stripe call is made with (``api_key=``)."""
        return bool(self.api_key)

    def create_subscription_plan(self, plan: Plan) -> str:
        """
        Stripe has no single "plan" resource for a new integration to target (the
        legacy `Plan` API is deprecated in favor of `Product` + `Price`) — a
        recurring price always has to be created against a product. The
        `Product` is created here alongside the `Price` rather than assumed to
        pre-exist, since nothing upstream of this adapter has a Stripe product id
        to pass in.
        """
        product = stripe.Product.create(
            name=plan.name,
            metadata={"plan_id": str(plan.id)},
            api_key=self.api_key,
        )
        price = stripe.Price.create(
            product=product.id,
            unit_amount=to_stripe_amount(plan.value, plan.currency),
            currency=plan.currency.lower(),
            recurring={"interval": _stripe_recurring_interval(plan.billing_interval)},
            api_key=self.api_key,
        )
        return price.id

    def update_subscription_plan(self, external_id: str, plan: Plan) -> str:
        """
        Stripe `Price` objects are immutable once created (amount, currency, and
        recurring cadence can never change) — MercadoPago's "update this plan in
        place, same id" has no Stripe equivalent. The idiomatic replacement is to
        archive the old price and mint a new one against the same product,
        returning the *new* external id — callers must persist it, which is why
        this (and `update_plan`, below) both return the id rather than assuming
        it's unchanged.
        """
        old_price = stripe.Price.retrieve(external_id, api_key=self.api_key)
        stripe.Price.modify(external_id, active=False, api_key=self.api_key)
        new_price = stripe.Price.create(
            product=_expandable_id(old_price.product),
            unit_amount=to_stripe_amount(plan.value, plan.currency),
            currency=plan.currency.lower(),
            recurring={"interval": _stripe_recurring_interval(plan.billing_interval)},
            api_key=self.api_key,
        )
        return new_price.id

    def create_subscription(
        self, subscription: Subscription, payment_token: str, idempotency_key: str = ""
    ) -> str:
        """
        `payment_token` is a Stripe `PaymentMethod` id — the closest Stripe
        equivalent to MercadoPago's `card_token_id`.

        `idempotency_key`, when set, guards the money-moving `Subscription.create`
        so a retried first-upgrade does not create a second subscription (and a
        second charge). It is *not* reused for `Customer.create`: Stripe scopes an
        idempotency key to identical request parameters, so reusing one key across
        two different calls would make the second error — and a duplicate Customer
        (unlike a duplicate Subscription) moves no money.
        """
        billing_profile = subscription.billing_profile
        full_name = " ".join(
            part for part in (billing_profile.first_name, billing_profile.last_name) if part
        )
        customer_params: dict = {
            "payment_method": payment_token,
            "invoice_settings": {"default_payment_method": payment_token},
            "metadata": {"subscription_id": str(subscription.id)},
            "api_key": self.api_key,
        }
        # `email`/`name` are optional keys, not `Optional[str]` values, in
        # Stripe's typed params — only included when actually present.
        if billing_profile.email:
            customer_params["email"] = billing_profile.email
        if full_name:
            customer_params["name"] = full_name
        customer = stripe.Customer.create(**customer_params)
        subscription_params: dict = {
            "customer": customer.id,
            "items": [{"price": subscription.plan.external_id}],
            "default_payment_method": payment_token,
            "metadata": {"subscription_id": str(subscription.id)},
            "api_key": self.api_key,
        }
        if idempotency_key:
            subscription_params["idempotency_key"] = idempotency_key
        stripe_subscription = stripe.Subscription.create(**subscription_params)
        return stripe_subscription.id

    def cancel_subscription(self, subscription: Subscription) -> None:
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot cancel subscription {subscription.id} with no external_id"
            )
        stripe.Subscription.cancel(subscription.external_id, api_key=self.api_key)

    def update_plan(self, plan: CreatedPlan) -> CreatedPlan:
        new_external_id = self.update_subscription_plan(plan.external_id, plan)
        return replace(plan, external_id=new_external_id)

    def change_subscription_plan(
        self, subscription: Subscription, new_plan: CreatedPlan, idempotency_key: str = ""
    ) -> None:
        """
        Stripe subscriptions are moved onto a new price by modifying the
        subscription's existing line item (a subscription always has exactly one
        here — this adapter creates it with a single ``items=[{"price": ...}]``
        in ``create_subscription``) rather than by re-creating the subscription.
        ``proration_behavior="always_invoice"`` makes Stripe compute the prorated
        amount server-side *and* invoice + attempt to charge it immediately
        against the subscription's default payment method, rather than only
        crediting/debiting the next regular invoice — matching "pay now" for an
        upgrade a user just requested.

        `idempotency_key`, when set, guards the money-moving `Subscription.modify`
        (which invoices the proration immediately) so a retried drive prorates at
        most once. The read-only `Subscription.retrieve` above does not need it.
        """
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot change plan for subscription {subscription.id} with no external_id"
            )
        stripe_subscription = stripe.Subscription.retrieve(
            subscription.external_id, api_key=self.api_key
        )
        item_id = stripe_subscription["items"]["data"][0]["id"]
        modify_params: dict = {
            "items": [{"id": item_id, "price": new_plan.external_id}],
            "proration_behavior": "always_invoice",
            "api_key": self.api_key,
        }
        if idempotency_key:
            modify_params["idempotency_key"] = idempotency_key
        stripe.Subscription.modify(subscription.external_id, **modify_params)

    def update_subscription_payment_token(
        self, subscription: Subscription, payment_token: str
    ) -> None:
        """
        Attaches `payment_token` to the subscription's Stripe customer *before*
        pointing anything at it: `PaymentMethod.attach` must run first, because
        both `Subscription.modify(default_payment_method=...)` and
        `Customer.modify(invoice_settings=...)` reject a `PaymentMethod` that
        is not already attached to the customer -- and a fresh Stripe Elements
        `pm_...` token from the browser is never attached yet.

        Also updates the customer's own `invoice_settings.default_payment_method`,
        not only `Subscription.default_payment_method` -- for parity with
        `create_subscription`, which pins both the same way at first payment.
        Without this, the two fall out of sync after the very first
        `retry_payment` recovery: `Invoice.pay`'s fallback precedence when no
        `payment_method` is passed explicitly consults the customer default,
        and nothing else in this adapter kept it pointed at the current card.
        `pay_outstanding_invoice` (below) passes `payment_token` to `Invoice.pay`
        explicitly and so does not itself depend on this precedence, but a
        dashboard-driven "Retry now" or any future direct `Invoice.pay` call
        with no explicit `payment_method` does.
        """
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot update payment token for subscription {subscription.id} "
                "with no external_id"
            )
        stripe_subscription = stripe.Subscription.retrieve(
            subscription.external_id, api_key=self.api_key
        )
        customer_id = _expandable_id(stripe_subscription.customer)
        stripe.PaymentMethod.attach(payment_token, customer=customer_id, api_key=self.api_key)
        stripe.Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": payment_token},
            api_key=self.api_key,
        )
        stripe.Subscription.modify(
            subscription.external_id,
            default_payment_method=payment_token,
            api_key=self.api_key,
        )

    def pay_outstanding_invoice(
        self, subscription: Subscription, payment_token: str, idempotency_key: str = ""
    ) -> None:
        """
        Stripe's actual "collect now" primitive -- see
        ``BaseSubscriptionAdapter.pay_outstanding_invoice`` for why this is not
        ``change_subscription_plan``: that method moves the subscription onto a
        plan/price and only invoices a *proration* as a side effect, which is
        exactly what collected $0.00 against a real past-due renewal invoice in
        this phase's probe (see the base docstring for the numbers). This
        method instead looks up the subscription's own unpaid invoices -- the
        ones dunning is actually chasing -- and pays each directly, the same
        action Stripe's own dashboard "Retry now" button drives.

        A subscription can carry **more than one** unpaid invoice at once: a
        non-zero proration invoice from ``change_subscription_plan`` (an
        upgrade drive) can sit alongside the original past-due renewal
        invoice dunning already opened. Every match is fetched (no ``limit``)
        and paid, oldest (``created``) first -- Stripe's list ordering is not
        documented as chronological, so sorting explicitly is the only way to
        pay the original past-due invoice before a later proration rather than
        the reverse (which would return 200 while the payer still owes the
        renewal).

        Invoices are queried in both ``open`` and ``uncollectible`` status:
        an account configured to mark an invoice ``uncollectible`` once its
        automatic retries are exhausted moves the balance out of ``open``
        without collecting it, and querying only ``open`` would make every
        subsequent retry-payment attempt return a spurious 409
        (``NoOutstandingBalanceError``) while money is still owed. This
        adapter does not control (and cannot verify from here) whether the
        connected Stripe account actually has that "mark uncollectible after
        retries" setting enabled -- if it does not, no invoice will ever reach
        ``uncollectible`` and this query is simply always empty.

        `payment_token` is passed to `Invoice.pay` as an explicit
        `payment_method=` rather than left to Stripe's default-payment-method
        precedence (`Subscription.default_payment_method`, then
        `Customer.invoice_settings.default_payment_method`) -- this phase's
        original probe could not confirm that precedence order because it had
        updated both to the new card. Passing it explicitly removes the
        ambiguity: every invoice paid here is always charged against the
        specific instrument `retry_payment` just attached
        (`update_subscription_payment_token`, called immediately before this).

        `idempotency_key`, when set, guards the money-moving `Invoice.pay`
        calls so a retried collection attempt resolves to the same payment
        attempts rather than new ones. It is namespaced *per invoice*
        (``{idempotency_key}-{invoice.id}``) rather than reused as-is across
        every invoice paid in this call -- Stripe scopes an idempotency key to
        a single set of request parameters, and reusing one key across two
        `Invoice.pay` calls with two different invoice ids would make the
        second call error instead of collecting.
        """
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot pay outstanding invoice for subscription {subscription.id} "
                "with no external_id"
            )
        outstanding_invoices: list = []
        for invoice_status in ("open", "uncollectible"):
            invoices = stripe.Invoice.list(
                subscription=subscription.external_id,
                status=invoice_status,
                api_key=self.api_key,
            )
            outstanding_invoices.extend(invoices.data)
        if not outstanding_invoices:
            raise NoOutstandingBalanceError(subscription.id)
        outstanding_invoices.sort(key=lambda invoice: invoice.created)
        logger.info(
            "Paying %d outstanding invoice(s) for subscription %s",
            len(outstanding_invoices),
            subscription.id,
        )
        for invoice in outstanding_invoices:
            pay_params: dict = {"payment_method": payment_token, "api_key": self.api_key}
            if idempotency_key:
                pay_params["idempotency_key"] = f"{idempotency_key}-{invoice.id}"
            stripe.Invoice.pay(invoice.id, **pay_params)

    def get_subscription_external_id_from_update(self, update_payload: dict) -> str | None:
        """
        Unlike MercadoPago's fixed `data.id` path, the subscription id's location
        in a Stripe webhook payload depends on the event type: a
        `customer.subscription.*` event's `data.object` *is* the subscription
        (so its own `id` is what we want), while an `invoice.*` event's
        `data.object` is an invoice that only *references* its subscription.

        As of the pinned `2026-06-24.dahlia` API version, `Invoice.subscription`
        no longer exists — the id lives at `parent.subscription_details.subscription`
        (`Invoice.parent` is only populated for invoices that came from a
        subscription; `type` is `"subscription_details"` in that case). The bare
        `subscription` field is still read as a fallback for any pre-dahlia
        payload this might ever see.
        """
        event_type = update_payload.get("type", "")
        obj = update_payload.get("data", {}).get("object", {})
        if event_type.startswith(SUBSCRIPTION_EVENT_TYPE_PREFIX):
            return obj.get("id")
        if event_type.startswith(INVOICE_EVENT_TYPE_PREFIX):
            subscription_details = (obj.get("parent") or {}).get("subscription_details") or {}
            return subscription_details.get("subscription") or obj.get("subscription")
        return None

    def get_update_id(self, update_payload: dict) -> str | None:
        return update_payload.get("id")

    def get_payment_payload(self, payment_external_id: str) -> dict:
        intent = stripe.PaymentIntent.retrieve(payment_external_id, api_key=self.api_key)
        return intent.to_dict()

    def create_subscription_payment_from_payment_payload(
        self, subscription_external_id: str, payment_payload: dict
    ) -> SubscriptionPayment:
        currency = (payment_payload.get("currency") or "").upper()
        amount = Decimal(payment_payload.get("amount", 0))
        value = amount if currency.lower() in ZERO_DECIMAL_CURRENCIES else amount / Decimal(100)
        return SubscriptionPayment(
            id=None,
            subscription_external_id=subscription_external_id,
            external_id=payment_payload.get("id", ""),
            value=value,
            currency=currency,
            payment_provider=PaymentProviders.STRIPE,
            status=payment_payload.get("status", ""),
            billing_profile=_billing_profile_from_payment_intent_payload(payment_payload),
            payment_method=(payment_payload.get("payment_method_types") or [""])[0],
            description=payment_payload.get("description") or "",
            status_updates=[],
        )

    def create_status_update_from_payment_payload(
        self, payment_payload: dict
    ) -> PaymentStatusUpdate:
        original_status = payment_payload.get("status", "")
        mapped_status = PAYMENT_INTENT_STATUS_MAPPING.get(original_status, PaymentStatuses.UNKNOWN)
        if mapped_status == PaymentStatuses.UNKNOWN:
            logger.error(
                "Unknown subscription payment status: payment_external_id=%s original_status=%s",
                payment_payload.get("id"),
                original_status,
            )
        last_payment_error = payment_payload.get("last_payment_error")
        description = last_payment_error.get("message") if last_payment_error else original_status
        return PaymentStatusUpdate(
            id=None,
            status=mapped_status,
            description=description,
            update_external_id=payment_payload.get("id"),
        )

    def is_payment_update(self, update_payload: dict) -> bool:
        return update_payload.get("type") in RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES

    def get_subscription_payload(self, subscription_external_id: str) -> dict:
        """
        `Invoice.payment_intent` no longer exists as of the pinned
        `2026-06-24.dahlia` API version — expanding it raises
        `invalid_request_error`. The PaymentIntent id is reached instead via
        `Invoice.payments` (a list of `InvoicePayment`s, itself only populated
        when expanded) -> `InvoicePayment.payment.payment_intent`. Only the id
        is needed (see `get_payment_external_id_from_subscription_payload`), so
        the payment_intent sub-field itself is left unexpanded.
        """
        subscription = stripe.Subscription.retrieve(
            subscription_external_id,
            expand=["latest_invoice.payments"],
            api_key=self.api_key,
        )
        return subscription.to_dict()

    def get_payment_external_id_from_subscription_payload(
        self, subscription_payload: dict
    ) -> str | None:
        latest_invoice = subscription_payload.get("latest_invoice")
        if not isinstance(latest_invoice, dict):
            return None
        return _select_payment_intent_id(latest_invoice.get("payments"))

    def _get_payment_external_id_from_invoice(self, invoice_id: str) -> str | None:
        """Resolve the PaymentIntent id off `invoice_id`'s own `payments` list --
        the specific invoice a webhook event was actually about -- rather than
        `Subscription.latest_invoice` (see `receive_payment_update`'s docstring
        for why the distinction matters). Same expand/field shape as
        `get_subscription_payload` -- `Invoice.payment_intent` no longer exists
        under the pinned `2026-06-24.dahlia` API version.
        """
        invoice = stripe.Invoice.retrieve(invoice_id, expand=["payments"], api_key=self.api_key)
        return _select_payment_intent_id(invoice.to_dict().get("payments"))

    def receive_payment_update(
        self, update_payload: dict
    ) -> tuple[SubscriptionPayment, PaymentStatusUpdate] | None:
        """Override `BaseSubscriptionAdapter.receive_payment_update`'s generic
        subscription-payload lookup for `invoice.*` events: resolve the payment
        off the invoice the event was actually about (`data.object.id` ->
        `Invoice.retrieve(..., expand=["payments"])`), not
        `Subscription.latest_invoice` -- the most recently *created* invoice,
        not necessarily the one this event fired for.

        This mattered concretely for grace recovery: the dunning ladder mints a
        fresh $0 proration invoice on every grace tick, so by the time a payer
        recovers through `retry_payment`, `latest_invoice` is that $0 invoice.
        A $0 invoice has no PaymentIntent, so the old code (which always went
        through `get_subscription_payload`/`get_payment_external_id_from
        _subscription_payload`) silently returned `None` here even though
        `Invoice.pay` had genuinely collected the real balance on the *actual*
        past-due invoice named in the `invoice.paid` event -- money moved, but
        no `Payment` row, no `PaymentStatusUpdate`, and the subscription rode
        GRACE straight to RESTRICTED (Billing API Contract Hardening, Phase 4
        reviewer finding BLOCKER 1).

        `customer.subscription.*` events have no specific invoice to resolve
        against -- their `data.object` *is* the subscription itself, not
        something that references one -- so those are unaffected and still go
        through the base class's `latest_invoice`-based lookup unchanged.
        """
        if not self.is_payment_update(update_payload):
            return None

        subscription_external_id = self.get_subscription_external_id_from_update(update_payload)
        if not subscription_external_id:
            logger.error(
                "Subscription external id not found in update payload. payload: %s",
                json.dumps(update_payload),
            )
            return None

        event_type = update_payload.get("type", "")
        if event_type.startswith(INVOICE_EVENT_TYPE_PREFIX):
            invoice_id = (update_payload.get("data", {}).get("object", {}) or {}).get("id")
            payment_external_id = (
                self._get_payment_external_id_from_invoice(invoice_id) if invoice_id else None
            )
        else:
            subscription_payload = self.get_subscription_payload(subscription_external_id)
            payment_external_id = self.get_payment_external_id_from_subscription_payload(
                subscription_payload
            )
        if not payment_external_id:
            return None

        payment_payload = self.get_payment_payload(payment_external_id)
        return (
            self.create_subscription_payment_from_payment_payload(
                subscription_external_id, payment_payload
            ),
            self.create_status_update_from_payment_payload(payment_payload),
        )

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_stripe_event(raw_body, headers, self.webhook_secret) is not None

    def get_event_id(self, raw_body: bytes, headers: Mapping[str, str], payload: dict) -> str:
        """See `StripePaymentAdapter.get_event_id` — same reasoning, same source
        of truth (a fresh, independently re-verified `construct_event` call, not
        `payload`)."""
        event = verify_stripe_event(raw_body, headers, self.webhook_secret)
        if event is None:
            raise ProviderWebhookEventIdMissingError
        return event.id
