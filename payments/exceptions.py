class PaymentError(ValueError):
    pass


class PaymentAdapterError(PaymentError):
    pass


class PaymentExternalIdMissingInNotificationError(PaymentAdapterError):
    pass


class SubscriptionExternalIdMissingInNotificationError(PaymentAdapterError):
    pass


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
