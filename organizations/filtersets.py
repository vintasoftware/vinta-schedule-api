from django_filters import rest_framework as filters

from organizations.models import OrganizationInvitation, OrganizationMembership


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


class OrganizationMembershipFilterSet(filters.FilterSet):
    first_name = filters.CharFilter(
        field_name="user__profile__first_name",
        lookup_expr="icontains",
        label="Filter by partial first name match",
    )
    last_name = filters.CharFilter(
        field_name="user__profile__last_name",
        lookup_expr="icontains",
        label="Filter by partial last name match",
    )
    email = filters.CharFilter(
        field_name="user__email",
        lookup_expr="icontains",
        label="Filter by partial email match",
    )
    search = filters.CharFilter(
        method="filter_search",
        label="Search by first name, last name, or email (OR)",
    )

    class Meta:
        model = OrganizationMembership
        fields = ("first_name", "last_name", "email", "search")

    def filter_search(self, queryset, name, value):
        from django.db.models import Q

        return queryset.filter(
            Q(user__profile__first_name__icontains=value)
            | Q(user__profile__last_name__icontains=value)
            | Q(user__email__icontains=value)
        )
