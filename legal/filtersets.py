import django_filters

from legal.models import PolicyDocument, PolicyDocumentType


class PolicyDocumentFilterSet(django_filters.FilterSet):
    """Optional ``document_type`` filter for the policy-document history endpoint."""

    document_type = django_filters.ChoiceFilter(
        field_name="document_type",
        choices=PolicyDocumentType.choices,
        label="Filter by document type",
    )

    class Meta:
        model = PolicyDocument
        fields = ("document_type",)
