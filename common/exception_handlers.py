"""Project-wide DRF exception handler.

Registered as ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``. It delegates to DRF's own
handler for everything it does not explicitly know about, so adding a case here
cannot change the *rendering* of any existing error.

It can, however, change **transactional semantics**, which is not the same claim.
Returning a ``Response`` swallows the exception, so under
``ATOMIC_REQUESTS = True`` (production) the request transaction would otherwise
*commit* everything written before the raise. Every branch that returns a
``Response`` here must therefore call ``rest_framework.views.set_rollback()``
first, exactly as DRF's own handler does for every ``APIException``.
"""

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback
from vinta_billing.exception_handling import billing_error_status
from vinta_billing.exceptions import (
    AddOnNotPurchasableError,
    ChargeDeclinedError,
    CollectionNotSupportedError,
    NoOutstandingBalanceError,
    OverLimitError,
    PaymentProviderNotConfiguredError,
    PaymentTokenRequiredError,
    RetryPaymentNotApplicableError,
    SubscriptionNotAttachedError,
    UnconfirmedPlanChangeError,
)


def vinta_exception_handler(exc: Exception, context: dict) -> Response | None:
    """Render domain exceptions that are not DRF ``APIException`` subclasses.

    ``OverLimitError`` is rendered as **HTTP 402 Payment Required** rather than
    403, so a client can distinguish "you are not allowed to do this" from "you
    have run out of capacity, here is how to get more". The body is the shared
    over-limit contract (``OverLimitError.as_error_body``) — the GraphQL surface
    renders the same dict through its own error extension, so the two surfaces
    stay byte-identical without either restating the shape.

    ``PaymentProviderNotConfiguredError`` is rendered as **HTTP 503 Service
    Unavailable** for "the provider this organization resolves to cannot be
    driven by this deployment". Handled centrally rather than per-action
    because it is reachable from every billing write that touches a provider
    (``SubscriptionViewSet.change_plan`` / ``cancel``, ``AddOnViewSet.create``,
    and anything added later), and a per-action ``except`` would have to be
    remembered at each new one — the failure mode of forgetting is a 500 on a
    money path. ``vinta_billing.views.PaymentProviderViewSet``'s two
    provider-credentials endpoints catch it themselves first, both answering
    503 as of ``vinta-django-billing`` 0.6.0, so they are unaffected by this
    branch and agree with it.

    This status was a hardcoded **409** here until ``vinta-django-billing``
    0.6.0, deliberately overriding the package's table. The reason no longer
    exists: the package's own ``change_plan`` / ``cancel`` / ``create``
    annotations used to promise 409 for this error while its handler returned
    503, and this project sided with the annotations. 0.6.0 resolved that in
    favour of 503 — an unconfigured provider is a deployment fault, the same
    request fails identically until an operator fixes it, and the two sibling
    faults in that family (``NoDefaultBillingPlanError``,
    ``IncompleteBillingPlanError``, the latter reachable from ``change-plan``)
    were already 503, so a 409 here would have split one family across two
    statuses. The carve-out is gone; every status in this function now comes
    from ``billing_error_status`` rather than being re-derived by hand.

    ``PaymentTokenRequiredError`` / ``AddOnNotPurchasableError`` (400) and
    ``UnconfirmedPlanChangeError`` (409): every ``BillingError`` subclass carries
    a stable ``code`` (see ``vinta_billing.exceptions.BillingError``), and these three
    render it through
    the shared ``as_error_body()`` contract instead of each view improvising its
    own ad hoc body (a field-keyed ``ValidationError`` for the first two, a plain
    ``{"detail": ...}`` 409 for the third). Handled centrally for the same reason
    as ``PaymentProviderNotConfiguredError`` above — a per-action ``except`` is
    something every new billing write has to remember, the handler is something
    it inherits for free.

    ``RetryPaymentNotApplicableError`` / ``SubscriptionNotAttachedError`` (409)
    are grace-recovery errors, raised by
    ``SubscriptionService.retry_payment``. Both 409 Conflict, same status as
    ``UnconfirmedPlanChangeError`` above -- the request is well-formed, but the
    subscription's current state (not GRACE/RESTRICTED, or never attached at
    the provider) conflicts with what retry-payment needs to be true.

    ``NoOutstandingBalanceError`` (409) is raised by
    ``BaseSubscriptionAdapter.pay_outstanding_invoice`` (via
    ``SubscriptionService.retry_payment``) when the provider reports nothing
    actually owed for a GRACE/RESTRICTED subscription. Same status as the two
    above, for the same reason: a well-formed request whose target state does
    not hold.

    ``CollectionNotSupportedError`` (409) is raised by
    ``BaseSubscriptionAdapter.pay_outstanding_invoice`` when the resolved
    provider (MercadoPago, as of this writing) has no verified "collect the
    outstanding balance" primitive to drive. Before this class and branch
    existed, this reached the client as an unhandled 500: the plain
    ``PaymentAdapterError`` it replaced is a ``BillingError``/``ValueError``,
    not a DRF ``APIException``, and had no branch here (reviewer finding
    SHOULD-FIX 7).

    ``ChargeDeclinedError`` (**402 Payment Required**) is raised by
    ``StripeSubscriptionAdapter.pay_outstanding_invoice`` (via
    ``SubscriptionService.retry_payment``) when the provider either attempts
    the charge and the card is declined, or refuses to attempt it at all
    (e.g. no default payment method on file) -- see ``ChargeDeclinedError``'s
    own docstring for the full translation. 402 rather than 409: this is not
    "the subscription's state conflicts with the request" (the 409 group
    above) but the literal, semantically exact "payment is required and this
    attempt did not provide it" -- the same status ``OverLimitError`` renders
    for a different reason. ``code`` (``"charge_declined"`` vs.
    ``OverLimitError``'s ``"limit_exceeded"``) is what a client branches on to
    tell the two 402s apart. Before this class and branch existed, a declined
    card reached ``retry_payment``'s caller as an unhandled 500 -- the
    user-facing half of the same live-probe BLOCKER that made the automatic
    dunning ladder's own retry (``SubscriptionService.retry_failed_charge``)
    redeliver forever against a still-dead card (see ``ChargeDeclinedError``'s
    own docstring).
    """
    if isinstance(
        exc,
        OverLimitError
        | PaymentProviderNotConfiguredError
        | PaymentTokenRequiredError
        | AddOnNotPurchasableError
        | UnconfirmedPlanChangeError
        | RetryPaymentNotApplicableError
        | SubscriptionNotAttachedError
        | NoOutstandingBalanceError
        | CollectionNotSupportedError
        | ChargeDeclinedError,
    ):
        # Mandatory before returning a Response: swallowing the exception here
        # would otherwise commit the ATOMIC_REQUESTS transaction, persisting
        # whatever a guarded service wrote before it hit the guard (an
        # invitation row, a membership reactivation, audit entries, a
        # `SubscriptionAddOn`, a `Subscription.plan` move, a `Refund` and its
        # status update) while the client is told the request did not succeed.
        # Applied uniformly across every branch here rather than reasoned about
        # per call site -- see this function's own docstring for why each of
        # these ten classes renders at the status it does.
        set_rollback()
        return Response(exc.as_error_body(), status=billing_error_status(exc))
    return drf_exception_handler(exc, context)
