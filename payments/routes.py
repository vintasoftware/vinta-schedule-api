"""Where this project mounts the billing engine's endpoints.

Still the host's module -- Phase 2 deletes it and has
``vinta_schedule_api/urls.py`` call ``vinta_billing.routing`` directly. Until
then the route table stays here, with the viewsets now coming from the package
through ``payments/views.py`` / ``payments/billing_views.py``.

``PaymentsViewSet`` is deliberately absent from the router table below.
``vinta-django-billing`` 0.4.0 took both provider webhooks out of the router:
each carries the provider slug as a URL segment, and a DRF ``@action`` can
spell that segment only one way -- as a regex (which this project's
``DefaultRouter(use_regex_path=False)`` would emit literally, producing a route
that can never match) or as a path converter (which the default regex router
would emit literally instead). They are bound below with ``re_path`` in
``extra_patterns`` instead, at ``billing/payments/{pk}/...``. The reverse
names -- ``Payments-payment-update`` and
``Payments-subscription-payment-update``, which both MercadoPago adapters build
their ``notification_url`` from -- are unchanged.
"""

from django.urls import path, re_path

from common.types import RouteDict

from .billing_views import (
    AddOnViewSet,
    BillingPeriodViewSet,
    BillingPlanViewSet,
    BillingUsageViewSet,
    MeteredOccurrenceViewSet,
    SubscriptionViewSet,
)
from .views import (
    BillingProfileViewSet,
    DefaultPaymentProviderView,
    PaymentProviderViewSet,
    PaymentsViewSet,
)


routes: list[RouteDict] = [
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
        "regex": r"billing/usage/periods",
        "viewset": BillingPeriodViewSet,
        "basename": "BillingUsagePeriod",
    },
    {
        "regex": r"billing/usage/occurrences",
        "viewset": MeteredOccurrenceViewSet,
        "basename": "BillingUsageOccurrence",
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

# Non-viewset routes -- URL patterns registered directly with the Django URL
# conf, bypassing the shared router. The two payment-provider endpoints are
# singletons (no list, no primary key, so a router prefix would imply a
# collection this has not got); the two webhooks are here for the reason in the
# module docstring.
#
# Spelled out rather than taken from `vinta_billing.routing.get_extra_patterns()`,
# which builds the same four paths but binds the *package's* view classes --
# losing this project's DI wiring and, on the payment-provider endpoint, its
# `X-Organization-Id` scoping. The regexes below are byte-identical to that
# function's, and `payments/tests/views/test_payment_provider_views.py` plus
# `payments/tests/views/test_payment_webhooks.py` exercise all four.
#
# `re_path` for the webhooks, not `path`: each carries the provider slug as a
# URL segment, which a router built with `use_regex_path=False` (this project's)
# cannot render from a DRF `@action`'s `url_path`.
extra_patterns = [
    re_path(
        r"^billing/payments/(?P<pk>[^/.]+)/payment-update/(?P<provider>[^/.]+)/$",
        PaymentsViewSet.as_view({"post": "payment_update"}, detail=True, basename="Payments"),
        name="Payments-payment-update",
    ),
    re_path(
        r"^billing/payments/(?P<pk>[^/.]+)/subscription-payment-update/(?P<provider>[^/.]+)/$",
        PaymentsViewSet.as_view(
            {"post": "subscription_payment_update"}, detail=True, basename="Payments"
        ),
        name="Payments-subscription-payment-update",
    ),
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
