import calendar
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

    def get_next_occurrence(self, after_date=None):
        """
        Get the next occurrence of this recurring event after the given date.
        If no date is provided, uses the current time.
        """
        if not self.is_recurring:
            return None

        if after_date is None:
            after_date = datetime.datetime.now(datetime.UTC)

        # Ensure after_date is timezone-aware
        if after_date.tzinfo is None:
            after_date = timezone.make_aware(after_date)

        # Start from the event's start time
        current_date = self.start_time
        rule = self.recurrence_rule

        # If we have an end date/count limit, check if we've exceeded it
        if rule.until and after_date > rule.until:
            return None

        # Simple implementation for basic recurrence patterns
        if rule.frequency == RecurrenceFrequency.DAILY:
            # Calculate days between start and after_date
            days_diff = (after_date.date() - current_date.date()).days
            if days_diff < 0:
                # after_date is before start_time
                return current_date

            # Calculate next occurrence
            next_occurrence_days = ((days_diff // rule.interval) + 1) * rule.interval
            next_occurrence = current_date + datetime.timedelta(days=next_occurrence_days)

            # Check count limit
            if rule.count and next_occurrence_days >= rule.count:
                return None

            return next_occurrence

        elif rule.frequency == RecurrenceFrequency.WEEKLY:
            # Calculate weeks between start and after_date
            weeks_diff = (after_date.date() - current_date.date()).days // 7
            if weeks_diff < 0:
                return current_date

            next_occurrence_weeks = ((weeks_diff // rule.interval) + 1) * rule.interval
            next_occurrence = current_date + datetime.timedelta(weeks=next_occurrence_weeks)

            # Check count limit
            if rule.count and next_occurrence_weeks >= rule.count:
                return None

            return next_occurrence

        elif rule.frequency == RecurrenceFrequency.MONTHLY:
            # Simple monthly recurrence (same day of month)
            months_diff = (after_date.year - current_date.year) * 12 + (
                after_date.month - current_date.month
            )
            if months_diff < 0 or (months_diff == 0 and after_date.day < current_date.day):
                return current_date

            next_occurrence_months = ((months_diff // rule.interval) + 1) * rule.interval

            # Check count limit
            if rule.count and next_occurrence_months >= rule.count:
                return None

            # Calculate next month/year
            target_month = current_date.month + next_occurrence_months
            target_year = current_date.year + ((target_month - 1) // 12)
            target_month = ((target_month - 1) % 12) + 1

            try:
                next_occurrence = current_date.replace(year=target_year, month=target_month)
                return next_occurrence
            except ValueError:
                # Handle cases like Feb 31 -> Feb 28/29
                last_day = calendar.monthrange(target_year, target_month)[1]
                if current_date.day > last_day:
                    next_occurrence = current_date.replace(
                        year=target_year, month=target_month, day=last_day
                    )
                    return next_occurrence
                return None

        elif rule.frequency == RecurrenceFrequency.YEARLY:
            # Simple yearly recurrence
            years_diff = after_date.year - current_date.year
            if years_diff < 0 or (
                years_diff == 0
                and after_date.timetuple().tm_yday < current_date.timetuple().tm_yday
            ):
                return current_date

            next_occurrence_years = ((years_diff // rule.interval) + 1) * rule.interval

            # Check count limit
            if rule.count and next_occurrence_years >= rule.count:
                return None

            try:
                next_occurrence = current_date.replace(
                    year=current_date.year + next_occurrence_years
                )
                return next_occurrence
            except ValueError:
                # Handle leap year edge case (Feb 29)
                next_occurrence = current_date.replace(
                    year=current_date.year + next_occurrence_years, day=28
                )
                return next_occurrence

        return None

    def generate_instances(self, start_date, end_date):
        """
        Generate recurring event instances between start_date and end_date.
        Returns a list of CalendarEvent instances (not saved to database).
        """
        if not self.is_recurring:
            return []

        instances = []
        current_date = self.start_time
        rule = self.recurrence_rule
        occurrence_count = 0

        # Get all existing exceptions for this recurring event
        exceptions = set()
        if hasattr(self, "recurrence_exceptions"):
            exceptions = set(exc.exception_date for exc in self.recurrence_exceptions.all())

        while current_date <= end_date:
            # Check if we've hit the until date
            if rule.until and current_date > rule.until:
                break

            # Check if we've hit the count limit
            if rule.count and occurrence_count >= rule.count:
                break

            # If this occurrence is within our date range and not an exception
            if current_date >= start_date and current_date not in exceptions:
                # Create an instance (not saved to database)
                instance = CalendarEvent(
                    calendar=self.calendar,
                    organization=self.organization,
                    title=self.title,
                    description=self.description,
                    start_time=current_date,
                    end_time=current_date + self.duration,
                    external_id=f"{self.external_id}_instance_{current_date.isoformat()}"
                    if self.external_id
                    else "",
                    recurrence_id=current_date,
                    parent_event=self,
                    is_recurring_exception=False,
                )
                instances.append(instance)

            # Calculate next occurrence
            if rule.frequency == RecurrenceFrequency.DAILY:
                current_date += datetime.timedelta(days=rule.interval)

            elif rule.frequency == RecurrenceFrequency.WEEKLY:
                if rule.by_weekday:
                    # Handle specific weekdays (e.g., MO,WE,FR)
                    weekdays = [day.strip() for day in rule.by_weekday.split(",")]
                    weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

                    # Find next occurrence within the current week or next week
                    current_weekday = current_date.weekday()
                    found_next = False

                    # Check remaining days in current week
                    for day_abbr in weekdays:
                        if day_abbr in weekday_map:
                            target_weekday = weekday_map[day_abbr]
                            if target_weekday > current_weekday:
                                days_ahead = target_weekday - current_weekday
                                current_date += datetime.timedelta(days=days_ahead)
                                found_next = True
                                break

                    # If no day found in current week, go to next week
                    if not found_next:
                        # Move to next week and find first occurrence
                        days_to_next_week = 7 - current_weekday
                        current_date += datetime.timedelta(days=days_to_next_week)

                        min_weekday = min(
                            weekday_map[day] for day in weekdays if day in weekday_map
                        )
                        days_to_target = min_weekday - current_date.weekday()
                        if days_to_target < 0:
                            days_to_target += 7
                        current_date += datetime.timedelta(days=days_to_target)

                        # Skip additional weeks based on interval
                        if rule.interval > 1:
                            current_date += datetime.timedelta(weeks=rule.interval - 1)
                else:
                    # Simple weekly recurrence
                    current_date += datetime.timedelta(weeks=rule.interval)

            elif rule.frequency == RecurrenceFrequency.MONTHLY:
                # Simple monthly recurrence (same day of month)
                next_month = current_date.month + rule.interval
                next_year = current_date.year + ((next_month - 1) // 12)
                next_month = ((next_month - 1) % 12) + 1

                try:
                    current_date = current_date.replace(year=next_year, month=next_month)
                except ValueError:
                    # Handle cases like Feb 31 -> Feb 28/29
                    last_day = calendar.monthrange(next_year, next_month)[1]
                    if current_date.day > last_day:
                        current_date = current_date.replace(
                            year=next_year, month=next_month, day=last_day
                        )
                    else:
                        break  # Something went wrong, exit

            elif rule.frequency == RecurrenceFrequency.YEARLY:
                try:
                    current_date = current_date.replace(year=current_date.year + rule.interval)
                except ValueError:
                    # Handle leap year edge case (Feb 29)
                    current_date = current_date.replace(
                        year=current_date.year + rule.interval, day=28
                    )
            else:
                # Unknown frequency, break to avoid infinite loop
                break

            occurrence_count += 1

            # Safety check to prevent infinite loops
            if occurrence_count > 1000:  # Reasonable limit
                break

        return instances

    def get_occurrences_in_range(self, start_date, end_date, include_exceptions=True):
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
        occurrences = []

        if self.is_recurring:
            # Get generated instances
            generated_instances = self.generate_instances(start_date, end_date)
            occurrences.extend(generated_instances)

            if include_exceptions and hasattr(self, "recurrence_exceptions"):
                # Add modified exceptions that fall within the date range
                for exception in self.recurrence_exceptions.filter(
                    exception_date__gte=start_date,
                    exception_date__lte=end_date,
                    is_cancelled=False,
                    modified_event__isnull=False,
                ):
                    if exception.modified_event:
                        occurrences.append(exception.modified_event)
        else:
            # For non-recurring events, just check if this event falls in the range
            if start_date <= self.start_time <= end_date:
                occurrences.append(self)

        # Sort by start time
        occurrences.sort(key=lambda x: x.start_time)
        return occurrences

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
