"""``common.exception_handlers.vinta_exception_handler`` -- status code and body
per ``BillingError`` subclass, now that both the exceptions and the status
table it (mostly) delegates to come from ``vinta_billing``.

Called directly rather than through a real view/request: the transactional half
(``set_rollback()`` actually preventing an ``ATOMIC_REQUESTS`` commit) is
already covered end-to-end by ``payments/tests/test_over_limit_rollback.py``,
which exists specifically because a direct-call test cannot observe that. This
file only pins "for this exception instance, the handler returns this status
and this body" -- the contract every DRF view built on the ten billing errors
below relies on.
"""

from rest_framework import status
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

from common.exception_handlers import vinta_exception_handler


class TestVintaExceptionHandlerStatusCodes:
    """One test per class this handler renders, each pinning today's status
    code -- not re-derived from ``billing_error_status`` or any other shared
    table, so a change to that table that silently changes a status here
    turns this test red rather than passing vacuously.
    """

    def test_over_limit_error_is_402(self):
        exc = OverLimitError(
            resource_key="organization_members",
            current_usage=1,
            limit=1,
            remedy="purchase_add_on",
        )

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.data == {
            "detail": "Organization is at its limit for organization members.",
            "code": "limit_exceeded",
            "resource": "organization_members",
            "current_usage": 1,
            "limit": 1,
            "remedy": "purchase_add_on",
        }

    def test_payment_provider_not_configured_error_is_503(self):
        """A hardcoded 409 here until ``vinta-django-billing`` 0.6.0, overriding
        the package's table. 0.6.0 settled the package's own 409/503
        contradiction in favour of 503, which removed the reason for the
        override -- see the handler's docstring. The literal below is still
        pinned by hand, not read from ``billing_error_status``, so the next
        table change that moves this status has to come here and say so.
        """
        exc = PaymentProviderNotConfiguredError(provider="stripe")

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.data == {
            "code": "payment_provider_not_configured",
            "detail": "Payment provider 'stripe' is not configured in this deployment",
        }

    def test_payment_token_required_error_is_400(self):
        exc = PaymentTokenRequiredError(organization_id=42)

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "payment_token_required"

    def test_add_on_not_purchasable_error_is_400(self):
        exc = AddOnNotPurchasableError(resource_key="resource_calendars")

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "add_on_not_purchasable"

    def test_unconfirmed_plan_change_error_is_409(self):
        exc = UnconfirmedPlanChangeError(organization_id=42)

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "unconfirmed_plan_change"

    def test_retry_payment_not_applicable_error_is_409(self):
        exc = RetryPaymentNotApplicableError(organization_id=42)

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "retry_payment_not_applicable"

    def test_subscription_not_attached_error_is_409(self):
        exc = SubscriptionNotAttachedError(organization_id=42)

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "subscription_not_attached"

    def test_no_outstanding_balance_error_is_409(self):
        exc = NoOutstandingBalanceError(subscription_id=42)

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "no_outstanding_balance"

    def test_collection_not_supported_error_is_409(self):
        exc = CollectionNotSupportedError(subscription_id=42, message="not supported")

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "collection_not_supported"

    def test_charge_declined_error_is_402(self):
        exc = ChargeDeclinedError(subscription_id=42, provider_message="card declined")

        response = vinta_exception_handler(exc, {})

        assert response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        assert response.data["code"] == "charge_declined"


class TestVintaExceptionHandlerFallsThroughForEverythingElse:
    def test_a_plain_exception_is_not_handled(self):
        """Anything that is not one of the ten classes above falls through to
        DRF's own handler, which returns ``None`` for an exception it does not
        recognise either (no request/view in ``context`` here).
        """
        assert vinta_exception_handler(ValueError("boom"), {}) is None
