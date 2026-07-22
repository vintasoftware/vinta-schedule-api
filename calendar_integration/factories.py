import datetime

from .constants import CalendarProvider, ExternalEventChangeKind, RecurrenceFrequency
from .models import (
    BookingPolicy,
    CalendarEvent,
    CalendarOwnership,
    EventAttendance,
    ExternalEventChangeRequest,
    RecurrenceRule,
)


def create_calendar_ownership(
    *,
    calendar,
    user,
    is_default: bool = False,
    with_membership: bool = True,
    **kwargs,
) -> CalendarOwnership:
    """Create a ``CalendarOwnership`` wired for the membership-scoped read path.

    Ownership reads/writes resolve owners through the denormalized
    ``membership_user_id`` column and the ``membership`` ForeignObject join to
    ``OrganizationMembership(organization_id, user_id)``. A bare ``user=...``
    ownership (the legacy shape) is therefore invisible to membership-based
    reads. This helper:

    - ensures an active ``OrganizationMembership`` exists for ``(user,
      calendar.organization)`` (unless ``with_membership=False``, which models
      an *orphan* ownership for the orphan-behaviour tests);
    - sets the ``membership_user_id`` denormalized column. Ownership is
      membership-only; there is no ``user`` FK.

    Pass ``with_membership=False`` to create an orphan ownership whose
    ``(user, organization)`` pair has no membership: ``membership_user_id`` is
    left ``NULL`` so membership-based reads exclude it (the intended end state).
    The raw-SQL composite FK to ``OrganizationMembership(user_id,
    organization_id)`` enforces that a non-NULL ``membership_user_id`` references
    a real membership, so the ``with_membership=True`` path must seed one.
    """
    from organizations.models import OrganizationMembership

    organization = calendar.organization

    membership_user_id = None
    if with_membership:
        OrganizationMembership.objects.get_or_create(
            user=user,
            organization=organization,
        )
        membership_user_id = user.id

    return CalendarOwnership.objects.create(
        organization=organization,
        calendar=calendar,
        membership_user_id=membership_user_id,
        is_default=is_default,
        **kwargs,
    )


def create_event_attendance(
    *,
    event,
    user,
    with_membership: bool = True,
    status: str = "pending",
    **kwargs,
) -> EventAttendance:
    """Create an ``EventAttendance`` wired for the membership-scoped read path.

    Attendee identity is carried by the denormalized ``membership_user_id``
    column and the ``membership`` ForeignObject join to
    ``OrganizationMembership(organization_id, user_id)``. There is no ``user`` FK.
    This helper:

    - ensures an active ``OrganizationMembership`` exists for ``(user,
      event.organization)`` (unless ``with_membership=False``, which models an
      *orphan* attendance for the orphan-behaviour tests);
    - sets ``membership_user_id`` so membership-based reads see the attendance.

    Pass ``with_membership=False`` to create an orphan attendance whose ``(user,
    organization)`` pair has no membership: ``membership_user_id`` stays ``NULL``
    so membership-based reads exclude it. The raw-SQL composite FK
    ``evattendance_membership_protect_fk`` enforces that a non-NULL
    ``membership_user_id`` references a real membership, so the
    ``with_membership=True`` path must seed one.
    """
    # Imported late to avoid an import cycle between this factory module and the
    # ``organizations`` app at module load time.
    from organizations.models import OrganizationMembership

    organization = event.organization

    membership_user_id = None
    if with_membership:
        OrganizationMembership.objects.get_or_create(
            user=user,
            organization=organization,
        )
        membership_user_id = user.id

    return EventAttendance.objects.create(
        organization=organization,
        event=event,
        membership_user_id=membership_user_id,
        status=status,
        **kwargs,
    )


class CalendarEventFactory:
    @staticmethod
    def create_recurring_event(
        calendar,
        title: str,
        description: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        frequency: str,
        interval: int = 1,
        count: int | None = None,
        until: datetime.datetime | None = None,
        by_weekday: str | None = None,
        **kwargs,
    ) -> CalendarEvent:
        """
        Create a recurring calendar event with a recurrence rule.

        Args:
            calendar: Calendar instance
            title: Event title
            description: Event description
            start_time: Event start time
            end_time: Event end time
            frequency: Recurrence frequency (DAILY, WEEKLY, MONTHLY, YEARLY)
            interval: Interval between occurrences (default: 1)
            count: Number of occurrences (optional)
            until: End date for recurrence (optional)
            by_weekday: Comma-separated weekdays for weekly recurrence (e.g., "MO,WE,FR")
            **kwargs: Additional CalendarEvent fields

        Returns:
            CalendarEvent instance with recurrence rule
        """
        # Create the recurrence rule
        recurrence_rule = RecurrenceRule.objects.create(
            organization=calendar.organization,
            frequency=frequency,
            interval=interval,
            count=count,
            until=until,
            by_weekday=by_weekday or "",
        )

        # Create the main event
        event = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            title=title,
            description=description,
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone=kwargs.get("timezone", "UTC"),
            recurrence_rule_fk=recurrence_rule,
            **kwargs,
        )

        return event

    @classmethod
    def create_daily_recurring_event(
        cls,
        calendar,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        interval: int = 1,
        count: int | None = None,
        until: datetime.datetime | None = None,
        **kwargs,
    ) -> CalendarEvent:
        """Create a daily recurring event."""
        return cls.create_recurring_event(
            calendar=calendar,
            title=title,
            description=kwargs.get("description", ""),
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            frequency=RecurrenceFrequency.DAILY,
            interval=interval,
            count=count,
            until=until,
            **kwargs,
        )

    @classmethod
    def create_weekly_recurring_event(
        cls,
        calendar,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        weekdays: list[str] | None = None,
        interval: int = 1,
        count: int | None = None,
        until: datetime.datetime | None = None,
        **kwargs,
    ) -> CalendarEvent:
        """
        Create a weekly recurring event.

        Args:
            weekdays: List of weekday abbreviations (e.g., ["MO", "WE", "FR"])
        """
        by_weekday = ",".join(weekdays) if weekdays else None
        return cls.create_recurring_event(
            calendar=calendar,
            title=title,
            description=kwargs.get("description", ""),
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            frequency=RecurrenceFrequency.WEEKLY,
            interval=interval,
            count=count,
            until=until,
            by_weekday=by_weekday,
            **kwargs,
        )

    @classmethod
    def create_monthly_recurring_event(
        cls,
        calendar,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        interval: int = 1,
        count: int | None = None,
        until: datetime.datetime | None = None,
        **kwargs,
    ) -> CalendarEvent:
        """Create a monthly recurring event."""
        return cls.create_recurring_event(
            calendar=calendar,
            title=title,
            description=kwargs.get("description", ""),
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            frequency=RecurrenceFrequency.MONTHLY,
            interval=interval,
            count=count,
            until=until,
            **kwargs,
        )

    @classmethod
    def get_events_in_range(
        cls,
        calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_recurring: bool = True,
    ) -> list[CalendarEvent]:
        """
        Get all events (recurring and non-recurring) in a date range for a calendar.

        Args:
            calendar: Calendar instance
            start_date: Start of date range
            end_date: End of date range
            include_recurring: Whether to expand recurring events into instances

        Returns:
            List of CalendarEvent instances
        """
        events = []

        # Get all events that start within the range
        calendar_events = calendar.events.annotate_recurring_occurrences_on_date_range().filter(
            start_time__lte=end_date, end_time__gte=start_date
        )

        for event in calendar_events:
            if event.is_recurring and include_recurring:
                # Get all occurrences of this recurring event in the range
                occurrences = event.get_occurrences_in_range(start_date, end_date)
                events.extend(occurrences)
            elif not event.is_recurring_instance:  # Avoid duplicating instances
                events.append(event)

        # Sort by start time
        events.sort(key=lambda x: x.start_time)
        return events


def create_external_event_change_request(
    *,
    event: CalendarEvent,
    kind: str = ExternalEventChangeKind.UPDATE,
    status: str = "pending",
    provider: str = CalendarProvider.GOOGLE,
    proposed_values: dict | None = None,
    proposed_payload: dict | None = None,
    retained_values: dict | None = None,
    organization=None,
    **kwargs,
) -> ExternalEventChangeRequest:
    """Create an ``ExternalEventChangeRequest`` linked to *event*.

    Defaults to a ``PENDING`` / ``update`` / ``google`` request with empty JSON
    fields.  Override any field via keyword arguments.

    The request is scoped to the same organization as *event*. If an explicit
    ``organization`` is passed it must match ``event.organization``; a mismatch
    raises ``ValueError`` to prevent silent cross-tenant data creation.
    """
    effective_org = event.organization
    if organization is not None and organization != effective_org:
        raise ValueError(
            f"organization mismatch: passed {organization!r} but event belongs to {effective_org!r}"
        )
    return ExternalEventChangeRequest.objects.create(
        organization=effective_org,
        event=event,
        kind=kind,
        status=status,
        provider=provider,
        proposed_values=proposed_values if proposed_values is not None else {},
        proposed_payload=proposed_payload if proposed_payload is not None else {},
        retained_values=retained_values if retained_values is not None else {},
        **kwargs,
    )


def create_booking_policy(
    *,
    calendar=None,
    membership_user_id: int | None = None,
    calendar_group=None,
    is_organization_default: bool = False,
    organization=None,
    lead_time_seconds: int = 0,
    max_horizon_seconds: int = 0,
    buffer_before_seconds: int = 0,
    buffer_after_seconds: int = 0,
    **kwargs,
) -> BookingPolicy:
    """Create a valid single-target ``BookingPolicy``.

    Exactly one target must be set: ``calendar`` (the default — pass a
    ``Calendar`` to attach a calendar-scoped policy), ``membership_user_id``,
    ``calendar_group``, or ``is_organization_default=True``. Passing zero or more
    than one target raises ``ValueError`` before hitting the DB, so tests that
    want to exercise the ``bookingpolicy_exactly_one_target`` check constraint
    should build the row directly via ``BookingPolicy.objects.create(...)``.

    The organization is inferred from the provided ``calendar`` /
    ``calendar_group`` target; for membership / organization-default targets the
    ``organization`` kwarg is required. A mismatch between an explicit
    ``organization`` and the target's organization raises ``ValueError``.
    """
    targets = [
        calendar is not None,
        membership_user_id is not None,
        calendar_group is not None,
        is_organization_default,
    ]
    if sum(targets) != 1:
        raise ValueError(
            "create_booking_policy requires exactly one target: calendar, "
            "membership_user_id, calendar_group, or is_organization_default."
        )

    effective_org = organization
    if calendar is not None:
        effective_org = calendar.organization
    elif calendar_group is not None:
        effective_org = calendar_group.organization

    if effective_org is None:
        raise ValueError(
            "organization is required when the target is a membership or the organization default."
        )
    if (
        organization is not None
        and (calendar is not None or calendar_group is not None)
        and organization != effective_org
    ):
        raise ValueError(
            f"organization mismatch: passed {organization!r} but target belongs to "
            f"{effective_org!r}"
        )

    return BookingPolicy.objects.create(
        organization=effective_org,
        calendar=calendar,
        membership_user_id=membership_user_id,
        calendar_group=calendar_group,
        is_organization_default=is_organization_default,
        lead_time_seconds=lead_time_seconds,
        max_horizon_seconds=max_horizon_seconds,
        buffer_before_seconds=buffer_before_seconds,
        buffer_after_seconds=buffer_after_seconds,
        **kwargs,
    )
