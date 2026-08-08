from common.types import RouteDict

from .billing_views import (
    AddOnViewSet,
    BillingPlanViewSet,
    BillingUsageViewSet,
    SubscriptionViewSet,
)
from .views import BillingProfileViewSet, PaymentProviderViewSet, PaymentsViewSet


routes: list[RouteDict] = [
    {"regex": r"payments", "viewset": PaymentsViewSet, "basename": "Payments"},
    {
        "regex": r"billing-profile",
        "viewset": BillingProfileViewSet,
        "basename": "BillingProfile",
    },
    {
        "regex": r"billing/payment-provider",
        "viewset": PaymentProviderViewSet,
        "basename": "BillingPaymentProvider",
    },
    {
        "regex": r"billing/plans",
        "viewset": BillingPlanViewSet,
        "basename": "BillingPlan",
    },
    {
        "regex": r"billing/usage",
        "viewset": BillingUsageViewSet,
        "basename": "BillingUsage",
    },
    {
        "regex": r"billing/subscription",
        "viewset": SubscriptionViewSet,
        "basename": "BillingSubscription",
    },
    {
        "regex": r"billing/add-ons",
        "viewset": AddOnViewSet,
        "basename": "BillingAddOn",
    },
]
