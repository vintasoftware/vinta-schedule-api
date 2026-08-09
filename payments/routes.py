from django.urls import path

from common.types import RouteDict

from .billing_views import (
    AddOnViewSet,
    BillingPlanViewSet,
    BillingUsageViewSet,
    SubscriptionViewSet,
)
from .views import (
    BillingProfileViewSet,
    DefaultPaymentProviderView,
    PaymentProviderViewSet,
    PaymentsViewSet,
)


routes: list[RouteDict] = [
    {"regex": r"payments", "viewset": PaymentsViewSet, "basename": "Payments"},
    {
        "regex": r"billing-profile",
        "viewset": BillingProfileViewSet,
        "basename": "BillingProfile",
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

# Non-viewset routes (APIViews / manually-bound ViewSets) — URL patterns registered
# directly with the Django URL conf, bypassing the shared router. See
# `PaymentProviderViewSet`'s docstring for why the org payment-provider endpoint is
# bound this way instead of through `router.register(...)`.
extra_patterns = [
    path(
        "billing/payment-provider/",
        PaymentProviderViewSet.as_view({"get": "retrieve_provider"}),
        name="payment-provider",
    ),
    path(
        "billing/payment-provider/default/",
        DefaultPaymentProviderView.as_view(),
        name="payment-provider-default",
    ),
]
