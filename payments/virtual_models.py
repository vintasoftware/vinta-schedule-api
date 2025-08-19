import django_virtual_models as v

from payments.models import BillingAddress, BillingProfile, Subscription


class SubscriptionVirtualModel(v.VirtualModel):
    """
    Virtual model for Subscription.
    """

    class Meta:
        model = Subscription


class BillingAddressVirtualModel(v.VirtualModel):
    """
    Virtual model for BillingAddress.
    """

    class Meta:
        model = BillingAddress


class BillingProfileVirtualModel(v.VirtualModel):
    """
    Virtual model for BillingProfile.
    """

    billing_address = BillingAddressVirtualModel()

    class Meta:
        model = BillingProfile
