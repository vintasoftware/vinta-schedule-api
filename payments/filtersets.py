from django_filters import rest_framework as filters

from payments.models import BillingPlan, SubscriptionAddOn


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
