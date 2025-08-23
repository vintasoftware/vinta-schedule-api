import datetime
from typing import TYPE_CHECKING, Annotated, cast

import django_virtual_models as v
from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from rest_framework import serializers

from calendar_integration.constants import CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    RecurrenceException,
    RecurrenceRule,
    ResourceAllocation,
)
from calendar_integration.services.calendar_service import (
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
    BlockedTimeVirtualModel,
    CalendarEventVirtualModel,
    CalendarOwnershipVirtualModel,
    CalendarVirtualModel,
    EventAttendanceVirtualModel,
    EventExternalAttendanceVirtualModel,
    ExternalAttendeeVirtualModel,
    RecurrenceExceptionVirtualModel,
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
        field = (
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
            "name",
            "description",
            "email",
            "external_id",
            "provider",
            "calendar_type",
            "capacity",
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
                else Calendar.objects.none()
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
        from django.core.exceptions import ValidationError as DjangoValidationError

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
        model = RecurrenceException
        virtual_model = RecurrenceExceptionVirtualModel
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
                else CalendarEvent.objects.none()
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
                else CalendarEvent.objects.none()
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
    recurrence_rule_data = RecurrenceRuleSerializer(
        write_only=True,
        required=False,
        help_text="Recurrence rule data for creating recurring events",
    )
    rrule_string = serializers.CharField(
        write_only=True, required=False, help_text="RRULE string for creating recurring events"
    )
    parent_event_id = serializers.IntegerField(
        write_only=True, required=False, help_text="ID of parent event for recurring instances"
    )
    is_recurring_instance = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is an instance of a recurring event"
    )
    is_recurring = serializers.SerializerMethodField(
        read_only=True, help_text="True if this is a recurring event"
    )
    parent_event = ParentEventSerializer(read_only=True)

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
            "created",
            "modified",
            "external_id",
            "external_attendances",
            "attendances",
            "resource_allocations",
            # Recurrence fields
            "recurrence_rule",
            "recurrence_rule_data",
            "rrule_string",
            "parent_event_id",
            "parent_event",
            "is_recurring_instance",
            "is_recurring",
            "is_recurring_exception",
            "recurrence_id",
        )
        read_only_fields = (
            "id",
            "external_id",
            "is_recurring_instance",
            "recurrence_rule",
            "recurrence_exceptions",
            "next_occurrence",
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
        user = self.context["request"].user

        # Initialize nested serializers with context
        self.fields["resource_allocations"] = ResourceAllocationSerializer(
            many=True, context=self.context
        )
        self.fields["attendances"] = EventAttendanceSerializer(many=True, context=self.context)
        self.fields["external_attendances"] = EventExternalAttendanceSerializer(
            many=True, context=self.context
        )
        self.fields["recurrence_rule"] = RecurrenceRuleSerializer(
            read_only=True, context=self.context
        )

        # add google_calendar_service_account and calendar fields dynamically to filter by
        # organization_id
        self.fields["google_calendar_service_account"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                GoogleCalendarServiceAccount.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).all()
                if user.is_authenticated
                else GoogleCalendarServiceAccount.objects.none()
            ),
            required=False,
            write_only=True,
        )
        self.fields["calendar"] = serializers.PrimaryKeyRelatedField(
            queryset=(
                Calendar.objects.filter_by_organization(
                    user.organization_membership.organization_id
                ).all()
                if user.is_authenticated
                else Calendar.objects.none()
            ),
            required=False,
            write_only=True,
        )

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
        recurrence_rule_data = attrs.get("recurrence_rule_data")
        rrule_string = attrs.get("rrule_string")
        parent_event_id = attrs.get("parent_event_id")

        if recurrence_rule_data and rrule_string:
            raise serializers.ValidationError(
                "Cannot specify both recurrence_rule_data and rrule_string. Use one or the other."
            )

        if (recurrence_rule_data or rrule_string) and parent_event_id:
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
        recurrence_rule_data = validated_data.pop("recurrence_rule_data", None)
        rrule_string = validated_data.pop("rrule_string", None)
        parent_event_id = validated_data.pop("parent_event_id", None)

        # Prepare recurrence rule for calendar service
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            from calendar_integration.models import RecurrenceRule

            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string

        event = self.calendar_service.create_event(
            calendar_id=calendar.external_id,
            event_data=CalendarEventInputData(
                title=validated_data.get("title"),
                description=validated_data.get("description"),
                start_time=validated_data.get("start_time"),
                end_time=validated_data.get("end_time"),
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
                parent_event_id=parent_event_id,
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
        recurrence_rule_data = validated_data.pop("recurrence_rule_data", None)
        rrule_string = validated_data.pop("rrule_string", None)
        parent_event_id = validated_data.pop("parent_event_id", None)

        # Prepare recurrence rule for calendar service
        final_rrule_string = None
        if recurrence_rule_data:
            # Convert recurrence_rule_data to RRULE string
            from calendar_integration.models import RecurrenceRule

            temp_rule = RecurrenceRule(organization=calendar.organization, **recurrence_rule_data)
            final_rrule_string = temp_rule.to_rrule_string()
        elif rrule_string:
            final_rrule_string = rrule_string
        elif instance.recurrence_rule:
            # Keep existing recurrence rule
            final_rrule_string = instance.recurrence_rule.to_rrule_string()

        event = self.calendar_service.update_event(
            calendar_id=calendar.external_id,
            event_id=instance.external_id,
            event_data=CalendarEventInputData(
                title=validated_data.get("title", instance.title),
                description=validated_data.get("description", instance.description),
                start_time=validated_data.get("start_time", instance.start_time),
                end_time=validated_data.get("end_time", instance.end_time),
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
                parent_event_id=parent_event_id
                or (instance.parent_event.id if instance.parent_event else None),
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


class BlockedTimeSerializer(VirtualModelSerializer):
    class Meta:
        model = BlockedTime
        virtual_model = BlockedTimeVirtualModel
        fields = (
            "id",
            "calendar",
            "start_time",
            "end_time",
            "reason",
            "created",
            "modified",
        )


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
