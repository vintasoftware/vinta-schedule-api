from django_filters import rest_framework as filters

from payments.models import BillingPeriodSummary, BillingPlan, SubscriptionAddOn


class BillingPlanFilterSet(filters.FilterSet):
    """FilterSet for ``GET /billing/plans/``."""

    is_active = filters.BooleanFilter(field_name="is_active")
    currency = filters.CharFilter(field_name="currency", lookup_expr="iexact")

    class Meta:
        model = BillingPlan
        fields = ("is_active", "currency")


class SubscriptionAddOnFilterSet(filters.FilterSet):
    """FilterSet for the add-ons list backing ``POST``/``DELETE
    /billing/add-ons/``'s underlying queryset."""

    resource_key = filters.CharFilter(field_name="resource_key")
    is_active = filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = SubscriptionAddOn
        fields = ("resource_key", "is_active")


class BillingPeriodSummaryFilterSet(filters.FilterSet):
    """FilterSet for ``GET /billing/usage/periods/`` -- the closed-period
    statement list."""

    billing_period_start_after = filters.DateTimeFilter(
        field_name="billing_period_start",
        lookup_expr="gte",
        label="Only periods starting on or after this instant",
        # drf-spectacular prefers `help_text` over `label` when building the
        # OpenAPI parameter `description` (see
        # `DjangoFilterExtension._get_field_description`) -- set explicitly so
        # the inclusive (`gte`) bound reaches the generated client docs rather
        # than depending on that fallback to the browsable-API `label`.
        help_text="Only periods starting on or after this instant (inclusive).",
    )
    billing_period_start_before = filters.DateTimeFilter(
        field_name="billing_period_start",
        lookup_expr="lte",
        label="Only periods starting on or before this instant",
        help_text="Only periods starting on or before this instant (inclusive).",
    )
    charged = filters.BooleanFilter(
        field_name="charged", label="Filter by whether the period's overage was charged"
    )

    class Meta:
        model = BillingPeriodSummary
        fields = ("billing_period_start_after", "billing_period_start_before", "charged")
