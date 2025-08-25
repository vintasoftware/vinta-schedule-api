import datetime
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from encrypted_fields.fields import EncryptedCharField  # type:ignore

from calendar_integration.constants import (
    CalendarOrganizationResourceImportStatus,
    CalendarProvider,
    CalendarSyncStatus,
    CalendarType,
    RecurrenceFrequency,
    RecurrenceWeekday,
    RSVPStatus,
)
from calendar_integration.managers import (
    CalendarEventManager,
    CalendarManager,
    CalendarSyncManager,
)
from organizations.models import (
    Organization,
    OrganizationForeignKey,
    OrganizationModel,
    OrganizationOneToOneField,
)
from users.models import User


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


class Calendar(OrganizationModel):
    """
    Represents a calendar that can be used for scheduling.
    """

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    provider = models.CharField(
        max_length=255, choices=CalendarProvider, default=CalendarProvider.INTERNAL
    )

    calendar_type = models.CharField(
        max_length=50,
        choices=CalendarType,
        default=CalendarType.PERSONAL,
        help_text=(
            "The type of calendar. Personal calendars are for individual use, resource calendars are for shared resources, "
            "and virtual calendars are for online meetings or events."
        ),
    )

    # only available for resource calendars
    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "The maximum number of attendees that can be accommodated in this calendar's events. "
            "This is only applicable for resource calendars."
        ),
    )

    manage_available_windows = models.BooleanField(
        default=False,
        help_text=(
            "If true, this calendar can manage its own available time windows. If not, it will "
            "use the available time windows of the external calendar it's attached to."
        ),
    )

    users: "models.ManyToManyField[User, CalendarOwnership]" = models.ManyToManyField(
        User,
        related_name="calendars",
        through="CalendarOwnership",
        through_fields=("calendar_fk", "user"),
        blank=True,
    )
    children: "models.ManyToManyField[Calendar, ChildrenCalendarRelationship]" = (
        models.ManyToManyField(
            "self",
            related_name="bundles",
            blank=True,
            through="ChildrenCalendarRelationship",
            through_fields=("bundle_calendar", "child_calendar"),
        )
    )

    objects: CalendarManager = CalendarManager()

    events: "RelatedManager[CalendarEvent]"
    blocked_times: "RelatedManager[BlockedTime]"
    syncs: "RelatedManager[CalendarSync]"
    available_times: "RelatedManager[AvailableTime]"

    def __str__(self):
        return self.name

    class Meta:
        unique_together = (("external_id", "provider", "organization_id"),)

    @property
    def is_virtual(self) -> bool:
        """
        Returns True if the calendar is a virtual calendar.
        """
        return self.calendar_type == CalendarType.VIRTUAL

    @property
    def is_personal(self) -> bool:
        """
        Returns True if the calendar is a personal calendar.
        """
        return self.calendar_type == CalendarType.PERSONAL

    @property
    def is_resource(self) -> bool:
        """
        Returns True if the calendar is a resource calendar.
        """
        return self.calendar_type == CalendarType.RESOURCE

    @property
    def latest_sync(self) -> "CalendarSync | None":
        """
        Returns the latest sync record for this calendar.
        """
        if hasattr(self, "_latest_sync") and self._latest_sync:
            return self._latest_sync[0]

        return self.syncs.filter(should_update_events=True).order_by("-start_datetime").first()


class ChildrenCalendarRelationship(OrganizationModel):
    bundle_calendar = OrganizationForeignKey(
        Calendar,
        on_delete=models.CASCADE,
        related_name="bundle_relationships",
    )
    child_calendar = OrganizationForeignKey(
        Calendar,
        on_delete=models.CASCADE,
        related_name="bundle_children_relatioships",
    )


class CalendarOwnership(OrganizationModel):
    """
    Represents the ownership of a calendar by an organization.
    This is used to link calendars to their respective organizations.
    """

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="ownerships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="calendar_ownerships",
    )
    is_default = models.BooleanField(
        default=False,
        help_text=(
            "If true, this calendar is the default calendar for the user in this organization. "
            "This means that events created by the user will be added to this calendar by default."
        ),
    )

    def __str__(self):
        return f"{self.calendar} owned by {self.user}"


class ExternalAttendee(OrganizationModel):
    """
    Represents an external user who can attend events in a calendar.
    """

    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField()

    def __str__(self):
        return f"{self.name} ({self.email})" if self.name else self.email


class EventExternalAttendance(OrganizationModel):
    """
    Represents the attendance of an external user at a event.
    """

    event = OrganizationForeignKey(
        "CalendarEvent",
        on_delete=models.CASCADE,
        null=True,
        related_name="external_attendances",
    )

    external_attendee = OrganizationForeignKey(
        ExternalAttendee,
        on_delete=models.CASCADE,
        null=True,
        related_name="external_attendances",
    )

    status = models.CharField(
        max_length=50,
        choices=[("accepted", "Accepted"), ("declined", "Declined"), ("pending", "Pending")],
        default="pending",
    )

    def __str__(self):
        return f"{self.external_attendee} - {self.event.title} ({self.status})"


class EventAttendance(OrganizationModel):
    """
    Represents the attendance of a user at a event.
    """

    event = OrganizationForeignKey(
        "CalendarEvent",
        on_delete=models.CASCADE,
        null=True,
        related_name="attendances",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_attendances"
    )
    status = models.CharField(
        max_length=50,
        choices=RSVPStatus,
        default=RSVPStatus.PENDING,
    )

    def __str__(self):
        return f"{self.user} - {self.event.title} ({self.status})"


class ResourceAllocation(OrganizationModel):
    """
    Represents the allocation of a resource to a calendar event.
    """

    event = OrganizationForeignKey(
        "CalendarEvent",
        on_delete=models.CASCADE,
        null=True,
        related_name="resource_allocations",
    )

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="resource_allocations",
    )
    status = models.CharField(
        max_length=50,
        choices=RSVPStatus,
        default=RSVPStatus.PENDING,
    )

    def __str__(self):
        return f"{self.calendar} allocated to {self.event}"


class RecurrenceRule(OrganizationModel):
    """
    Represents a recurrence rule for recurring events following RFC 5545 (RRULE).
    """

    frequency = models.CharField(
        max_length=10,
        choices=RecurrenceFrequency,
        help_text="How often the event repeats (DAILY, WEEKLY, MONTHLY, YEARLY)",
    )
    interval = models.PositiveIntegerField(
        default=1, help_text="The interval between each frequency iteration (e.g., every 2 weeks)"
    )
    count = models.PositiveIntegerField(
        null=True, blank=True, help_text="Number of occurrences after which the recurrence ends"
    )
    until = models.DateTimeField(
        null=True, blank=True, help_text="The date and time until which the recurrence is valid"
    )
    by_weekday = models.CharField(
        max_length=100, blank=True, help_text="Comma-separated list of weekdays (e.g., 'MO,WE,FR')"
    )
    by_month_day = models.CharField(
        max_length=100,
        blank=True,
        help_text="Comma-separated list of month days (e.g., '1,15,-1' for 1st, 15th, last day)",
    )
    by_month = models.CharField(
        max_length=50, blank=True, help_text="Comma-separated list of months (1-12)"
    )
    by_year_day = models.CharField(
        max_length=100,
        blank=True,
        help_text="Comma-separated list of year days (1-366 or -366 to -1)",
    )
    by_week_number = models.CharField(
        max_length=100,
        blank=True,
        help_text="Comma-separated list of week numbers (1-53 or -53 to -1)",
    )
    by_hour = models.CharField(
        max_length=100, blank=True, help_text="Comma-separated list of hours (0-23)"
    )
    by_minute = models.CharField(
        max_length=200, blank=True, help_text="Comma-separated list of minutes (0-59)"
    )
    by_second = models.CharField(
        max_length=200, blank=True, help_text="Comma-separated list of seconds (0-59)"
    )
    week_start = models.CharField(
        max_length=2,
        choices=RecurrenceWeekday,
        default=RecurrenceWeekday.MONDAY,
        help_text="First day of the week",
    )

    def __str__(self):
        return f"Recurrence: {self.frequency} every {self.interval}"

    def to_rrule_string(self) -> str:
        """
        Convert the recurrence rule to an RRULE string following RFC 5545.
        """
        parts = [f"FREQ={self.frequency}"]

        if self.interval and self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")

        if self.count:
            parts.append(f"COUNT={self.count}")

        if self.until:
            # Format as YYYYMMDDTHHMMSSZ in UTC
            parts.append(f"UNTIL={self.until.strftime('%Y%m%dT%H%M%SZ')}")

        if self.by_weekday:
            parts.append(f"BYDAY={self.by_weekday}")

        if self.by_month_day:
            parts.append(f"BYMONTHDAY={self.by_month_day}")

        if self.by_month:
            parts.append(f"BYMONTH={self.by_month}")

        if self.by_year_day:
            parts.append(f"BYYEARDAY={self.by_year_day}")

        if self.by_week_number:
            parts.append(f"BYWEEKNO={self.by_week_number}")

        if self.by_hour:
            parts.append(f"BYHOUR={self.by_hour}")

        if self.by_minute:
            parts.append(f"BYMINUTE={self.by_minute}")

        if self.by_second:
            parts.append(f"BYSECOND={self.by_second}")

        if self.week_start != RecurrenceWeekday.MONDAY:
            parts.append(f"WKST={self.week_start}")

        return ";".join(parts)

    @classmethod
    def from_rrule_string(cls, rrule_string: str, organization: Organization) -> "RecurrenceRule":
        """
        Create a RecurrenceRule instance from an RRULE string.
        """
        if rrule_string.startswith("RRULE:"):
            rrule_string = rrule_string[6:]  # Remove RRULE: prefix

        parts = rrule_string.split(";")
        rule_data: dict = {"organization": organization}

        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)

            if key == "FREQ":
                rule_data["frequency"] = value
            elif key == "INTERVAL":
                rule_data["interval"] = int(value)
            elif key == "COUNT":
                rule_data["count"] = int(value)
            elif key == "UNTIL":
                # Parse YYYYMMDDTHHMMSSZ format
                if value.endswith("Z"):
                    dt = datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
                    rule_data["until"] = timezone.make_aware(dt, datetime.UTC)
            elif key == "BYDAY":
                rule_data["by_weekday"] = value
            elif key == "BYMONTHDAY":
                rule_data["by_month_day"] = value
            elif key == "BYMONTH":
                rule_data["by_month"] = value
            elif key == "BYYEARDAY":
                rule_data["by_year_day"] = value
            elif key == "BYWEEKNO":
                rule_data["by_week_number"] = value
            elif key == "BYHOUR":
                rule_data["by_hour"] = value
            elif key == "BYMINUTE":
                rule_data["by_minute"] = value
            elif key == "BYSECOND":
                rule_data["by_second"] = value
            elif key == "WKST":
                rule_data["week_start"] = value

        return cls(**rule_data)

    def clean(self):
        """
        Validate the recurrence rule for common issues.
        """

        # Ensure count and until are not both specified
        if self.count and self.until:
            raise ValidationError("Cannot specify both 'count' and 'until' in a recurrence rule.")

        # Validate weekdays format
        if self.by_weekday:
            valid_weekdays = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
            weekdays = [day.strip() for day in self.by_weekday.split(",")]
            invalid_weekdays = [day for day in weekdays if day not in valid_weekdays]
            if invalid_weekdays:
                raise ValidationError(
                    f"Invalid weekdays: {', '.join(invalid_weekdays)}. "
                    "Valid options are: MO, TU, WE, TH, FR, SA, SU"
                )

        # Validate month days
        if self.by_month_day:
            try:
                month_days = [int(day.strip()) for day in self.by_month_day.split(",")]
                invalid_days = [day for day in month_days if day == 0 or day > 31 or day < -31]
                if invalid_days:
                    raise ValidationError(
                        f"Invalid month days: {', '.join(map(str, invalid_days))}. "
                        "Must be between 1-31 or -1 to -31."
                    )
            except ValueError as e:
                raise ValidationError("Month days must be integers separated by commas.") from e

        # Validate months
        if self.by_month:
            try:
                months = [int(month.strip()) for month in self.by_month.split(",")]
                invalid_months = [month for month in months if month < 1 or month > 12]
                if invalid_months:
                    raise ValidationError(
                        f"Invalid months: {', '.join(map(str, invalid_months))}. "
                        "Must be between 1-12."
                    )
            except ValueError as e:
                raise ValidationError("Months must be integers separated by commas.") from e

        # Validate interval
        if self.interval < 1:
            raise ValidationError("Interval must be at least 1.")

    def save(self, *args, **kwargs):
        """Override save to run validation."""
        self.clean()
        super().save(*args, **kwargs)


class CalendarEvent(OrganizationModel):
    """
    Represents an event in a calendar.
    """

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="events",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    external_id = models.CharField(max_length=255, unique=True, blank=True)

    # Recurrence fields
    recurrence_rule = OrganizationOneToOneField(
        RecurrenceRule,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="event",
        help_text="The recurrence rule for this event. If set, this event is recurring.",
    )
    recurrence_id = models.DateTimeField(
        null=True,
        blank=True,
        help_text="For recurring event instances, this identifies which occurrence this is",
    )
    parent_event = OrganizationForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="recurring_instances",
        help_text="If this is an instance of a recurring event, points to the parent event",
    )
    is_recurring_exception = models.BooleanField(
        default=False,
        help_text="True if this event is an exception to the recurrence rule (modified occurrence)",
    )

    attendees = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="calendar_events",
        through=EventAttendance,
        through_fields=("event", "user"),
        blank=True,
    )
    external_attendees = models.ManyToManyField(ExternalAttendee, related_name="calendar_events")
    resources = models.ManyToManyField(
        Calendar,
        related_name="allocated_events",
        through=ResourceAllocation,
        through_fields=("event", "calendar"),
        blank=True,
    )

    resource_allocations: "RelatedManager[ResourceAllocation]"
    attendances: "RelatedManager[EventAttendance]"
    external_attendances: "RelatedManager[EventExternalAttendance]"
    recurring_instances: "RelatedManager[CalendarEvent]"

    objects: CalendarEventManager = CalendarEventManager()

    def __str__(self):
        return f"{self.title} ({self.start_time} - {self.end_time})"

    @property
    def is_recurring(self) -> bool:
        """
        Returns True if this event has a recurrence rule.
        """
        return self.recurrence_rule is not None

    @property
    def is_recurring_instance(self) -> bool:
        """
        Returns True if this event is an instance of a recurring event.
        """
        return self.parent_event is not None

    @property
    def duration(self):
        """
        Returns the duration of the event as a timedelta.
        """
        return self.end_time - self.start_time

    def get_next_occurrence(
        self, after_date: datetime.datetime | None = None
    ) -> "CalendarEvent | None":
        """
        Get the next occurrence of this recurring event after the given date.
        If no date is provided, uses the current time.
        """
        if not self.is_recurring:
            return None

        after_date = after_date or timezone.now()

        try:
            # Add microsecond to ensure we get occurrences strictly after the given date
            search_start_date = after_date + datetime.timedelta(microseconds=1)

            # Use rule's until date if specified, otherwise use a reasonable future date
            if self.recurrence_rule and self.recurrence_rule.until:
                end_date = min(
                    self.recurrence_rule.until + datetime.timedelta(days=1),
                    after_date + datetime.timedelta(days=10 * 365),
                )
            else:
                end_date = after_date + datetime.timedelta(days=10 * 365)

            # Get just the next occurrence after the given date
            future_occurrences = self.get_occurrences_in_range(
                start_date=search_start_date,
                end_date=end_date,
                include_self=False,
                include_exceptions=False,
                max_occurrences=1,
            )

            if not future_occurrences:
                return None

            return future_occurrences[0]
        except (IndexError, AttributeError):
            return None

    def get_occurrences_in_range(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_self=True,
        include_exceptions=True,
        max_occurrences=10000,
    ) -> list["CalendarEvent"]:
        """
        Get all occurrences of this event in the given date range.
        This includes both generated instances and any saved exceptions.

        Args:
            start_date: Start of the date range
            end_date: End of the date range
            include_exceptions: Whether to include modified exceptions

        Returns:
            List of CalendarEvent instances (mix of generated and saved events)
        """
        if not self.is_recurring:
            return []

        if hasattr(self, "recurring_occurrences"):
            occurrences = self.recurring_occurrences
        else:
            occurrences = (
                self.__class__.objects.annotate_recurring_occurrences_on_date_range(
                    start_date, end_date, max_occurrences
                )
                .filter(organization_id=self.organization_id, id=self.id)
                .values_list("recurring_occurrences", flat=True)
                .first()
            )

        all_exception_events_by_id: dict[int, CalendarEvent] = {
            e.pk: e
            for e in self.__class__.objects.filter(
                organization_id=self.organization_id,
                id__in=[o["modified_event_id"] for o in occurrences if "modified_event_id" in o],
            )
        }

        events: list[CalendarEvent] = []
        for occurrence in occurrences:
            occurrence_start_time = datetime.datetime.fromisoformat(occurrence["start_time"])
            occurrence_end_time = datetime.datetime.fromisoformat(occurrence["end_time"])
            if (
                include_self
                and occurrence_start_time == self.start_time
                and occurrence_end_time == self.end_time
            ):
                events.append(self)
                continue

            if occurrence["exception_type"] == "cancelled":
                continue

            if occurrence["modified_event_id"] and (
                exception_event := all_exception_events_by_id.get(occurrence["modified_event_id"])
            ):
                if include_exceptions:
                    events.append(exception_event)
                continue

            events.append(
                CalendarEvent(
                    calendar_fk=self.calendar,
                    organization=self.organization,
                    title=self.title,
                    description=self.description,
                    start_time=occurrence_start_time,
                    end_time=occurrence_end_time,
                    recurrence_rule_fk=self.recurrence_rule,
                    recurrence_id=occurrence_start_time,
                )
            )

        return events

    def get_generated_occurrences_in_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> list["CalendarEvent"]:
        """
        Generate recurring event instances between start_date and end_date.
        Returns a list of CalendarEvent instances (not saved to database).
        """
        return self.get_occurrences_in_range(
            start_date, end_date, include_self=False, include_exceptions=False
        )

    def create_exception(self, exception_date, is_cancelled=True, modified_event=None):
        """
        Create an exception for a specific occurrence of this recurring event.

        Args:
            exception_date: The date of the occurrence to create an exception for
            is_cancelled: True if the occurrence is cancelled, False if modified
            modified_event: If not cancelled, the modified event instance

        Returns:
            RecurrenceException instance
        """
        if not self.is_recurring:
            raise ValueError("Cannot create exception for non-recurring event")

        org_id = getattr(self, "organization_id", None)
        if org_id is None and getattr(self, "organization", None) is not None:
            org_id = self.organization.id
        if org_id is None:
            raise ValueError("CalendarEvent is missing organization (cannot create exception)")

        qs = RecurrenceException.objects.filter(
            organization_id=org_id,
            parent_event_fk=self,
            exception_date=exception_date,
        )
        exception = qs.first()
        if exception:
            exception.is_cancelled = is_cancelled
            # Assign underlying FK for modified_event if provided
            if modified_event is not None:
                exception.modified_event_fk = modified_event
            else:
                exception.modified_event_fk = None
            exception.save(update_fields=["is_cancelled", "modified_event_fk", "modified"])
            return exception

        # Create new exception
        exception = RecurrenceException(
            organization_id=org_id,
            parent_event_fk=self,
            exception_date=exception_date,
            is_cancelled=is_cancelled,
        )
        if modified_event is not None:
            exception.modified_event_fk = modified_event
        exception.save()
        return exception


class RecurrenceException(OrganizationModel):
    """
    Represents an exception to a recurring event (cancelled or modified occurrence).
    """

    parent_event = OrganizationForeignKey(
        CalendarEvent,
        on_delete=models.CASCADE,
        null=True,
        related_name="recurrence_exceptions",
        help_text="The recurring event this exception applies to",
    )
    exception_date = models.DateTimeField(
        help_text="The original start time of the occurrence being excepted"
    )
    is_cancelled = models.BooleanField(
        default=False, help_text="True if this occurrence is cancelled, False if it's modified"
    )
    modified_event = OrganizationForeignKey(
        CalendarEvent,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="exception_for",
        help_text="If the occurrence is modified (not cancelled), points to the modified event",
    )

    def __str__(self):
        status = "cancelled" if self.is_cancelled else "modified"
        return f"Exception for {self.parent_event.title} on {self.exception_date} ({status})"


class BlockedTime(OrganizationModel):
    """
    Represents a blocked time period in a calendar.
    """

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="blocked_times",
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"Blocked from {self.start_time} to {self.end_time} ({self.reason})"

    class Meta:
        unique_together = (("calendar_fk_id", "external_id"),)


class AvailableTime(OrganizationModel):
    """
    Represents available time slots in a calendar.
    """

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="available_times",
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    def __str__(self):
        return f"Available from {self.start_time} to {self.end_time}"


class CalendarSync(OrganizationModel):
    """
    Represents a synchronization record for a calendar.
    """

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="syncs",
    )
    next_sync_token = models.CharField(max_length=255, blank=True)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    should_update_events = models.BooleanField()
    status = models.CharField(
        max_length=50,
        choices=CalendarSyncStatus,
        default=CalendarSyncStatus.NOT_STARTED,
    )
    error_message = models.TextField(blank=True)

    objects: CalendarSyncManager = CalendarSyncManager()

    def __str__(self):
        return f"Sync for {self.calendar} at {self.created}"


class CalendarOrganizationResourcesImport(OrganizationModel):
    """
    Represents a scheduled import of calendar resources for an organization.
    This is used to import resources from external calendar providers.
    """

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(
        max_length=50,
        choices=CalendarOrganizationResourceImportStatus,
        default=CalendarOrganizationResourceImportStatus.NOT_STARTED,
    )
    error_message = models.TextField(blank=True)

    def __str__(self):
        return f"Resources Import for {self.organization} from {self.start_time} to {self.end_time}"


class GoogleCalendarServiceAccount(OrganizationModel):
    """
    Represents a Google Calendar service account.
    """

    calendar = OrganizationForeignKey(  # type:ignore
        Calendar,
        on_delete=models.CASCADE,
        null=True,
        related_name="google_service_accounts",
    )
    email = models.EmailField()
    audience = models.CharField(max_length=255)
    public_key = models.TextField()
    private_key_id = EncryptedCharField(max_length=255)
    private_key = EncryptedCharField(max_length=255)

    def __str__(self):
        return f"Service Account for {self.calendar} ({self.email})"
