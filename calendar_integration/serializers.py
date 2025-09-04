import datetime
import zoneinfo
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, TypedDict, cast

from django.core.exceptions import ValidationError as DjangoValidationError

import django_virtual_models as v
from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
    ResourceAllocation,
)
from calendar_integration.services.dataclasses import (
    BlockedTimeData,
    CalendarEventData,
    CalendarEventInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
    UnavailableTimeWindow,
)
from calendar_integration.virtual_models import (
    AvailableTimeVirtualModel,
    BlockedTimeVirtualModel,
    CalendarEventVirtualModel,
    CalendarOwnershipVirtualModel,
    CalendarVirtualModel,
    EventAttendanceVirtualModel,
    EventExternalAttendanceVirtualModel,
    EventRecurrenceExceptionVirtualModel,
    ExternalAttendeeVirtualModel,
    RecurrenceRuleVirtualModel,
    ResourceAllocationVirtualModel,
)
from common.utils.serializer_utils import VirtualModelSerializer
from users.models import User
from users.serializers import UserSerializer


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service import CalendarService


class CalendarOwnershipSerializer(VirtualModelSerializer):
    class Meta:
        model = CalendarOwnership
        virtual_model = CalendarOwnershipVirtualModel
        fields = (
            "id",
            "user",
            "calendar",
            "is_default",
            "created",
            "modified",
        )


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
        )
        read_only_fields = (
            "email",
            "external_id",
            "provider",
            "calendar_type",
            "capacity",
        )

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)

    def create(self, validated_data):
        user = self.context["request"].user
        organization = user.organization_membership.organization
        self.calendar_service.initialize_without_provider(organization)
        return self.calendar_service.create_virtual_calendar(
            name=validated_data.get("name"),
            description=validated_data.get("description"),
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
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        user = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )

        self.fields["bundle_calendars"] = serializers.PrimaryKeyRelatedField(
            many=True,
            queryset=(
                Calendar.objects.filter_by_organization(
                    organization_id=user.organization_membership.organization_id
                )
                if user
                and user.is_authenticated
                and hasattr(user, "organization_membership")
                and user.organization_membership
                else Calendar.original_manager.none()
            ),
        )
        self.fields["primary_calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    organization_id=user.organization_membership.organization_id
                )
                if user
                and user.is_authenticated
                and hasattr(user, "organization_membership")
                and user.organization_membership
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
        user = self.context["request"].user
        organization = user.organization_membership.organization
        self.calendar_service.initialize_without_provider(organization)

        return self.calendar_service.create_bundle_calendar(
            name=validated_data.get("name"),
            description=validated_data.get("description"),
            child_calendars=validated_data.get("bundle_calendars"),
            primary_calendar=validated_data.get("primary_calendar"),
        )


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
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
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

        user = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )

        if not self.calendar_service:
            raise ValueError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        # Initialize calendar service
        self.calendar_service.authenticate(
            account=SocialAccount.objects.get(user=user, provider=parent_event.calendar.provider),
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


class ExternalAttendeeSerializer(VirtualModelSerializer):
    id = serializers.IntegerField(  # noqa: A003
        allow_null=True, required=False, help_text="ID of the external attendee."
    )

    class Meta:
        model = ExternalAttendee
        virtual_model = ExternalAttendeeVirtualModel
        fields = (
            "id",
            "name",
            "email",
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
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source="user", queryset=User.objects.all(), required=True, allow_null=False, write_only=True
    )

    class Meta:
        model = EventAttendance
        virtual_model = EventAttendanceVirtualModel
        fields = (
            "id",
            "user",
            "user_id",
            "status",
            "created",
            "modified",
        )
        read_only_fields = (
            "user",
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
        user = getattr(self.context.get("request"), "user", None)
        # add calendar field dynamically to filter by organization_id
        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).filter(
                    calendar_type=CalendarType.RESOURCE,
                )
                if user and user.is_authenticated
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
        user = self.context.get("request").user
        # add parent_event field dynamically to filter by organization_id
        self.fields["parent_event"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                CalendarEvent.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).all()
                if user.is_authenticated
                else CalendarEvent.original_manager.none()
            ),
            required=False,
            allow_null=True,
        )
        self.fields["modified_event"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                CalendarEvent.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).all()
                if user.is_authenticated
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
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        user = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )

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
                    RecurrenceRule.objects.filter_by_organization(
                        user.organization_membership.organization_id
                    ).all()
                    if user and user.is_authenticated and user.organization_membership
                    else RecurrenceRule.original_manager.none()
                ),
                write_only=True,
            )

        # add google_calendar_service_account and calendar fields dynamically to filter by
        # organization_id
        self.fields["google_calendar_service_account"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                GoogleCalendarServiceAccount.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).all()
                if user and user.is_authenticated and user.organization_membership
                else GoogleCalendarServiceAccount.original_manager.none()
            ),
            required=False,
            write_only=True,
        )
        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).all()
                if user and user.is_authenticated and user.organization_membership
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

        user = self.context["request"].user
        if not SocialAccount.objects.filter(user=user, provider=provider).exists():
            raise serializers.ValidationError(
                "User does not have a social account from the selected provider linked."
            )

        return provider

    def validated_start_time(self, start_time):
        if start_time <= datetime.datetime.now(tz=datetime.UTC):
            raise serializers.ValidationError("Start time must be in the future.")

        return start_time

    def validate(self, attrs):
        calendar = attrs.get("calendar")
        if (
            not calendar
            and not attrs.get("provider")
            and not attrs.get("google_calendar_service_account")
        ):
            raise serializers.ValidationError(
                "You need to select either a calendar, provider, or a service account to create "
                "an event."
            )

        if attrs["start_time"] >= attrs["end_time"]:
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

        if not calendar:
            user = self.context["request"].user
            organization_id = user.organization_membership.organization_id
            if attrs.get("provider"):
                attrs["calendar"] = CalendarOwnership.objects.filter(
                    organization=organization_id,
                    calendar__provider=attrs.get("provider"),
                    is_default=True,
                ).first()
            if not attrs.get("google_calendar_service_account"):
                attrs["calendar"] = attrs.get("google_calendar_service_account").calendar

        return attrs

    def create(self, validated_data):
        if not self.calendar_service:
            raise ValueError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        calendar: Calendar = validated_data.pop("calendar")
        user = self.context["request"].user
        if validated_data.get("google_calendar_service_account"):
            account = validated_data.get("google_calendar_service_account")
        else:
            account = SocialAccount.objects.filter(
                user=user,
                provider=calendar.provider,
            ).first()
        self.calendar_service.authenticate(
            account=account,
            organization=calendar.organization,
        )

        resource_allocations = validated_data.pop("resource_allocations", [])
        attendances = validated_data.pop("attendances", [])
        external_attendances = validated_data.pop("external_attendances", [])

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
                        )
                    )
                    for ext in external_attendances
                ],
                # Recurrence fields
                recurrence_rule=final_rrule_string,
                parent_event_id=parent_recurring_object_id,
                is_recurring_exception=validated_data.get("is_recurring_exception", False),
            ),
        )

        return event

    def update(self, instance: CalendarEvent, validated_data: dict) -> CalendarEvent:
        if not self.calendar_service:
            raise ValueError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        calendar: Calendar = validated_data.pop("calendar", instance.calendar)
        user = self.context["request"].user
        user = self.context["request"].user
        if validated_data.get("google_calendar_service_account"):
            account = validated_data.get("google_calendar_service_account")
        else:
            account = SocialAccount.objects.filter(
                user=user,
                provider=calendar.provider,
            ).first()
        self.calendar_service.authenticate(
            account=account,
            organization=calendar.organization,
        )

        resource_allocations = validated_data.pop(
            "resource_allocations",
            [{"resource_id": ra.calendar.id} for ra in instance.resource_allocations.all()],
        )
        attendances = validated_data.pop(
            "attendances", [{"user_id": att.user.id} for att in instance.attendances.all()]
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

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        user = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )

        if self.instance:
            self.fields["recurrence_rule_id"] = serializers.PrimaryKeyRelatedField(
                source="recurrence_rule_fk",
                many=False,
                required=False,
                queryset=(
                    RecurrenceRule.objects.filter_by_organization(
                        user.organization_membership.organization_id
                    ).all()
                    if user and user.is_authenticated and user.organization_membership
                    else RecurrenceRule.original_manager.none()
                ),
                write_only=True,
            )

        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    organization_id=user.organization_membership.organization_id
                )
                if user
                and user.is_authenticated
                and hasattr(user, "organization_membership")
                and user.organization_membership
                else Calendar.original_manager.none()
            ),
            allow_null=True,
        )

    def create(self, validated_data: dict):
        if not self.calendar_service:
            raise ValueError(
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

        calendar = validated_data.pop("calendar")
        self.calendar_service.initialize_without_provider(user.organization_membership.organization)

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

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        self.calendar_service = calendar_service
        super().__init__(*args, **kwargs)
        user = (
            self.context["request"].user if self.context and self.context.get("request") else None
        )

        if self.instance:
            self.fields["recurrence_rule_id"] = serializers.PrimaryKeyRelatedField(
                source="recurrence_rule_fk",
                many=False,
                required=False,
                queryset=(
                    RecurrenceRule.objects.filter_by_organization(
                        user.organization_membership.organization_id
                    ).all()
                    if user and user.is_authenticated and user.organization_membership
                    else RecurrenceRule.original_manager.none()
                ),
                write_only=True,
            )

        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    organization_id=user.organization_membership.organization_id
                )
                if user
                and user.is_authenticated
                and hasattr(user, "organization_membership")
                and user.organization_membership
                else Calendar.original_manager.none()
            ),
            allow_null=True,
        )

    def create(self, validated_data: dict):
        if not self.calendar_service:
            raise ValueError(
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

        calendar = validated_data.pop("calendar")
        self.calendar_service.initialize_without_provider(user.organization_membership.organization)

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


class UnavailableTimeWindowSerializer(serializers.Serializer):
    id = serializers.IntegerField()  # noqa: A003
    reason = serializers.CharField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    reason_description = serializers.SerializerMethodField()

    def get_reason_description(self, obj: UnavailableTimeWindow) -> str:
        if obj.reason == "calendar_event":
            event_data = cast(CalendarEventData, obj.data)
            return event_data.title

        blocked_time_data = cast(BlockedTimeData, obj.data)
        return blocked_time_data.reason


class BulkBlockedTimeSerializer(serializers.Serializer):
    """Serializer for creating multiple blocked times."""

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

        self.fields["blocked_times"] = BlockedTimeSerializer(many=True, context=self.context)

    def validate_blocked_times(self, blocked_times_data):
        """Validate bulk blocked times data."""
        if not blocked_times_data:
            raise serializers.ValidationError("At least one blocked time must be provided")

        # check all blocked time instances are for the same calendar
        first_blocked_time_calendar = blocked_times_data[0].get("calendar")
        for blocked_time in blocked_times_data[1:]:
            if blocked_time.get("calendar") != first_blocked_time_calendar:
                raise serializers.ValidationError("All blocked times must be for the same calendar")

        return blocked_times_data

    def save(self, **kwargs):
        """Create multiple blocked times using calendar service."""
        if not self.calendar_service:
            raise ValueError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        user = self.context["request"].user
        organization = user.organization_membership.organization

        self.calendar_service.initialize_without_provider(organization)

        # Convert to the format expected by bulk_create_manual_blocked_times
        blocked_times_tuples = [
            (bt["start_time"], bt["end_time"], bt["reason"], bt.get("rrule_string"))
            for bt in self.validated_data["blocked_times"]
        ]
        calendar = self.validated_data["blocked_times"][0]["calendar"]

        blocked_times = self.calendar_service.bulk_create_manual_blocked_times(
            calendar=calendar, blocked_times=blocked_times_tuples
        )
        return list(blocked_times)


class BulkAvailableTimeSerializer(serializers.Serializer):
    """Serializer for creating multiple available times."""

    @inject
    def __init__(
        self,
        *args,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calendar_service = calendar_service

        self.fields["available_times"] = AvailableTimeSerializer(many=True, context=self.context)

    def validate_available_times(self, available_times_data):
        """Validate bulk available times data."""
        if not available_times_data:
            raise serializers.ValidationError("At least one available time must be provided")

        # check all available time instances are for the same calendar
        first_available_time_calendar = available_times_data[0].get("calendar")
        for available_time in available_times_data[1:]:
            if available_time.get("calendar") != first_available_time_calendar:
                raise serializers.ValidationError(
                    "All available times must be for the same calendar"
                )

        return available_times_data

    def save(self, **kwargs):
        """Create multiple available times using calendar service."""
        if not self.calendar_service:
            raise ValueError(
                "calendar_service is not defined, please configure your DI container correctly"
            )

        user = self.context["request"].user
        organization = user.organization_membership.organization
        calendar = self.validated_data["available_times"][0]["calendar"]

        self.calendar_service.initialize_without_provider(organization)

        # Convert to the format expected by bulk_create_availability_windows
        availability_tuples = [
            (at["start_time"], at["end_time"], at.get("rrule_string"))
            for at in self.validated_data["available_times"]
        ]

        available_times = self.calendar_service.bulk_create_availability_windows(
            calendar=calendar, availability_windows=availability_tuples
        )
        return list(available_times)


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
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
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
            raise ValueError(
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
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
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
