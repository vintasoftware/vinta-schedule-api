import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from typing import Literal, Protocol, TypedDict, TypeGuard, cast

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
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarOwnership,
    CalendarSync,
    ChildrenCalendarRelationship,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
    ResourceAllocation,
)
from organizations.models import Organization


User = get_user_model()


@dataclass
class EventAttendeeData:
    email: str
    name: str
    status: Literal["accepted", "declined", "pending"]


@dataclass
class ResourceData:
    email: str
    title: str
    external_id: str | None = None
    status: Literal["accepted", "declined", "pending"] | None = None


@dataclass
class EventAttendanceInputData:
    user_id: int


@dataclass
class ExternalAttendeeInputData:
    email: str
    name: str = ""
    id: int | None = None  # noqa: A003


@dataclass
class EventExternalAttendanceInputData:
    external_attendee: ExternalAttendeeInputData


@dataclass
class ResourceAllocationInputData:
    resource_id: int


@dataclass
class CalendarEventInputData:
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    attendances: list[EventAttendanceInputData] = dataclass_field(default_factory=list)
    external_attendances: list[EventExternalAttendanceInputData] = dataclass_field(
        default_factory=list
    )
    resource_allocations: list[ResourceAllocationInputData] = dataclass_field(default_factory=list)
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string
    parent_event_id: int | None = None  # For creating instances/exceptions
    is_recurring_exception: bool = False


@dataclass
class CalendarEventAdapterInputData:
    calendar_external_id: str
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    attendees: list[EventAttendeeData]
    resources: list[ResourceData] = dataclass_field(default_factory=list)
    original_payload: dict | None = None
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string for creating recurring events
    is_recurring_instance: bool = False  # True if this is a single instance of a recurring event


@dataclass
class CalendarEventData:
    calendar_external_id: str
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    attendees: list[EventAttendeeData]
    external_id: str
    status: Literal["confirmed", "cancelled"] = "confirmed"
    original_payload: dict | None = None
    id: int | None = None  # noqa: A003
    resources: list[ResourceData] = dataclass_field(default_factory=list)
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string
    recurring_event_id: str | None = None  # ID of the master recurring event


@dataclass
class CalendarResourceData:
    name: str
    description: str
    provider: str
    external_id: str
    email: str | None = None
    capacity: int | None = None
    original_payload: dict | None = None
    is_default: bool = False


@dataclass
@dataclass
class EventsSyncChanges:
    events_to_update: list[CalendarEvent] = dataclass_field(default_factory=list)
    events_to_create: list[CalendarEvent] = dataclass_field(default_factory=list)
    blocked_times_to_create: list[BlockedTime] = dataclass_field(default_factory=list)
    blocked_times_to_update: list[BlockedTime] = dataclass_field(default_factory=list)
    attendances_to_create: list[EventAttendance] = dataclass_field(default_factory=list)
    external_attendances_to_create: list[EventExternalAttendance] = dataclass_field(
        default_factory=list
    )
    events_to_delete: list[str] = dataclass_field(default_factory=list)
    blocks_to_delete: list[str] = dataclass_field(default_factory=list)
    matched_event_ids: set[str] = dataclass_field(default_factory=set)
    # New fields for recurring events
    recurrence_rules_to_create: list = dataclass_field(
        default_factory=list
    )  # RecurrenceRule objects


@dataclass
class ApplicationCalendarData:
    id: int | None  # noqa: A003
    organization_id: int | None
    external_id: str
    name: str
    description: str | None = None
    email: str | None = None
    provider: CalendarProvider = CalendarProvider.GOOGLE
    original_payload: dict | None = None


class CalendarEventsSyncTypedDict(TypedDict):
    events: Iterable[CalendarEventData]
    next_sync_token: str | None


@dataclass
class AvailableTimeWindow:
    start_time: datetime.datetime
    end_time: datetime.datetime
    id: int | None = None  # noqa: A003
    can_book_partially: bool = False


@dataclass
class BlockedTimeData:
    id: int | None  # noqa: A003
    calendar_external_id: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    reason: str
    external_id: str | None
    meta: dict | None


@dataclass
class UnavailableTimeWindow:
    start_time: datetime.datetime
    end_time: datetime.datetime
    reason: Literal["blocked_time"] | Literal["calendar_event"]
    id: int  # noqa: A003
    data: BlockedTimeData | CalendarEventData


class CalendarAdapter(Protocol):
    provider: str

    def __init__(self, credentials: dict | None = None):
        ...

    def create_application_calendar(self, name: str) -> ApplicationCalendarData:
        """
        Create a new application calendar.
        :param calendar_name: Name of the calendar to create.
        :return: Created Calendar instance.
        """
        ...

    def create_event(self, event_data: CalendarEventAdapterInputData) -> CalendarEventData:
        """
        Create a new event in the calendar.
        :param event_data: Dictionary containing event details.
        :return: Response from the calendar client.
        """
        ...

    def get_events(
        self,
        calendar_id: str,
        calendar_is_resource: bool,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str | None = None,
        max_results_per_page: int = 250,
    ) -> CalendarEventsSyncTypedDict:
        """
        Retrieve events within a specified date range.
        :param start_date: Start date for the event search.
        :param end_date: End date for the event search.
        :return: CalendarEventsSyncTypedDict.
        """
        ...

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEventData:
        """
        Retrieve a specific event by its unique identifier.
        :param event_id: Unique identifier of the event to retrieve.
        :return: Event details if found, otherwise None.
        """
        ...

    def update_event(
        self, calendar_id: str, event_id: str, event_data: CalendarEventData
    ) -> CalendarEventData:
        """
        Update an existing event in the calendar.
        :param event_id: Unique identifier of the event to update.
        :param event_data: Dictionary containing updated event details.
        :return: Response from the calendar client.
        """
        ...

    def delete_event(self, calendar_id: str, event_id: str):
        """
        Delete an event from the calendar.
        :param event_id: Unique identifier of the event to delete.
        :return: Response from the calendar client.
        """
        ...

    def get_account_calendars(self) -> Iterable[CalendarResourceData]:
        """
        Retrieve account account calendar.
        """
        ...

    def get_calendar_resources(self) -> Iterable[CalendarResourceData]:
        """
        Retrieve resources associated with the calendar.
        :return: List of resources.
        """
        ...

    def get_calendar_resource(self, resource_id: str) -> CalendarResourceData:
        """
        Retrieve a specific calendar resource by its unique identifier.
        :param resource_id: Unique identifier of the resource to retrieve.
        :return: Resource details if found, otherwise None.
        """
        ...

    def get_available_calendar_resources(
        self, start_time: datetime.datetime, end_time: datetime.datetime
    ) -> Iterable[CalendarResourceData]:
        """
        Retrieve available calendar resources within a specified time range.
        :param start_time: Start time for the availability check.
        :param end_time: End time for the availability check.
        :return: List of available resources.
        """
        ...

    def subscribe_to_calendar_events(self, resource_id: str, callback_url: str) -> None:
        ...


class BaseCalendarService(Protocol):
    @staticmethod
    def get_calendar_adapter_for_account(
        account: SocialAccount | GoogleCalendarServiceAccount,
    ) -> CalendarAdapter:
        ...

    def authenticate(
        self,
        account: SocialAccount | GoogleCalendarServiceAccount,
        organization: Organization,
    ) -> None:
        ...

    def import_account_calendars(self):
        ...

    def request_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        ...

    def import_organization_calendar_resources(
        self,
        import_workflow_state: CalendarOrganizationResourcesImport,
    ) -> None:
        ...

    def create_application_calendar(
        self, name: str, organization: Organization
    ) -> ApplicationCalendarData:
        ...

    def create_event(self, calendar_id: str, event_data: CalendarEventInputData) -> CalendarEvent:
        ...

    def update_event(
        self, calendar_id: str, event_id: str, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        ...

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        ...

    def transfer_event(self, event: CalendarEvent, new_calendar: Calendar) -> CalendarEvent:
        ...

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
    ) -> CalendarSync:
        ...

    def sync_events(
        self,
        calendar_sync: CalendarSync,
    ) -> None:
        ...

    def get_unavailable_time_windows_in_range(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindow]:
        ...

    def get_availability_windows_in_range(
        self, calendar: Calendar, start_datetime: datetime.datetime, end_datetime: datetime.datetime
    ) -> Iterable[AvailableTimeWindow]:
        ...

    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> Iterable[AvailableTime]:
        ...

    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str]],
    ) -> Iterable[BlockedTime]:
        ...

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> Iterable[CalendarResourceData]:
        ...

    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar:
        ...

    def _execute_calendar_sync(
        self,
        calendar_sync: CalendarSync,
        sync_token: str | None = None,
    ) -> None:
        ...

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        ...


class AuthenticatedCalendarService(BaseCalendarService):
    organization: Organization
    account: SocialAccount | GoogleCalendarServiceAccount
    calendar_adapter: CalendarAdapter


class NoProviderCalendarService(BaseCalendarService):
    organization: Organization
    account: None
    calendar_adapter: None


def is_calendar_service_authenticated(
    calendar_service: BaseCalendarService,
) -> TypeGuard[AuthenticatedCalendarService]:
    return (
        hasattr(calendar_service, "organization")
        and calendar_service.organization is not None
        and hasattr(calendar_service, "account")
        and calendar_service.account is not None
        and hasattr(calendar_service, "calendar_adapter")
        and calendar_service.calendar_adapter is not None
    )


def is_calendar_service_initialized_without_provider(
    calendar_service: BaseCalendarService,
) -> TypeGuard[NoProviderCalendarService]:
    return (
        hasattr(calendar_service, "organization")
        and calendar_service.organization is not None
        and hasattr(calendar_service, "account")
        and calendar_service.account is None
        and hasattr(calendar_service, "calendar_adapter")
        and calendar_service.calendar_adapter is None
    )


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

        # if not is_calendar_service_authenticated(self):
        #     raise ValueError(
        #         "This method requires authentication. Please call the `authenticate` method first."
        #     )

        import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
            organization=self.organization,
            start_time=start_time,
            end_time=end_time,
        )

        if not self.organization or not self.organization.id:
            raise NotImplementedError(
                "Calendar organization is not set for the current service instance."
            )

        if not self.account or not self.account.id:
            raise NotImplementedError("Account is not set for the current service instance.")

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
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

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
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

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
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

        query_kwargs = {
            "external_id": calendar_external_id,
            "organization_id": self.organization.id,
        }
        if self.calendar_adapter:
            query_kwargs["provider"] = self.calendar_adapter.provider
        return Calendar.objects.get(**query_kwargs)

    @lru_cache(maxsize=128)  # noqa: B019
    def _get_calendar_by_id(self, calendar_id: str) -> Calendar:
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

        return Calendar.objects.get(
            id=calendar_id,
            organization_id=self.organization.id,
        )

    def request_calendars_import(self) -> None:
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.
        """
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

        from calendar_integration.tasks import import_account_calendars_task

        if not self.organization or not self.organization.id:
            raise NotImplementedError(
                "Calendar organization is not set for the current service instance."
            )

        if not self.account or not self.account.id:
            raise NotImplementedError("Account is not set for the current service instance.")

        import_account_calendars_task.delay(  # type: ignore
            account_type="google_service_account"
            if isinstance(self.account, GoogleCalendarServiceAccount)
            else "social_account",
            account_id=self.account.id,
            organization_id=self.organization.id,
        )

    def import_account_calendars(self):
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.
        """
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

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
        if not is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup without a provider. "
                "Please call `initialize_without_provider` first."
            )

        if not self.organization:
            raise NotImplementedError(
                "Calendar organization is not set for the current service instance."
            )

        calendar = Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.VIRTUAL,
            original_payload={},
        )

        return calendar

    def create_bundle_calendar(
        self,
        name: str,
        description: str | None = None,
        child_calendars: Iterable[Calendar] | None = None,
    ) -> Calendar:
        """
        Create a new bundle calendar in the application without linking to an external provider.
        :param name: Name of the calendar.
        :param description: Description of the calendar.
        :param child_calendars: Iterable of child Calendar instances to include in the bundle.
        :return: Created Calendar instance.
        """
        if not is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup without a provider. "
                "Please call `initialize_without_provider` first."
            )
        if not self.organization:
            raise NotImplementedError(
                "Calendar organization is not set for the current service instance."
            )
        bundle_calendar = Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.BUNDLE,
            original_payload={},
        )

        for calendar in child_calendars or []:
            if calendar.organization_id != self.organization.id:
                raise ValueError(
                    "All child calendars must belong to the same organization as the bundle."
                )

            ChildrenCalendarRelationship.objects.create(
                parent_calendar=bundle_calendar,
                child_calendar=calendar,
            )
        return bundle_calendar

    def create_event(self, calendar_id: str, event_data: CalendarEventInputData) -> CalendarEvent:
        """
        Create a new event in the calendar.
        :param calendar_id: External ID of the calendar
        :param event_data: Dictionary containing event details.
        :return: Response from the calendar client.
        """
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )

        calendar = self._get_calendar_by_external_id(calendar_id)
        available_windows = self.get_availability_windows_in_range(
            calendar,
            event_data.start_time,
            event_data.end_time,
        )
        if not available_windows:
            raise ValueError("No available time windows for the event.")

        external_id = ""
        original_payload = {}
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
            parent_event_fk=parent_event,
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

    def update_event(
        self, calendar_id: str, event_id: str, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        """
        Update an existing event in the calendar.
        :param calendar_id: External ID of the calendar
        :param event_id: Unique identifier of the event to update.
        :param event_data: Dictionary containing updated event details.
        :return: Updated CalendarEvent instance.
        """
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
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
                ).filter(event__external_id=event_id, user_id__in=users_by_id.keys())
            }
            resources_by_id = {
                r.id: r
                for r in Calendar.objects.filter_by_organization(self.organization.id).filter(
                    id__in=[r.resource_id for r in event_data.resource_allocations]
                )
            }
            updated_event = self.calendar_adapter.update_event(
                calendar_id,
                event_id,
                CalendarEventData(
                    calendar_external_id=calendar_id,
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
                    external_id=event_id,
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

        calendar_event = CalendarEvent.objects.get(
            calendar__external_id=calendar_id,
            external_id=event_id,
            organization_id=self.organization.id,
        )

        calendar_event.title = event_data.title
        calendar_event.description = event_data.description
        calendar_event.start_time = event_data.start_time
        calendar_event.end_time = event_data.end_time
        if self.calendar_adapter:
            calendar_event.meta["latest_original_payload"] = original_payload

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
        calendar_id: str,
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

        :param calendar_id: External ID of the calendar
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

    def create_recurring_exception(
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

        :param parent_event: The recurring event to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_title: New title for the modified occurrence (if not cancelled)
        :param modified_description: New description for the modified occurrence (if not cancelled)
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified event or None if cancelled
        """
        if not parent_event.is_recurring:
            raise ValueError("Cannot create exception for non-recurring event")

        if is_cancelled:
            # Create a cancelled exception
            parent_event.create_exception(exception_date, is_cancelled=True)
            return None
        else:
            # Create a modified event
            modified_event_data = CalendarEventInputData(
                title=modified_title or parent_event.title,
                description=modified_description or parent_event.description,
                start_time=modified_start_time or exception_date,
                end_time=modified_end_time or (exception_date + parent_event.duration),
                parent_event_id=parent_event.id,
                is_recurring_exception=True,
            )

            modified_event = self.create_event(
                parent_event.calendar.external_id, modified_event_data
            )
            parent_event.create_exception(
                exception_date, is_cancelled=False, modified_event=modified_event
            )
            return modified_event

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
        if not recurring_event.is_recurring:
            return [recurring_event] if start_date <= recurring_event.start_time <= end_date else []

        return recurring_event.get_occurrences_in_range(
            start_date, end_date, include_self=True, include_exceptions=include_exceptions
        )

    def _get_events_occurrences_in_range(
        self,
        non_recurring_events: Iterable[CalendarEvent],
        recurring_events: Iterable[CalendarEvent],
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ):
        """
        Get all occurrences (non-recurring and recurring) within a date range.

        :param non_recurring_events: Iterable of non-recurring events
        :param recurring_events: Iterable of recurring events
        :param start_date: Start of the date range
        :param end_date: End of the date range
        :return: List of all event occurrences in the range
        """

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
        base_qs = (
            CalendarEvent.objects.annotate_recurring_occurrences_on_date_range(start_date, end_date)
            .select_related("recurrence_rule")
            .filter(
                parent_event__isnull=True,  # Master events only
            )
        )
        if calendar.calendar_type == CalendarType.BUNDLE:
            base_qs = base_qs.filter(
                organization_id=calendar.organization_id,
                calendar__in=calendar.children.all(),
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
        return events

    def delete_event(self, calendar_id: str, event_id: str, delete_series: bool = False) -> None:
        """
        Delete an event from the calendar.
        :param calendar_id: External ID of the calendar
        :param event_id: Unique identifier of the event to delete.
        :param delete_series: If True and the event is recurring, delete the entire series
        :return: None
        """
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )

        event = CalendarEvent.objects.get(
            calendar__external_id=calendar_id,
            external_id=event_id,
            organization_id=self.organization.id,
        )

        if self.calendar_adapter:
            if event.is_recurring and delete_series:
                # Delete the entire recurring series from external calendar
                self.calendar_adapter.delete_event(calendar_id, event_id)
            elif event.is_recurring_instance and not delete_series:
                # Create a cancellation exception instead of deleting
                if event.parent_event:
                    event.parent_event.create_exception(event.recurrence_id, is_cancelled=True)
            else:
                # Delete single event or instance
                self.calendar_adapter.delete_event(calendar_id, event_id)

        if event.is_recurring and delete_series:
            # Delete the entire series including all instances and exceptions
            event.recurring_instances.all().delete()
            event.recurrence_exceptions.all().delete()
            if event.recurrence_rule:
                event.recurrence_rule.delete()
            event.delete()
        elif event.is_recurring_instance and not delete_series:
            # For instances, we create an exception rather than delete
            if event.parent_event and event.recurrence_id:
                event.parent_event.create_exception(event.recurrence_id, is_cancelled=True)
        else:
            # Delete single non-recurring event
            event.delete()

    def transfer_event(self, event: CalendarEvent, new_calendar: Calendar) -> CalendarEvent:
        """
        Transfer an event to a different calendar.
        :param event_id: Unique identifier of the event to transfer.
        :param new_calendar_external_id: External ID of the new calendar.
        :return: Transferred CalendarEvent instance.
        """
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

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
        new_event = self.create_event(new_calendar.external_id, new_event_data)

        # Delete the old event
        self.delete_event(event.calendar.external_id, event.external_id)

        return new_event

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

        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

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
        if not is_calendar_service_authenticated(self):
            raise ValueError(
                "This method requires authentication. Please call the `authenticate` method first."
            )

        if not self.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

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
        if not self.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

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
        ) = self._get_existing_calendar_data(calendar.external_id, start_date, end_date)

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
                calendar.external_id,
                calendar_events_by_external_id,
                changes.matched_event_ids,
                start_date,
            )
        else:
            calendar_sync.next_sync_token = next_sync_token or ""
            calendar_sync.save(update_fields=["next_sync_token"])

        # Apply all changes to database
        self._apply_sync_changes(calendar.external_id, changes)

        # Update available time windows if needed
        if calendar.manage_available_windows:
            self._remove_available_time_windows_that_overlap_with_blocked_times_and_events(
                calendar.external_id,
                changes.blocked_times_to_create + changes.blocked_times_to_update,
                changes.events_to_update,
                start_date,
                end_date,
            )

    def _get_existing_calendar_data(
        self, calendar_id: str, start_date: datetime.datetime, end_date: datetime.datetime
    ):
        """Get existing calendar events and blocked times for the date range."""
        if not self.organization:
            return ({}, {})

        calendar_events_by_external_id = {
            e.external_id: e
            for e in CalendarEvent.objects.filter(
                calendar__external_id=calendar_id,
                start_time__gte=start_date,
                end_time__lte=end_date,
                organization_id=self.organization.id,
            )
        }
        blocked_times_by_external_id = {
            e.external_id: e
            for e in BlockedTime.objects.filter(
                calendar__external_id=calendar_id,
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
        calendar = self._get_calendar_by_external_id(calendar.external_id)

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
                    parent_event_fk=parent_event,
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
        calendar_id: str,
        calendar_events_by_external_id: dict,
        matched_event_ids: set[str],
        start_date: datetime.datetime,
    ):
        """Handle deletions when doing a full sync (no sync_token)."""
        if not self.organization:
            return

        deleted_ids = set(calendar_events_by_external_id.keys()) - matched_event_ids
        CalendarEvent.objects.filter(
            calendar__external_id=calendar_id,
            external_id__in=deleted_ids,
            start_time__gte=start_date,
            organization_id=self.organization.id,
        ).delete()

    def _apply_sync_changes(self, calendar_id: str, changes: EventsSyncChanges):
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
                calendar__external_id=calendar_id,
                external_id__in=changes.events_to_delete,
                organization=self.organization,
            ).delete()

        if changes.blocks_to_delete:
            BlockedTime.objects.filter(
                calendar__external_id=calendar_id,
                external_id__in=changes.blocks_to_delete,
                organization=self.organization,
            ).delete()

        # After all changes are applied, link orphaned recurring instances to their parents
        self._link_orphaned_recurring_instances(calendar_id)

    def _link_orphaned_recurring_instances(self, calendar_id: str):
        """
        Link recurring event instances that were created before their parent events
        were synced. This happens when webhook events come out of order.
        """
        if not self.organization:
            return

        # Find events that have a pending parent external ID in their meta
        orphaned_instances = CalendarEvent.objects.filter(
            calendar__external_id=calendar_id,
            organization_id=self.organization.id,
            parent_event__isnull=True,
            meta__pending_parent_external_id__isnull=False,
        )

        # Also find blocked times that might be orphaned instances
        orphaned_blocked_times = BlockedTime.objects.filter(
            calendar__external_id=calendar_id,
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
                    instance.parent_event_fk = parent_event
                    instance.recurrence_id = instance.start_time
                    # Clear the pending parent ID
                    instance.meta.pop("pending_parent_external_id", None)
                    instance.save(update_fields=["parent_event_fk", "recurrence_id", "meta"])
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
        calendar_id: str,
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
            calendar__external_id=calendar_id,
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
            calendar__external_id=calendar_id,
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
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )

        # Get expanded calendar events (including recurring instances)
        # This handles both master events and their generated instances
        calendar_events = self.get_calendar_events_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        # Get blocked times that overlap with the time range
        # Using overlap logic: blocked_time.start_time < end_datetime AND blocked_time.end_time > start_datetime
        blocked_times = calendar.blocked_times.filter(
            start_time__lt=end_datetime,
            end_time__gt=start_datetime,
        ).order_by("start_time")

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
                                event.parent_event.attendances.all()
                                if event.parent_event
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
                                event.parent_event.external_attendances.all()
                                if event.parent_event
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
                                event.parent_event.resource_allocations.all()
                                if event.parent_event
                                else (event.resource_allocations.all() if event.id else [])
                            )
                        ],
                        external_id=event.external_id,
                    ),
                )
                for event in calendar_events
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
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )

        if calendar.manage_available_windows:
            return [
                AvailableTimeWindow(
                    start_time=available_time.start_time,
                    end_time=available_time.end_time,
                    id=available_time.id,
                    can_book_partially=False,
                )
                for available_time in AvailableTime.objects.filter(
                    calendar=calendar,
                    start_time__gte=start_datetime,
                    end_time__lte=end_datetime,
                )
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

    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> Iterable[AvailableTime]:
        """
        Create a new availability window for a calendar.
        :param calendar: The calendar to create the availability window for.
        :param start_time: Start time of the availability window.
        :param end_time: End time of the availability window.
        :return: Created AvailableTime instance.
        """
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )

        if not calendar.manage_available_windows:
            raise ValueError("This calendar does not manage available windows.")

        return AvailableTime.objects.bulk_create(
            [
                AvailableTime(
                    calendar=calendar,
                    start_time=start_time,
                    end_time=end_time,
                    organization_id=calendar.organization_id,
                )
                for start_time, end_time in availability_windows
            ]
        )

    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str]],
    ) -> Iterable[BlockedTime]:
        """
        Create new blocked times for a calendar.
        :param calendar: The calendar to create the blocked times for.
        :param blocked_times: Iterable of tuples containing start time, end time, and reason.
        :return: List of created BlockedTime instances.
        """
        if not is_calendar_service_authenticated(
            self
        ) or is_calendar_service_initialized_without_provider(self):
            raise ValueError(
                "This method requires calendar organization setup. "
                "Please call either `authenticate` or `initialize_without_provider` first."
            )

        return BlockedTime.objects.bulk_create(
            [
                BlockedTime(
                    calendar=calendar,
                    start_time=start_time,
                    end_time=end_time,
                    reason=reason,
                    organization_id=calendar.organization_id,
                )
                for start_time, end_time, reason in blocked_times
            ]
        )
