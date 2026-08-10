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

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback

from payments.exceptions import (
    AddOnNotPurchasableError,
    OverLimitError,
    PaymentProviderNotConfiguredError,
    PaymentTokenRequiredError,
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

    ``PaymentProviderNotConfiguredError`` is rendered as **HTTP 409 Conflict**,
    the status the payment-provider-selection plan's **Guiding Decisions** commits
    to for "the provider this organization resolves to cannot be driven by this
    deployment". Handled centrally rather than per-action because Phase 4 makes it
    reachable from every billing write that touches a provider
    (``SubscriptionViewSet.change_plan`` / ``cancel``, ``AddOnViewSet.create``,
    and anything added later), and a per-action ``except`` would have to be
    remembered at each new one — the failure mode of forgetting is a 500 on a
    money path. ``payments.views``'s two provider-credentials endpoints catch it
    themselves first (409 on the org endpoint, 503 on the unauthenticated default
    one, which is a deployment error rather than a request conflict), so they are
    unaffected by this branch.

    ``PaymentTokenRequiredError`` / ``AddOnNotPurchasableError`` (400) and
    ``UnconfirmedPlanChangeError`` (409) are the billing API contract hardening
    plan's Phase 1: every ``BillingError`` subclass now carries a stable ``code``
    (see ``payments.exceptions.BillingError``), and these three render it through
    the shared ``as_error_body()`` contract instead of each view improvising its
    own ad hoc body (a field-keyed ``ValidationError`` for the first two, a plain
    ``{"detail": ...}`` 409 for the third). Handled centrally for the same reason
    as ``PaymentProviderNotConfiguredError`` above — a per-action ``except`` is
    something every new billing write has to remember, the handler is something
    it inherits for free.
    """
    if isinstance(exc, OverLimitError):
        # Mandatory before returning a Response: swallowing the exception here
        # would otherwise commit the ATOMIC_REQUESTS transaction, persisting
        # whatever a guarded service wrote before it hit the limit check (an
        # invitation row, a membership reactivation, audit entries) while the
        # client is told 402.
        set_rollback()
        return Response(exc.as_error_body(), status=status.HTTP_402_PAYMENT_REQUIRED)
    if isinstance(exc, PaymentProviderNotConfiguredError):
        # Same mandatory rollback as above, and it matters more here: the charge
        # paths that raise this write local rows (a `Refund` + its status update,
        # a `SubscriptionAddOn`, a `Subscription.plan` move) before or around the
        # provider call, and committing those while telling the client 409 would
        # leave capacity or a refund recorded that no provider ever saw.
        set_rollback()
        return Response(exc.as_error_body(), status=status.HTTP_409_CONFLICT)
    if isinstance(exc, PaymentTokenRequiredError | AddOnNotPurchasableError):
        # Same mandatory rollback discipline as the branches above.
        set_rollback()
        return Response(exc.as_error_body(), status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, UnconfirmedPlanChangeError):
        # A different plan change is already in flight and unconfirmed -- 409
        # Conflict rather than a validation error on any one field.
        set_rollback()
        return Response(exc.as_error_body(), status=status.HTTP_409_CONFLICT)
    return drf_exception_handler(exc, context)
