from django_filters import rest_framework as filters
from rest_framework.exceptions import ValidationError

from payments.models import BillingPeriodSummary, BillingPlan, MeteredOccurrence, SubscriptionAddOn


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


class MeteredOccurrenceFilterSet(filters.FilterSet):
    """FilterSet for ``GET /billing/usage/occurrences/`` -- the line-item ledger.

    ``billing_period_start`` has no default here: when it is omitted the view
    (``MeteredOccurrenceViewSet.get_queryset``) supplies the current, open
    period instead. This class only narrows further once a queryset already
    exists.
    """

    billing_period_start = filters.IsoDateTimeFilter(
        field_name="billing_period_start",
        label="Only rows billed to this exact period",
        help_text=(
            "Only rows billed to this exact period. Defaults to the current, open "
            "billing period when omitted."
        ),
    )
    is_within_allowance = filters.BooleanFilter(
        field_name="is_within_allowance",
        label="Filter by whether the occurrence fell inside the included allowance",
    )
    organization = filters.NumberFilter(
        method="filter_organization",
        label="Only rows attributed to this organization",
        help_text=(
            "Only rows attributed to this organization. Must be inside the caller's "
            "pooled billing subtree -- an id outside it is a validation error, not "
            "an empty result."
        ),
    )
    occurrence_start_after = filters.IsoDateTimeFilter(
        field_name="occurrence_start",
        lookup_expr="gte",
        label="Only occurrences starting on or after this instant",
        help_text="Only occurrences starting on or after this instant (inclusive).",
    )
    occurrence_start_before = filters.IsoDateTimeFilter(
        field_name="occurrence_start",
        lookup_expr="lte",
        label="Only occurrences starting on or before this instant",
        help_text="Only occurrences starting on or before this instant (inclusive).",
    )
    ordering = filters.OrderingFilter(
        fields=(("occurrence_start", "occurrence_start"),),
        label="Ordering",
        help_text=(
            "Order by occurrence_start. Prefix with '-' for descending. Defaults "
            "to -occurrence_start (newest first)."
        ),
    )

    class Meta:
        model = MeteredOccurrence
        fields = (
            "billing_period_start",
            "is_within_allowance",
            "organization",
            "occurrence_start_after",
            "occurrence_start_before",
        )

    def filter_organization(self, queryset, name, value):
        """Restrict to one organization -- but only one **inside the caller's
        pool**. ``MeteredOccurrenceViewSet.get_queryset`` stashes the resolved
        pool on the request so this doesn't need its own DI/service access.

        An id outside the pool is a validation error (400), never a silent
        empty ``200`` -- see the plan's Guiding Decisions: a quietly-empty
        result for an organization the caller cannot see would read as "you
        used nothing" for a question the caller was never allowed to ask.
        """
        pooled_organization_ids = getattr(self.request, "pooled_organization_ids", ())
        if value not in pooled_organization_ids:
            raise ValidationError(
                {"organization": (f"Organization {value} is not within your billing pool.")}
            )
        return queryset.filter(organization_id=value)
