class PaymentError(ValueError):
    pass


class PaymentAdapterError(PaymentError):
    pass


class PaymentExternalIdMissingInNotificationError(PaymentAdapterError):
    pass


class SubscriptionExternalIdMissingInNotificationError(PaymentAdapterError):
    pass
