from payments.billing_constants import BillingInterval
from payments.models import Subscription
from payments.services.dataclasses import CreatedPlan
from payments.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory


class BillingPlanFactory(BaseSubscriptionPlanFactory):
    """Builds the payment-gateway-facing ``CreatedPlan`` dataclass from a
    ``Subscription``'s catalog ``BillingPlan``.

    Replaces the dead ``OrganizationSubscriptionPlanFactory``, which read the
    now-deleted per-organization subscription-plan model that used to live in the
    ``organizations`` app, and never actually implemented ``make_plan_from_subscription``
    — the method name ``PaymentService`` calls.
    """

    def make_plan_from_subscription(self, subscription: Subscription) -> CreatedPlan:
        plan = subscription.plan
        value = (
            plan.annual_price
            if subscription.billing_interval == BillingInterval.ANNUAL
            and plan.annual_price is not None
            else plan.monthly_price
        )
        return CreatedPlan(
            id=plan.pk,
            name=plan.name,
            value=value,
            currency=plan.currency,
            # The day-of-month the provider bills on. Derived from when the current
            # period started rather than stored separately — `Subscription` carries
            # no standalone `billing_day` field. Clamped to 28: both MercadoPago and
            # Stripe reject or mishandle billing_day > 28 for monthly recurrence (not
            # every month has a 29th/30th/31st), so a period that started on one of
            # those days bills on the 28th instead of failing the provider call
            # outright.
            billing_day=min(subscription.current_period_start.day, 28),
            # Required since the Stripe adapter was added: `Plan` no longer defaults to
            # a monthly cadence, because that silently made annual plans impossible.
            # Sourced from the subscription rather than the catalog plan — the same
            # plan can be sold monthly or annually.
            billing_interval=subscription.billing_interval,
            external_id=subscription.plan_external_id,
        )
