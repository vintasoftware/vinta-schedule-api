from django_filters import rest_framework as filters

from organizations.models import OrganizationInvitation


class OrganizationInvitationFilterSet(filters.FilterSet):
    """
    FilterSet for OrganizationInvitation model.
    """

    email = filters.CharFilter(
        field_name="email",
        lookup_expr="icontains",
        label="Filter by partial email match",
    )
    is_accepted = filters.BooleanFilter(
        field_name="accepted_at",
        lookup_expr="isnull",
        exclude=True,
        label="Filter by acceptance status",
    )
    is_expired = filters.BooleanFilter(
        method="filter_is_expired",
        label="Filter by expiration status",
    )
    invited_by = filters.NumberFilter(
        field_name="invited_by_id",
        label="Filter by inviter user ID",
    )

    class Meta:
        model = OrganizationInvitation
        fields = (
            "email",
            "is_accepted",
            "is_expired",
            "invited_by",
        )

    def filter_is_expired(self, queryset, name, value):
        """Filter by whether the invitation has expired."""
        from django.utils import timezone

        now = timezone.now()
        if value:
            # Show only expired invitations
            return queryset.filter(expires_at__lt=now)
        else:
            # Show only non-expired invitations
            return queryset.filter(expires_at__gte=now)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
