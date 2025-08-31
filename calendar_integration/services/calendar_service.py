import copy
import datetime
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any, Literal, cast

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import (
    CalendarOrganizationResourceImportStatus,
    CalendarProvider,
    CalendarSyncStatus,
    CalendarType,
)
from calendar_integration.models import (
    AvailableTime,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarOwnership,
    CalendarSync,
    ChildrenCalendarRelationship,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
    RecurringMixin,
    ResourceAllocation,
)
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    AvailableTimeWindow,
    BlockedTimeData,
    CalendarEventAdapterInputData,
    CalendarEventData,
    CalendarEventInputData,
    CalendarResourceData,
    EventAttendanceInputData,
    EventAttendeeData,
    EventExternalAttendanceInputData,
    EventsSyncChanges,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
    ResourceData,
    UnavailableTimeWindow,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from calendar_integration.services.type_guards import (
    is_authenticated_calendar_service,
    is_initialized_or_authenticated_calendar_service,
)
from organizations.models import Organization


User = get_user_model()


class CalendarService(BaseCalendarService):
    organization: Organization | None
    account: SocialAccount | GoogleCalendarServiceAccount | None
    calendar_adapter: CalendarAdapter | None

    def __init__(self) -> None:
        """Initialize a CalendarService instance. Call authenticate() before using calendar operations."""
        self.organization = None
        self.account = None
        self.calendar_adapter = None

    @staticmethod
    def get_calendar_adapter_for_account(
        account: SocialAccount | GoogleCalendarServiceAccount,
    ) -> CalendarAdapter:
        """
        Retrieve a calendar adapter for the given social account.
        :param account: Social account instance or GoogleCalendarServiceAccount instance.
        :return: CalendarAdapter instance
        """
        if isinstance(account, GoogleCalendarServiceAccount):
            from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
                GoogleCalendarAdapter,
            )

            return GoogleCalendarAdapter.from_service_account_credentials(
                {
                    "account_id": str(account.id),
                    "email": account.email,
                    "public_key": account.public_key,
                    "private_key_id": account.private_key_id,
                    "private_key": account.private_key,
                    "audience": account.audience,
                }
            )

        now = datetime.datetime.now(datetime.UTC)
        token: SocialToken = (
            account.socialtoken_set.filter(expires_at__gte=now).order_by("-id").first()
        )

        if account.provider == CalendarProvider.GOOGLE:
            from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
                GoogleCalendarAdapter,
            )

            return GoogleCalendarAdapter(
                credentials_dict={
                    "token": token.token,
                    "refresh_token": token.token_secret,
                    "account_id": f"social-{account.id}",
                }
            )

        if account.provider == CalendarProvider.MICROSOFT:
            from calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter import (
                MSOutlookCalendarAdapter,
            )

            return MSOutlookCalendarAdapter(
                credentials_dict={
                    "token": token.token,
                    "refresh_token": token.token_secret,
                    "account_id": f"social-{account.id}",
                }
            )

        raise NotImplementedError(
            f"Calendar adapter for provider {account.provider} is not implemented."
        )

    def authenticate(
        self,
        account: SocialAccount | GoogleCalendarServiceAccount,
        organization: Organization,
    ) -> None:
        """
        Authenticate the service with the provided social account.
        :param account: Social account instance or GoogleCalendarServiceAccount instance.
        :param organization: Calendar organization instance.
        """
        self.account = account
        self.organization = organization
        self.calendar_adapter = self.get_calendar_adapter_for_account(account)

    def initialize_without_provider(
        self,
        organization: Organization | None = None,
    ):
        """
        Initialize the service without a specific calendar provider.
        :param organization: Calendar organization instance.
        """
        self.organization = organization
        self.account = None
        self.calendar_adapter = None

    def request_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        from calendar_integration.tasks import import_organization_calendar_resources_task

        if not is_authenticated_calendar_service(self):
            raise

        import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
            organization=self.organization,
            start_time=start_time,
            end_time=end_time,
        )

        import_organization_calendar_resources_task.delay(  # type: ignore
            account_type="google_service_account"
            if isinstance(self.account, GoogleCalendarServiceAccount)
            else "social_account",
            account_id=self.account.id,
            organization_id=self.organization.id,
            import_workflow_state_id=import_workflow_state.id,
        )

    def import_organization_calendar_resources(
        self,
        import_workflow_state: CalendarOrganizationResourcesImport,
    ) -> None:
        """
        Import organization calendar resources within a specified time range.
        :param start_time: Start time for the availability check.
        :param end_time: End time for the availability check.
        :return: List of available resources.
        """
        if not is_authenticated_calendar_service(self):
            raise

        import_workflow_state.status = CalendarOrganizationResourceImportStatus.IN_PROGRESS
        import_workflow_state.save(update_fields=["status"])

        try:
            with transaction.atomic():
                self._execute_organization_calendar_resources_import(
                    start_time=import_workflow_state.start_time,
                    end_time=import_workflow_state.end_time,
                )
        except Exception as e:  # noqa: BLE001
            import_workflow_state.status = CalendarOrganizationResourceImportStatus.FAILED
            import_workflow_state.error_message = str(e)
            import_workflow_state.save(update_fields=["status", "error_message"])
            return

        import_workflow_state.status = CalendarOrganizationResourceImportStatus.SUCCESS
        import_workflow_state.save(update_fields=["status"])

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> Iterable[CalendarResourceData]:
        """
        Import organization calendar resources within a specified time range.
        :param start_time: Start time for the availability check.
        :param end_time: End time for the availability check.
        :return: List of available resources.
        """
        if not is_authenticated_calendar_service(self):
            raise

        if not self.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

        resources = self.calendar_adapter.get_available_calendar_resources(start_time, end_time)
        for resource in resources:
            self.request_calendar_sync(
                calendar=Calendar.objects.get_or_create(
                    external_id=resource.external_id,
                    organization=self.organization,
                    defaults={
                        "name": resource.name,
                        "description": resource.description,
                        "provider": CalendarProvider(resource.provider),
                        "email": resource.email,
                        "calendar_type": CalendarType.RESOURCE,
                    },
                )[0],
                start_datetime=start_time,
                end_datetime=end_time,
                should_update_events=True,
            )
        return resources

    def create_application_calendar(
        self, name: str, organization: Organization
    ) -> ApplicationCalendarData:
        """
        Create a new application calendar using the calendar adapter.
        :return: Created ApplicationCalendarData instance.
        """
        if not is_authenticated_calendar_service(self):
            raise

        if self.calendar_adapter:
            created_calendar = self.calendar_adapter.create_application_calendar(name)
        else:
            created_calendar = ApplicationCalendarData(
                id=None,
                organization_id=organization.id,
                external_id="",
                name=name,
                description=None,
                email=None,
                provider=CalendarProvider.INTERNAL,
                original_payload={},
            )

        calendar = Calendar.objects.create(
            organization=organization,
            external_id=created_calendar.external_id,
            name=created_calendar.name,
            description=created_calendar.description,
            provider=self.calendar_adapter.provider
            if self.calendar_adapter
            else CalendarProvider.INTERNAL,
            original_payload=created_calendar.original_payload or {},
        )

        if self.calendar_adapter:
            if isinstance(self.account, GoogleCalendarServiceAccount):
                self.account.calendar = calendar
                self.account.save(update_fields=["calendar_fk"])
            self.request_calendar_sync(
                calendar=calendar,
                start_datetime=datetime.datetime.now(datetime.UTC),
                end_datetime=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
                should_update_events=True,
            )

        return created_calendar

    @lru_cache(maxsize=128)  # noqa: B019
    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar:
        if not is_authenticated_calendar_service(self):
            raise

        query_kwargs = {
            "external_id": calendar_external_id,
            "organization_id": self.organization.id,
        }
        if self.calendar_adapter:
            query_kwargs["provider"] = self.calendar_adapter.provider
        return Calendar.objects.get(**query_kwargs)

    @lru_cache(maxsize=128)  # noqa: B019
    def _get_calendar_by_id(self, calendar_id: int) -> Calendar:
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        return Calendar.objects.get(
            id=calendar_id,
            organization_id=self.organization.id,
        )

    @transaction.atomic()
    def request_calendars_import(self) -> None:
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.
        """
        from calendar_integration.tasks import import_account_calendars_task

        if not is_authenticated_calendar_service(self):
            raise

        if not self.account or not self.account.id:
            raise NotImplementedError("Account is not set for the current service instance.")

        import_account_calendars_task.delay(  # type: ignore
            account_type="google_service_account"
            if isinstance(self.account, GoogleCalendarServiceAccount)
            else "social_account",
            account_id=self.account.id,
            organization_id=self.organization.id,
        )

    @transaction.atomic()
    def import_account_calendars(self):
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.
        """
        if not is_authenticated_calendar_service(self):
            raise

        calendars = self.calendar_adapter.get_account_calendars()

        for calendar_data in calendars:
            calendar, _ = Calendar.objects.update_or_create(
                external_id=calendar_data.external_id,
                organization=self.organization,
                calendar_type=CalendarType.PERSONAL,
                defaults={
                    "name": calendar_data.name,
                    "description": calendar_data.description,
                    "email": calendar_data.email,
                    "provider": CalendarProvider(calendar_data.provider),
                    "meta": {
                        "latest_original_payload": calendar_data.original_payload or {},
                    },
                },
            )
            CalendarOwnership.objects.update_or_create(
                organization=self.organization,
                calendar=calendar,
                user=self.account.user if self.account else None,
                defaults={"is_default": calendar_data.is_default},
            )
            self.request_calendar_sync(
                calendar=calendar,
                start_datetime=datetime.datetime.now(datetime.UTC),
                end_datetime=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
                should_update_events=True,
            )

    @transaction.atomic()
    def create_virtual_calendar(
        self,
        name: str,
        description: str | None = None,
    ) -> Calendar:
        """
        Create a new calendar in the application without linking to an external provider.
        :param name: Name of the calendar.
        :param description: Description of the calendar.
        :return: Created Calendar instance.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        return Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.VIRTUAL,
            original_payload={},
        )

    @transaction.atomic()
    def create_bundle_calendar(
        self,
        name: str,
        description: str | None = None,
        child_calendars: Iterable[Calendar] | None = None,
        primary_calendar: Calendar | None = None,
    ) -> Calendar:
        """
        Create a new bundle calendar in the application without linking to an external provider.
        :param name: Name of the calendar.
        :param description: Description of the calendar.
        :param child_calendars: Iterable of child Calendar instances to include in the bundle.
        :param primary_calendar: The child calendar to be designated as primary. Must be in child_calendars.
        :return: Created Calendar instance.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        child_calendars_list = list(child_calendars or [])

        # Validate primary calendar
        if primary_calendar and primary_calendar not in child_calendars_list:
            raise ValueError("Primary calendar must be one of the child calendars")

        bundle_calendar = Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description or "",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.BUNDLE,
        )

        for calendar in child_calendars_list:
            if calendar.organization_id != self.organization.id:
                raise ValueError(
                    "All child calendars must belong to the same organization as the bundle."
                )

            if calendar.calendar_type == CalendarType.BUNDLE:
                raise ValueError(
                    "Child calendars of a bundle must not be bundle calendars themselves."
                )

            is_primary = primary_calendar is not None and calendar.id == primary_calendar.id
            ChildrenCalendarRelationship.objects.create(
                bundle_calendar=bundle_calendar,
                child_calendar=calendar,
                organization=self.organization,
                is_primary=is_primary,
            )
        return bundle_calendar

    def _create_bundle_event(
        self, bundle_calendar: Calendar, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        """
        Create an event in a bundle calendar by:
        1. Selecting a primary PROVIDER calendar or defaulting to INTERNAL
        2. Creating the main event in the primary calendar
        3. Creating BlockedTime entries in other PROVIDER calendars
        4. Creating CalendarEvent entries in INTERNAL calendars
        5. Adding users from non-primary calendars as attendees
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if bundle_calendar.calendar_type != CalendarType.BUNDLE:
            raise ValueError("Calendar must be a bundle calendar")

        child_calendars = list(bundle_calendar.bundle_children.all())
        if not child_calendars:
            raise ValueError("Bundle calendar has no child calendars")

        # Check availability across all child calendars
        for child_calendar in child_calendars:
            available_windows = self.get_availability_windows_in_range(
                child_calendar, event_data.start_time, event_data.end_time
            )
            if not available_windows:
                raise ValueError(f"No availability in child calendar {child_calendar.name}")

        # Get the designated primary calendar
        primary_calendar = self._get_primary_calendar(bundle_calendar)

        # Collect all attendees from child calendar ownerships
        all_attendees = self._collect_bundle_attendees(child_calendars, event_data)

        # Create the primary event
        primary_event_data = CalendarEventInputData(
            title=event_data.title,
            description=event_data.description,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            attendances=all_attendees,
            external_attendances=event_data.external_attendances,
            resource_allocations=event_data.resource_allocations,
            recurrence_rule=event_data.recurrence_rule,
        )

        primary_event = self.create_event(primary_calendar.id, primary_event_data)

        # Mark primary event as part of bundle
        primary_event.bundle_calendar = bundle_calendar
        primary_event.is_bundle_primary = True
        primary_event.save()

        # Create representations in other calendars
        for child_calendar in child_calendars:
            if child_calendar.id == primary_calendar.id:
                continue

            if child_calendar.provider == CalendarProvider.INTERNAL:
                # Create full CalendarEvent for internal calendars
                child_event_data = CalendarEventInputData(
                    title=f"[Bundle] {event_data.title}",
                    description=f"Bundle event from {bundle_calendar.name}\n\n{event_data.description}",
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    attendances=[],  # No direct attendances for linked events
                    external_attendances=[],
                    resource_allocations=[],
                )

                child_event = self.create_event(child_calendar.id, child_event_data)

                # Link to primary event and bundle
                child_event.bundle_calendar = bundle_calendar
                child_event.bundle_primary_event = primary_event
                child_event.save()

            else:
                # Create BlockedTime for other PROVIDER calendars
                BlockedTime.objects.create(
                    calendar=child_calendar,
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    reason=f"Bundle event: {event_data.title}",
                    organization=child_calendar.organization,
                    bundle_calendar=bundle_calendar,
                    bundle_primary_event=primary_event,
                )

        return primary_event

    def _get_primary_calendar(self, bundle_calendar: Calendar) -> Calendar:
        """Get the designated primary calendar for a bundle."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        primary_relationship = ChildrenCalendarRelationship.objects.filter(
            bundle_calendar=bundle_calendar,
            is_primary=True,
            organization=self.organization,
        ).first()

        if not primary_relationship:
            raise ValueError("Bundle calendar has no designated primary child calendar")

        return primary_relationship.child_calendar

    def _collect_bundle_attendees(
        self, child_calendars: list[Calendar], event_data: "CalendarEventInputData"
    ) -> list["EventAttendanceInputData"]:
        """Collect attendees from calendar ownerships and explicit attendances."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        attendee_user_ids = {attendance.user_id for attendance in event_data.attendances}

        # Add users who own child calendars
        calendar_owners = User.objects.filter(
            calendar_ownerships__calendar__in=child_calendars,
            calendar_ownerships__organization=self.organization,
        ).distinct()

        for owner in calendar_owners:
            attendee_user_ids.add(owner.id)

        return [EventAttendanceInputData(user_id=user_id) for user_id in attendee_user_ids]

    @transaction.atomic()
    def create_event(self, calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        """
        Create a new event in the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_data: Dictionary containing event details.
        :return: Response from the calendar client.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar = self._get_calendar_by_id(calendar_id)

        if calendar.calendar_type == CalendarType.BUNDLE:
            return self._create_bundle_event(bundle_calendar=calendar, event_data=event_data)

        available_windows = self.get_availability_windows_in_range(
            calendar,
            event_data.start_time,
            event_data.end_time,
        )
        if not available_windows:
            raise ValueError("No available time windows for the event.")

        external_id = ""
        original_payload: dict = {}
        if self.calendar_adapter:
            users_by_id = {
                u.id: u
                for u in User.objects.filter(id__in=[a.user_id for a in event_data.attendances])
            }
            resources_by_id = {
                r.id: r
                for r in Calendar.objects.filter_by_organization(self.organization.id).filter(
                    id__in=[r.resource_id for r in event_data.resource_allocations]
                )
            }
            created_event = self.calendar_adapter.create_event(
                CalendarEventAdapterInputData(
                    calendar_external_id=calendar.external_id,
                    title=event_data.title,
                    description=event_data.description,
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    attendees=[
                        EventAttendeeData(
                            email=users_by_id[a.user_id].email,
                            name=(
                                users_by_id[a.user_id].get_full_name()
                                if hasattr(users_by_id[a.user_id], "profile")
                                and hasattr(users_by_id[a.user_id].profile, "__str__")
                                else None
                            )
                            or users_by_id[a.user_id].username,
                            status="pending",
                        )
                        for a in event_data.attendances
                    ],
                    resources=[
                        ResourceData(
                            email=resources_by_id[r.resource_id].email,
                            title=resources_by_id[r.resource_id].name,
                            external_id=resources_by_id[r.resource_id].external_id,
                            status="accepted",
                        )
                        for r in event_data.resource_allocations
                    ],
                    recurrence_rule=event_data.recurrence_rule,
                    is_recurring_instance=event_data.is_recurring_exception,
                )
            )
            external_id = created_event.external_id
            original_payload = created_event.original_payload or {}

        # Handle parent event for exceptions/instances
        parent_event = None
        if event_data.parent_event_id:
            parent_event = CalendarEvent.objects.get(
                id=event_data.parent_event_id,
                organization_id=self.organization.id,
            )

        # Create recurrence rule if provided
        recurrence_rule = None
        if event_data.recurrence_rule and not event_data.parent_event_id:
            recurrence_rule = RecurrenceRule.from_rrule_string(
                event_data.recurrence_rule, self.organization
            )
            recurrence_rule.save()

        # Create the event using the manager's create method to ensure proper organization handling
        event = CalendarEvent(
            calendar_fk=calendar,
            organization=self.organization,
            title=event_data.title,
            description=event_data.description,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            external_id=external_id,
            meta={"latest_original_payload": original_payload} if self.calendar_adapter else {},
            parent_recurring_object_fk=parent_event,
            is_recurring_exception=event_data.is_recurring_exception,
            recurrence_id=event_data.start_time if parent_event else None,
        )

        if recurrence_rule:
            event.recurrence_rule_fk = recurrence_rule  # type: ignore

        event.save()

        EventExternalAttendance.objects.bulk_create(
            [
                EventExternalAttendance(
                    organization=self.organization,
                    event=event,
                    external_attendee=ExternalAttendee.objects.create(
                        organization=self.organization,
                        email=attendance_data.external_attendee.email,
                        name=attendance_data.external_attendee.name,
                    ),
                )
                for attendance_data in event_data.external_attendances
            ]
        )

        EventAttendance.objects.bulk_create(
            [
                EventAttendance(
                    organization=self.organization,
                    event=event,
                    user_id=attendance_data.user_id,
                )
                for attendance_data in event_data.attendances
            ]
        )

        ResourceAllocation.objects.bulk_create(
            [
                ResourceAllocation(
                    organization=self.organization,
                    event=event,
                    calendar_fk_id=resource_allocation_data.resource_id,
                )
                for resource_allocation_data in event_data.resource_allocations
            ]
        )

        return event

    def _update_bundle_event(
        self, bundle_event: CalendarEvent, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        """Update a bundle event and all its representations."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not bundle_event.is_bundle_primary:
            raise ValueError("Event must be a bundle primary event")

        bundle_calendar = bundle_event.bundle_calendar

        # Update the primary event
        updated_primary = self.update_event(bundle_event.calendar.id, bundle_event.id, event_data)

        # Update all representation events
        if not self.organization:
            raise ValueError("Organization is required for bundle operations")

        representation_events = CalendarEvent.objects.filter(
            organization_id=self.organization.id, bundle_primary_event=bundle_event
        )

        for representation_event in representation_events:
            representation_data = CalendarEventInputData(
                title=f"[Bundle] {event_data.title}",
                description=f"Bundle event from {bundle_calendar.name}\n\n{event_data.description}",
                start_time=event_data.start_time,
                end_time=event_data.end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            self.update_event(
                representation_event.calendar.id,
                representation_event.id,
                representation_data,
            )

        # Update all blocked time representations
        blocked_time_representations = BlockedTime.objects.filter(
            organization_id=self.organization.id, bundle_primary_event=bundle_event
        )

        for blocked_time in blocked_time_representations:
            blocked_time.start_time = event_data.start_time
            blocked_time.end_time = event_data.end_time
            blocked_time.reason = f"Bundle event: {event_data.title}"
            blocked_time.save(update_fields=["start_time", "end_time", "reason"])

        return updated_primary

    @transaction.atomic()
    def update_event(
        self, calendar_id: int, event_id: int, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        """
        Update an existing event in the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_id: Unique identifier of the event to update.
        :param event_data: Dictionary containing updated event details.
        :return: Updated CalendarEvent instance.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar_event = CalendarEvent.objects.select_related("calendar").get(
            calendar_fk_id=calendar_id,
            id=event_id,
            organization_id=self.organization.id,
        )

        if calendar_event.is_bundle_primary:
            return self._update_bundle_event(calendar_event, event_data)
        elif calendar_event.is_bundle_event:
            raise ValueError(
                "Cannot update an event created from bundle calendar from a non-primary "
                "calendar event"
            )

        original_payload = {}
        if self.calendar_adapter:
            users_by_id = {
                u.id: u
                for u in User.objects.filter(id__in=[a.user_id for a in event_data.attendances])
            }
            attendance_status_by_user_id = {
                a.user_id: a.status
                for a in EventAttendance.objects.filter_by_organization(
                    self.organization.id
                ).filter(event__id=event_id, user_id__in=users_by_id.keys())
            }
            resources_by_id = {
                r.id: r
                for r in Calendar.objects.filter_by_organization(self.organization.id).filter(
                    id__in=[r.resource_id for r in event_data.resource_allocations]
                )
            }
            updated_event = self.calendar_adapter.update_event(
                calendar_event.calendar.id,
                calendar_event.id,
                CalendarEventData(
                    calendar_external_id=calendar_event.calendar.external_id,
                    title=event_data.title,
                    description=event_data.description,
                    start_time=event_data.start_time,
                    end_time=event_data.end_time,
                    attendees=[
                        EventAttendeeData(
                            email=users_by_id[a.user_id].email,
                            name=(
                                users_by_id[a.user_id].get_full_name()
                                if hasattr(users_by_id[a.user_id], "profile")
                                and hasattr(users_by_id[a.user_id].profile, "__str__")
                                else None
                            )
                            or users_by_id[a.user_id].username,
                            status=(
                                attendance_status_by_user_id[a.user_id]
                                if a.user_id in attendance_status_by_user_id
                                else "pending"
                            ),
                        )
                        for a in event_data.attendances
                    ],
                    id=calendar_event.id,
                    external_id=calendar_event.external_id,
                    resources=[
                        ResourceData(
                            email=resources_by_id[r.resource_id].email,
                            title=resources_by_id[r.resource_id].name,
                            external_id=resources_by_id[r.resource_id].external_id,
                            status="accepted",
                        )
                        for r in event_data.resource_allocations
                    ],
                ),
            )
            original_payload = updated_event.original_payload or {}

        calendar_event.title = event_data.title
        calendar_event.description = event_data.description
        calendar_event.start_time = event_data.start_time
        calendar_event.end_time = event_data.end_time
        if self.calendar_adapter:
            calendar_event.meta["latest_original_payload"] = original_payload

        # update recurrence rule
        if event_data.recurrence_rule:
            recurrence_rule = RecurrenceRule.from_rrule_string(
                rrule_string=event_data.recurrence_rule,
                organization=self.organization,
            )
            if calendar_event.recurrence_rule:
                recurrence_rule.id = calendar_event.recurrence_rule.id
            recurrence_rule.save()
            calendar_event.recurrence_rule = recurrence_rule
        elif calendar_event.recurrence_rule:
            # turn recurring event into non-recurring
            calendar_event.recurrence_rule.delete()
            calendar_event.recurrence_rule = None

        calendar_event.save()

        existing_attendances = {a.user_id: a for a in calendar_event.attendances.all()}
        existing_external_attendances = {
            a.external_attendee_fk_id: a for a in calendar_event.external_attendances.all()
        }
        existing_resource_allocation = {
            r.calendar_fk_id: r for r in calendar_event.resource_allocations.all()
        }

        maintained_external_attendees_ids = []
        external_attendees_to_update = []
        external_attendees_to_create = []
        external_attenances_to_create = []
        for external_attendance_data in event_data.external_attendances:
            if (
                external_attendance_data.external_attendee.id
                and external_attendance_data.external_attendee.id
                in existing_external_attendances.keys()
            ):
                attendance_to_update = existing_external_attendances[
                    external_attendance_data.external_attendee.id
                ]
                attendance_to_update.external_attendee.email = (
                    external_attendance_data.external_attendee.email
                )
                attendance_to_update.external_attendee.name = (
                    external_attendance_data.external_attendee.name
                )
                external_attendees_to_update.append(attendance_to_update.external_attendee)
            else:
                external_attendee = ExternalAttendee(
                    organization=self.organization,
                    email=external_attendance_data.external_attendee.email,
                    name=external_attendance_data.external_attendee.name,
                )
                external_attendees_to_create.append(external_attendee)
                external_attenances_to_create.append(
                    EventExternalAttendance(
                        organization=self.organization,
                        event=calendar_event,
                        external_attendee=external_attendee,
                    )
                )
            if external_attendance_data.external_attendee:
                maintained_external_attendees_ids.append(
                    external_attendance_data.external_attendee.id
                )
        ExternalAttendee.objects.bulk_update(external_attendees_to_update, ["email", "name"])
        ExternalAttendee.objects.bulk_create(external_attendees_to_create)
        EventExternalAttendance.objects.bulk_create(external_attenances_to_create)

        external_attendees_to_delete = set(existing_external_attendances.keys()) - set(
            maintained_external_attendees_ids
        )

        EventExternalAttendance.objects.filter_by_organization(self.organization.id).filter(
            external_attendee_fk_id__in=external_attendees_to_delete
        ).delete()
        ExternalAttendee.objects.filter_by_organization(self.organization.id).filter(
            id__in=external_attendees_to_delete
        ).delete()

        maintained_attendees_ids = []
        event_attendances_to_create = []
        for attendance_data in event_data.attendances:
            if not existing_attendances.get(attendance_data.user_id):
                event_attendances_to_create.append(
                    EventAttendance(
                        organization=self.organization,
                        event=calendar_event,
                        user_id=attendance_data.user_id,
                    )
                )
            maintained_attendees_ids.append(attendance_data.user_id)

        EventAttendance.objects.bulk_create(event_attendances_to_create)
        attendances_to_delete = set(existing_attendances.keys()) - set(maintained_attendees_ids)
        EventAttendance.objects.filter_by_organization(self.organization.id).filter(
            user_id__in=attendances_to_delete
        ).delete()

        maintained_resources_ids = []
        resource_allocations_to_create = []
        for resource_allocation_data in event_data.resource_allocations:
            if resource_allocation_data.resource_id not in existing_resource_allocation.keys():
                resource_allocations_to_create.append(
                    ResourceAllocation(
                        organization_id=self.organization.id,
                        event=calendar_event,
                        calendar_fk_id=resource_allocation_data.resource_id,
                    )
                )
            maintained_resources_ids.append(resource_allocation_data.resource_id)

        ResourceAllocation.objects.bulk_create(resource_allocations_to_create)
        resources_to_delete = set(existing_resource_allocation) - set(maintained_resources_ids)
        ResourceAllocation.objects.filter_by_organization(self.organization.id).filter(
            calendar_fk_id__in=resources_to_delete
        ).delete()

        return calendar_event

    def create_recurring_event(
        self,
        calendar_id: int,
        title: str,
        description: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        recurrence_rule: str,
        attendances: list[EventAttendanceInputData] | None = None,
        external_attendances: list[EventExternalAttendanceInputData] | None = None,
        resource_allocations: list[ResourceAllocationInputData] | None = None,
    ) -> CalendarEvent:
        """
        Create a recurring event with the specified recurrence rule.

        This method is just a shortcut, the `create_event` method also supports the
        creation of recurring events.

        :param calendar_id: Internal ID of the calendar
        :param title: Event title
        :param description: Event description
        :param start_time: Start time for the first occurrence
        :param end_time: End time for the first occurrence
        :param recurrence_rule: RRULE string defining the recurrence pattern
        :param attendances: List of internal attendees
        :param external_attendances: List of external attendees
        :param resource_allocations: List of resource allocations
        :return: Created CalendarEvent with recurrence rule
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        event_data = CalendarEventInputData(
            title=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            recurrence_rule=recurrence_rule,
            attendances=attendances or [],
            external_attendances=external_attendances or [],
            resource_allocations=resource_allocations or [],
        )
        return self.create_event(calendar_id, event_data)

    def _create_recurring_exception_generic(
        self,
        object_type_name: str,
        parent_object: RecurringMixin,
        exception_date: datetime.date,
        is_cancelled: bool,
        modification_data: dict[str, Any] | None = None,
        create_new_recurring_callback: Callable[
            [RecurringMixin, RecurringMixin, RecurrenceRule], RecurringMixin
        ]
        | None = None,
        create_modified_object_callback: Callable[
            [RecurringMixin, datetime.datetime, dict[str, Any]], RecurringMixin
        ]
        | None = None,
        exception_manager_update_callback: Callable[[RecurringMixin, RecurringMixin], None]
        | None = None,
        exception_manager_delete_callback: Callable[[RecurringMixin], None] | None = None,
    ) -> RecurringMixin | None:
        """
        Generic method for creating exceptions for recurring objects (events, blocked times, available times).

        :param parent_object: The recurring object to create an exception for
        :param exception_date: The datetime of the occurrence to modify/cancel
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :param modification_data: Dictionary of fields to modify (if not cancelled)
        :param create_new_recurring_callback: Callback to create new recurring object after master modification
        :param create_modified_object_callback: Callback to create modified object for non-cancelled exceptions
        :param exception_manager_update_callback: Callback to update exception manager references
        :param exception_manager_delete_callback: Callback to delete exception manager references
        :return: Created/modified object or None if cancelled
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not parent_object.is_recurring:
            raise ValueError(f"Cannot create exception for non-recurring {object_type_name}")

        exception_datetime = datetime.datetime.combine(
            exception_date, parent_object.start_time.time(), tzinfo=parent_object.start_time.tzinfo
        )

        if exception_date == parent_object.start_time.date():
            # Exception is on the master object date
            second_occurrence = parent_object.get_next_occurrence(exception_datetime)
            old_recurrence_rule = parent_object.recurrence_rule

            if not second_occurrence:
                # No future occurrences, make the object non-recurring
                if exception_manager_delete_callback:
                    exception_manager_delete_callback(parent_object)
                if old_recurrence_rule:
                    old_recurrence_rule.delete()
            else:
                # Create new recurring object starting from second occurrence
                new_recurrence_rule: RecurrenceRule = copy.copy(old_recurrence_rule)
                new_recurrence_rule.id = None
                new_recurrence_rule.count = (
                    new_recurrence_rule.count - 1 if new_recurrence_rule.count else None
                )

                if create_new_recurring_callback:
                    new_recurring_object = create_new_recurring_callback(
                        parent_object, second_occurrence, new_recurrence_rule
                    )
                    if exception_manager_update_callback:
                        exception_manager_update_callback(parent_object, new_recurring_object)

                if old_recurrence_rule:
                    old_recurrence_rule.delete()

            # Update the master object to be non-recurring
            parent_object.recurrence_rule_fk_id = None
            if modification_data:
                for field, value in modification_data.items():
                    if value is not None:
                        setattr(parent_object, field, value)
                    # Keep original value if modification is None (fallback behavior)
            parent_object.save()

            # Return the updated master object
            from django.db import models

            parent_model = cast(models.Model, parent_object)
            return parent_object.__class__.objects.get(
                organization_id=parent_object.organization_id, id=parent_model.pk
            )

        # Exception is on a future occurrence
        if is_cancelled:
            parent_object.create_exception(exception_datetime, is_cancelled=True)
            return None
        else:
            # Create modified object for the specific occurrence
            if create_modified_object_callback:
                modified_object = create_modified_object_callback(
                    parent_object, exception_datetime, modification_data or {}
                )
                modified_object.is_recurring_exception = True
                modified_object.save()

                parent_object.create_exception(
                    exception_datetime, is_cancelled=False, modified_object=modified_object
                )
                return modified_object
            return None

    def create_recurring_event_exception(
        self,
        parent_event: CalendarEvent,
        exception_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        is_cancelled: bool = False,
    ) -> CalendarEvent | None:
        """
        Create an exception for a recurring event (either cancelled or modified).

        If the exception is on the master event, this method makes the master event non-recurring
        and creates a new recurring event on the second occurrence

        :param parent_event: The recurring event to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_title: New title for the modified occurrence (if not cancelled)
        :param modified_description: New description for the modified occurrence (if not cancelled)
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified event or None if cancelled
        """

        def create_new_recurring_event(
            parent_obj: RecurringMixin,
            second_occurrence: RecurringMixin,
            new_recurrence_rule: RecurrenceRule,
        ) -> RecurringMixin:
            parent_event = cast(CalendarEvent, parent_obj)
            second_event = cast(CalendarEvent, second_occurrence)
            new_recurring_event = self.create_recurring_event(
                calendar_id=parent_event.calendar.id,
                title=parent_event.title,
                description=parent_event.description,
                start_time=second_event.start_time,
                end_time=second_event.end_time,
                recurrence_rule=new_recurrence_rule.to_rrule_string(),
                attendances=[
                    EventAttendanceInputData(user_id=a.user_id)
                    for a in parent_event.attendances.all()
                ],
                external_attendances=[
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            email=ea.external_attendee.email,
                            name=ea.external_attendee.name,
                            id=ea.external_attendee.id,
                        )
                    )
                    for ea in parent_event.external_attendances.all()
                ],
                resource_allocations=[
                    ResourceAllocationInputData(resource_id=r.calendar_fk_id)  # type: ignore
                    for r in parent_event.resource_allocations.all()
                ],
            )
            return new_recurring_event

        def create_modified_event(
            parent_obj: RecurringMixin,
            exception_datetime: datetime.datetime,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent_event = cast(CalendarEvent, parent_obj)
            modified_event_data = CalendarEventInputData(
                title=modification_data.get("title") or parent_event.title,
                description=modification_data.get("description") or parent_event.description,
                start_time=modification_data.get("start_time") or exception_datetime,
                end_time=modification_data.get("end_time")
                or (exception_datetime + parent_event.duration),
                parent_event_id=parent_event.id,
                is_recurring_exception=True,
            )
            return self.create_event(parent_event.calendar.id, modified_event_data)

        def update_exception_manager(
            parent_obj: RecurringMixin, new_recurring_obj: RecurringMixin
        ) -> None:
            EventRecurrenceException.objects.filter(parent_event=parent_obj).update(
                parent_event_fk=new_recurring_obj
            )

        def delete_exception_manager(parent_obj: RecurringMixin) -> None:
            EventRecurrenceException.objects.filter(parent_event=parent_obj).delete()

        modification_data = {
            "title": modified_title,
            "description": modified_description,
            "start_time": modified_start_time,
            "end_time": modified_end_time,
        }

        result = self._create_recurring_exception_generic(
            object_type_name="event",
            parent_object=parent_event,
            exception_date=exception_date,
            is_cancelled=is_cancelled,
            modification_data=modification_data,
            create_new_recurring_callback=create_new_recurring_event,
            create_modified_object_callback=create_modified_event,
            exception_manager_update_callback=update_exception_manager,
            exception_manager_delete_callback=delete_exception_manager,
        )
        return cast(CalendarEvent, result) if result else None

    def get_recurring_event_instances(
        self,
        recurring_event: CalendarEvent,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_exceptions: bool = True,
    ) -> list[CalendarEvent]:
        """
        Get all instances of a recurring event within a date range.

        :param recurring_event: The recurring event
        :param start_date: Start of the date range
        :param end_date: End of the date range
        :param include_exceptions: Whether to include modified exceptions
        :return: List of event instances
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not recurring_event.is_recurring:
            return [recurring_event] if start_date <= recurring_event.start_time <= end_date else []

        return recurring_event.get_occurrences_in_range(
            start_date, end_date, include_self=True, include_exceptions=include_exceptions
        )

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        """
        Get all calendar events in a date range with recurring events expanded to instances.

        For all calendars (both external and internal), this method:
        1. Gets non-recurring events within the date range
        2. Gets recurring master events and generates their instances dynamically
        3. Includes synced exceptions (modified/cancelled instances from external providers)
        4. Excludes master recurring events from the final result (only instances are returned)

        External providers (Google, Microsoft) only store master recurring events and sync
        exceptions, so we generate instances on our side while respecting their exceptions.

        :param calendar: The calendar to get events from
        :param start_date: Start of the date range
        :param end_date: End of the date range
        :return: List of all event instances in the range
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        base_qs = (
            CalendarEvent.objects.annotate_recurring_occurrences_on_date_range(start_date, end_date)
            .select_related("recurrence_rule")
            .filter(
                parent_recurring_object__isnull=True,  # Master events only
            )
        )
        if calendar.calendar_type == CalendarType.BUNDLE:
            base_qs = base_qs.filter(
                organization_id=calendar.organization_id,
                calendar__in=calendar.bundle_children.all(),
            )
        else:
            base_qs = base_qs.filter(
                organization_id=calendar.organization_id,
                calendar=calendar,
            )

        # Get non-recurring events within the date range
        non_recurring_events = base_qs.filter(
            Q(start_time__range=(start_date, end_date)) | Q(end_time__range=(start_date, end_date)),
            recurrence_rule__isnull=True,  # Non-recurring only
            is_recurring_exception=False,  # Exclude exception objects
        )

        # Get recurring master events and generate their instances
        recurring_events = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )

        events: list[CalendarEvent] = list(non_recurring_events)

        for master_event in recurring_events:
            instances = master_event.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True
            )
            events.extend(instances)

        # Sort by start time
        events.sort(key=lambda x: x.start_time)

        # If this is a bundle calendar, filter out bundle representations to avoid duplicates
        if calendar.calendar_type == CalendarType.BUNDLE:
            # Remove duplicates (keep primary events, remove representations)
            seen_primary_events = set()
            unique_events = []

            for event in events:
                if event.is_bundle_representation:
                    # Skip representations - we want to show the primary event instead
                    continue
                elif event.is_bundle_primary:
                    # For bundle primary events, check if we've already seen this one
                    if event.id not in seen_primary_events:
                        seen_primary_events.add(event.id)
                        unique_events.append(event)
                else:
                    # For non-bundle events, include them normally
                    unique_events.append(event)

            events = unique_events
            events.sort(key=lambda x: x.start_time)

        return events

    def _delete_bundle_event(self, bundle_event: CalendarEvent) -> None:
        """Delete a bundle event and all its representations."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not bundle_event.is_bundle_primary:
            raise ValueError("Event must be a bundle primary event")

        if not self.organization:
            raise ValueError("Organization is required for bundle operations")

        # Delete all representation events
        representation_events = CalendarEvent.objects.filter(
            organization_id=self.organization.id, bundle_primary_event=bundle_event
        )

        for representation_event in representation_events:
            self.delete_event(representation_event.calendar.id, representation_event.id)

        # Delete all blocked time representations
        BlockedTime.objects.filter(
            organization_id=self.organization.id, bundle_primary_event=bundle_event
        ).delete()

        # Delete the primary event
        self.delete_event(bundle_event.calendar.id, bundle_event.id)

    @transaction.atomic()
    def delete_event(self, calendar_id: int, event_id: int, delete_series: bool = False) -> None:
        """
        Delete an event from the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_id: Unique identifier of the event to delete.
        :param delete_series: If True and the event is recurring, delete the entire series
        :return: None
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        event = CalendarEvent.objects.select_related("calendar").get(
            calendar_fk_id=calendar_id,
            id=event_id,
            organization_id=self.organization.id,
        )

        if event.is_bundle_primary:
            self._delete_bundle_event(event)
            return

        if self.calendar_adapter:
            if event.is_recurring and delete_series:
                # Delete the entire recurring series from external calendar
                self.calendar_adapter.delete_event(event.calendar.external_id, event.external_id)
            elif event.is_recurring_instance and not delete_series:
                # Create a cancellation exception instead of deleting
                if event.parent_recurring_object:
                    event.parent_recurring_object.create_exception(
                        event.recurrence_id, is_cancelled=True
                    )
            else:
                # Delete single event or instance
                self.calendar_adapter.delete_event(event.calendar.external_id, event.external_id)

        if event.is_recurring and delete_series:
            # Delete the entire series including all instances and exceptions
            event.calendarevent_recurring_instances.all().delete()
            event.recurrence_exceptions.all().delete()
            if event.recurrence_rule:
                event.recurrence_rule.delete()
        elif event.is_recurring_instance and not delete_series:
            # For instances, we create an exception rather than delete
            if event.parent_recurring_object and event.recurrence_id:
                event.parent_recurring_object.create_exception(
                    event.recurrence_id, is_cancelled=True
                )

        event.delete()

    def transfer_event(self, event: CalendarEvent, new_calendar: Calendar) -> CalendarEvent:
        """
        Transfer an event to a different calendar.
        :param event_id: Unique identifier of the event to transfer.
        :param new_calendar_external_id: External ID of the new calendar.
        :return: Transferred CalendarEvent instance.
        """
        if not is_authenticated_calendar_service(self):
            raise

        event_data = self.calendar_adapter.get_event(event.calendar.external_id, event.external_id)

        # Create a new event in the target calendar
        new_event_data = CalendarEventInputData(
            title=event_data.title,
            description=event_data.description,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            attendances=[
                EventAttendanceInputData(
                    user_id=a.user_id,
                )
                for a in event.attendances.all()
            ],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        id=a.external_attendee.id,
                        email=a.external_attendee.email,
                        name=a.external_attendee.name,
                    )
                )
                for a in event.external_attendances.all()
            ],
            resource_allocations=[
                ResourceAllocationInputData(
                    resource_id=r.calendar_fk_id,
                )
                for r in event.resource_allocations.all()
                if r.calendar_fk_id
            ],
        )
        new_event = self.create_event(new_calendar.id, new_event_data)

        # Delete the old event
        self.delete_event(event.calendar.id, event.id)

        return new_event

    def _create_recurrence_rule_if_needed(self, rrule_string: str | None) -> RecurrenceRule | None:
        """Helper method to create recurrence rule from RRULE string if provided."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not rrule_string:
            return None

        recurrence_rule = RecurrenceRule.from_rrule_string(rrule_string, self.organization)
        recurrence_rule.save()
        return recurrence_rule

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
    ) -> CalendarSync:
        """
        Request a calendar synchronization for a specific date range.
        :param calendar: The calendar to synchronize.
        :param start_datetime: Start date for the event search.
        :param end_datetime: End date for the event search.
        :param should_update_events: Whether to update existing events.
        :return: Created CalendarSync instance.
        """
        from calendar_integration.tasks import sync_calendar_task

        if not is_authenticated_calendar_service(self):
            raise

        if not self.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

        calendar_sync = CalendarSync.objects.create(
            calendar=calendar,
            organization_id=calendar.organization_id,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=should_update_events,
        )
        account_type: Literal["social_account", "google_service_account"] = (
            "social_account"
            if isinstance(self.account, SocialAccount)
            else "google_service_account"
        )

        if not self.account or not self.account.id:
            raise NotImplementedError("Account is not set for the current service instance.")

        sync_calendar_task.delay(  # type: ignore
            account_type, self.account.id, calendar_sync.id, calendar.organization_id
        )
        return calendar_sync

    def sync_events(
        self,
        calendar_sync: CalendarSync,
    ) -> None:
        """
        Synchronize events for a calendar within a specified date range.
        :param calendar: The calendar to synchronize.
        :param start_date: Start date for the event search.
        :param end_date: End date for the event search.
        :param update_events: Whether to update existing events.
        :param sync_token: Token for incremental sync, if available.
        """
        if not is_authenticated_calendar_service(self):
            raise

        latest_sync = calendar_sync.calendar.latest_sync

        calendar_sync.status = CalendarSyncStatus.IN_PROGRESS
        calendar_sync.save(update_fields=["status"])

        try:
            with transaction.atomic():
                self._execute_calendar_sync(
                    calendar_sync,
                    latest_sync.next_sync_token if latest_sync else None,
                )
        except Exception as e:  # noqa: BLE001
            # Handle exceptions during synchronization
            # This could include logging the error or re-raising it
            calendar_sync.status = CalendarSyncStatus.FAILED
            calendar_sync.error_message = str(e)
            calendar_sync.save(update_fields=["status", "error_message"])
            return

        calendar_sync.status = CalendarSyncStatus.SUCCESS
        calendar_sync.save(update_fields=["status"])

    def _execute_calendar_sync(
        self,
        calendar_sync: CalendarSync,
        sync_token: str | None = None,
    ) -> None:
        if not is_authenticated_calendar_service(self):
            raise

        calendar: Calendar = calendar_sync.calendar
        start_date = calendar_sync.start_datetime
        end_date = calendar_sync.end_datetime
        should_update_events = calendar_sync.should_update_events

        events_dict = self.calendar_adapter.get_events(
            calendar.external_id, calendar.is_resource, start_date, end_date, sync_token
        )
        events = events_dict["events"]
        next_sync_token = events_dict["next_sync_token"]

        # Prepare existing data mappings
        (
            calendar_events_by_external_id,
            blocked_times_by_external_id,
        ) = self._get_existing_calendar_data(calendar.id, start_date, end_date)

        # Process events and collect changes
        changes = self._process_events_for_sync(
            events,
            calendar_events_by_external_id,
            blocked_times_by_external_id,
            calendar,
            should_update_events,
        )

        # Handle deletions for full sync
        if not sync_token:
            self._handle_deletions_for_full_sync(
                calendar.id,
                calendar_events_by_external_id,
                changes.matched_event_ids,
                start_date,
            )
        else:
            calendar_sync.next_sync_token = next_sync_token or ""
            calendar_sync.save(update_fields=["next_sync_token"])

        # Apply all changes to database
        self._apply_sync_changes(calendar.id, changes)

        # Update available time windows if needed
        if calendar.manage_available_windows:
            self._remove_available_time_windows_that_overlap_with_blocked_times_and_events(
                calendar.id,
                changes.blocked_times_to_create + changes.blocked_times_to_update,
                changes.events_to_update,
                start_date,
                end_date,
            )

    def _get_existing_calendar_data(
        self, calendar_id: int, start_date: datetime.datetime, end_date: datetime.datetime
    ):
        """Get existing calendar events and blocked times for the date range."""
        if not self.organization:
            return ({}, {})

        calendar_events_by_external_id = {
            e.external_id: e
            for e in CalendarEvent.objects.filter(
                calendar_fk_id=calendar_id,
                start_time__gte=start_date,
                end_time__lte=end_date,
                organization_id=self.organization.id,
            )
        }
        blocked_times_by_external_id = {
            e.external_id: e
            for e in BlockedTime.objects.filter(
                calendar_fk_id=calendar_id,
                start_time__gte=start_date,
                end_time__lte=end_date,
                organization_id=self.organization.id,
            )
        }
        return calendar_events_by_external_id, blocked_times_by_external_id

    def _process_events_for_sync(
        self,
        events: Iterable[CalendarEventData],
        calendar_events_by_external_id: dict,
        blocked_times_by_external_id: dict,
        calendar: Calendar,
        update_events: bool,
    ) -> EventsSyncChanges:
        """Process events and determine what changes need to be made."""
        changes = EventsSyncChanges()

        for event in events:
            existing_event = calendar_events_by_external_id.get(event.external_id)
            existing_blocked_time = blocked_times_by_external_id.get(event.external_id)

            if existing_event:
                self._process_existing_event(event, existing_event, changes, update_events)
            elif existing_blocked_time:
                self._process_existing_blocked_time(event, existing_blocked_time, changes)
            else:
                self._process_new_event(event, calendar, changes)

        return changes

    def _process_existing_event(
        self,
        event: CalendarEventData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
        update_events: bool,
    ):
        """Process an existing calendar event."""
        if not update_events:
            return

        if event.status == "cancelled":
            changes.events_to_delete.append(existing_event.external_id)
            changes.matched_event_ids.add(existing_event.external_id)
            return

        # Update existing event
        existing_event.title = event.title
        existing_event.description = event.description
        existing_event.start_time = event.start_time
        existing_event.end_time = event.end_time
        existing_event.meta["latest_original_payload"] = event.original_payload or {}
        changes.events_to_update.append(existing_event)
        changes.matched_event_ids.add(existing_event.external_id)

        # Process attendees
        self._process_event_attendees(event, existing_event, changes)

    def _process_existing_blocked_time(
        self,
        event: CalendarEventData,
        existing_blocked_time: BlockedTime,
        changes: EventsSyncChanges,
    ):
        """Process an existing blocked time."""
        if event.status == "cancelled":
            changes.blocks_to_delete.append(existing_blocked_time.external_id)
            changes.matched_event_ids.add(existing_blocked_time.external_id)
            return

        # Update existing blocked time
        existing_blocked_time.start_time = event.start_time
        existing_blocked_time.end_time = event.end_time
        existing_blocked_time.reason = event.title
        existing_blocked_time.external_id = event.external_id
        existing_blocked_time.meta["latest_original_payload"] = event.original_payload or {}
        changes.blocked_times_to_update.append(existing_blocked_time)
        changes.matched_event_ids.add(existing_blocked_time.external_id)

    def _process_new_event(
        self, event: CalendarEventData, calendar: Calendar, changes: EventsSyncChanges
    ):
        """Process a new event by creating appropriate records."""
        if event.recurring_event_id:
            # This is an instance of a recurring event from external service
            try:
                parent_event = CalendarEvent.objects.get(
                    external_id=event.recurring_event_id,
                    organization_id=calendar.organization_id,
                )
                # Parent exists in our system, so this instance should be a CalendarEvent
                # (because the parent was created through our API)
                calendar_event = CalendarEvent(
                    calendar_fk=calendar,
                    start_time=event.start_time,
                    end_time=event.end_time,
                    title=event.title,
                    description=event.description,
                    external_id=event.external_id,
                    meta={"latest_original_payload": event.original_payload or {}},
                    organization_id=calendar.organization_id,
                    parent_recurring_object_fk=parent_event,
                    recurrence_id=event.start_time,
                    is_recurring_exception=True,
                )
                changes.events_to_create.append(calendar_event)
            except CalendarEvent.DoesNotExist:
                # Parent doesn't exist in our system, so this is an instance of an externally-created
                # recurring event. Create as BlockedTime since we shouldn't modify external events.
                changes.blocked_times_to_create.append(
                    BlockedTime(
                        calendar_fk=calendar,
                        start_time=event.start_time,
                        end_time=event.end_time,
                        reason=event.title,
                        external_id=event.external_id,
                        meta={
                            "latest_original_payload": event.original_payload or {},
                            "pending_parent_external_id": event.recurring_event_id,
                        },
                        organization_id=calendar.organization_id,
                    )
                )
        elif event.recurrence_rule:
            # This is a master recurring event coming from external sync
            # We need to determine if this was created through our API or externally
            # For now, if it's coming through sync, we'll assume it was created externally
            # and store as CalendarEvent with recurrence rule for visibility, but instances will be BlockedTime
            recurrence_rule = RecurrenceRule.from_rrule_string(
                event.recurrence_rule, calendar.organization
            )
            calendar_event = CalendarEvent(
                calendar_fk=calendar,
                start_time=event.start_time,
                end_time=event.end_time,
                title=event.title,
                description=event.description,
                external_id=event.external_id,
                meta={"latest_original_payload": event.original_payload or {}},
                organization_id=calendar.organization_id,
                recurrence_rule_fk=recurrence_rule,
            )
            changes.events_to_create.append(calendar_event)
            changes.recurrence_rules_to_create.append(recurrence_rule)
        else:
            # Regular single event from external sync - create as BlockedTime
            # since we shouldn't modify events created externally
            changes.blocked_times_to_create.append(
                BlockedTime(
                    calendar_fk=calendar,
                    start_time=event.start_time,
                    end_time=event.end_time,
                    reason=event.title,
                    external_id=event.external_id,
                    meta={"latest_original_payload": event.original_payload or {}},
                    organization_id=calendar.organization_id,
                )
            )

        changes.matched_event_ids.add(event.external_id)

    def _process_event_attendees(
        self, event: CalendarEventData, existing_event: CalendarEvent, changes: EventsSyncChanges
    ):
        """Process attendees for an existing event."""
        for attendee in event.attendees:
            user = User.objects.filter(email=attendee.email).first()

            if user and not existing_event.attendees.filter(id=user.id).exists():
                changes.attendances_to_create.append(
                    EventAttendance(
                        event=existing_event,
                        user=None,
                        status=attendee.status,
                    )
                )
            elif (
                not user
                and not existing_event.external_attendances.filter(
                    external_attendee__email=attendee.email
                ).exists()
            ):
                external_attendee, _created = ExternalAttendee.objects.get_or_create(
                    email=attendee.email,
                    organization_id=existing_event.calendar.organization_id,
                    defaults={"name": attendee.name},
                )
                changes.external_attendances_to_create.append(
                    EventExternalAttendance(
                        event=existing_event,
                        external_attendee=external_attendee,
                        status=attendee.status,
                        organization_id=existing_event.calendar.organization_id,
                    )
                )
            else:
                # Update existing attendance status if needed
                attendance = (
                    existing_event.attendances.filter(user=user).first()
                    or existing_event.external_attendances.filter(
                        external_attendee__email=attendee.email
                    ).first()
                )
                if attendance:
                    attendance.status = attendee.status

    def _handle_deletions_for_full_sync(
        self,
        calendar_id: int,
        calendar_events_by_external_id: dict,
        matched_event_ids: set[str],
        start_date: datetime.datetime,
    ):
        """Handle deletions when doing a full sync (no sync_token)."""
        if not self.organization:
            return

        deleted_ids = set(calendar_events_by_external_id.keys()) - matched_event_ids
        CalendarEvent.objects.filter(
            calendar_fk_id=calendar_id,
            external_id__in=deleted_ids,
            start_time__gte=start_date,
            organization_id=self.organization.id,
        ).delete()

    def _apply_sync_changes(self, calendar_id: int, changes: EventsSyncChanges):
        """Apply all the collected changes to the database."""
        # Create recurrence rules first
        if changes.recurrence_rules_to_create:
            RecurrenceRule.objects.bulk_create(changes.recurrence_rules_to_create)

        # Create events (which may reference recurrence rules)
        if changes.events_to_create:
            CalendarEvent.objects.bulk_create(changes.events_to_create)

        if changes.blocked_times_to_create:
            BlockedTime.objects.bulk_create(changes.blocked_times_to_create)

        if changes.events_to_update:
            CalendarEvent.objects.bulk_update(
                changes.events_to_update, ["title", "description", "start_time", "end_time"]
            )

        if changes.attendances_to_create:
            EventAttendance.objects.bulk_create(changes.attendances_to_create)

        if changes.external_attendances_to_create:
            EventExternalAttendance.objects.bulk_create(changes.external_attendances_to_create)

        if changes.blocked_times_to_update:
            BlockedTime.objects.bulk_update(
                changes.blocked_times_to_update, ["start_time", "end_time", "reason", "external_id"]
            )

        if changes.events_to_delete:
            CalendarEvent.objects.filter(
                calendar_fk_id=calendar_id,
                external_id__in=changes.events_to_delete,
                organization=self.organization,
            ).delete()

        if changes.blocks_to_delete:
            BlockedTime.objects.filter(
                calendar_fk_id=calendar_id,
                external_id__in=changes.blocks_to_delete,
                organization=self.organization,
            ).delete()

        # After all changes are applied, link orphaned recurring instances to their parents
        self._link_orphaned_recurring_instances(calendar_id)

    def _link_orphaned_recurring_instances(self, calendar_id: int):
        """
        Link recurring event instances that were created before their parent events
        were synced. This happens when webhook events come out of order.
        """
        if not self.organization:
            return

        # Find events that have a pending parent external ID in their meta
        orphaned_instances = CalendarEvent.objects.filter(
            calendar_fk_id=calendar_id,
            organization_id=self.organization.id,
            parent_recurring_object__isnull=True,
            meta__pending_parent_external_id__isnull=False,
        )

        # Also find blocked times that might be orphaned instances
        orphaned_blocked_times = BlockedTime.objects.filter(
            calendar_fk_id=calendar_id,
            organization_id=self.organization.id,
            meta__pending_parent_external_id__isnull=False,
        )

        # Link orphaned CalendarEvent instances
        for instance in orphaned_instances:
            parent_external_id = instance.meta.get("pending_parent_external_id")
            if parent_external_id:
                try:
                    parent_event = CalendarEvent.objects.get(
                        external_id=parent_external_id,
                        organization_id=self.organization.id,
                    )
                    # Link the instance to its parent
                    instance.parent_recurring_object_fk = parent_event
                    instance.recurrence_id = instance.start_time
                    # Clear the pending parent ID
                    instance.meta.pop("pending_parent_external_id", None)
                    instance.save(
                        update_fields=["parent_recurring_object_fk", "recurrence_id", "meta"]
                    )
                except CalendarEvent.DoesNotExist:
                    # Parent still not synced, leave it for next sync
                    continue

        # For orphaned BlockedTime instances, we just clear the pending parent ID
        # since BlockedTime doesn't have parent relationships
        for blocked_time in orphaned_blocked_times:
            parent_external_id = blocked_time.meta.get("pending_parent_external_id")
            if parent_external_id:
                try:
                    # Check if parent exists now
                    CalendarEvent.objects.get(
                        external_id=parent_external_id,
                        organization_id=self.organization.id,
                    )
                    # Parent exists, clear the pending flag
                    blocked_time.meta.pop("pending_parent_external_id", None)
                    blocked_time.save(update_fields=["meta"])
                except CalendarEvent.DoesNotExist:
                    # Parent still not synced, leave it for next sync
                    continue

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self,
        calendar_id: int,
        blocked_times: Iterable[BlockedTime],
        events: Iterable[CalendarEvent],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ):
        """
        Removes AvailableTime windows that overlap with BlockedTime or CalendarEvent instances.
        """
        if not self.organization:
            return

        blocked_times = list(blocked_times)
        events = list(events)

        available_time_windows = AvailableTime.objects.filter(
            calendar_fk_id=calendar_id,
            start_time__gte=start_time,
            end_time__lte=end_time,
            organization_id=self.organization.id,
        )

        available_time_windows_to_delete: list[int] = []

        for available_time in available_time_windows:
            # Check if the available time overlaps with any blocked time
            overlaps_with_blocked = any(
                bt.start_time < available_time.end_time and bt.end_time > available_time.start_time
                for bt in blocked_times
            )
            # Check if the available time overlaps with any event
            overlaps_with_event = any(
                event.start_time < available_time.end_time
                and event.end_time > available_time.start_time
                for event in events
            )

            if overlaps_with_blocked or overlaps_with_event:
                # If it overlaps, remove it from the list of blocked times
                available_time_windows_to_delete.append(available_time.id)

        AvailableTime.objects.filter(
            id__in=available_time_windows_to_delete,
            organization_id=self.organization.id,
            calendar_fk_id=calendar_id,
        ).delete()

    def get_unavailable_time_windows_in_range(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindow]:
        """
        Retrieve unavailable time windows for a calendar within a specified date range.
        This includes both calendar events (with recurring instances) and blocked times
        that overlap with the given time range.

        :param calendar: The calendar to retrieve unavailable time windows for.
        :param start_datetime: Start date for the availability search.
        :param end_datetime: End date for the availability search.
        :return: List of UnavailableTimeWindow instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        # Get expanded calendar events (including recurring instances)
        # This handles both master events and their generated instances
        calendar_events = self.get_calendar_events_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        # Get expanded blocked times (including recurring instances)
        # Replace the current blocked_times query with:
        blocked_times = self.get_blocked_times_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        # If this calendar is part of any bundles, include bundle events
        bundle_calendars = Calendar.objects.filter(
            calendar_type=CalendarType.BUNDLE,
            bundle_children=calendar,
            organization_id=calendar.organization_id,
        )

        bundle_events: list[CalendarEvent] = []
        for bundle_calendar in bundle_calendars:
            # Get bundle events from the bundle calendar directly
            bundle_calendar_events = CalendarEvent.objects.filter(
                bundle_calendar=bundle_calendar,
                start_time__lt=end_datetime,
                end_time__gt=start_datetime,
                organization_id=bundle_calendar.organization_id,
            )
            # Only include bundle events that aren't already in our calendar_events
            # (to avoid counting the same event twice)
            bundle_events.extend(
                bundle_event
                for bundle_event in bundle_calendar_events
                if all(ce.id != bundle_event.id for ce in calendar_events)
            )

        # Combine regular events with bundle events
        all_events = calendar_events + bundle_events

        return sorted(
            [
                UnavailableTimeWindow(
                    start_time=event.start_time,
                    end_time=event.end_time,
                    reason="calendar_event",
                    id=event.id,
                    data=CalendarEventData(
                        id=event.id,
                        calendar_external_id=event.calendar.external_id,
                        start_time=event.start_time,
                        end_time=event.end_time,
                        title=event.title,
                        description=event.description,
                        original_payload=event.meta.get("latest_original_payload", {})
                        if hasattr(event, "meta") and event.meta
                        else {},
                        attendees=[
                            EventAttendeeData(
                                email=attendance.user.email,
                                name=attendance.user.get_full_name(),
                                status=cast(
                                    Literal["accepted", "declined", "pending"], attendance.status
                                ),
                            )
                            # For recurring instances, get attendances from the parent event; for regular events, use their own
                            for attendance in (
                                event.parent_recurring_object.attendances.all()
                                if event.parent_recurring_object
                                else (event.attendances.all() if event.id else [])
                            )
                        ]
                        + [
                            EventAttendeeData(
                                email=external_attendance.external_attendee.email,
                                name=external_attendance.external_attendee.name,
                                status=cast(
                                    Literal["accepted", "declined", "pending"],
                                    external_attendance.status,
                                ),
                            )
                            # For recurring instances, get external attendances from the parent event; for regular events, use their own
                            for external_attendance in (
                                event.parent_recurring_object.external_attendances.all()
                                if event.parent_recurring_object
                                else (event.external_attendances.all() if event.id else [])
                            )
                        ],
                        resources=[
                            ResourceData(
                                title=resource_allocation.calendar.name,
                                email=resource_allocation.calendar.email,
                                external_id=resource_allocation.calendar.external_id,
                                status=cast(
                                    Literal["accepted", "declined", "pending"],
                                    resource_allocation.status,
                                ),
                            )
                            # For recurring instances, get resource allocations from the parent event; for regular events, use their own
                            for resource_allocation in (
                                event.parent_recurring_object.resource_allocations.all()
                                if event.parent_recurring_object
                                else (event.resource_allocations.all() if event.id else [])
                            )
                        ],
                        external_id=event.external_id,
                    ),
                )
                for event in all_events
            ]
            + [
                UnavailableTimeWindow(
                    start_time=blocked_time.start_time,
                    end_time=blocked_time.end_time,
                    reason="blocked_time",
                    id=blocked_time.id,
                    data=BlockedTimeData(
                        id=blocked_time.id,
                        calendar_external_id=blocked_time.calendar.external_id,
                        start_time=blocked_time.start_time,
                        end_time=blocked_time.end_time,
                        reason=blocked_time.reason,
                        external_id=blocked_time.external_id,
                        meta=blocked_time.meta or {},
                    ),
                )
                for blocked_time in blocked_times
            ],
            key=lambda x: x.start_time,
        )

    def get_availability_windows_in_range(
        self, calendar: Calendar, start_datetime: datetime.datetime, end_datetime: datetime.datetime
    ) -> Iterable[AvailableTimeWindow]:
        """
        Retrieve availability windows for a calendar within a specified date range.
        :param calendar_id: ID of the calendar to retrieve availability for.
        :param start_datetime: Start date for the availability search.
        :param end_datetime: End date for the availability search.
        :return: Iterable of AvalableTimeWindow instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if calendar.manage_available_windows:
            # Replace the current query with:
            available_times = self.get_available_times_expanded(
                calendar=calendar,
                start_date=start_datetime,
                end_date=end_datetime,
            )

            return [
                AvailableTimeWindow(
                    start_time=available_time.start_time,
                    end_time=available_time.end_time,
                    id=available_time.id,
                    can_book_partially=False,
                )
                for available_time in available_times
            ]

        unavailable_windows_sorted_by_start_datetime = self.get_unavailable_time_windows_in_range(
            calendar, start_datetime, end_datetime
        )
        available_windows = []

        if not unavailable_windows_sorted_by_start_datetime:
            # If there are no unavailable windows, the entire range is available
            return [
                AvailableTimeWindow(
                    start_time=start_datetime,
                    end_time=end_datetime,
                    id=None,  # ID will be set when saving to the database
                    can_book_partially=True,
                )
            ]

        if start_datetime < unavailable_windows_sorted_by_start_datetime[0].start_time:
            available_windows.append(
                (start_datetime, unavailable_windows_sorted_by_start_datetime[0].start_time)
            )
        for i in range(len(unavailable_windows_sorted_by_start_datetime) - 1):
            current_end = unavailable_windows_sorted_by_start_datetime[i].end_time
            next_start = unavailable_windows_sorted_by_start_datetime[i + 1].start_time
            if current_end < next_start:
                available_windows.append((current_end, next_start))
        if end_datetime > unavailable_windows_sorted_by_start_datetime[-1].end_time:
            available_windows.append(
                (unavailable_windows_sorted_by_start_datetime[-1].end_time, end_datetime)
            )

        return [
            AvailableTimeWindow(
                start_time=start,
                end_time=end,
                can_book_partially=True,
                # this calendar doesn't manage available windows, so there is no
                # AvailableTime record in the database
                id=None,
            )
            for start, end in available_windows
        ]

    @transaction.atomic()
    @transaction.atomic()
    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[tuple[datetime.datetime, datetime.datetime, str | None]],
    ) -> Iterable[AvailableTime]:
        """
        Create availability windows for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the availability windows for.
        :param availability_windows: Iterable of tuples containing (start_time, end_time, rrule_string).
        :return: List of created AvailableTime instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not calendar.manage_available_windows:
            raise ValueError("This calendar does not manage available windows.")

        availability_windows_to_create = []

        for start_time, end_time, rrule_string in availability_windows:
            # Create recurrence rule if provided
            recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)

            available_time = AvailableTime(
                calendar=calendar,
                start_time=start_time,
                end_time=end_time,
                organization_id=calendar.organization_id,
                recurrence_rule=recurrence_rule,
            )
            availability_windows_to_create.append(available_time)

        return AvailableTime.objects.bulk_create(availability_windows_to_create)

    @transaction.atomic()
    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str, str | None]],
    ) -> Iterable[BlockedTime]:
        """
        Create new blocked times for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the blocked times for.
        :param blocked_times: Iterable of tuples containing (start_time, end_time, reason, rrule_string).
        :return: List of created BlockedTime instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        blocked_times_to_create = []

        for i, (start_time, end_time, reason, rrule_string) in enumerate(blocked_times):
            # Create recurrence rule if provided
            recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)

            # Generate unique external_id to avoid constraint violations
            external_id = f"manual-{start_time.isoformat()}-{i}"

            blocked_time = BlockedTime(
                calendar=calendar,
                start_time=start_time,
                end_time=end_time,
                reason=reason,
                external_id=external_id,
                organization_id=calendar.organization_id,
                recurrence_rule=recurrence_rule,
            )
            blocked_times_to_create.append(blocked_time)

        return BlockedTime.objects.bulk_create(blocked_times_to_create)

    # Convenience methods for single object creation
    @transaction.atomic()
    def create_blocked_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        reason: str = "",
        rrule_string: str | None = None,
    ) -> BlockedTime:
        """Create a single blocked time (optionally recurring)."""
        result = self.bulk_create_manual_blocked_times(
            calendar=calendar, blocked_times=[(start_time, end_time, reason, rrule_string)]
        )
        return next(iter(result))

    @transaction.atomic()
    def create_available_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        rrule_string: str | None = None,
    ) -> AvailableTime:
        """Create a single available time (optionally recurring)."""
        result = self.bulk_create_availability_windows(
            calendar=calendar, availability_windows=[(start_time, end_time, rrule_string)]
        )
        return next(iter(result))

    def get_blocked_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[BlockedTime]:
        """Get all blocked times in a date range with recurring blocked times expanded to instances."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        # Get calendars to query - includes the main calendar and bundle children if applicable
        calendars_to_query = [calendar]
        if calendar.calendar_type == CalendarType.BUNDLE:
            # Add all bundle children calendars
            bundle_children = calendar.bundle_children.all()
            calendars_to_query.extend(bundle_children)

        base_qs = (
            BlockedTime.objects.annotate_recurring_occurrences_on_date_range(start_date, end_date)
            .select_related("recurrence_rule")
            .filter(
                organization_id=calendar.organization_id,
                calendar__in=calendars_to_query,
                parent_recurring_object__isnull=True,  # Master times only
            )
        )

        # Get non-recurring times within the date range
        non_recurring_times = base_qs.filter(
            Q(start_time__range=(start_date, end_date)) | Q(end_time__range=(start_date, end_date)),
            recurrence_rule__isnull=True,  # Non-recurring only
            is_recurring_exception=False,  # Exclude exception objects
        )

        # Get recurring master times and generate their instances
        recurring_times = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )

        times: list[BlockedTime] = list(non_recurring_times)

        for master_time in recurring_times:
            instances = master_time.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True
            )
            times.extend(instances)

        # Sort by start time
        times.sort(key=lambda x: x.start_time)
        return times

    def get_available_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[AvailableTime]:
        """Get all available times in a date range with recurring available times expanded to instances."""
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        base_qs = (
            AvailableTime.objects.annotate_recurring_occurrences_on_date_range(start_date, end_date)
            .select_related("recurrence_rule")
            .filter(
                organization_id=calendar.organization_id,
                calendar=calendar,
                parent_recurring_object__isnull=True,  # Master times only
            )
        )

        # Get non-recurring times within the date range
        non_recurring_times = base_qs.filter(
            Q(start_time__range=(start_date, end_date)) | Q(end_time__range=(start_date, end_date)),
            recurrence_rule__isnull=True,  # Non-recurring only
            is_recurring_exception=False,  # Exclude exception objects
        )

        # Get recurring master times and generate their instances
        recurring_times = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )

        times: list[AvailableTime] = list(non_recurring_times)

        for master_time in recurring_times:
            instances = master_time.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True
            )
            times.extend(instances)

        # Sort by start time
        times.sort(key=lambda x: x.start_time)
        return times

    def create_recurring_blocked_time_exception(
        self,
        parent_blocked_time: BlockedTime,
        exception_date: datetime.date,
        modified_reason: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        is_cancelled: bool = False,
    ) -> BlockedTime | None:
        """
        Create an exception for a recurring blocked time (either cancelled or modified).

        :param parent_blocked_time: The recurring blocked time to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_reason: New reason for the modified occurrence (if not cancelled)
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified blocked time or None if cancelled
        """

        def create_new_recurring_blocked_time(
            parent_obj: RecurringMixin,
            second_occurrence: RecurringMixin,
            new_recurrence_rule: RecurrenceRule,
        ) -> RecurringMixin:
            parent_blocked_time = cast(BlockedTime, parent_obj)
            second_blocked_time = cast(BlockedTime, second_occurrence)
            return self.create_blocked_time(
                calendar=parent_blocked_time.calendar,
                start_time=second_blocked_time.start_time,
                end_time=second_blocked_time.end_time,
                reason=second_blocked_time.reason,
                rrule_string=new_recurrence_rule.to_rrule_string(),
            )

        def create_modified_blocked_time(
            parent_obj: RecurringMixin,
            exception_datetime: datetime.datetime,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent_blocked_time = cast(BlockedTime, parent_obj)
            return self.create_blocked_time(
                calendar=parent_blocked_time.calendar,
                start_time=modification_data.get("start_time") or exception_datetime,
                end_time=modification_data.get("end_time")
                or (exception_datetime + parent_blocked_time.duration),
                reason=modification_data.get("reason") or parent_blocked_time.reason,
            )

        def update_exception_manager(
            parent_obj: RecurringMixin, new_recurring_obj: RecurringMixin
        ) -> None:
            BlockedTimeRecurrenceException.objects.filter(parent_blocked_time=parent_obj).update(
                parent_blocked_time_fk=new_recurring_obj
            )

        def delete_exception_manager(parent_obj: RecurringMixin) -> None:
            BlockedTimeRecurrenceException.objects.filter(parent_blocked_time=parent_obj).delete()

        modification_data = {
            "reason": modified_reason,
            "start_time": modified_start_time,
            "end_time": modified_end_time,
        }

        result = self._create_recurring_exception_generic(
            object_type_name="blocked time",
            parent_object=parent_blocked_time,
            exception_date=datetime.datetime.combine(
                exception_date,
                parent_blocked_time.start_time.time(),
                tzinfo=parent_blocked_time.start_time.tzinfo,
            ),
            is_cancelled=is_cancelled,
            modification_data=modification_data,
            create_new_recurring_callback=create_new_recurring_blocked_time,
            create_modified_object_callback=create_modified_blocked_time,
            exception_manager_update_callback=update_exception_manager,
            exception_manager_delete_callback=delete_exception_manager,
        )
        return cast(BlockedTime, result) if result else None

    def create_recurring_available_time_exception(
        self,
        parent_available_time: AvailableTime,
        exception_date: datetime.date,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        is_cancelled: bool = False,
    ) -> AvailableTime | None:
        """
        Create an exception for a recurring available time (either cancelled or modified).

        :param parent_available_time: The recurring available time to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified available time or None if cancelled
        """

        def create_new_recurring_available_time(
            parent_obj: RecurringMixin,
            second_occurrence: RecurringMixin,
            new_recurrence_rule: RecurrenceRule,
        ) -> RecurringMixin:
            parent_available_time = cast(AvailableTime, parent_obj)
            second_available_time = cast(AvailableTime, second_occurrence)
            return self.create_available_time(
                calendar=parent_available_time.calendar,
                start_time=second_available_time.start_time,
                end_time=second_available_time.end_time,
                rrule_string=new_recurrence_rule.to_rrule_string(),
            )

        def create_modified_available_time(
            parent_obj: RecurringMixin,
            exception_datetime: datetime.datetime,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent_available_time = cast(AvailableTime, parent_obj)
            return self.create_available_time(
                calendar=parent_available_time.calendar,
                start_time=modification_data.get("start_time") or exception_datetime,
                end_time=modification_data.get("end_time")
                or (exception_datetime + parent_available_time.duration),
            )

        def update_exception_manager(
            parent_obj: RecurringMixin, new_recurring_obj: RecurringMixin
        ) -> None:
            AvailableTimeRecurrenceException.objects.filter(
                parent_available_time=parent_obj
            ).update(parent_available_time_fk=new_recurring_obj)

        def delete_exception_manager(parent_obj: RecurringMixin) -> None:
            AvailableTimeRecurrenceException.objects.filter(
                parent_available_time=parent_obj
            ).delete()

        modification_data = {
            "start_time": modified_start_time,
            "end_time": modified_end_time,
        }

        result = self._create_recurring_exception_generic(
            object_type_name="available time",
            parent_object=parent_available_time,
            exception_date=datetime.datetime.combine(
                exception_date,
                parent_available_time.start_time.time(),
                tzinfo=parent_available_time.start_time.tzinfo,
            ),
            is_cancelled=is_cancelled,
            modification_data=modification_data,
            create_new_recurring_callback=create_new_recurring_available_time,
            create_modified_object_callback=create_modified_available_time,
            exception_manager_update_callback=update_exception_manager,
            exception_manager_delete_callback=delete_exception_manager,
        )
        return cast(AvailableTime, result) if result else None
