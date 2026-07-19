class PaymentError(ValueError):
    pass


class PaymentAdapterError(PaymentError):
    pass


class PaymentExternalIdMissingInNotificationError(PaymentAdapterError):
    pass


class SubscriptionExternalIdMissingInNotificationError(PaymentAdapterError):
    pass


class ProviderWebhookEventIdMissingError(PaymentAdapterError):
    """The webhook payload has no id usable as the idempotency ledger key.

    Raised by ``BasePaymentAdapter.get_event_id`` / ``BaseSubscriptionAdapter.get_event_id``
    when the provider's own notification id (normally ``get_update_id``'s return value) is
    absent. Without a stable id, delivery cannot be deduplicated safely.
    """

    def __init__(self, message="Webhook payload is missing the notification id"):
        super().__init__(message)


class UnknownPaymentProviderError(PaymentError):
    """Raised when a ``provider`` slug doesn't match any registered adapter.

    Surfaces as a 404 at the webhook views — an unregistered provider slug in the
    URL is a routing/configuration error, not an authentication failure.
    """

    def __init__(self, provider: str):
        super().__init__(f"Unknown payment provider: {provider!r}")
        self.provider = provider


class MissingBillingProfileError(PaymentError):
    def __init__(self, message="User does not have a billing profile"):
        super().__init__(message)


class BillingProfileContactEmailMissingError(PaymentError):
    def __init__(
        self,
        message="BillingProfile.contact_email is required to send the payer identity "
        "to the payment gateway",
    ):
        super().__init__(message)
