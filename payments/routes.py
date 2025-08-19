from common.types import RouteDict

from .views import BillingProfileViewSet, PaymentsViewSet


routes: list[RouteDict] = [
    {"regex": r"payments", "viewset": PaymentsViewSet, "basename": "Payments"},
    {
        "regex": r"billing-profile",
        "viewset": BillingProfileViewSet,
        "basename": "BillingProfile",
    },
]
