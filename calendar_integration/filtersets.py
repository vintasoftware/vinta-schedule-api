from django_filters import rest_framework as filters
from rest_framework.exceptions import ValidationError

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ExternalEventChangeRequestStatus,
)
from calendar_integration.external_client_identifiers import normalize_system
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    ExternalEventChangeRequest,
)


class CalendarFilterSet(filters.FilterSet):
    """FilterSet for listing calendars.

    Lets org admins narrow the calendar list to resource calendars, by provider
    (manual ``internal`` vs synced ``google``/``microsoft``/...), and by sync state.
    """

    calendar_type = filters.ChoiceFilter(
        field_name="calendar_type",
        choices=CalendarType.choices,
        label="Filter by calendar type (e.g. resource)",
    )
    provider = filters.ChoiceFilter(
        field_name="provider",
        choices=CalendarProvider.choices,
        label="Filter by provider (internal = manual, others = synced)",
    )
    sync_enabled = filters.BooleanFilter(
        field_name="sync_enabled",
        label="Filter by whether provider sync is enabled",
    )

    class Meta:
        model = Calendar
        fields = (
            "calendar_type",
            "provider",
            "sync_enabled",
        )


class CalendarEventFilterSet(filters.FilterSet):
    """
    FilterSet for CalendarEvent model.
    """

    start_time = filters.DateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Start time (greater than or equal to)",
    )
    end_time = filters.DateTimeFilter(
        field_name="end_time",
        lookup_expr="lte",
        label="End time (less than or equal to)",
    )
    start_time_range = filters.DateTimeFromToRangeFilter(
        field_name="start_time",
        label="Start time range",
    )
    end_time_range = filters.DateTimeFromToRangeFilter(
        field_name="end_time",
        label="End time range",
    )
    title = filters.CharFilter(
        field_name="title",
        lookup_expr="icontains",
        label="Filter by partial title match",
    )
    calendar = filters.NumberFilter(
        field_name="calendar_fk_id",
        label="Filter by calendar ID",
    )
    external_client_identifier_system = filters.CharFilter(
        method="filter_external_client_identifier",
        label=(
            "Filter by client identifier system. Must be supplied together with "
            "external_client_identifier_identifier."
        ),
    )
    external_client_identifier_identifier = filters.CharFilter(
        method="filter_external_client_identifier",
        label=(
            "Filter by client identifier value. Must be supplied together with "
            "external_client_identifier_system."
        ),
    )

    class Meta:
        model = CalendarEvent
        fields = (
            "start_time",
            "end_time",
            "start_time_range",
            "end_time_range",
            "title",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        membership = self.request.organization_membership if self.request else None
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )

    def filter_external_client_identifier(self, queryset, name, value):
        """Both-or-neither: supplying one of the pair alone is a 400.

        django-filter's method-filter wrapper only calls this when the
        triggering field's own value is non-empty, so it fires once (if only
        one of the pair is supplied -- the error case) or twice (if both are
        supplied). The actual join is applied only once, on the ``system``
        field's call, to avoid filtering twice.

        Keeps the query on the ``extclientid_uniq_system_ident`` index prefix
        (leading column ``organization``) -- an identifier-only filter would not
        use it. ``system`` is normalized before matching.
        """
        system = self.data.get("external_client_identifier_system")
        identifier = self.data.get("external_client_identifier_identifier")
        if not system or not identifier:
            raise ValidationError(
                {
                    "external_client_identifier_system": (
                        "external_client_identifier_system and "
                        "external_client_identifier_identifier must be supplied together."
                    )
                }
            )

        if name != "external_client_identifier_system":
            return queryset

        membership = self.request.organization_membership if self.request else None
        if membership is None:
            return queryset.none()

        normalized_system = normalize_system(system)
        return queryset.filter(
            external_client_identifiers__system=normalized_system,
            external_client_identifiers__identifier=identifier,
            external_client_identifiers__organization=membership.organization_id,
        )


class BlockedTimeFilterSet(filters.FilterSet):
    start_time = filters.DateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Start time (greater than or equal to)",
    )
    end_time = filters.DateTimeFilter(
        field_name="end_time",
        lookup_expr="lte",
        label="End time (less than or equal to)",
    )
    reason = filters.CharFilter(
        field_name="title",
        lookup_expr="icontains",
        label="Filter by partial title match",
    )

    class Meta:
        model = BlockedTime
        fields = (
            "start_time",
            "end_time",
            "reason",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        membership = self.request.organization_membership if self.request else None
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class CalendarGroupFilterSet(filters.FilterSet):
    """FilterSet for CalendarGroup."""

    name = filters.CharFilter(
        field_name="name",
        lookup_expr="icontains",
        label="Filter by partial name match",
    )

    class Meta:
        model = CalendarGroup
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        membership = self.request.organization_membership if self.request else None
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="slots__memberships__calendar_fk_id",
            label="Filter to groups whose slot pools include this calendar",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class AvailableTimeFilterSet(filters.FilterSet):
    start_time = filters.DateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Start time (greater than or equal to)",
    )
    end_time = filters.DateTimeFilter(
        field_name="end_time",
        lookup_expr="lte",
        label="End time (less than or equal to)",
    )

    class Meta:
        model = AvailableTime
        fields = (
            "start_time",
            "end_time",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        membership = self.request.organization_membership if self.request else None
        self.filters["calendar"] = filters.ModelChoiceFilter(
            field_name="calendar_fk_id",
            label="Filter by calendar ID",
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class ExternalEventChangeRequestFilterSet(filters.FilterSet):
    """FilterSet for ``ExternalEventChangeRequest``.

    Lets callers narrow the eligibility-scoped list by status and/or event.
    Defaults to ``PENDING`` when no ``status`` filter is provided; the viewset
    applies the default via ``get_queryset()`` before the filterset runs.
    """

    status = filters.ChoiceFilter(
        field_name="status",
        choices=ExternalEventChangeRequestStatus.choices,
        label="Filter by request status (default: pending)",
    )
    event = filters.NumberFilter(
        field_name="event_fk_id",
        label="Filter by CalendarEvent ID",
    )

    class Meta:
        model = ExternalEventChangeRequest
        fields = ("status", "event")
