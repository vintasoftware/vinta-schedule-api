import django_virtual_models as v

from payments.models import (
    BillingAddress,
    BillingPlan,
    BillingProfile,
    PlanEntitlement,
    PlanLimit,
    Subscription,
    SubscriptionAddOn,
)


class PlanLimitVirtualModel(v.VirtualModel):
    class Meta:
        model = PlanLimit


class PlanEntitlementVirtualModel(v.VirtualModel):
    class Meta:
        model = PlanEntitlement


class BillingPlanVirtualModel(v.VirtualModel):
    """
    Virtual model for BillingPlan, prefetching its ``PlanLimit``/``PlanEntitlement``
    rows -- ``BillingPlanSerializer`` renders both nested on every plan.
    """

    limits = PlanLimitVirtualModel(many=True)
    entitlements = PlanEntitlementVirtualModel(many=True)

    class Meta:
        model = BillingPlan


class SubscriptionAddOnVirtualModel(v.VirtualModel):
    class Meta:
        model = SubscriptionAddOn


class SubscriptionVirtualModel(v.VirtualModel):
    """
    Virtual model for Subscription.
    """

    plan = BillingPlanVirtualModel()
    pending_plan = BillingPlanVirtualModel()
    add_ons = SubscriptionAddOnVirtualModel(many=True)

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
