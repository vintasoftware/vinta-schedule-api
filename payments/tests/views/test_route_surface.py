"""Every billing route still resolves at the path it resolved at before this
phase, now that ``vinta_schedule_api/urls.py`` mounts
``vinta_billing.routing.get_routes()`` / ``get_extra_patterns()`` directly
instead of the host's own ``payments/routes.py`` (deleted this phase).

Pinned as literal paths, not just "reverse() does not raise": a route that
silently moved (a different URL for the same name) would pass a bare
``reverse()`` smoke test and still break every client and every provider
integration pointed at the old path. The two provider webhooks matter most
here -- both MercadoPago adapters build their ``notification_url`` by
reversing ``Payments-payment-update`` / ``Payments-subscription-payment-update``,
so a silently-moved webhook is the failure this migration has been most
careful about (see the plan's Risk & Rollout Notes).

Every path below is asserted against the literal this project served before
Phase 2 -- copied from ``payments/routes.py`` (git history) and cross-checked
against the regenerated ``schema.yml``, whose diff against the pre-Phase-2
commit is empty.
"""

from django.urls import reverse

import pytest


pytestmark = pytest.mark.django_db


class TestRouterMountedRoutes:
    """The seven viewsets ``vinta_billing.routing.get_routes()`` returns,
    mounted on this project's router under the ``api:`` namespace -- exactly
    where ``payments/routes.py`` mounted its own copies of these classes."""

    @pytest.mark.parametrize(
        ("url_name", "kwargs", "expected_path"),
        [
            ("api:BillingProfile-create", {}, "/billing-profile/create_billing_profile/"),
            ("api:BillingProfile-retrieve", {}, "/billing-profile/retrieve_billing_profile/"),
            ("api:BillingProfile-update", {}, "/billing-profile/update_billing_profile/"),
            (
                "api:BillingProfile-partial_update",
                {},
                "/billing-profile/partial_update_billing_profile/",
            ),
            ("api:BillingPlan-list", {}, "/billing/plans/"),
            ("api:BillingUsage-retrieve", {}, "/billing/usage/retrieve_usage/"),
            ("api:BillingUsagePeriod-list", {}, "/billing/usage/periods/"),
            ("api:BillingUsagePeriod-detail", {"pk": 7}, "/billing/usage/periods/7/"),
            ("api:BillingUsageOccurrence-list", {}, "/billing/usage/occurrences/"),
            (
                "api:BillingSubscription-retrieve",
                {},
                "/billing/subscription/retrieve_subscription/",
            ),
            ("api:BillingSubscription-change-plan", {}, "/billing/subscription/change-plan/"),
            ("api:BillingSubscription-cancel", {}, "/billing/subscription/cancel/"),
            ("api:BillingSubscription-retry-payment", {}, "/billing/subscription/retry-payment/"),
            ("api:BillingAddOn-list", {}, "/billing/add-ons/"),
            ("api:BillingAddOn-detail", {"pk": 7}, "/billing/add-ons/7/"),
        ],
    )
    def test_the_route_resolves_at_its_pre_phase_2_path(self, url_name, kwargs, expected_path):
        assert reverse(url_name, kwargs=kwargs) == expected_path


class TestExtraPatternRoutes:
    """The four routes ``vinta_billing.routing.get_extra_patterns()`` binds
    directly (not through the router), unnamespaced -- exactly where
    ``payments/routes.py``'s own ``extra_patterns`` bound its copies."""

    def test_the_two_billing_payment_provider_patterns_resolve_unchanged(self):
        assert reverse("payment-provider") == "/billing/payment-provider/"
        assert reverse("payment-provider-default") == "/billing/payment-provider/default/"

    def test_the_two_provider_webhooks_resolve_under_their_unchanged_names(self):
        """The reverse names a real provider integration depends on.

        Both shipped MercadoPago adapters build their ``notification_url`` by
        reversing exactly these two names -- see
        ``payments/services/subscription_adapters/mercadopago_subscription_adapter.py``.
        A rename here breaks every future webhook registration silently: the
        adapter would build a URL for a route that does not exist under that
        name, and nothing would fail until the provider tried to call back.
        """
        assert (
            reverse("Payments-payment-update", kwargs={"pk": 7, "provider": "stripe"})
            == "/billing/payments/7/payment-update/stripe/"
        )
        assert (
            reverse("Payments-subscription-payment-update", kwargs={"pk": 7, "provider": "stripe"})
            == "/billing/payments/7/subscription-payment-update/stripe/"
        )

    def test_the_two_webhooks_moved_out_of_the_router_survive_that_move(self):
        """Proves what the reverse names alone cannot: 0.4.0 took
        ``PaymentsViewSet`` out of ``get_routes()`` entirely (see
        ``vinta_billing.routing.get_routes``'s own docstring) because a DRF
        ``@action``'s ``url_path`` cannot spell the provider-slug segment as
        both a regex and a path converter. Mounting only ``get_routes()`` --
        the one-line omission this project's ``urls.py`` deliberately avoids
        -- would silently drop both webhooks while every other billing route
        kept working. This is the regression that mistake would produce.
        """
        from vinta_schedule_api.urls import payments_routes

        router_basenames = {route["basename"] for route in payments_routes}
        assert "Payments" not in router_basenames

        # And yet the name still resolves -- proving it came from
        # `get_extra_patterns()`, not from a basename this project forgot to
        # drop from the table above.
        assert reverse("Payments-payment-update", kwargs={"pk": 7, "provider": "stripe"})
