import datetime
import logging
import zoneinfo
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, TypedDict, cast

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone

import django_virtual_models as v
from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    CalendarType,
    CalendarVisibility,
    QuotaPeriod,
)
from calendar_integration.exceptions import (
    CalendarGroupError,
    CalendarIntegrationError,
    CalendarServiceNotInjectedError,
    DuplicateBookingPolicyError,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    BookingPolicy,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
    CalendarOwnership,
    CalendarSync,
    ChildrenCalendarRelationship,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    ExternalEventChangeRequest,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
    ResourceAllocation,
)
from calendar_integration.services.dataclasses import (
    BlockedTimeData,
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    CalendarGroupSlotSelectionInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ExternalClientIdentifierData,
    ResourceAllocationInputData,
    UnavailableTimeWindow,
)
from calendar_integration.virtual_models import (
    AvailableTimeVirtualModel,
    BlockedTimeVirtualModel,
    CalendarEventGroupSelectionVirtualModel,
    CalendarEventVirtualModel,
    CalendarGroupSlotMembershipVirtualModel,
    CalendarGroupSlotVirtualModel,
    CalendarGroupVirtualModel,
    CalendarOwnershipVirtualModel,
    CalendarVirtualModel,
    EventAttendanceVirtualModel,
    EventExternalAttendanceVirtualModel,
    EventRecurrenceExceptionVirtualModel,
    ExternalAttendeeVirtualModel,
    ExternalEventChangeRequestVirtualModel,
    GroupScopedAvailabilityWindowVirtualModel,
    GroupScopedBlockedTimeVirtualModel,
    GroupScopedQuotaRuleVirtualModel,
    RecurrenceRuleVirtualModel,
    ResourceAllocationVirtualModel,
)
from common.utils.serializer_utils import VirtualModelSerializer
from organizations.models import (
    Organization,
    OrganizationMembership,
)
from users.models import User


if TYPE_CHECKING:
    from calendar_integration.services.calendar_group_service import CalendarGroupService
    from calendar_integration.services.calendar_service import CalendarService


logger = logging.getLogger(__name__)

# Window a calendar activated through PATCH is first synced over. Matches the
# window `CalendarSyncService.import_account_calendars` uses for the calendars it
# imports live, so an activated calendar ends up holding the same stretch of
# events an imported one does.
ACTIVATION_SYNC_LOOKAHEAD = datetime.timedelta(days=365)


def _localize_times_in_representation(
    data: dict,
    instance,
    tz_name: str | None,
    fields: tuple[str, ...] = ("start_time", "end_time"),
) -> dict:
    """Re-render datetime fields in ``tz_name`` instead of UTC, in place.

    start_time/end_time are stored/computed as UTC-aware instants; Django returns
    them UTC, so DRF would emit e.g. 12:00:00Z for 09:00 America/Recife. Re-express
    them in the record's IANA timezone so responses carry the local wall-clock. No-op
    when the timezone is missing or unknown.
    """
    if not tz_name or not isinstance(tz_name, str):
        return data
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return data
    for field in fields:
        value = getattr(instance, field, None)
        if value is not None:
            data[field] = value.astimezone(tz).isoformat()
    return data


class OwnershipMembershipSerializer(serializers.Serializer):
    """Membership identity for a calendar owner.

    A membership has no scalar id (it is identified by the ``(user_id,
    organization_id)`` pair), so the representation exposes that pair.

    It used to carry the membership's ``role`` as well. That left with the rest
    of ``role``'s API surface: authorization is answered from permissions, and
    this identity representation is not the place to publish a member's
    capabilities -- ``GET /organization-members/`` is. Consumers that read
    ``role`` here to decide what to render should read ``permissions`` there.
    """

    user_id = serializers.IntegerField(read_only=True)
    organization_id = serializers.IntegerField(read_only=True)


class CalendarOwnershipSerializer(VirtualModelSerializer):
    membership = OwnershipMembershipSerializer(read_only=True)

    class Meta:
        model = CalendarOwnership
        virtual_model = CalendarOwnershipVirtualModel
        fields = (
            "id",
            "membership",
            "calendar",
            "is_default",
            "created",
            "modified",
        )


class CalendarSyncSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalendarSync
        fields = (
            "id",
            "status",
            "start_datetime",
            "end_datetime",
            "should_update_events",
            "trigger_source",
            "error_message",
        )
        read_only_fields = (
            "id",
            "status",
            "trigger_source",
            "error_message",
        )


class CalendarSyncRequestSerializer(serializers.Serializer):
    start_datetime = serializers.DateTimeField(required=True)
    end_datetime = serializers.DateTimeField(required=True)
    should_update_events = serializers.BooleanField(required=False, default=False)


class CalendarImportRequestSerializer(serializers.Serializer):
    sync_after_import = serializers.BooleanField(required=False, default=True)


class CalendarSerializer(VirtualModelSerializer):
    class Meta:
        model = Calendar
        virtual_model = CalendarVirtualModel
        fields = (
            "id",
            "name",
            "description",
            "email",
            "external_id",
            "provider",
            "calendar_type",
            "capacity",
            "manage_available_windows",
            "visibility",
            "sync_enabled",
        )
        read_only_fields = (
            "email",
            "external_id",
            "provider",
            "calendar_type",
        )

    @inject
    def __init__(
        self,
        *args,
        # `Provide[...]` as the default rather than `None`: `@inject` supplies it on every
        # construction and both `create` and `update` dereference it unconditionally.
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        # `capacity` is only meaningful for resource calendars and is a management
        # concern, so restrict edits to org admins acting on RESOURCE calendars.
        # (Creation of resource calendars goes through ResourceCalendarCreateSerializer.)
        if "capacity" in attrs:
            calendar_type = getattr(self.instance, "calendar_type", None)
            if calendar_type != CalendarType.RESOURCE:
                raise serializers.ValidationError(
                    {"capacity": "Capacity can only be set on resource calendars."}
                )
            user = self.context["request"].user
            if not user.is_organization_admin(self.instance.organization_id):
                raise serializers.ValidationError(
                    {"capacity": "Only org admins can change a resource calendar's capacity."}
                )
        return attrs

    def update(self, instance, validated_data):
        """Apply the edit, then request a first sync when sync is switched on.

        Every imported calendar except the account's default one lands with
        ``sync_enabled=False`` and no events (see
        ``CalendarSyncService.import_account_calendars``), so flipping the flag on
        is the moment the calendar starts mattering for scheduling -- and it is
        still empty. Requesting the sync here makes one PATCH both activate and
        backfill the calendar, instead of every client having to follow up with
        ``POST /calendar/{id}/request-sync/``.
        """
        was_sync_enabled = instance.sync_enabled
        calendar = super().update(instance, validated_data)

        if not was_sync_enabled and calendar.sync_enabled:
            self._request_activation_sync(calendar)

        return calendar

    def _request_activation_sync(self, calendar: Calendar) -> None:
        """Enqueue the first sync for a calendar whose ``sync_enabled`` just flipped on.

        Best-effort by design: the activation is already committed by the time this
        runs, so a missing owner, a missing linked account, or a provider error is
        logged and left for an explicit ``request-sync`` call rather than failing
        the PATCH and rolling the user's activation back.
        """
        if calendar.provider == CalendarProvider.INTERNAL:
            # Virtual/internal calendars have no external provider to pull from.
            return

        # Sync against the *owner's* account rather than the caller's: an org admin
        # activating a member's calendar holds no token for it. Same owner
        # resolution as `CalendarViewSet.admin_sync` -- membership-backed rows only
        # (an orphan ownership resolves no account), default owner first.
        ownership = (
            CalendarOwnership.objects.filter_by_organization(calendar.organization_id)
            .filter(
                calendar=calendar,
                membership_user_id__isnull=False,
            )
            .order_by("-is_default", "id")
            .first()
        )
        if ownership is None:
            logger.info(
                "Calendar %s activated but has no membership-backed owner; skipping initial sync.",
                calendar.id,
            )
            return

        social_account = SocialAccount.objects.filter(
            user_id=ownership.membership_user_id, provider=calendar.provider
        ).first()
        if social_account is None:
            logger.info(
                "Calendar %s activated but its owner has no linked %s account; "
                "skipping initial sync.",
                calendar.id,
                calendar.provider,
            )
            return

        now = datetime.datetime.now(datetime.UTC)
        try:
            self.calendar_service.authenticate(
                account=social_account,
                organization=calendar.organization,
            )
            self.calendar_service.request_calendar_sync(
                calendar=calendar,
                start_datetime=now,
                end_datetime=now + ACTIVATION_SYNC_LOOKAHEAD,
                should_update_events=True,
                trigger_source=CalendarSyncTriggerSource.MANUAL,
            )
        except (ValueError, CalendarIntegrationError, NotImplementedError):
            logger.exception(
                "Failed to request the initial sync for activated calendar %s.", calendar.id
            )

    def create(self, validated_data):
        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )
        # Pass the acting user. The service writes the creator's `CalendarOwnership`
        # only when `user_or_token` is a `User`, and `CalendarViewSet.get_queryset`
        # filters non-admin listings on that ownership -- so a calendar created
        # without one is invisible to the member who created it.
        self.calendar_service.initialize_without_provider(
            user_or_token=self.context["request"].user,
            organization=membership.organization,
        )
        return self.calendar_service.create_virtual_calendar(
            name=validated_data.get("name"),
            description=validated_data.get("description"),
        )


class ResourceCalendarCreateSerializer(VirtualModelSerializer):
    """Create an internal (manual) resource calendar. Admin-gated at the view layer."""

    class Meta:
        model = Calendar
        virtual_model = CalendarVirtualModel
        fields = ("name", "description", "capacity", "manage_available_windows")

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

    def create(self, validated_data):
        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )
        # Pass the acting user so the creator gets a `CalendarOwnership` --
        # see `CalendarSerializer.create` above.
        self.calendar_service.initialize_without_provider(
            user_or_token=self.context["request"].user,
            organization=membership.organization,
        )
        return self.calendar_service.create_resource_calendar(
            name=validated_data.get("name"),
            description=validated_data.get("description"),
            capacity=validated_data.get("capacity"),
            manage_available_windows=validated_data.get("manage_available_windows", False),
        )


class CalendarBundleCreateSerializer(VirtualModelSerializer):
    class Meta:
        model = Calendar
        virtual_model = CalendarVirtualModel
        fields = ("name",)

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        active_membership = request.__dict__.get("organization_membership") if request else None
        self.fields["bundle_calendars"] = serializers.PrimaryKeyRelatedField(
            many=True,
            queryset=(
                Calendar.objects.filter_by_organization(
                    active_membership.organization_id
                ).exclude_inactive()
                if active_membership
                else Calendar.original_manager.none()
            ),
        )
        self.fields["primary_calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    active_membership.organization_id
                ).exclude_inactive()
                if active_membership
                else Calendar.original_manager.none()
            ),
            allow_null=True,
        )

    def validate_bundle_calendars(self, bundle_calendars):
        if len(bundle_calendars) < 2:
            raise serializers.ValidationError(
                "At least two calendars are required to create a bundle."
            )
        return bundle_calendars

    def validate(self, attrs: dict) -> dict:
        primary_calendar: Calendar | None = attrs.get("primary_calendar")
        bundle_calendars: Iterable[Calendar] = attrs.get("bundle_calendars", [])

        bundle_calendars_has_integration_calendars = any(
            calendar.provider != CalendarProvider.INTERNAL for calendar in bundle_calendars
        )

        if bundle_calendars_has_integration_calendars and (
            not primary_calendar or primary_calendar.provider == CalendarProvider.INTERNAL
        ):
            raise serializers.ValidationError(
                "Primary calendar needs to be an integration calendar if one or more calendars "
                "in the bundle are integration calendars."
            )

        return attrs

    def create(self, validated_data):
        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )
        # Pass the acting user so the creator gets a `CalendarOwnership` --
        # see `CalendarSerializer.create` above.
        self.calendar_service.initialize_without_provider(
            user_or_token=self.context["request"].user,
            organization=membership.organization,
        )

        return self.calendar_service.create_bundle_calendar(
            name=validated_data.get("name"),
            description=validated_data.get("description"),
            child_calendars=validated_data.get("bundle_calendars"),
            primary_calendar=validated_data.get("primary_calendar"),
        )


class CalendarBundleUpdateSerializer(serializers.Serializer):
    """Serializer for updating a bundle calendar's child calendars and primary calendar."""

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        active_membership = request.__dict__.get("organization_membership") if request else None

        # Build queryset: active calendars + existing children (even if disabled)
        org_id = active_membership.organization_id if active_membership else None

        if org_id:
            # The bundle being updated is passed as `instance`.
            bundle = self.instance

            # Fetch existing child IDs
            existing_child_ids = []
            if bundle:
                existing_child_ids = list(
                    ChildrenCalendarRelationship.objects.filter_by_organization(org_id)
                    .filter(
                        bundle_calendar=bundle,
                    )
                    .values_list("child_calendar_fk_id", flat=True)
                )

            # Build the queryset: (active OR in existing_child_ids) AND org-scoped
            qs = Calendar.objects.filter_by_organization(org_id).filter(
                ~Q(visibility=CalendarVisibility.INACTIVE) | Q(id__in=existing_child_ids)
            )
        else:
            qs = Calendar.objects.unscoped().none()

        self.fields["bundle_calendars"] = serializers.PrimaryKeyRelatedField(
            many=True,
            queryset=qs,
        )
        self.fields["primary_calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=qs,
            allow_null=True,
            required=False,
        )

    def validate_bundle_calendars(self, bundle_calendars):
        """Require at least two children, mirroring create."""
        if len(bundle_calendars) < 2:
            raise serializers.ValidationError("At least two calendars are required in a bundle.")
        return bundle_calendars

    def validate(self, attrs: dict) -> dict:
        if self.instance and self.instance.calendar_type != CalendarType.BUNDLE:
            raise serializers.ValidationError("Calendar is not a bundle.")

        primary_calendar: Calendar | None = attrs.get("primary_calendar")
        bundle_calendars: list[Calendar] = attrs.get("bundle_calendars", [])

        if primary_calendar and primary_calendar not in bundle_calendars:
            raise serializers.ValidationError(
                "primary_calendar must be one of the bundle_calendars."
            )

        return attrs

    def update(self, instance: Calendar, validated_data: dict) -> Calendar:
        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )

        self.calendar_service.initialize_without_provider(organization=membership.organization)
        try:
            self.calendar_service.update_bundle_calendar(
                bundle_calendar=instance,
                child_calendars=validated_data["bundle_calendars"],
                primary_calendar=validated_data.get("primary_calendar"),
            )
        except (ValueError, CalendarIntegrationError) as e:
            raise serializers.ValidationError(str(e)) from e

        return instance


class EventRecurringExceptionSerializer(serializers.Serializer):
    """Serializer for creating recurring event exceptions."""

    exception_date = serializers.DateField(
        required=True, help_text="The date of the occurrence to modify or cancel"
    )
    modified_title = serializers.CharField(
        required=False,
        allow_null=True,
        max_length=255,
        help_text="New title for the modified occurrence (if not cancelled)",
    )
    modified_description = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="New description for the modified occurrence (if not cancelled)",
    )
    modified_start_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="New start time for the modified occurrence (if not cancelled)",
    )
    modified_end_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="New end time for the modified occurrence (if not cancelled)",
    )
    is_cancelled = serializers.BooleanField(
        default=False, help_text="True if cancelling the occurrence, False if modifying"
    )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

    def validate(self, attrs: dict) -> dict:
        """Validate the exception data."""
        is_cancelled = attrs.get("is_cancelled", False)

        if not is_cancelled:
            # If not cancelled, at least one modification field should be provided
            has_modifications = any(
                [
                    attrs.get("modified_title"),
                    attrs.get("modified_description"),
                    attrs.get("modified_start_time"),
                    attrs.get("modified_end_time"),
                ]
            )

            if not has_modifications:
                raise serializers.ValidationError(
                    "For non-cancelled exceptions, at least one modification field must be provided."
                )

        # Validate that start_time is before end_time if both are provided
        start_time = attrs.get("modified_start_time")
        end_time = attrs.get("modified_end_time")

        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError(
                "modified_start_time must be before modified_end_time."
            )

        return attrs

    def save(self, **kwargs) -> None:
        """Create a recurring event exception."""
        parent_event = self.context["parent_event"]

        user: User | None = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )

        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        if not user or not user.is_authenticated:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Only authenticated users can create Blocked Times",
                    ]
                }
            )

        # Initialize calendar service
        self.calendar_service.authenticate(
            account=user,
            organization=parent_event.organization,
        )

        # Convert date to datetime for the exception_date
        exception_date = self.validated_data["exception_date"]
        self.instance = self.calendar_service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_date,
            modified_title=self.validated_data.get("modified_title"),
            modified_description=self.validated_data.get("modified_description"),
            modified_start_time=self.validated_data.get("modified_start_time"),
            modified_end_time=self.validated_data.get("modified_end_time"),
            is_cancelled=self.validated_data.get("is_cancelled", False),
        )


class ExternalClientIdentifierSerializer(serializers.Serializer):
    """One ``(system, identifier)`` client-owned reference pair.

    ``system`` is normalized (case + trailing slash) and validated as an absolute
    URL by ``ExternalClientIdentifierService`` before storage/matching -- this
    input layer does no format validation of its own, matching the public
    GraphQL ``ExternalClientIdentifierInput``. See
    ``calendar_integration.external_client_identifiers.normalize_system``.
    """

    system = serializers.CharField(max_length=500)
    identifier = serializers.CharField(max_length=255)


def _map_external_client_identifiers(
    identifiers: list[dict[str, str]] | None,
) -> list[ExternalClientIdentifierData] | None:
    """Map the REST tri-state input to the tri-state Phase 2 dataclass list.

    A key absent from ``validated_data`` -- the caller never sent it, or a
    ``PATCH`` omitted it -- reaches this function as ``None`` (the ``.pop(...,
    None)``/``.get(...)`` default used at every call site below), which maps to
    ``None`` here too: "leave untouched". An explicit list -- including an
    explicit ``[]`` -- maps to a (possibly empty) list, which replaces the
    stored set and clears it when empty. See
    ``ExternalClientIdentifierService.replace_for_target``.
    """
    if identifiers is None:
        return None
    return [
        ExternalClientIdentifierData(system=item["system"], identifier=item["identifier"])
        for item in identifiers
    ]


class ExternalAttendeeSerializer(VirtualModelSerializer):
    id = serializers.IntegerField(  # noqa: A003
        allow_null=True, required=False, help_text="ID of the external attendee."
    )
    external_client_identifiers = ExternalClientIdentifierSerializer(
        many=True,
        required=False,
        help_text=(
            "Client-owned (system, identifier) pairs for this external attendee. "
            "Omitted leaves the stored set untouched (a no-op on create, since there "
            "is nothing to leave untouched yet); an explicit list (including []) "
            "replaces it."
        ),
    )

    class Meta:
        model = ExternalAttendee
        virtual_model = ExternalAttendeeVirtualModel
        fields = (
            "id",
            "name",
            "email",
            "external_client_identifiers",
            "created",
            "modified",
        )


class EventExternalAttendanceSerializer(VirtualModelSerializer):
    id = serializers.IntegerField(  # noqa: A003
        allow_null=True, required=False, help_text="ID of the external attendee."
    )
    external_attendee = ExternalAttendeeSerializer()

    class Meta:
        model = EventExternalAttendance
        virtual_model = EventExternalAttendanceVirtualModel
        fields = (
            "id",
            "external_attendee",
            "status",
            "created",
            "modified",
        )
        read_only_fields = ("status",)


class EventAttendanceSerializer(VirtualModelSerializer):
    id = serializers.IntegerField(  # noqa: A003
        allow_null=True, required=False, help_text="ID of the external attendee."
    )
    membership = OwnershipMembershipSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source="user", queryset=User.objects.all(), required=True, allow_null=False, write_only=True
    )

    class Meta:
        model = EventAttendance
        virtual_model = EventAttendanceVirtualModel
        fields = (
            "id",
            "membership",
            "user_id",
            "status",
            "created",
            "modified",
        )
        read_only_fields = (
            "membership",
            "status",
        )


class ResourceAllocationSerializer(VirtualModelSerializer):
    id = serializers.IntegerField(  # noqa: A003
        allow_null=True, required=False, help_text="ID of the external attendee."
    )

    class Meta:
        model = ResourceAllocation
        virtual_model = ResourceAllocationVirtualModel
        fields = (
            "id",
            "calendar",
            "status",
            "created",
            "modified",
        )
        read_only_fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        membership = request.__dict__.get("organization_membership") if request else None
        # add calendar field dynamically to filter by organization_id
        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                .exclude_inactive()
                .filter(calendar_type=CalendarType.RESOURCE)
                if membership
                else Calendar.original_manager.none()
            ),
        )


class RecurrenceRuleSerializer(VirtualModelSerializer):
    rrule_string = serializers.SerializerMethodField()

    class Meta:
        model = RecurrenceRule
        virtual_model = RecurrenceRuleVirtualModel
        fields = (
            "id",
            "frequency",
            "interval",
            "count",
            "until",
            "by_weekday",
            "by_month_day",
            "by_month",
            "by_year_day",
            "by_week_number",
            "by_hour",
            "by_minute",
            "by_second",
            "week_start",
            "rrule_string",
            "created",
            "modified",
        )

    @v.hints.no_deferred_fields()
    def get_rrule_string(self, obj: RecurrenceRule) -> str:
        return obj.to_rrule_string()

    def validate(self, attrs):
        """Validate the recurrence rule data using the model's validation."""

        # Create a temporary RecurrenceRule instance for validation
        # We don't save it, just use it for validation
        temp_rule = RecurrenceRule(**attrs)

        try:
            temp_rule.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(
                e.message_dict if hasattr(e, "message_dict") else str(e)
            ) from e

        return attrs

    def validate_by_weekday(self, value):
        """Validate weekdays format."""
        if not value:
            return value

        valid_weekdays = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
        weekdays = [day.strip() for day in value.split(",")]
        invalid_weekdays = [day for day in weekdays if day not in valid_weekdays]

        if invalid_weekdays:
            raise serializers.ValidationError(
                f"Invalid weekdays: {', '.join(invalid_weekdays)}. "
                "Valid options are: MO, TU, WE, TH, FR, SA, SU"
            )

        return value

    def validate_by_month_day(self, value):
        """Validate month days format."""
        if not value:
            return value

        try:
            month_days = [int(day.strip()) for day in value.split(",")]
            invalid_days = [day for day in month_days if day == 0 or day > 31 or day < -31]
            if invalid_days:
                raise serializers.ValidationError(
                    f"Invalid month days: {', '.join(map(str, invalid_days))}. "
                    "Must be between 1-31 or -1 to -31."
                )
        except ValueError as e:
            raise serializers.ValidationError(
                "Month days must be integers separated by commas."
            ) from e

        return value

    def validate_by_month(self, value):
        """Validate months format."""
        if not value:
            return value

        try:
            months = [int(month.strip()) for month in value.split(",")]
            invalid_months = [month for month in months if month < 1 or month > 12]
            if invalid_months:
                raise serializers.ValidationError(
                    f"Invalid months: {', '.join(map(str, invalid_months))}. Must be between 1-12."
                )
        except ValueError as e:
            raise serializers.ValidationError("Months must be integers separated by commas.") from e

        return value


class RecurrenceExceptionSerializer(VirtualModelSerializer):
    class Meta:
        model = EventRecurrenceException
        virtual_model = EventRecurrenceExceptionVirtualModel
        fields = (
            "id",
            "exception_date",
            "is_cancelled",
            "created",
            "modified",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        membership = request.__dict__.get("organization_membership") if request else None
        # add parent_event field dynamically to filter by organization_id
        self.fields["parent_event"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                CalendarEvent.objects.filter_by_organization(membership.organization_id).all()
                if membership
                else CalendarEvent.original_manager.none()
            ),
            required=False,
            allow_null=True,
        )
        self.fields["modified_event"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                CalendarEvent.objects.filter_by_organization(membership.organization_id).all()
                if membership
                else CalendarEvent.original_manager.none()
            ),
            required=False,
            allow_null=True,
        )


class ParentEventSerializer(VirtualModelSerializer):
    class Meta:
        model = CalendarEvent
        virtual_model = CalendarEventVirtualModel
        fields = (
            "id",
            "title",
            "external_id",
            "start_time",
            "end_time",
            "created",
            "modified",
        )
        read_only_fields = ("id", "external_id", "created", "modified")


class CalendarEventSerializer(VirtualModelSerializer):
    user: User | None
    token: str | None

    provider = serializers.CharField(required=False, write_only=True)
    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for creating recurring events",
    )
    rrule_string = serializers.CharField(
        write_only=True, required=False, help_text="RRULE string for creating recurring events"
    )
    parent_recurring_object_id = serializers.IntegerField(
        write_only=True, required=False, help_text="ID of parent event for recurring instances"
    )
    is_recurring_instance = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is an instance of a recurring event"
    )
    is_recurring = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is a recurring event"
    )
    start_time = serializers.DateTimeField(required=True)
    end_time = serializers.DateTimeField(required=True)
    parent_recurring_object = ParentEventSerializer(read_only=True)
    external_client_identifiers = ExternalClientIdentifierSerializer(
        many=True,
        required=False,
        help_text=(
            "Client-owned (system, identifier) pairs for this event. Omitted on a "
            "partial update leaves the stored set untouched; an explicit list "
            "(including []) replaces it."
        ),
    )

    class Meta:
        model = CalendarEvent
        virtual_model = CalendarEventVirtualModel
        fields = (
            "id",
            "provider",
            "title",
            "description",
            "start_time",
            "end_time",
            "timezone",
            "created",
            "modified",
            "external_id",
            "external_attendances",
            "attendances",
            "resource_allocations",
            "external_client_identifiers",
            # Recurrence fields
            "recurrence_rule",
            "rrule_string",
            "parent_recurring_object_id",
            "parent_recurring_object",
            "is_recurring_instance",
            "is_recurring",
            "is_recurring_exception",
            "recurrence_id",
        )
        read_only_fields = (
            "id",
            "external_id",
            "is_recurring_instance",
            "recurrence_exceptions",
        )
        write_only_fields = ("recurrence_rule_id",)

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

        # Check if we have token context (for token-based requests)
        token = self.context.get("token") if self.context else None
        token_str_base64 = self.context.get("token_str_base64") if self.context else None
        organization = self.context.get("organization") if self.context else None

        # For token-based requests, user will be None - that's expected
        user = (
            self.context["request"].user
            if self.context and self.context.get("request") and not token
            else None
        )

        # Store user, token, and organization as instance attributes for use in create/update methods
        self.user = user
        self.token = token
        self.token_str_base64 = token_str_base64
        self.organization = organization

        # Determine organization_id from either user or token context
        organization_id = None
        if organization:
            # Token-based request - use organization from context
            organization_id = organization.id
        else:
            # Regular authenticated user request — require an active membership
            request = self.context.get("request") if self.context else None
            active_membership = request.__dict__.get("organization_membership") if request else None
            if active_membership:
                organization_id = active_membership.organization_id

        # Use organization_id to set up querysets
        user_is_authenticated = (user and user.is_authenticated) or bool(token)

        # Initialize nested serializers with context
        self.fields["resource_allocations"] = ResourceAllocationSerializer(
            many=True, context=self.context
        )
        self.fields["attendances"] = EventAttendanceSerializer(many=True, context=self.context)
        self.fields["external_attendances"] = EventExternalAttendanceSerializer(
            many=True, context=self.context
        )

        if self.instance:
            self.fields["recurrence_rule_id"] = serializers.PrimaryKeyRelatedField(
                source="recurrence_rule_fk",
                many=False,
                required=False,
                queryset=(
                    RecurrenceRule.objects.filter_by_organization(organization_id).all()
                    if user_is_authenticated and organization_id
                    else RecurrenceRule.original_manager.none()
                ),
                write_only=True,
            )

        # add google_calendar_service_account and calendar fields dynamically to filter by
        # organization_id
        self.fields["google_calendar_service_account"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                GoogleCalendarServiceAccount.objects.filter_by_organization(organization_id).all()
                if user_is_authenticated and organization_id
                else GoogleCalendarServiceAccount.original_manager.none()
            ),
            required=False,
            write_only=True,
        )
        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(organization_id).all()
                if user_is_authenticated and organization_id
                else Calendar.original_manager.none()
            ),
            required=False,
            write_only=True,
        )

    def validate_timezone(self, timezone):
        if not timezone:
            raise serializers.ValidationError("Timezone is required.")

        # check timezone is a valid IANA timezone
        try:
            datetime.timezone(datetime.timedelta(0)).tzname(None)  # Dummy call to access tzinfo
            zoneinfo.ZoneInfo(timezone)
        except zoneinfo.ZoneInfoNotFoundError as e:
            raise serializers.ValidationError(f"Invalid timezone: {timezone}") from e

        return timezone

    def validate_provider(self, provider):
        if not provider:
            return provider

        # Use the stored user (works for both regular and token-based requests)
        user = self.user
        if user and not SocialAccount.objects.filter(user=user, provider=provider).exists():
            raise serializers.ValidationError(
                "User does not have a social account from the selected provider linked."
            )

        return provider

    def validate_start_time(self, start_time):
        if start_time <= datetime.datetime.now(tz=datetime.UTC):
            raise serializers.ValidationError("Start time must be in the future.")

        return start_time

    def validate(self, attrs):
        # Incoming datetimes carry wall-clock local to the request ``timezone``
        # (symmetric with how responses render instants in the record timezone).
        # DRF coerces naive input to UTC under the server default (TIME_ZONE=UTC),
        # so reinterpret the wall-clock in ``timezone`` to recover the true instant
        # before it reaches the service, which treats start_time/end_time as true
        # instants. Without this, "16:30" + "America/Recife" reaches the service as
        # 16:30 UTC (13:30 Recife) instead of the intended 19:30 UTC.
        tz_name = attrs.get("timezone") or (self.instance.timezone if self.instance else None)
        if tz_name:
            tz = zoneinfo.ZoneInfo(tz_name)
            for field in ("start_time", "end_time"):
                value = attrs.get(field)
                if value is not None:
                    wall_clock = value.astimezone(datetime.UTC).replace(tzinfo=None)
                    attrs[field] = wall_clock.replace(tzinfo=tz).astimezone(datetime.UTC)

        calendar = attrs.get("calendar")

        # For token-based requests, we can infer the calendar from the token context
        # Only check for calendar/provider/service account during creation for non-token requests
        if (
            not self.instance  # This is a creation, not an update
            and not calendar
            and not attrs.get("provider")
            and not attrs.get("google_calendar_service_account")
            and not self.token  # No token present - require explicit calendar/provider/service account
        ):
            raise serializers.ValidationError(
                "You need to select either a calendar, provider, or a service account to create "
                "an event."
            )

        # Validate start_time and end_time only if both are provided or if this is a creation
        start_time = attrs.get("start_time")
        end_time = attrs.get("end_time")

        # For updates, use existing instance values if not provided in attrs
        if self.instance:
            start_time = start_time or self.instance.start_time_tz_unaware
            end_time = end_time or self.instance.end_time_tz_unaware

        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError("End time must be after start time.")

        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")
        parent_recurring_object_id = attrs.get("parent_recurring_object_id")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule and rrule_string. Use one or the other."
            )

        if (recurrence_rule_data or rrule_string) and parent_recurring_object_id:
            raise serializers.ValidationError(
                "Cannot specify recurrence rule for event instances. Recurrence rules are only for master events."
            )

        # Only auto-assign calendar during creation, not updates
        if not calendar and not self.instance:
            if self.token:
                # For token-based requests, we'll infer the calendar in the create method
                # after the calendar service is initialized, so we can skip calendar assignment here
                pass
            else:
                # Use stored user or organization from regular authentication
                request = self.context.get("request") if self.context else None
                active_membership = (
                    request.__dict__.get("organization_membership") if request else None
                )
                if active_membership:
                    organization_id = active_membership.organization_id
                elif self.organization:
                    organization_id = self.organization.id
                else:
                    raise serializers.ValidationError(
                        "Cannot determine organization for calendar selection."
                    )

                if attrs.get("provider"):
                    attrs["calendar"] = (
                        CalendarOwnership.objects.filter_by_organization(organization_id)
                        .filter(
                            calendar__provider=attrs.get("provider"),
                            is_default=True,
                        )
                        .first()
                    )
                elif attrs.get("google_calendar_service_account"):
                    attrs["calendar"] = attrs.get("google_calendar_service_account").calendar

        return attrs

    def create(self, validated_data):
        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        calendar: Calendar | None = validated_data.pop("calendar", None)

        # Use token or user for authentication
        if self.token_str_base64:
            # Token-based authentication - initialize without provider
            # We need to get the organization from the token context first
            organization = self.organization
            if not organization:
                raise serializers.ValidationError(
                    "Organization context is required for token-based authentication"
                )

            self.calendar_service.initialize_without_provider(
                user_or_token=self.token_str_base64, organization=organization
            )

            # If no calendar was provided, infer it from the token after service initialization
            if not calendar:
                # The calendar service now has the permission service initialized with the token
                permission_service = self.calendar_service.calendar_permission_service
                if permission_service and permission_service.token:
                    if permission_service.token.calendar_fk:
                        # Calendar-level token - use the token's calendar
                        calendar = permission_service.token.calendar_fk
                    elif permission_service.token.event_fk:
                        # Event-level token - use the event's calendar
                        calendar = permission_service.token.event_fk.calendar_fk
                    else:
                        raise serializers.ValidationError(
                            "Unable to determine calendar from token context."
                        )
                else:
                    raise serializers.ValidationError(
                        "Token authentication failed or calendar could not be determined."
                    )
        elif self.user:
            # Regular user authentication
            if not calendar:
                raise serializers.ValidationError(
                    "Calendar is required for user-based authentication"
                )

            user = self.user
            if validated_data.get("google_calendar_service_account"):
                account = validated_data.get("google_calendar_service_account")
                self.calendar_service.authenticate(
                    account=account,
                    organization=calendar.organization,
                )
            else:
                self.calendar_service.authenticate(
                    account=user,
                    organization=calendar.organization,
                )
        else:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "You need either an authenticated user or a Calendar Management Token"
                    ]
                }
            )

        resource_allocations = validated_data.pop("resource_allocations", [])
        attendances = validated_data.pop("attendances", [])
        external_attendances = validated_data.pop("external_attendances", [])
        # None = key absent (nothing to leave untouched on create yet); an explicit
        # list -- including [] -- is meaningless on create but handled uniformly by
        # _map_external_client_identifiers / the service either way.
        external_client_identifiers = validated_data.pop("external_client_identifiers", None)

        # Handle recurrence fields
        recurrence_rule_data = validated_data.pop("recurrence_rule", None)
        rrule_string = validated_data.pop("rrule_string", None)
        parent_recurring_object_id = validated_data.pop("parent_recurring_object_id", None)

        # Prepare recurrence rule for calendar service
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        event = self.calendar_service.create_event(
            calendar_id=calendar.id,
            event_data=CalendarEventInputData(
                title=validated_data.get("title"),
                description=validated_data.get("description"),
                start_time=validated_data.get("start_time"),
                end_time=validated_data.get("end_time"),
                timezone=validated_data.get("timezone"),
                resource_allocations=[
                    ResourceAllocationInputData(resource_id=ra["calendar"].id)
                    for ra in resource_allocations
                ],
                attendances=[
                    EventAttendanceInputData(user_id=att["user"].id) for att in attendances
                ],
                external_attendances=[
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            id=ext["external_attendee"].get("id"),
                            email=ext["external_attendee"]["email"],
                            name=ext["external_attendee"]["name"],
                            external_client_identifiers=_map_external_client_identifiers(
                                ext["external_attendee"].get("external_client_identifiers")
                            ),
                        )
                    )
                    for ext in external_attendances
                ],
                # Recurrence fields
                recurrence_rule=final_rrule_string,
                parent_event_id=parent_recurring_object_id,
                is_recurring_exception=validated_data.get("is_recurring_exception", False),
                external_client_identifiers=_map_external_client_identifiers(
                    external_client_identifiers
                ),
            ),
        )

        return event

    def update(self, instance: CalendarEvent, validated_data: dict) -> CalendarEvent:
        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        calendar: Calendar = validated_data.pop("calendar", instance.calendar)

        # Use token or user for authentication
        if self.token_str_base64:
            # Token-based authentication - initialize without provider
            self.calendar_service.initialize_without_provider(
                user_or_token=self.token_str_base64, organization=calendar.organization
            )
        elif self.user:
            # Regular user authentication
            user: User = self.user
            if validated_data.get("google_calendar_service_account"):
                account: GoogleCalendarServiceAccount = validated_data[
                    "google_calendar_service_account"
                ]
                self.calendar_service.authenticate(
                    account=account,
                    organization=calendar.organization,
                )
            else:
                self.calendar_service.authenticate(
                    account=user,
                    organization=calendar.organization,
                )
        else:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "You need either an authenticated user or a Calendar Management Token"
                    ]
                }
            )

        resource_allocations = validated_data.pop(
            "resource_allocations",
            [{"resource_id": ra.calendar.id} for ra in instance.resource_allocations.all()],
        )
        attendances = validated_data.pop(
            "attendances",
            [
                {"user_id": att.membership_user_id}
                for att in instance.attendances.all()
                if att.membership_user_id is not None
            ],
        )
        external_attendances = validated_data.pop(
            "external_attendances",
            [
                {
                    "external_attendee": {
                        "id": ext.external_attendee.id,
                        "email": ext.external_attendee.email,
                        "name": ext.external_attendee.name,
                    }
                }
                for ext in instance.external_attendances.all()
            ],
        )
        # ``None`` = key absent from the request (a PATCH that never mentioned
        # identifiers) -- passed straight through to the service as "leave
        # untouched" rather than reconstructed from ``instance``, since the
        # service (unlike ``resource_allocations``/``attendances``/
        # ``external_attendances`` above) already treats ``None`` natively as a
        # no-op. Reconstructing here would collapse that distinction.
        external_client_identifiers = validated_data.pop("external_client_identifiers", None)

        # Handle recurrence fields for updates
        recurrence_rule_instance = validated_data.pop("recurrence_rule_id", None)
        recurrence_rule_data = validated_data.pop("recurrence_rule", None)
        rrule_string = validated_data.pop("rrule_string", None)
        parent_recurring_object_id = validated_data.pop("parent_recurring_object_id", None)

        # Prepare recurrence rule for calendar service
        final_rrule_string = None
        if recurrence_rule_instance:
            final_rrule_string = recurrence_rule_instance.to_rrule_string()
        elif recurrence_rule_data:
            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string
        elif instance.recurrence_rule:
            # Keep existing recurrence rule
            final_rrule_string = instance.recurrence_rule.to_rrule_string()

        event = self.calendar_service.update_event(
            calendar_id=calendar.id,
            event_id=instance.id,
            event_data=CalendarEventInputData(
                title=validated_data.get("title", instance.title),
                description=validated_data.get("description", instance.description),
                start_time=validated_data.get("start_time", instance.start_time),
                end_time=validated_data.get("end_time", instance.end_time),
                timezone=validated_data.get("timezone", instance.timezone),
                resource_allocations=[
                    ResourceAllocationInputData(resource_id=ra["calendar"].id)
                    for ra in resource_allocations
                ],
                attendances=[
                    EventAttendanceInputData(user_id=att["user"].id) for att in attendances
                ],
                external_attendances=[
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            id=ext["external_attendee"].get("id"),
                            email=ext["external_attendee"]["email"],
                            name=ext["external_attendee"]["name"],
                            external_client_identifiers=_map_external_client_identifiers(
                                ext["external_attendee"].get("external_client_identifiers")
                            ),
                        )
                    )
                    for ext in external_attendances
                ],
                # Recurrence fields
                recurrence_rule=final_rrule_string,
                parent_event_id=parent_recurring_object_id
                or (
                    instance.parent_recurring_object.id
                    if instance.parent_recurring_object
                    else None
                ),
                is_recurring_exception=validated_data.get(
                    "is_recurring_exception", instance.is_recurring_exception
                ),
                external_client_identifiers=_map_external_client_identifiers(
                    external_client_identifiers
                ),
            ),
        )

        return event

    @v.hints.no_deferred_fields()
    def get_is_recurring_instance(self, obj: CalendarEvent) -> bool:
        """
        Returns True if this event is an instance of a recurring event.
        """
        return obj.is_recurring_instance

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: CalendarEvent) -> bool:
        """
        Returns True if this event is a recurring event.
        """
        return obj.is_recurring

    def to_representation(self, instance):
        """Render start_time/end_time in the event's IANA timezone, not UTC."""
        data = super().to_representation(instance)
        return _localize_times_in_representation(
            data, instance, getattr(instance, "timezone", None)
        )


class CalendarEventTransferSerializer(serializers.Serializer):
    target_calendar_id = serializers.IntegerField()

    def validate_target_calendar_id(self, value):
        event = self.context["event"]
        if value == event.calendar_fk_id:
            raise serializers.ValidationError("Event is already on the target calendar.")
        try:
            return Calendar.objects.filter_by_organization(event.organization_id).get(id=value)
        except Calendar.DoesNotExist as exc:
            raise serializers.ValidationError(
                "target_calendar_id invalid or not in your organization."
            ) from exc


class SerializedParentBlockedTimeTypedDict(TypedDict):
    id: int
    reason: str | None


class BlockedTimeSerializer(VirtualModelSerializer):
    """Serializer for BlockedTime model with recurring support."""

    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for creating recurring blocked times",
    )
    rrule_string = serializers.CharField(
        write_only=True,
        required=False,
        help_text="RRULE string for creating recurring blocked times",
    )
    is_recurring_instance = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is an instance of a recurring blocked time"
    )
    is_recurring = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is a recurring blocked time"
    )
    start_time = serializers.DateTimeField(required=True)
    end_time = serializers.DateTimeField(required=True)
    parent_blocked_time = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = BlockedTime
        virtual_model = BlockedTimeVirtualModel
        fields = (
            "id",
            "start_time",
            "end_time",
            "timezone",
            "reason",
            "recurrence_rule",
            "rrule_string",
            "external_id",
            "is_recurring_instance",
            "is_recurring",
            "parent_blocked_time",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            "external_id",
            "is_recurring_instance",
            "is_recurring",
            "parent_blocked_time",
            "recurrence_id",
            "is_recurring_exception",
            "created",
            "modified",
        )
        write_only_fields = ("recurrence_rule_id",)

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: BlockedTime) -> bool:
        """Check if blocked time is recurring."""
        return obj.is_recurring

    @v.hints.no_deferred_fields()
    def get_is_recurring_instance(self, obj: BlockedTime) -> bool:
        """Check if blocked time is a recurring instance."""
        return obj.is_recurring_instance

    @v.hints.no_deferred_fields()
    def get_parent_blocked_time(
        self, obj: BlockedTime
    ) -> SerializedParentBlockedTimeTypedDict | None:
        """Get parent blocked time for instances."""
        if obj.parent_recurring_object:
            return {
                "id": obj.parent_recurring_object.id,
                "reason": obj.parent_recurring_object.reason,
            }
        return None

    def to_representation(self, instance):
        """Render start_time/end_time in the record's IANA timezone, not UTC."""
        data = super().to_representation(instance)
        return _localize_times_in_representation(
            data, instance, getattr(instance, "timezone", None)
        )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        membership = request.__dict__.get("organization_membership") if request else None

        if self.instance:
            self.fields["recurrence_rule_id"] = serializers.PrimaryKeyRelatedField(
                source="recurrence_rule_fk",
                many=False,
                required=False,
                queryset=(
                    RecurrenceRule.objects.filter_by_organization(membership.organization_id).all()
                    if membership
                    else RecurrenceRule.original_manager.none()
                ),
                write_only=True,
            )

        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
            allow_null=True,
            required=False,
        )

    def create(self, validated_data: dict):
        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        user: User | None = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )
        if not user or not user.is_authenticated:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Only authenticated users can create Blocked Times",
                    ]
                }
            )

        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )

        calendar = validated_data.pop("calendar", None)
        organization: Organization = membership.organization
        self.calendar_service.initialize_without_provider(user, organization)
        if calendar is None:
            # No calendar specified — fall back to the user's default calendar.
            calendar = self.calendar_service.get_default_calendar_for_user(user)
            if calendar is None:
                raise serializers.ValidationError(
                    {
                        "non_field_errors": [
                            "No calendar specified and you have no default calendar."
                        ]
                    }
                )

        # Handle recurrence fields
        recurrence_rule_data = validated_data.pop("recurrence_rule", None)
        rrule_string = validated_data.pop("rrule_string", None)

        # Prepare recurrence rule for calendar service
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        return self.calendar_service.create_blocked_time(
            calendar=calendar,
            reason=cast(str, validated_data.get("reason", "")),
            start_time=validated_data["start_time"],
            end_time=validated_data["end_time"],
            timezone=validated_data["timezone"],
            rrule_string=final_rrule_string,
        )

    def update(self, instance: BlockedTime, validated_data: dict) -> BlockedTime:
        # Handle recurrence fields for updates
        recurrence_rule_instance = validated_data.pop("recurrence_rule_id", None)
        recurrence_rule_data = validated_data.pop("recurrence_rule", None)
        rrule_string = validated_data.pop("rrule_string", None)

        # Prepare recurrence rule
        if recurrence_rule_instance:
            instance.recurrence_rule = recurrence_rule_instance
        elif recurrence_rule_data:
            calendar = validated_data.get("calendar", instance.calendar)
            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            temp_rule.save()
            instance.recurrence_rule = temp_rule
        elif rrule_string:
            # Parse rrule_string and create/update RecurrenceRule
            calendar = validated_data.get("calendar", instance.calendar)
            recurrence_rule = RecurrenceRule.from_rrule_string(rrule_string, calendar.organization)
            recurrence_rule.save()
            instance.recurrence_rule = recurrence_rule

        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance

    def validate_timezone(self, timezone):
        if not timezone:
            raise serializers.ValidationError("Timezone is required.")

        # check timezone is a valid IANA timezone
        try:
            datetime.timezone(datetime.timedelta(0)).tzname(None)  # Dummy call to access tzinfo
            zoneinfo.ZoneInfo(timezone)
        except zoneinfo.ZoneInfoNotFoundError as e:
            raise serializers.ValidationError(f"Invalid timezone: {timezone}") from e

        return timezone

    def validate(self, attrs):
        """Validate blocked time data."""
        if attrs.get("start_time") and attrs.get("end_time"):
            if attrs["start_time"] >= attrs["end_time"]:
                raise serializers.ValidationError("start_time must be before end_time")

        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")
        parent_blocked_time_id = attrs.get("parent_blocked_time_id")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule and rrule_string. Use one or the other."
            )

        if (recurrence_rule_data or rrule_string) and parent_blocked_time_id:
            raise serializers.ValidationError(
                "Cannot specify recurrence rule for blocked time instances. Recurrence rules are only for master blocked times."
            )

        return attrs


class SerializedParentAvailableTimeTypedDict(TypedDict):
    id: int


class AvailableTimeSerializer(VirtualModelSerializer):
    """Serializer for AvailableTime model with recurring support."""

    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for creating recurring available times",
    )
    rrule_string = serializers.CharField(
        write_only=True,
        required=False,
        help_text="RRULE string for creating recurring available times",
    )
    is_recurring_instance = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is an instance of a recurring available time"
    )
    is_recurring = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is a recurring available time"
    )
    start_time = serializers.DateTimeField(required=True)
    end_time = serializers.DateTimeField(required=True)
    parent_available_time = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = AvailableTime
        virtual_model = AvailableTimeVirtualModel
        fields = (
            "id",
            "start_time",
            "end_time",
            "timezone",
            "recurrence_rule",
            "rrule_string",
            "is_recurring_instance",
            "is_recurring",
            "parent_available_time",
            "recurrence_id",
            "created",
            "modified",
        )
        read_only_fields = (
            "id",
            "is_recurring_instance",
            "is_recurring",
            "parent_available_time",
            "is_recurring_exception",
            "recurrence_id",
            "created",
            "modified",
        )
        write_only_fields = ("recurrence_rule_id",)

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: AvailableTime) -> bool:
        """Check if available time is recurring."""
        return obj.is_recurring

    @v.hints.no_deferred_fields()
    def get_is_recurring_instance(self, obj: AvailableTime) -> bool:
        """Check if available time is a recurring instance."""
        return obj.is_recurring_instance

    @v.hints.no_deferred_fields()
    def get_parent_available_time(
        self, obj: AvailableTime
    ) -> SerializedParentAvailableTimeTypedDict | None:
        """Get parent available time for instances."""
        if obj.parent_recurring_object:
            return {
                "id": obj.parent_recurring_object.id,
            }
        return None

    def to_representation(self, instance):
        """Render start_time/end_time in the record's IANA timezone, not UTC."""
        data = super().to_representation(instance)
        return _localize_times_in_representation(
            data, instance, getattr(instance, "timezone", None)
        )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        membership = request.__dict__.get("organization_membership") if request else None

        if self.instance:
            self.fields["recurrence_rule_id"] = serializers.PrimaryKeyRelatedField(
                source="recurrence_rule_fk",
                many=False,
                required=False,
                queryset=(
                    RecurrenceRule.objects.filter_by_organization(membership.organization_id).all()
                    if membership
                    else RecurrenceRule.original_manager.none()
                ),
                write_only=True,
            )

        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
            allow_null=True,
            required=False,
        )

    def create(self, validated_data: dict):
        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        user: User | None = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )
        if not user or not user.is_authenticated:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Only authenticated users can create Available Times",
                    ]
                }
            )

        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )

        calendar = validated_data.pop("calendar", None)
        organization: Organization = membership.organization
        self.calendar_service.initialize_without_provider(user, organization)
        if calendar is None:
            # No calendar specified — fall back to the user's default calendar.
            calendar = self.calendar_service.get_default_calendar_for_user(user)
            if calendar is None:
                raise serializers.ValidationError(
                    {
                        "non_field_errors": [
                            "No calendar specified and you have no default calendar."
                        ]
                    }
                )

        # Handle recurrence fields
        recurrence_rule_data = validated_data.pop("recurrence_rule", None)
        rrule_string = validated_data.pop("rrule_string", None)

        # Prepare recurrence rule for calendar service
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        return self.calendar_service.create_available_time(
            calendar=calendar,
            start_time=validated_data["start_time"],
            end_time=validated_data["end_time"],
            timezone=validated_data["timezone"],
            rrule_string=final_rrule_string,
        )

    def update(self, instance: AvailableTime, validated_data: dict) -> AvailableTime:
        # Handle recurrence fields for updates
        recurrence_rule_instance = validated_data.pop("recurrence_rule_id", None)
        recurrence_rule_data = validated_data.pop("recurrence_rule", None)
        rrule_string = validated_data.pop("rrule_string", None)

        # Prepare recurrence rule
        if recurrence_rule_instance:
            instance.recurrence_rule = recurrence_rule_instance
        elif recurrence_rule_data:
            calendar = validated_data.get("calendar", instance.calendar)
            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            temp_rule.save()
            instance.recurrence_rule = temp_rule
        elif rrule_string:
            # Parse rrule_string and create/update RecurrenceRule
            calendar = validated_data.get("calendar", instance.calendar)
            recurrence_rule = RecurrenceRule.from_rrule_string(rrule_string, calendar.organization)
            recurrence_rule.save()
            instance.recurrence_rule = recurrence_rule

        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance

    def validate(self, attrs):
        """Validate available time data."""
        if attrs.get("start_time") and attrs.get("end_time"):
            if attrs["start_time"] >= attrs["end_time"]:
                raise serializers.ValidationError("start_time must be before end_time")

        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")
        parent_available_time_id = attrs.get("parent_available_time_id")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule and rrule_string. Use one or the other."
            )

        if (recurrence_rule_data or rrule_string) and parent_available_time_id:
            raise serializers.ValidationError(
                "Cannot specify recurrence rule for available time instances. Recurrence rules are only for master available times."
            )

        return attrs


class AvailableTimeWindowSerializer(serializers.Serializer):
    id = serializers.IntegerField()  # noqa: A003
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    can_book_partially = serializers.BooleanField()

    def to_representation(self, instance):
        """Render start_time/end_time in the window's IANA timezone, not UTC."""
        data = super().to_representation(instance)
        return _localize_times_in_representation(
            data, instance, getattr(instance, "timezone", None)
        )


class UnavailableTimeWindowSerializer(serializers.Serializer):
    id = serializers.IntegerField()  # noqa: A003
    reason = serializers.CharField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    reason_description = serializers.SerializerMethodField()

    def get_reason_description(self, obj: UnavailableTimeWindow) -> str:
        if obj.reason == "calendar_event":
            event_data = cast(CalendarEventAdapterOutputData, obj.data)
            return event_data.title

        blocked_time_data = cast(BlockedTimeData, obj.data)
        return blocked_time_data.reason

    def to_representation(self, instance):
        """Render start_time/end_time in the underlying record's timezone, not UTC."""
        data = super().to_representation(instance)
        # UnavailableTimeWindow has no timezone of its own; take it from the
        # underlying event/blocked-time payload it wraps.
        tz_name = getattr(getattr(instance, "data", None), "timezone", None)
        return _localize_times_in_representation(data, instance, tz_name)


class BulkBlockedTimeSerializer(serializers.Serializer):
    """Serializer for creating multiple blocked times."""

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

        self.fields["blocked_times"] = BlockedTimeSerializer(many=True, context=self.context)

    def validate_blocked_times(self, blocked_times_data):
        """Validate bulk blocked times data."""
        if not blocked_times_data:
            raise serializers.ValidationError("At least one blocked time must be provided")

        # `calendar` is optional: when omitted on every item, save() falls back to the
        # user's default calendar. All items must agree on the calendar (including all
        # omitting it) — mixing a calendar with omissions is ambiguous and rejected.
        first_blocked_time_calendar = blocked_times_data[0].get("calendar")
        for blocked_time in blocked_times_data[1:]:
            if blocked_time.get("calendar") != first_blocked_time_calendar:
                raise serializers.ValidationError("All blocked times must be for the same calendar")

        return blocked_times_data

    def save(self, **kwargs):
        """Create multiple blocked times using calendar service."""
        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        user = self.context["request"].user
        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )

        # `organization` is the 2nd param (1st is user_or_token) — must be keyword,
        # else self.organization stays None and the type guard raises.
        self.calendar_service.initialize_without_provider(organization=membership.organization)

        # Convert to the format expected by bulk_create_manual_blocked_times
        blocked_times_tuples = [
            (bt["start_time"], bt["end_time"], bt["reason"], bt.get("rrule_string"))
            for bt in self.validated_data["blocked_times"]
        ]
        calendar = self.validated_data["blocked_times"][0].get("calendar")
        if calendar is None:
            # No calendar specified — fall back to the user's default calendar.
            calendar = self.calendar_service.get_default_calendar_for_user(user)
            if calendar is None:
                raise serializers.ValidationError(
                    {
                        "non_field_errors": [
                            "No calendar specified and you have no default calendar."
                        ]
                    }
                )

        blocked_times = self.calendar_service.bulk_create_manual_blocked_times(
            calendar=calendar, blocked_times=blocked_times_tuples
        )
        return list(blocked_times)


class AvailableTimeOperationSerializer(serializers.Serializer):
    """A single create/update/delete operation in an available-times batch."""

    action = serializers.ChoiceField(choices=["create", "update", "delete"])
    id = serializers.IntegerField(
        required=False, help_text="Target AvailableTime id (required for update/delete)."
    )
    start_time = serializers.DateTimeField(required=False)
    end_time = serializers.DateTimeField(required=False)
    timezone = serializers.CharField(required=False)
    rrule_string = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="RRULE string; null clears recurrence. Omit to leave unchanged on update.",
    )

    def validate(self, attrs):
        action = attrs["action"]

        if action == "create":
            if attrs.get("id") is not None:
                raise serializers.ValidationError("`id` is not allowed for create operations.")
            missing = [f for f in ("start_time", "end_time", "timezone") if attrs.get(f) is None]
            if missing:
                raise serializers.ValidationError(
                    f"create operations require: {', '.join(missing)}."
                )
        else:  # update / delete
            if attrs.get("id") is None:
                raise serializers.ValidationError(f"`id` is required for {action} operations.")

        start_time = attrs.get("start_time")
        end_time = attrs.get("end_time")
        if start_time and end_time and end_time <= start_time:
            raise serializers.ValidationError("end_time must be after start_time.")

        return attrs


class AvailableTimeBatchSerializer(serializers.Serializer):
    """Transactional batch of create/update/delete operations on a calendar's available times.

    All operations target a single calendar (resolved from ``calendar`` or the user's
    default) and run in one transaction — any failure rolls the whole batch back.
    """

    operations = AvailableTimeOperationSerializer(many=True)

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

        request = self.context.get("request") if self.context else None
        membership = request.__dict__.get("organization_membership") if request else None
        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(membership.organization_id)
                if membership
                else Calendar.original_manager.none()
            ),
            allow_null=True,
            required=False,
            help_text="Calendar to apply the batch to. Defaults to the user's default calendar.",
        )

    def validate_operations(self, operations):
        if not operations:
            raise serializers.ValidationError("At least one operation must be provided.")
        return operations

    def save(self, **kwargs):
        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        user = self.context["request"].user
        membership = self.context["request"].organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )

        self.calendar_service.initialize_without_provider(organization=membership.organization)

        calendar = self.validated_data.get("calendar")
        if calendar is None:
            # No calendar specified — fall back to the user's default calendar.
            calendar = self.calendar_service.get_default_calendar_for_user(user)
            if calendar is None:
                raise serializers.ValidationError(
                    {
                        "non_field_errors": [
                            "No calendar specified and you have no default calendar."
                        ]
                    }
                )

        try:
            self.calendar_service.batch_modify_available_times(
                calendar=calendar, operations=self.validated_data["operations"]
            )
        except ValueError as e:
            raise serializers.ValidationError({"non_field_errors": [str(e)]}) from e

        return calendar


class BlockedTimeRecurringExceptionSerializer(serializers.Serializer):
    """Serializer for creating recurring blocked time exceptions."""

    exception_date = serializers.DateField(
        required=True, help_text="The date of the occurrence to modify or cancel"
    )
    modified_reason = serializers.CharField(
        required=False,
        allow_null=True,
        max_length=255,
        help_text="New reason for the modified occurrence (if not cancelled)",
    )
    modified_start_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="New start time for the modified occurrence (if not cancelled)",
    )
    modified_end_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="New end time for the modified occurrence (if not cancelled)",
    )
    is_cancelled = serializers.BooleanField(
        default=False, help_text="True if cancelling the occurrence, False if modifying"
    )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

    def validate(self, attrs: dict) -> dict:
        """Validate the exception data."""
        is_cancelled = attrs.get("is_cancelled", False)

        if not is_cancelled:
            # If not cancelled, at least one modification field should be provided
            has_modifications = any(
                [
                    attrs.get("modified_reason"),
                    attrs.get("modified_start_time"),
                    attrs.get("modified_end_time"),
                ]
            )

            if not has_modifications:
                raise serializers.ValidationError(
                    "For non-cancelled exceptions, at least one modification field must be provided."
                )

        # Validate that start_time is before end_time if both are provided
        start_time = attrs.get("modified_start_time")
        end_time = attrs.get("modified_end_time")

        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError(
                "modified_start_time must be before modified_end_time."
            )

        return attrs

    def save(self, **kwargs) -> None:
        """Create a recurring event exception."""
        parent_blocked_time = self.context["parent_blocked_time"]

        if not self.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        # Initialize calendar service
        self.calendar_service.initialize_without_provider(
            organization=parent_blocked_time.organization,
        )

        # Convert date to datetime for the exception_date
        exception_date = self.validated_data["exception_date"]

        self.instance = self.calendar_service.create_recurring_blocked_time_exception(
            parent_blocked_time=parent_blocked_time,
            exception_date=exception_date,
            modified_reason=self.validated_data.get("modified_reason"),
            modified_start_time=self.validated_data.get("modified_start_time"),
            modified_end_time=self.validated_data.get("modified_end_time"),
            is_cancelled=self.validated_data.get("is_cancelled", False),
        )


class AvailableTimeRecurringExceptionSerializer(serializers.Serializer):
    """Serializer for creating recurring available time exceptions."""

    exception_date = serializers.DateField(
        required=True, help_text="The date of the occurrence to modify or cancel"
    )
    modified_start_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="New start time for the modified occurrence (if not cancelled)",
    )
    modified_end_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="New end time for the modified occurrence (if not cancelled)",
    )
    is_cancelled = serializers.BooleanField(
        default=False, help_text="True if cancelling the occurrence, False if modifying"
    )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: "CalendarService" = Provide["calendar_service"],
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

    def validate(self, attrs: dict) -> dict:
        """Validate the exception data."""
        is_cancelled = attrs.get("is_cancelled", False)

        if not is_cancelled:
            # If not cancelled, at least one modification field should be provided
            has_modifications = any(
                [
                    attrs.get("modified_start_time"),
                    attrs.get("modified_end_time"),
                ]
            )

            if not has_modifications:
                raise serializers.ValidationError(
                    "For non-cancelled exceptions, at least one modification field must be provided."
                )

        # Validate that start_time is before end_time if both are provided
        start_time = attrs.get("modified_start_time")
        end_time = attrs.get("modified_end_time")

        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError(
                "modified_start_time must be before modified_end_time."
            )

        return attrs

    def save(self, **kwargs) -> None:
        """Create a recurring event exception."""
        parent_available_time = self.context["parent_available_time"]

        if not self.calendar_service:
            raise ValueError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        # Initialize calendar service
        self.calendar_service.initialize_without_provider(
            organization=parent_available_time.organization,
        )

        # Convert date to datetime for the exception_date
        exception_date = self.validated_data["exception_date"]

        self.instance = self.calendar_service.create_recurring_available_time_exception(
            parent_available_time=parent_available_time,
            exception_date=exception_date,
            modified_start_time=self.validated_data.get("modified_start_time"),
            modified_end_time=self.validated_data.get("modified_end_time"),
            is_cancelled=self.validated_data.get("is_cancelled", False),
        )


class EventBulkModificationSerializer(serializers.Serializer):
    """Serializer for creating bulk modifications on recurring events from a given date."""

    modification_start_date = serializers.DateField(required=True)
    modified_title = serializers.CharField(required=False, allow_null=True)
    modified_description = serializers.CharField(required=False, allow_null=True)
    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for the modification range",
    )
    rrule_string = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="RRULE string for the modification range",
    )
    modified_start_time_offset = serializers.DurationField(required=False, allow_null=True)
    modified_end_time_offset = serializers.DurationField(required=False, allow_null=True)
    is_cancelled = serializers.BooleanField(default=False)

    def validate(self, attrs):
        """Validate bulk modification data."""
        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule and rrule_string. Use one or the other."
            )

        return attrs

    def save(self, **kwargs):
        parent_event = self.context["parent_event"]
        calendar_service = self.context.get("calendar_service")
        if not calendar_service:
            raise ValueError("calendar_service not provided in context")

        # Handle recurrence fields
        recurrence_rule_data = self.validated_data.get("recurrence_rule")
        rrule_string = self.validated_data.get("rrule_string")

        # Prepare final rrule string
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            temp_rule = RecurrenceRule(
                organization=parent_event.organization, **recurrence_rule_data
            )
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        # Build modification datetime from date and parent_event start_time timezone
        start_date = self.validated_data["modification_start_date"]
        modification_start_dt = datetime.datetime.combine(
            start_date, parent_event.start_time.time(), tzinfo=parent_event.start_time.tzinfo
        )

        return (
            calendar_service.modify_recurring_event_from_date(
                parent_event=parent_event,
                modification_start_date=modification_start_dt,
                modified_title=self.validated_data.get("modified_title"),
                modified_description=self.validated_data.get("modified_description"),
                modified_start_time_offset=self.validated_data.get("modified_start_time_offset"),
                modified_end_time_offset=self.validated_data.get("modified_end_time_offset"),
                modification_rrule_string=final_rrule_string,
            )
            if not self.validated_data.get("is_cancelled", False)
            else calendar_service.cancel_recurring_event_from_date(
                parent_event=parent_event,
                modification_start_date=modification_start_dt,
                modification_rrule_string=final_rrule_string,
            )
        )


class BlockedTimeBulkModificationSerializer(serializers.Serializer):
    modification_start_date = serializers.DateField(required=True)
    modified_reason = serializers.CharField(required=False, allow_null=True)
    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for the modification range",
    )
    rrule_string = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="RRULE string for the modification range",
    )
    modified_start_time_offset = serializers.DurationField(required=False, allow_null=True)
    modified_end_time_offset = serializers.DurationField(required=False, allow_null=True)
    is_cancelled = serializers.BooleanField(default=False)

    def validate(self, attrs):
        """Validate bulk modification data."""
        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule and rrule_string. Use one or the other."
            )

        return attrs

    def save(self, **kwargs):
        parent_blocked_time = self.context["parent_blocked_time"]
        calendar_service = self.context.get("calendar_service")
        if not calendar_service:
            raise ValueError("calendar_service not provided in context")

        # Handle recurrence fields
        recurrence_rule_data = self.validated_data.get("recurrence_rule")
        rrule_string = self.validated_data.get("rrule_string")

        # Prepare final rrule string
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            temp_rule = RecurrenceRule(
                organization=parent_blocked_time.organization, **recurrence_rule_data
            )
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        start_date = self.validated_data["modification_start_date"]
        modification_start_dt = datetime.datetime.combine(
            start_date,
            parent_blocked_time.start_time.time(),
            tzinfo=parent_blocked_time.start_time.tzinfo,
        )

        if self.validated_data.get("is_cancelled", False):
            return calendar_service.cancel_recurring_blocked_time_from_date(
                parent_blocked_time=parent_blocked_time,
                modification_start_date=modification_start_dt,
                modification_rrule_string=final_rrule_string,
            )

        return calendar_service.modify_recurring_blocked_time_from_date(
            parent_blocked_time=parent_blocked_time,
            modification_start_date=modification_start_dt,
            modified_reason=self.validated_data.get("modified_reason"),
            modified_start_time_offset=self.validated_data.get("modified_start_time_offset"),
            modified_end_time_offset=self.validated_data.get("modified_end_time_offset"),
            modification_rrule_string=final_rrule_string,
        )


class AvailableTimeBulkModificationSerializer(serializers.Serializer):
    modification_start_date = serializers.DateField(required=True)
    recurrence_rule = RecurrenceRuleSerializer(
        required=False,
        help_text="Recurrence rule data for the modification range",
    )
    rrule_string = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="RRULE string for the modification range",
    )
    modified_start_time_offset = serializers.DurationField(required=False, allow_null=True)
    modified_end_time_offset = serializers.DurationField(required=False, allow_null=True)
    is_cancelled = serializers.BooleanField(default=False)

    def validate(self, attrs):
        """Validate bulk modification data."""
        # Validate recurrence fields
        recurrence_rule_data = attrs.get("recurrence_rule")
        rrule_string = attrs.get("rrule_string")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule and rrule_string. Use one or the other."
            )

        return attrs

    def save(self, **kwargs):
        parent_available_time = self.context["parent_available_time"]
        calendar_service = self.context.get("calendar_service")
        if not calendar_service:
            raise ValueError("calendar_service not provided in context")

        # Handle recurrence fields
        recurrence_rule_data = self.validated_data.get("recurrence_rule")
        rrule_string = self.validated_data.get("rrule_string")

        # Prepare final rrule string
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            temp_rule = RecurrenceRule(
                organization=parent_available_time.organization, **recurrence_rule_data
            )
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        start_date = self.validated_data["modification_start_date"]
        modification_start_dt = datetime.datetime.combine(
            start_date,
            parent_available_time.start_time.time(),
            tzinfo=parent_available_time.start_time.tzinfo,
        )

        if self.validated_data.get("is_cancelled", False):
            return calendar_service.cancel_recurring_available_time_from_date(
                parent_available_time=parent_available_time,
                modification_start_date=modification_start_dt,
                modification_rrule_string=final_rrule_string,
            )

        return calendar_service.modify_recurring_available_time_from_date(
            parent_available_time=parent_available_time,
            modification_start_date=modification_start_dt,
            modified_start_time_offset=self.validated_data.get("modified_start_time_offset"),
            modified_end_time_offset=self.validated_data.get("modified_end_time_offset"),
            modification_rrule_string=final_rrule_string,
        )


# ---------------------------------------------------------------------------
# CalendarGroup REST serializers
# ---------------------------------------------------------------------------
def _translate_group_error(exc: CalendarGroupError) -> serializers.ValidationError:
    return serializers.ValidationError({"non_field_errors": [str(exc)]})


class CalendarGroupSlotMembershipSerializer(VirtualModelSerializer):
    calendar = CalendarSerializer(read_only=True)

    class Meta:
        model = CalendarGroupSlotMembership
        virtual_model = CalendarGroupSlotMembershipVirtualModel
        fields = ("id", "calendar")


class GroupScopedAvailabilityWindowSerializer(VirtualModelSerializer):
    """Read representation of a group-scoped availability window.

    Deliberately narrower than ``AvailableTimeSerializer``: there is no nested
    ``recurrence_rule`` write path here, only ``rrule_string`` -- matching
    exactly what ``CalendarGroupService``'s window-write methods accept, so the
    REST shape and the service signature cannot drift apart.
    """

    calendar_id = serializers.IntegerField(source="calendar_fk_id", read_only=True)
    group_slot_id = serializers.IntegerField(source="group_slot_fk_id", read_only=True)
    rrule_string = serializers.SerializerMethodField(read_only=True)
    is_recurring = serializers.SerializerMethodField(read_only=True)
    start_time = serializers.DateTimeField(read_only=True)
    end_time = serializers.DateTimeField(read_only=True)

    class Meta:
        model = AvailableTime
        virtual_model = GroupScopedAvailabilityWindowVirtualModel
        fields = (
            "id",
            "calendar_id",
            "group_slot_id",
            "start_time",
            "end_time",
            "timezone",
            "rrule_string",
            "is_recurring",
            "created",
            "modified",
        )
        read_only_fields = fields

    @v.hints.no_deferred_fields()
    def get_rrule_string(self, obj: AvailableTime) -> str | None:
        """RRULE string for the window's recurrence, or ``None`` when it doesn't recur."""
        return obj.recurrence_rule.to_rrule_string() if obj.recurrence_rule else None

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: AvailableTime) -> bool:
        return obj.is_recurring

    def to_representation(self, instance):
        """Render start_time/end_time in the window's own IANA timezone, not UTC."""
        data = super().to_representation(instance)
        return _localize_times_in_representation(
            data, instance, getattr(instance, "timezone", None)
        )


class GroupScopedAvailabilityWindowCreateSerializer(serializers.Serializer):
    """Input for creating a group-scoped availability window.

    Field names map 1:1 onto
    ``CalendarGroupService.create_group_scoped_availability_window``'s keyword
    arguments (``calendar_id``, ``start_time``, ``end_time``, ``tz``,
    ``rrule_string``) so the REST shape can never silently drift from the
    service signature it delegates to.
    """

    calendar = serializers.PrimaryKeyRelatedField(
        queryset=Calendar.original_manager.none(),
        help_text="Calendar this window applies to. Must be a member of the target slot.",
    )
    start_time = serializers.DateTimeField(required=True)
    end_time = serializers.DateTimeField(required=True)
    timezone = serializers.CharField(required=True)
    rrule_string = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="RRULE string for a recurring window. Omit for a one-off window.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        membership = request.__dict__.get("organization_membership") if request else None
        self.fields["calendar"].queryset = (
            Calendar.objects.filter_by_organization(membership.organization_id)
            if membership
            else Calendar.original_manager.none()
        )

    def validate(self, attrs):
        if attrs["start_time"] >= attrs["end_time"]:
            raise serializers.ValidationError("start_time must be before end_time.")
        return attrs


class GroupScopedAvailabilityWindowUpdateSerializer(serializers.Serializer):
    """Input for partially updating a group-scoped availability window.

    Every field is optional -- only provided fields change, mirroring
    ``CalendarGroupService.update_group_scoped_availability_window``.
    """

    start_time = serializers.DateTimeField(required=False)
    end_time = serializers.DateTimeField(required=False)
    timezone = serializers.CharField(required=False)
    rrule_string = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="RRULE string for a recurring window. Set to null to make it non-recurring.",
    )

    def validate(self, attrs):
        start_time = attrs.get("start_time")
        end_time = attrs.get("end_time")
        if start_time is not None and end_time is not None and start_time >= end_time:
            raise serializers.ValidationError("start_time must be before end_time.")
        return attrs


class GroupScopedAvailabilityOrphanedBookingSerializer(serializers.Serializer):
    """Minimal identification of a booking a narrowing write orphaned (spec UC-6)
    -- enough for an admin to act on. Nothing about the booking itself is
    modified by the write that produced this entry.
    """

    id = serializers.IntegerField(read_only=True)
    calendar_id = serializers.IntegerField(source="calendar_fk_id", read_only=True)
    title = serializers.CharField(read_only=True)
    start_time = serializers.DateTimeField(read_only=True)
    end_time = serializers.DateTimeField(read_only=True)


class GroupScopedAvailabilityWriteResultSerializer(serializers.Serializer):
    """Wraps a ``GroupScopedAvailabilityWriteResult``: the saved window plus any
    confirmed future bookings the write orphaned. Returned by the create and
    update actions of ``GroupScopedAvailabilityWindowViewSet``.
    """

    window = GroupScopedAvailabilityWindowSerializer(read_only=True)
    orphaned_bookings = GroupScopedAvailabilityOrphanedBookingSerializer(
        many=True,
        read_only=True,
        help_text=(
            "Confirmed future bookings in this slot for this window's calendar that no "
            "longer fall inside the calendar's group-scoped availability after this "
            "write. Nothing about them is modified or cancelled -- act on them manually "
            "if needed."
        ),
    )


class GroupScopedBlockedTimeSerializer(VirtualModelSerializer):
    """Read representation of a group-scoped blocked time.

    Mirrors ``GroupScopedAvailabilityWindowSerializer`` exactly, plus
    ``reason`` -- there is no nested ``recurrence_rule`` write path here,
    only ``rrule_string``, matching exactly what ``CalendarGroupService``'s
    block-write methods accept, so the REST shape and the service signature
    cannot drift apart.
    """

    calendar_id = serializers.IntegerField(source="calendar_fk_id", read_only=True)
    group_slot_id = serializers.IntegerField(source="group_slot_fk_id", read_only=True)
    rrule_string = serializers.SerializerMethodField(read_only=True)
    is_recurring = serializers.SerializerMethodField(read_only=True)
    start_time = serializers.DateTimeField(read_only=True)
    end_time = serializers.DateTimeField(read_only=True)

    class Meta:
        model = BlockedTime
        virtual_model = GroupScopedBlockedTimeVirtualModel
        fields = (
            "id",
            "calendar_id",
            "group_slot_id",
            "start_time",
            "end_time",
            "timezone",
            "reason",
            "rrule_string",
            "is_recurring",
            "created",
            "modified",
        )
        read_only_fields = fields

    @v.hints.no_deferred_fields()
    def get_rrule_string(self, obj: BlockedTime) -> str | None:
        """RRULE string for the block's recurrence, or ``None`` when it doesn't recur."""
        return obj.recurrence_rule.to_rrule_string() if obj.recurrence_rule else None

    @v.hints.no_deferred_fields()
    def get_is_recurring(self, obj: BlockedTime) -> bool:
        return obj.is_recurring

    def to_representation(self, instance):
        """Render start_time/end_time in the block's own IANA timezone, not UTC."""
        data = super().to_representation(instance)
        return _localize_times_in_representation(
            data, instance, getattr(instance, "timezone", None)
        )


class GroupScopedBlockedTimeCreateSerializer(serializers.Serializer):
    """Input for creating a group-scoped blocked time.

    Field names map 1:1 onto
    ``CalendarGroupService.create_group_scoped_blocked_time``'s keyword
    arguments (``calendar_id``, ``start_time``, ``end_time``, ``tz``,
    ``reason``, ``rrule_string``) so the REST shape can never silently drift
    from the service signature it delegates to.
    """

    calendar = serializers.PrimaryKeyRelatedField(
        queryset=Calendar.original_manager.none(),
        help_text="Calendar this block applies to. Must be a member of the target slot.",
    )
    start_time = serializers.DateTimeField(required=True)
    end_time = serializers.DateTimeField(required=True)
    timezone = serializers.CharField(required=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    rrule_string = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="RRULE string for a recurring block. Omit for a one-off block.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        membership = request.__dict__.get("organization_membership") if request else None
        self.fields["calendar"].queryset = (
            Calendar.objects.filter_by_organization(membership.organization_id)
            if membership
            else Calendar.original_manager.none()
        )

    def validate(self, attrs):
        if attrs["start_time"] >= attrs["end_time"]:
            raise serializers.ValidationError("start_time must be before end_time.")
        return attrs


class GroupScopedBlockedTimeUpdateSerializer(serializers.Serializer):
    """Input for partially updating a group-scoped blocked time.

    Every field is optional -- only provided fields change, mirroring
    ``CalendarGroupService.update_group_scoped_blocked_time``.
    """

    start_time = serializers.DateTimeField(required=False)
    end_time = serializers.DateTimeField(required=False)
    timezone = serializers.CharField(required=False)
    reason = serializers.CharField(required=False, allow_blank=True)
    rrule_string = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="RRULE string for a recurring block. Set to null to make it non-recurring.",
    )

    def validate(self, attrs):
        start_time = attrs.get("start_time")
        end_time = attrs.get("end_time")
        if start_time is not None and end_time is not None and start_time >= end_time:
            raise serializers.ValidationError("start_time must be before end_time.")
        return attrs


class GroupScopedBlockOrphanedBookingSerializer(serializers.Serializer):
    """Minimal identification of a booking a block write orphaned (spec
    UC-6's rule applied to blocks) -- enough for an admin to act on. Nothing
    about the booking itself is modified by the write that produced this
    entry.
    """

    id = serializers.IntegerField(read_only=True)
    calendar_id = serializers.IntegerField(source="calendar_fk_id", read_only=True)
    title = serializers.CharField(read_only=True)
    start_time = serializers.DateTimeField(read_only=True)
    end_time = serializers.DateTimeField(read_only=True)


class GroupScopedBlockWriteResultSerializer(serializers.Serializer):
    """Wraps a ``GroupScopedBlockWriteResult``: the saved block plus any
    confirmed future bookings the write orphaned. Returned by the create and
    update actions of ``GroupScopedBlockedTimeViewSet``.
    """

    block = GroupScopedBlockedTimeSerializer(read_only=True)
    orphaned_bookings = GroupScopedBlockOrphanedBookingSerializer(
        many=True,
        read_only=True,
        help_text=(
            "Confirmed future bookings in this slot for this block's calendar that now "
            "fall inside the calendar's group-scoped blocked time after this write. "
            "Nothing about them is modified or cancelled -- act on them manually if "
            "needed."
        ),
    )


class GroupScopedQuotaRuleSerializer(VirtualModelSerializer):
    """Read representation of a group-scoped quota rule.

    Simpler than ``GroupScopedAvailabilityWindowSerializer``/
    ``GroupScopedBlockedTimeSerializer``: quota rules are non-recurring (no
    ``rrule_string``, no ``timezone``, no time range) -- just the period and
    the cap, matching exactly what ``CalendarGroupService``'s quota-write
    methods accept, so the REST shape and the service signature cannot drift
    apart.
    """

    calendar_id = serializers.IntegerField(source="calendar_fk_id", read_only=True)
    group_slot_id = serializers.IntegerField(source="group_slot_fk_id", read_only=True)

    class Meta:
        model = CalendarGroupSlotQuotaRule
        virtual_model = GroupScopedQuotaRuleVirtualModel
        fields = (
            "id",
            "calendar_id",
            "group_slot_id",
            "period",
            "cap",
            "created",
            "modified",
        )
        read_only_fields = fields


class GroupScopedQuotaRuleCreateSerializer(serializers.Serializer):
    """Input for creating a group-scoped quota rule.

    Field names map 1:1 onto
    ``CalendarGroupService.create_group_scoped_quota_rule``'s keyword
    arguments (``calendar_id``, ``period``, ``cap``) so the REST shape can
    never silently drift from the service signature it delegates to.
    """

    calendar = serializers.PrimaryKeyRelatedField(
        queryset=Calendar.original_manager.none(),
        help_text="Calendar this quota rule applies to. Must be a member of the target slot.",
    )
    period = serializers.ChoiceField(
        choices=QuotaPeriod.choices,
        help_text="Fixed calendar period the cap applies to (day, week, or month).",
    )
    cap = serializers.IntegerField(
        min_value=1,
        help_text=(
            "Maximum number of live bookings made through this group slot the "
            "calendar may hold within one period."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request") if self.context else None
        membership = request.__dict__.get("organization_membership") if request else None
        self.fields["calendar"].queryset = (
            Calendar.objects.filter_by_organization(membership.organization_id)
            if membership
            else Calendar.original_manager.none()
        )


class GroupScopedQuotaRuleUpdateSerializer(serializers.Serializer):
    """Input for partially updating a group-scoped quota rule.

    Every field is optional -- only provided fields change, mirroring
    ``CalendarGroupService.update_group_scoped_quota_rule``.
    """

    period = serializers.ChoiceField(choices=QuotaPeriod.choices, required=False)
    cap = serializers.IntegerField(min_value=1, required=False)


class CalendarGroupSlotSerializer(VirtualModelSerializer):
    """Nested slot representation used inside CalendarGroupSerializer.

    On write, accepts `calendar_ids: list[int]`; on read exposes the calendar
    pool via `calendars` (the M2M). We deliberately keep slot writes to
    payload-time data only — persistence happens through
    `CalendarGroupSerializer` which delegates to `CalendarGroupService`.
    """

    calendars = CalendarSerializer(many=True, read_only=True)
    calendar_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=True
    )

    class Meta:
        model = CalendarGroupSlot
        virtual_model = CalendarGroupSlotVirtualModel
        fields = (
            "id",
            "name",
            "description",
            "order",
            "required_count",
            "calendars",
            "calendar_ids",
        )
        read_only_fields = ("id",)


class CalendarGroupSerializer(VirtualModelSerializer):
    slots = CalendarGroupSlotSerializer(many=True)

    class Meta:
        model = CalendarGroup
        virtual_model = CalendarGroupVirtualModel
        fields = (
            "id",
            "name",
            "description",
            "slots",
            "public_booking_slug",
            "created",
            "modified",
        )
        # public_booking_slug: read-only so an organization admin can read it
        # to build a codeless public booking link
        # (public/booking/calendar-groups/<public_booking_slug>/events/), but
        # it is never client-settable -- it is generated once, at model
        # creation, by CalendarGroup's own field default (Phase 3b).
        read_only_fields = ("id", "created", "modified", "public_booking_slug")

    @inject
    def __init__(
        self,
        *args,
        calendar_group_service: Annotated[
            "CalendarGroupService | None", Provide["calendar_group_service"]
        ] = None,
        **kwargs,
    ):
        self.calendar_group_service = calendar_group_service
        super().__init__(*args, **kwargs)

    def _organization(self) -> Organization:
        request = self.context.get("request") if self.context else None
        if not request or not getattr(request, "user", None):
            raise serializers.ValidationError(
                {"non_field_errors": ["Authenticated user with organization is required."]}
            )
        membership = request.organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )
        return membership.organization

    def _to_input_data(self, validated_data: dict) -> CalendarGroupInputData:
        return CalendarGroupInputData(
            name=validated_data["name"],
            description=validated_data.get("description", ""),
            slots=[
                CalendarGroupSlotInputData(
                    name=slot["name"],
                    calendar_ids=list(slot["calendar_ids"]),
                    required_count=slot.get("required_count", 1),
                    description=slot.get("description", ""),
                    order=slot.get("order", 0),
                )
                for slot in validated_data.get("slots", [])
            ],
        )

    def create(self, validated_data: dict) -> CalendarGroup:
        if not self.calendar_group_service:
            raise CalendarServiceNotInjectedError(
                "calendar_group_service is not defined; configure the DI container."
            )
        organization = self._organization()
        self.calendar_group_service.initialize(organization=organization)
        try:
            return self.calendar_group_service.create_group(self._to_input_data(validated_data))
        except CalendarGroupError as e:
            raise _translate_group_error(e) from e

    def update(self, instance: CalendarGroup, validated_data: dict) -> CalendarGroup:
        if not self.calendar_group_service:
            raise CalendarServiceNotInjectedError(
                "calendar_group_service is not defined; configure the DI container."
            )
        organization = self._organization()
        self.calendar_group_service.initialize(organization=organization)
        try:
            return self.calendar_group_service.update_group(
                group_id=instance.id, data=self._to_input_data(validated_data)
            )
        except CalendarGroupError as e:
            raise _translate_group_error(e) from e


class CalendarEventGroupSelectionSerializer(VirtualModelSerializer):
    slot = CalendarGroupSlotSerializer(read_only=True)
    calendar = CalendarSerializer(read_only=True)

    class Meta:
        model = CalendarEventGroupSelection
        virtual_model = CalendarEventGroupSelectionVirtualModel
        fields = ("id", "slot", "calendar")


class _CalendarGroupSlotSelectionInputSerializer(serializers.Serializer):
    slot_id = serializers.IntegerField()
    calendar_ids = serializers.ListField(child=serializers.IntegerField())


class _EndTimeAfterStartTimeSerializerMixin(serializers.Serializer):
    """Shared ``validate_end_time`` rejecting an ``end_time`` at/before ``start_time``.

    Parses a string ``start_time`` from ``initial_data`` -- at the point
    ``validate_end_time`` runs, DRF has not yet validated/coerced sibling
    fields, so ``start_time`` is read from the raw input instead of
    ``validated_data``. Silently skips the check when ``start_time`` is
    missing or unparsable -- the field-level validator for ``start_time``
    surfaces that failure separately.
    """

    def validate_end_time(self, end_time: datetime.datetime) -> datetime.datetime:
        start_time = self.initial_data.get("start_time") if self.initial_data else None
        if start_time:
            try:
                start_time_parsed = (
                    datetime.datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    if isinstance(start_time, str)
                    else start_time
                )
            except ValueError:
                start_time_parsed = None
            if start_time_parsed and end_time <= start_time_parsed:
                raise serializers.ValidationError("end_time must be after start_time.")
        return end_time


class CalendarGroupEventCreateSerializer(_EndTimeAfterStartTimeSerializerMixin):
    """Input for booking an event through a CalendarGroup.

    On `save()` this delegates to `CalendarGroupService.create_grouped_event`
    and returns the created `CalendarEvent`. The view is responsible for
    serializing the result (typically with `CalendarEventSerializer`).
    """

    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False, default="")
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    timezone = serializers.CharField()
    slot_selections = _CalendarGroupSlotSelectionInputSerializer(many=True)
    attendances = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    external_attendances = serializers.ListField(
        child=serializers.DictField(), required=False, default=list
    )

    @inject
    def __init__(
        self,
        *args,
        calendar_group_service: Annotated[
            "CalendarGroupService | None", Provide["calendar_group_service"]
        ] = None,
        **kwargs,
    ):
        self.calendar_group_service = calendar_group_service
        super().__init__(*args, **kwargs)

    def save(self, **kwargs):
        if not self.calendar_group_service or not self.calendar_group_service.calendar_service:
            raise CalendarServiceNotInjectedError(
                "calendar_group_service / calendar_service not defined; configure the DI container."
            )
        group = kwargs.get("group")
        if group is None:
            raise serializers.ValidationError(
                {"non_field_errors": ["group is required via serializer.save(group=…)"]}
            )
        request = self.context.get("request") if self.context else None
        if not request or not getattr(request, "user", None):
            raise serializers.ValidationError(
                {"non_field_errors": ["Authenticated user with organization is required."]}
            )
        membership = request.organization_membership
        if not membership:
            raise serializers.ValidationError(
                {"non_field_errors": ["User has no organization membership."]}
            )
        organization = membership.organization

        # Initialize the nested CalendarService on the group service — the
        # grouped-event flow delegates to `self.calendar_group_service.calendar_service.create_event`
        # internally, so that exact instance needs to be initialized.
        self.calendar_group_service.calendar_service.initialize_without_provider(
            user_or_token=request.user, organization=organization
        )
        self.calendar_group_service.initialize(organization=organization)

        data = CalendarGroupEventInputData(
            title=self.validated_data["title"],
            description=self.validated_data.get("description", ""),
            start_time=self.validated_data["start_time"],
            end_time=self.validated_data["end_time"],
            timezone=self.validated_data["timezone"],
            group_id=group.id,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=s["slot_id"], calendar_ids=list(s["calendar_ids"])
                )
                for s in self.validated_data["slot_selections"]
            ],
            attendances=[
                EventAttendanceInputData(user_id=a["user_id"])
                for a in self.validated_data.get("attendances", [])
            ],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=e["external_attendee"]["email"],
                        name=e["external_attendee"].get("name", ""),
                        id=e["external_attendee"].get("id"),
                    )
                )
                for e in self.validated_data.get("external_attendances", [])
            ],
        )
        try:
            return self.calendar_group_service.create_grouped_event(data)
        except CalendarGroupError as e:
            raise _translate_group_error(e) from e


class CalendarGroupSlotAvailabilitySerializer(serializers.Serializer):
    slot_id = serializers.IntegerField()
    available_calendar_ids = serializers.ListField(child=serializers.IntegerField())
    required_count = serializers.IntegerField()
    is_bookable = serializers.SerializerMethodField()

    def get_is_bookable(self, obj) -> bool:
        return len(obj["available_calendar_ids"]) >= obj["required_count"]


class CalendarGroupRangeAvailabilitySerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    slots = CalendarGroupSlotAvailabilitySerializer(many=True)


class _RangeInputSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()


class CalendarGroupAvailabilityQuerySerializer(serializers.Serializer):
    """Input for the availability action: list of [start, end] windows."""

    ranges = _RangeInputSerializer(many=True)


class BookableSlotProposalSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()


class BookingPolicySerializer(serializers.ModelSerializer):
    """Serializer for ``BookingPolicy`` CRUD.

    Exactly one of ``calendar``, ``membership_user_id``, ``calendar_group``, or
    ``is_organization_default`` must be set on create.  Targets are immutable
    after creation — only the four rule-field seconds are writable on update.

    Validation:
    - ``validate()`` enforces the exactly-one-target invariant on create.
    - ``validate_membership_user_id()`` checks that the supplied user id belongs
      to the caller's organization (on create only; targets are immutable on update).
    - ``DuplicateBookingPolicyError`` from the service is caught and surfaced as
      a 400 validation error so the client gets a named conflict message.
    - The four rule fields use ``min_value=0`` so DRF rejects negatives with a
      clear field-level 400 before the value reaches the model's
      ``PositiveIntegerField`` constraint.

    Write paths (create / update) delegate to ``BookingPolicyService`` stored on
    the serializer context as ``"booking_policy_service"`` (the viewset sets it).
    """

    # Rule fields — the effective guard is DRF's min_value=0; the model's
    # PositiveIntegerField is a secondary DB-level constraint.
    lead_time_seconds = serializers.IntegerField(min_value=0, default=0)
    max_horizon_seconds = serializers.IntegerField(min_value=0, default=0)
    buffer_before_seconds = serializers.IntegerField(min_value=0, default=0)
    buffer_after_seconds = serializers.IntegerField(min_value=0, default=0)

    # membership_user_id is a denormalized integer FK (not a model FK field) —
    # expose it directly.  Nullable so the client can omit it when another target
    # is set.
    membership_user_id = serializers.IntegerField(required=False, allow_null=True, default=None)

    # is_organization_default default False so the client can omit it.
    is_organization_default = serializers.BooleanField(default=False)

    class Meta:
        model = BookingPolicy
        fields = (
            "id",
            "calendar",
            "calendar_group",
            "membership_user_id",
            "is_organization_default",
            "lead_time_seconds",
            "max_horizon_seconds",
            "buffer_before_seconds",
            "buffer_after_seconds",
            "created",
            "modified",
        )
        read_only_fields = ("id", "created", "modified")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Build org-scoped querysets for FK fields; fall back to empty querysets
        # when there is no authenticated user with a membership (anonymous or
        # membership-less callers are rejected at the permission layer anyway).
        request = self.context.get("request")
        membership = request.__dict__.get("organization_membership") if request else None
        org_id = membership.organization_id if membership else None

        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(org_id)
                if org_id is not None
                else Calendar.original_manager.none()
            ),
            required=False,
            allow_null=True,
        )
        self.fields["calendar_group"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                CalendarGroup.objects.filter_by_organization(org_id)
                if org_id is not None
                else CalendarGroup.original_manager.none()
            ),
            required=False,
            allow_null=True,
        )

    def validate_membership_user_id(self, value):
        """Reject a ``membership_user_id`` that is not a member of the caller's org.

        Skipped on update (targets are immutable and already stripped in ``validate``).
        A bogus or cross-org id would otherwise reach the composite PROTECT FK
        at commit time and raise an ``IntegrityError`` (HTTP 500); returning it here
        gives a clean 400 instead.
        """
        if value is None:
            return value
        if self.instance is not None:
            # Update path — target fields are immutable; skip the check.
            return value
        request = self.context.get("request")
        membership = request.__dict__.get("organization_membership") if request else None
        if membership is None:
            # No org context — permission layer will deny the request.
            return value
        if not OrganizationMembership.objects.filter(
            organization_id=membership.organization_id, user_id=value
        ).exists():
            raise serializers.ValidationError(
                "No membership with this user id in your organization."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        """Enforce the exactly-one-target invariant on creates only.

        Targets are immutable after creation: the service's ``update_booking_policy``
        only accepts rule-field changes, so we skip the target check on updates
        (``self.instance is not None``).
        """
        if self.instance is not None:
            # Update path — targets cannot change; strip them from attrs so the
            # service update method only sees rule-field changes.
            attrs.pop("calendar", None)
            attrs.pop("calendar_group", None)
            attrs.pop("membership_user_id", None)
            attrs.pop("is_organization_default", None)
            return attrs

        # Create path — exactly one target must be set.
        calendar = attrs.get("calendar")
        membership_user_id = attrs.get("membership_user_id")
        calendar_group = attrs.get("calendar_group")
        is_org_default = attrs.get("is_organization_default", False)

        target_count = sum(
            [
                calendar is not None,
                membership_user_id is not None,
                calendar_group is not None,
                bool(is_org_default),
            ]
        )

        if target_count != 1:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Exactly one of 'calendar', 'membership_user_id', 'calendar_group', "
                        "or 'is_organization_default' must be set."
                    ]
                }
            )

        return attrs

    def create(self, validated_data: dict) -> BookingPolicy:
        """Delegate to ``BookingPolicyService.create_booking_policy``."""
        service = self.context.get("booking_policy_service")
        if service is None:
            raise CalendarServiceNotInjectedError(
                "booking_policy_service is not in serializer context. "
                "The viewset must set it before calling serializer.save()."
            )

        try:
            return service.create_booking_policy(
                calendar=validated_data.get("calendar"),
                membership_user_id=validated_data.get("membership_user_id"),
                calendar_group=validated_data.get("calendar_group"),
                is_organization_default=validated_data.get("is_organization_default", False),
                lead_time_seconds=validated_data.get("lead_time_seconds", 0),
                max_horizon_seconds=validated_data.get("max_horizon_seconds", 0),
                buffer_before_seconds=validated_data.get("buffer_before_seconds", 0),
                buffer_after_seconds=validated_data.get("buffer_after_seconds", 0),
            )
        except DuplicateBookingPolicyError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc

    def update(self, instance: BookingPolicy, validated_data: dict) -> BookingPolicy:
        """Delegate to ``BookingPolicyService.update_booking_policy`` (rule fields only)."""
        service = self.context.get("booking_policy_service")
        if service is None:
            raise CalendarServiceNotInjectedError(
                "booking_policy_service is not in serializer context. "
                "The viewset must set it before calling serializer.save()."
            )

        return service.update_booking_policy(
            instance,
            lead_time_seconds=validated_data.get("lead_time_seconds"),
            max_horizon_seconds=validated_data.get("max_horizon_seconds"),
            buffer_before_seconds=validated_data.get("buffer_before_seconds"),
            buffer_after_seconds=validated_data.get("buffer_after_seconds"),
        )


class ExternalEventChangeRequestSerializer(VirtualModelSerializer):
    """Read-only serializer for ``ExternalEventChangeRequest``.

    Exposes the fields needed by the first-party frontend to list, approve,
    and reject change requests.  The ``resolved_by`` field surfaces only the
    user id and display name — never raw membership / organization ids — to
    avoid leaking cross-tenant identity data.
    """

    event_id = serializers.IntegerField(source="event_fk_id", read_only=True)
    resolved_by_user_id = serializers.SerializerMethodField()

    class Meta:
        model = ExternalEventChangeRequest
        fields = (
            "id",
            "event_id",
            "kind",
            "status",
            "provider",
            "proposed_values",
            "retained_values",
            "resolved_by_user_id",
            "resolved_at",
            "created",
        )
        read_only_fields = fields
        virtual_model = ExternalEventChangeRequestVirtualModel

    def get_resolved_by_user_id(self, obj: ExternalEventChangeRequest) -> int | None:
        """Return the resolver's user id, or ``None`` when unresolved."""
        return obj.resolved_by_user_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Booking-code REST surface (public/booking/) -- Phase 1+
# ---------------------------------------------------------------------------


class _BookingCodeExternalAttendeeSerializer(serializers.Serializer):
    """External attendee for the unauthenticated booking-code write endpoints.

    Deliberately NOT ``ExternalAttendeeSerializer`` (a full ``VirtualModelSerializer``
    exposing read-only model fields like ``id`` / ``created`` / ``modified``) -- this
    is a plain two-field write input, mirroring ``ExternalAttendeeCodeInput`` (the
    GraphQL input the code-gated mutations already use).
    """

    email = serializers.EmailField()
    name = serializers.CharField(required=False, allow_blank=True, default="")


class BookingCodeEventCreateSerializer(_EndTimeAfterStartTimeSerializerMixin):
    """Input for ``POST /public/booking/calendar-events/``.

    Mirrors ``CreateEventWithCodeInput`` (GraphQL) minus its ``code`` field -- the
    booking code travels as the ``X-Booking-Code`` header instead (see
    ``calendar_integration.booking_auth``). ``calendar_id`` is never accepted here:
    it comes strictly from the resolved token, never from client input.
    """

    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False, default="")
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    timezone = serializers.CharField()
    external_attendee = _BookingCodeExternalAttendeeSerializer()


class BookingCodeGroupEventCreateSerializer(_EndTimeAfterStartTimeSerializerMixin):
    """Input for ``POST /public/booking/calendar-groups/<public_slug>/events/``.

    Mirrors ``CreateGroupEventWithCodeInput`` (GraphQL) minus its ``code`` field --
    the booking code travels as the ``X-Booking-Code`` header instead (see
    ``calendar_integration.booking_auth``). The group is never accepted here: on
    the coded branch it comes strictly from the resolved token's
    ``calendar_group``, never from client input or the path (Phase 3b: the path
    carries only ``CalendarGroup.public_booking_slug``, an opaque identifier, and
    even that is only a routing convenience -- see
    ``BookingCodeGroupEventViewSet.create``). Reuses
    ``_CalendarGroupSlotSelectionInputSerializer``, the same slot-selection shape
    ``CalendarGroupEventCreateSerializer`` already uses for the authenticated
    group-booking endpoint.
    """

    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False, default="")
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    timezone = serializers.CharField()
    slot_selections = _CalendarGroupSlotSelectionInputSerializer(many=True)
    external_attendee = _BookingCodeExternalAttendeeSerializer()


class BookingCodeRescheduleSerializer(_EndTimeAfterStartTimeSerializerMixin):
    """Input for ``POST /public/booking/events/reschedule/`` and
    ``POST /public/booking/group-events/reschedule/``.

    Mirrors ``RescheduleWithCodeInput`` / ``RescheduleGroupWithCodeInput`` (GraphQL)
    minus their ``code`` field -- the booking code travels as the ``X-Booking-Code``
    header instead. Only the new start/end/timezone are accepted: title, description,
    attendees, and resource allocations are never client-settable here -- the view
    snapshots them, unchanged, from the existing event so that only the RESCHEDULE
    permission is required (see ``BookingCodeRescheduleEventViewSet.create``).
    """

    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    timezone = serializers.CharField()


class BookingCodeCreateSerializer(serializers.Serializer):
    """Input for ``POST /booking-codes/`` -- the authenticated minting endpoint.

    Collapses GraphQL's six ``create*BookingCode`` mutations into one resource:
    ``purpose`` x {``calendar``, ``calendar_group``} is the same cross product those
    six mutations cover, no more and no less. ``purpose`` maps onto the permission(s)
    the minted token carries:

    - ``book`` -> ``[EventManagementPermissions.CREATE]``
    - ``reschedule`` -> ``[EventManagementPermissions.RESCHEDULE]``
    - ``cancel`` -> ``[EventManagementPermissions.CANCEL]``

    ``calendar`` / ``calendar_group`` / ``event`` are plain integer ids, not
    ``PrimaryKeyRelatedField`` -- object existence and org/authorization checks
    happen in ``BookingCodeViewSet.create`` so a cross-organization target can be
    answered ``404`` there rather than a serializer-level ``400``, keeping "target
    exists but you can't see it" indistinguishable from "target does not exist".

    ``duration_seconds`` is the only way to mint a code that pins the event
    duration -- GraphQL's mint mutations are deliberately unchanged.
    """

    PURPOSE_BOOK = "book"
    PURPOSE_RESCHEDULE = "reschedule"
    PURPOSE_CANCEL = "cancel"
    PURPOSE_CHOICES = (PURPOSE_BOOK, PURPOSE_RESCHEDULE, PURPOSE_CANCEL)

    purpose = serializers.ChoiceField(choices=PURPOSE_CHOICES)
    calendar = serializers.IntegerField(required=False, allow_null=True, default=None)
    calendar_group = serializers.IntegerField(required=False, allow_null=True, default=None)
    event = serializers.IntegerField(required=False, allow_null=True, default=None)
    expires_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    duration_seconds = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1
    )

    def validate(self, attrs: dict) -> dict:
        calendar = attrs.get("calendar")
        calendar_group = attrs.get("calendar_group")
        if (calendar is None) == (calendar_group is None):
            raise serializers.ValidationError(
                "Exactly one of 'calendar' or 'calendar_group' must be set."
            )

        purpose = attrs["purpose"]
        event = attrs.get("event")
        if purpose == self.PURPOSE_BOOK and event is not None:
            raise serializers.ValidationError("'event' is forbidden for purpose='book'.")
        if purpose in (self.PURPOSE_RESCHEDULE, self.PURPOSE_CANCEL) and event is None:
            raise serializers.ValidationError(f"'event' is required for purpose='{purpose}'.")

        expires_at = attrs.get("expires_at")
        if expires_at is not None and expires_at <= timezone.now():
            raise serializers.ValidationError("'expires_at' must be in the future.")

        if purpose == self.PURPOSE_CANCEL and attrs.get("duration_seconds") is not None:
            raise serializers.ValidationError(
                "'duration_seconds' is forbidden for purpose='cancel'."
            )

        return attrs


class BookingCodeCreateResultSerializer(serializers.Serializer):
    """One-time response for ``POST /booking-codes/``.

    ``code`` carries the plaintext booking code -- it is generated fresh by
    ``CalendarPermissionService.create_booking_token`` and never persisted or
    re-derivable, so this is the only response that will ever expose it. Every
    field here is read-only: this serializer only ever renders a response, it
    is never used to validate input.
    """

    id = serializers.IntegerField(read_only=True)
    code = serializers.CharField(read_only=True)
    purpose = serializers.ChoiceField(choices=BookingCodeCreateSerializer.PURPOSE_CHOICES)
    calendar = serializers.IntegerField(read_only=True, allow_null=True)
    calendar_group = serializers.IntegerField(read_only=True, allow_null=True)
    event = serializers.IntegerField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    duration_seconds = serializers.IntegerField(read_only=True, allow_null=True)
