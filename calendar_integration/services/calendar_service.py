import datetime
import json
import logging
from collections.abc import Callable, Iterable
from typing import Annotated, Literal, TypedDict

from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.urls import reverse

from allauth.socialaccount.models import SocialAccount, SocialToken
from dependency_injector.wiring import Provide, inject

from calendar_integration.constants import (
    CalendarOrganizationResourceImportStatus,
    CalendarProvider,
    CalendarSyncStatus,
    CalendarSyncTriggerSource,
    CalendarType,
    CalendarVisibility,
    IncomingWebhookProcessingStatus,
)
from calendar_integration.exceptions import (
    InvalidCalendarTokenError,
    ServiceNotAuthenticatedError,
    WebhookIgnoredError,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarOwnership,
    CalendarSync,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
)
from calendar_integration.querysets import CalendarEventQuerySet
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_bundle_service import CalendarBundleService
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_service_utils import (
    convert_naive_utc_datetime_to_timezone as _convert_naive_utc_datetime_to_timezone,
)
from calendar_integration.services.calendar_service_utils import (
    get_calendar_by_external_id as _get_calendar_by_external_id_util,
)
from calendar_integration.services.calendar_service_utils import (
    get_calendar_by_id as _get_calendar_by_id_util,
)
from calendar_integration.services.calendar_service_utils import (
    grant_calendar_owner_permissions as _grant_calendar_owner_permissions_util,
)
from calendar_integration.services.calendar_service_utils import (
    grant_event_attendee_permissions as _grant_event_attendee_permissions_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event as _serialize_event_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event_data_input as _serialize_event_data_input_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event_external_attendee as _serialize_event_external_attendee_util,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event_internal_attendee as _serialize_event_internal_attendee_util,
)
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    AvailableTimeWindow,
    CalendarEventAdapterOutputData,
    CalendarEventData,
    CalendarEventInputData,
    CalendarResourceData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
    EventsSyncChanges,
    ResourceAllocationInputData,
    UnavailableTimeWindow,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from calendar_integration.services.recurrence_manager import RecurrenceManager
from calendar_integration.services.type_guards import (
    is_authenticated_calendar_service,
    is_initialized_or_authenticated_calendar_service,
)
from organizations.models import Organization
from public_api.models import SystemUser
from users.models import User


class WebhookHealthStatus(TypedDict):
    total_subscriptions: int
    active_subscriptions: int
    expired_subscriptions: int
    expiring_soon_subscriptions: int
    recent_events_count: int
    failed_events_count: int
    success_rate: float


logger = logging.getLogger(__name__)


class CalendarService(BaseCalendarService):
    organization: Organization | None
    user_or_token: User | str | SystemUser | None
    account: SocialAccount | GoogleCalendarServiceAccount | None
    calendar_adapter: CalendarAdapter | None

    @inject
    def __init__(
        self,
        calendar_side_effects_service: Annotated[
            "CalendarSideEffectsService | None", Provide["calendar_side_effects_service"]
        ] = None,
        calendar_permission_service: Annotated[
            "CalendarPermissionService | None", Provide["calendar_permission_service"]
        ] = None,
    ) -> None:
        """Initialize a CalendarService instance. Call authenticate() before using calendar operations."""
        self.organization = None
        self.user_or_token = None
        self.account = None
        self.calendar_adapter = None
        self.calendar_side_effects_service = calendar_side_effects_service
        self.calendar_permission_service = calendar_permission_service
        # Per-instance calendar lookup cache: keyed on (organization_id, id_or_external_id).
        # This replaces the @lru_cache approach which was keyed only on id/external_id and
        # could return a cached Calendar from a different organization when the service
        # instance is reused across organizations (multi-tenant safety bug).
        self._calendar_cache: dict[tuple[int, str | int], Calendar] = {}
        # Shared auth-context snapshot; set by authenticate() / initialize_without_provider().
        self._context: CalendarServiceContext | None = None
        # Stateless recurrence engine shared by event/blocked-time/available-time methods.
        # Constructed once; it holds no auth state (everything arrives as method params).
        self._recurrence_manager = RecurrenceManager()

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None:
        """
        Grant calendar management permissions to all owners of a calendar.
        """
        if not self.calendar_permission_service:
            return

        _grant_calendar_owner_permissions_util(self.calendar_permission_service, calendar)

    def _grant_event_attendee_permissions(self, event: CalendarEvent) -> None:
        """
        Grant event management permissions to all attendees of an event.
        """
        if not self.calendar_permission_service:
            return

        _grant_event_attendee_permissions_util(self.calendar_permission_service, event)

    def _serialize_event_internal_attendee(
        self, attendance: EventAttendance
    ) -> EventInternalAttendeeData:
        return _serialize_event_internal_attendee_util(attendance)

    def _serialize_event_external_attendee(
        self, external_attendance: EventExternalAttendance
    ) -> EventExternalAttendeeData:
        return _serialize_event_external_attendee_util(external_attendance)

    def _serialize_event(self, event: CalendarEvent) -> CalendarEventData:
        """Build webhook payload for calendar event."""
        return _serialize_event_util(event)

    @staticmethod
    def _get_calendar_adapter_cls_for_provider(provider: CalendarProvider):
        if provider == CalendarProvider.GOOGLE:
            from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
                GoogleCalendarAdapter,
            )

            return GoogleCalendarAdapter

        if provider == CalendarProvider.MICROSOFT:
            from calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter import (
                MSOutlookCalendarAdapter,
            )

            return MSOutlookCalendarAdapter

        raise NotImplementedError(f"Calendar adapter for provider {provider} is not implemented.")

    @staticmethod
    def get_calendar_adapter_for_account(
        account: "User | SocialAccount | GoogleCalendarServiceAccount",
    ) -> tuple[CalendarAdapter, SocialAccount | GoogleCalendarServiceAccount]:
        """
        Retrieve a calendar adapter for the given account.
        :param account: A ``User`` (resolves the newest valid token across the
            user's connected providers), a specific ``SocialAccount`` (resolves
            that account's token — provider-precise), or a
            ``GoogleCalendarServiceAccount``.
        :return: CalendarAdapter instance and the account used (SocialAccount or GoogleCalendarServiceAccount).
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
            ), account

        # Do NOT exclude expired tokens here: an expired access token that still
        # carries a refresh_token (token_secret) is refreshed by the adapter on
        # construction. Filtering on expires_at would hide exactly those tokens
        # (and any with a NULL expiry), producing a false "reauthenticate" error.
        token_qs = SocialToken.objects.select_related("account").filter(
            account__provider__in=[CalendarProvider.GOOGLE, CalendarProvider.MICROSOFT],
        )
        if isinstance(account, SocialAccount):
            # Provider-precise: the token for exactly this connected account.
            token_qs = token_qs.filter(account=account)
        else:
            # A User: newest token across their connected providers.
            token_qs = token_qs.filter(account__user=account)
        token = token_qs.order_by("-id").first()

        if not token or not token.token:
            # Diagnostic: dump what tokens DO exist for this account/user so the
            # docker logs reveal WHY resolution failed (missing token row, empty
            # token, wrong provider, ...) instead of a bare "reauthenticate".
            if isinstance(account, SocialAccount):
                scope = SocialToken.objects.filter(account=account)
                who = f"social_account id={account.id} provider={account.provider!r}"
            else:
                scope = SocialToken.objects.filter(account__user=account)
                who = f"user id={getattr(account, 'id', None)}"
            diag = [
                {
                    "token_id": t.id,
                    "provider": t.account.provider,
                    "has_token": bool(t.token),
                    "has_refresh": bool(t.token_secret),
                    "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                }
                for t in scope.select_related("account")
            ]
            logger.warning(
                "calendar token resolution failed for %s; all tokens for it = %s",
                who,
                diag,
            )
            raise InvalidCalendarTokenError(
                "User doesn't have a valid calendar token. Please reauthenticate"
            )

        logger.info(
            "resolved calendar token id=%s provider=%s has_refresh=%s expires_at=%s",
            token.id,
            token.account.provider,
            bool(token.token_secret),
            token.expires_at.isoformat() if token.expires_at else None,
        )

        calendar_adapter_cls = CalendarService._get_calendar_adapter_cls_for_provider(
            token.account.provider
        )

        return calendar_adapter_cls(
            credentials_dict={
                "token": token.token,
                "refresh_token": token.token_secret,
                "account_id": f"social-{token.account_id}",
                "expiry": token.expires_at,
                "social_token_id": token.id,
            }
        ), token.account

    def authenticate(
        self,
        account: "User | SocialAccount | GoogleCalendarServiceAccount",
        organization: Organization,
    ) -> None:
        """
        Authenticate the service with the provided account.
        :param account: A ``User``, a ``SocialAccount``, or a
            ``GoogleCalendarServiceAccount``. When a ``SocialAccount`` is given,
            the owning ``User`` is used for record attribution (e.g.
            ``CalendarOwnership``).
        :param organization: Calendar organization instance.
        """
        if isinstance(account, User):
            self.user_or_token = account
        elif isinstance(account, SocialAccount):
            self.user_or_token = account.user
        else:
            self.user_or_token = None
        self.organization = organization
        self.calendar_adapter, self.account = self.get_calendar_adapter_for_account(account)

        # Reset the per-instance calendar lookup cache whenever the auth context changes.
        # This ensures no stale cross-organization entries survive a re-authentication.
        self._calendar_cache = {}

        # Build the immutable auth-context snapshot consumed by sub-services.
        self._context = CalendarServiceContext(
            organization=self.organization,
            user_or_token=self.user_or_token,
            account=self.account,
            calendar_adapter=self.calendar_adapter,
            calendar_permission_service=self.calendar_permission_service,
            calendar_side_effects_service=self.calendar_side_effects_service,
        )

    def initialize_without_provider(
        self,
        user_or_token: User | str | SystemUser | None = None,
        organization: Organization | None = None,
    ):
        """
        Initialize the service without a specific calendar provider.
        :param organization: Calendar organization instance.
        """
        self.organization = organization
        self.user_or_token = user_or_token
        self.account = None
        self.calendar_adapter = None

        if (
            self.calendar_permission_service
            and self.organization
            and isinstance(self.user_or_token, str)
        ):
            self.calendar_permission_service.initialize_with_token(
                self.user_or_token, organization_id=self.organization.id
            )

        # Reset the per-instance calendar lookup cache whenever the auth context changes.
        self._calendar_cache = {}

        # Build the immutable auth-context snapshot consumed by sub-services.
        self._context = CalendarServiceContext(
            organization=self.organization,
            user_or_token=self.user_or_token,
            account=self.account,
            calendar_adapter=self.calendar_adapter,
            calendar_permission_service=self.calendar_permission_service,
            calendar_side_effects_service=self.calendar_side_effects_service,
        )

    def _build_context_snapshot(self) -> CalendarServiceContext:
        """Build a context snapshot from the current auth-state instance attributes.

        Read live each time the event sub-service is requested so the snapshot always
        reflects the facade's current auth state — including the (unset) state before
        ``authenticate()`` / ``initialize_without_provider()`` (the sub-service guard
        then fires exactly as the former facade method did) and any direct mutation of
        the facade's ``account`` / ``calendar_adapter`` attributes.
        """
        return CalendarServiceContext(
            organization=self.organization,
            user_or_token=self.user_or_token,
            account=self.account,
            calendar_adapter=self.calendar_adapter,
            calendar_permission_service=self.calendar_permission_service,
            calendar_side_effects_service=self.calendar_side_effects_service,
        )

    def _get_event_service(self) -> CalendarEventService:
        """Return the event sub-service bound to the facade's current auth context.

        The event service shares the facade-owned calendar cache and recurrence
        manager, and routes availability (Phase 4), bundle fan-out (Phase 3), and the
        shared write-adapter / attendee-permission helpers back through the facade
        (``host=self``). The context is rebuilt from the live facade attributes each
        call (a cheap dataclass construction — no network / adapter rebuild), so it
        never goes stale across re-authentication or direct attribute mutation.
        """
        return CalendarEventService(
            context=self._build_context_snapshot(),
            recurrence_manager=self._recurrence_manager,
            calendar_cache=self._calendar_cache,
            host=self,
        )

    def _get_bundle_service(self) -> CalendarBundleService:
        """Return the bundle sub-service bound to the facade's current auth context.

        The bundle service routes availability (Phase 4), event CRUD, and the
        shared write-adapter / permission helpers back through the facade
        (``host=self``). The context is rebuilt from the live facade attributes each
        call (a cheap dataclass construction — no network / adapter rebuild), so it
        never goes stale across re-authentication or direct attribute mutation.
        """
        return CalendarBundleService(
            context=self._build_context_snapshot(),
            host=self,
        )

    def _get_availability_service(self) -> AvailabilityService:
        """Return the availability sub-service bound to the facade's current auth context.

        The availability service shares the facade-owned recurrence manager and routes
        event reads (Phase 2), facade-resident blocked-time bulk creation, and the
        shared recurrence-rule helper back through the facade (``host=self``). The
        context is rebuilt from the live facade attributes each call (a cheap dataclass
        construction — no network / adapter rebuild), so it never goes stale across
        re-authentication or direct attribute mutation.
        """
        return AvailabilityService(
            context=self._build_context_snapshot(),
            recurrence_manager=self._recurrence_manager,
            host=self,
        )

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

        # Capture ids by value so the closure is independent of mutable self state.
        _account_type = (
            "google_service_account"
            if isinstance(self.account, GoogleCalendarServiceAccount)
            else "social_account"
        )
        _account_id = self.account.id
        _organization_id = self.organization.id
        _import_workflow_state_id = import_workflow_state.id

        transaction.on_commit(
            lambda: import_organization_calendar_resources_task.delay(  # type: ignore
                account_type=_account_type,
                account_id=_account_id,
                organization_id=_organization_id,
                import_workflow_state_id=_import_workflow_state_id,
            )
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
                calendar=Calendar.objects.update_or_create(
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
            meta={"latest_original_payload": created_calendar.original_payload} or {},
        )

        # Create calendar ownership for the user who created it
        if isinstance(self.user_or_token, User):
            CalendarOwnership.objects.create(
                organization=organization,
                calendar=calendar,
                user=self.user_or_token,
                is_default=False,
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

        # Grant permissions to calendar owners
        self._grant_calendar_owner_permissions(calendar)

        return created_calendar

    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar:
        if not is_authenticated_calendar_service(self):
            raise

        return _get_calendar_by_external_id_util(
            self._calendar_cache,
            calendar_external_id,
            self.organization,
            self.calendar_adapter,
        )

    def _get_calendar_by_id(self, calendar_id: int) -> Calendar:
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        return _get_calendar_by_id_util(
            self._calendar_cache,
            calendar_id,
            self.organization,
        )

    def request_calendars_import(self, sync_after_import: bool = True) -> None:
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.

        :param sync_after_import: When True (default), each imported sync-enabled
            calendar is also synced. Pass False to only discover/refresh calendar
            rows without pulling events.
        """
        from calendar_integration.tasks import import_account_calendars_task

        if not is_authenticated_calendar_service(self):
            raise

        # Capture ids by value so the closure is independent of mutable self state.
        _account_type = (
            "google_service_account"
            if isinstance(self.account, GoogleCalendarServiceAccount)
            else "social_account"
        )
        _account_id = self.account.id
        _organization_id = self.organization.id
        _sync_after_import = sync_after_import

        transaction.on_commit(
            lambda: import_account_calendars_task.delay(  # type: ignore
                account_type=_account_type,
                account_id=_account_id,
                organization_id=_organization_id,
                sync_after_import=_sync_after_import,
            )
        )

    @staticmethod
    def _sync_enabled_default_for_access_role(access_role: str | None) -> bool:
        """Decide whether a freshly imported calendar should sync by default.

        Calendars the account owns or can write to (the user's own calendars) sync.
        Subscribed read-only calendars — holidays, birthdays, shared org-wide
        calendars — default to disabled: their events typically duplicate events
        already on the user's own calendars, and they aren't useful for scheduling.
        Unknown access role (e.g. a provider that doesn't report one) defaults to
        enabled to preserve prior behavior.
        """
        if access_role is None:
            return True
        return access_role.lower() in ("owner", "writer")

    @transaction.atomic()
    def import_account_calendars(self, sync_after_import: bool = True):
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.

        :param sync_after_import: When True (default), enqueue an event sync for each
            imported calendar that has sync enabled. The per-calendar ``sync_enabled``
            flag still gates whether a sync actually runs.
        """
        if not is_authenticated_calendar_service(self):
            raise

        calendars = self.calendar_adapter.get_account_calendars()

        for calendar_data in calendars:
            calendar, _ = Calendar.objects.update_or_create(
                external_id=calendar_data.external_id,
                organization=self.organization,
                defaults={
                    "name": calendar_data.name,
                    "description": calendar_data.description,
                    "email": calendar_data.email,
                    "provider": CalendarProvider(calendar_data.provider),
                    "meta": {
                        "latest_original_payload": calendar_data.original_payload or {},
                    },
                },
                # calendar_type, sync_enabled and visibility are seeded only on first import
                # (create), never on re-import. calendar_type must stay out of the lookup so
                # that resource calendars returned by the provider's calendarList (rooms visible
                # to the user) don't collide with the unique (external_id, provider, org)
                # constraint — and don't accidentally get re-typed as PERSONAL.
                create_defaults={
                    "name": calendar_data.name,
                    "description": calendar_data.description,
                    "email": calendar_data.email,
                    "provider": CalendarProvider(calendar_data.provider),
                    "meta": {
                        "latest_original_payload": calendar_data.original_payload or {},
                    },
                    "calendar_type": CalendarType.PERSONAL,
                    "sync_enabled": self._sync_enabled_default_for_access_role(
                        calendar_data.access_role
                    ),
                    "visibility": CalendarVisibility.ACTIVE,
                    # Imported calendars manage their own availability windows by
                    # default. Seeded on create only (create_defaults), so a later
                    # user toggle via PATCH /calendars/{id}/ is never clobbered on
                    # re-import.
                    "manage_available_windows": True,
                },
            )

            # Resource calendars are owned and synced via the rooms-sync path; skip
            # personal ownership and sync for them here.
            if calendar.calendar_type == CalendarType.RESOURCE:
                continue

            CalendarOwnership.objects.update_or_create(
                organization=self.organization,
                calendar=calendar,
                user=self.account.user if self.account else None,
                defaults={"is_default": calendar_data.is_default},
            )

            # Grant permissions to calendar owners
            self._grant_calendar_owner_permissions(calendar)

            if sync_after_import:
                self.request_calendar_sync(
                    calendar=calendar,
                    start_datetime=datetime.datetime.now(datetime.UTC),
                    end_datetime=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
                    should_update_events=True,
                    trigger_source=CalendarSyncTriggerSource.IMPORT,
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

        calendar = Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.VIRTUAL,
            original_payload={},
        )

        # Create calendar ownership for the user who created it
        if isinstance(self.user_or_token, User):
            CalendarOwnership.objects.create(
                organization=self.organization,
                calendar=calendar,
                user=self.user_or_token,
                is_default=False,
            )

        # Grant permissions to calendar owners
        self._grant_calendar_owner_permissions(calendar)

        return calendar

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
        return self._get_bundle_service().create_bundle_calendar(
            name=name,
            description=description,
            child_calendars=child_calendars,
            primary_calendar=primary_calendar,
        )

    @transaction.atomic()
    def update_bundle_calendar(
        self,
        bundle_calendar: Calendar,
        child_calendars: Iterable[Calendar],
        primary_calendar: Calendar | None = None,
    ) -> Calendar:
        """
        Reconcile the children and primary designation for an existing bundle calendar.

        Adds `ChildrenCalendarRelationship` rows for newly-added children, removes rows
        for dropped children, and updates `is_primary` so that exactly one row is primary
        when `primary_calendar` is provided.

        :param bundle_calendar: The bundle Calendar instance to update.
        :param child_calendars: Full desired set of child Calendar instances.
        :param primary_calendar: The child to designate as primary; must be in child_calendars.
        :return: The (unchanged) bundle_calendar instance after reconciliation.
        :raises ValueError: If bundle_calendar is not a BUNDLE type, children are cross-org,
                            or primary_calendar is not in child_calendars.
        """
        return self._get_bundle_service().update_bundle_calendar(
            bundle_calendar=bundle_calendar,
            child_calendars=child_calendars,
            primary_calendar=primary_calendar,
        )

    def _create_bundle_event(
        self, bundle_calendar: Calendar, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        """Create a bundle event — delegates to ``CalendarBundleService``.

        This method is the ``EventServiceHost`` seam: ``CalendarEventService`` calls
        ``self._host._create_bundle_event(...)`` when the target calendar is a BUNDLE.
        The facade is the host, so control reaches here and is forwarded to the bundle
        sub-service. See ``CalendarBundleService.create_bundle_event`` for full semantics.
        """
        return self._get_bundle_service().create_bundle_event(bundle_calendar, event_data)

    def _get_primary_calendar(self, bundle_calendar: Calendar) -> Calendar:
        """Get the designated primary calendar for a bundle — delegates to ``CalendarBundleService``."""
        return self._get_bundle_service()._get_primary_calendar(bundle_calendar)

    def _collect_bundle_attendees(
        self, child_calendars: list[Calendar], event_data: "CalendarEventInputData"
    ) -> list["EventAttendanceInputData"]:
        """Collect bundle attendees — delegates to ``CalendarBundleService``."""
        return self._get_bundle_service()._collect_bundle_attendees(child_calendars, event_data)

    def _get_write_adapter_for_calendar(self, calendar: Calendar) -> CalendarAdapter | None:
        # if the authenticated account doesn't own the calendar:
        if not self.account or not (
            (
                isinstance(self.account, SocialAccount)
                and calendar.users.filter(id=self.account.user_id).exists()
            )
            or (
                isinstance(self.account, GoogleCalendarServiceAccount)
                and self.account.calendar == calendar
            )
        ):
            # gets social account of one of the owners if they exist, favoring the owners that have
            # this calendar as default
            ownership = (
                calendar.ownerships.order_by("-is_default", "created")
                .select_related("user")
                .filter(
                    user__in=User.objects.filter(
                        socialaccount__provider=calendar.provider,
                    )
                )
                .first()
            )

            if ownership:
                return CalendarService.get_calendar_adapter_for_account(ownership.user)[0]

            # if the calendar doesn't have a valid owner, try to use self.calendar_adapter

        return self.calendar_adapter

    def convert_naive_utc_datetime_to_timezone(
        self, datetime_obj: datetime.datetime, iana_tz: str
    ) -> datetime.datetime:
        """Return the naive local wall-clock of an instant in the given IANA timezone.

        Delegates to the shared module-level utility in ``calendar_service_utils``.
        See that function's docstring for full semantics.

        e.g. 12:00Z + "America/Recife" -> 09:00 (naive).
        """
        return _convert_naive_utc_datetime_to_timezone(datetime_obj, iana_tz)

    def _serialize_event_data_input(
        self, event: CalendarEvent, event_data: CalendarEventInputData
    ) -> CalendarEventData:
        return _serialize_event_data_input_util(event, event_data, self.organization)

    @transaction.atomic()
    def create_event(self, calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        """
        Create a new event in the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_data: Dictionary containing event details.
        :return: Response from the calendar client.
        """
        return self._get_event_service().create_event(calendar_id, event_data)

    def _update_bundle_event(
        self, bundle_event: CalendarEvent, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        """Update a bundle event — delegates to ``CalendarBundleService``.

        This method is the ``EventServiceHost`` seam: ``CalendarEventService`` calls
        ``self._host._update_bundle_event(...)`` when updating a bundle primary event.
        The facade is the host, so control reaches here and is forwarded to the bundle
        sub-service. See ``CalendarBundleService.update_bundle_event`` for full semantics.
        """
        return self._get_bundle_service().update_bundle_event(bundle_event, event_data)

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
        return self._get_event_service().update_event(calendar_id, event_id, event_data)

    def create_recurring_event(
        self,
        calendar_id: int,
        title: str,
        description: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        recurrence_rule: str,
        attendances: list[EventAttendanceInputData] | None = None,
        external_attendances: list[EventExternalAttendanceInputData] | None = None,
        resource_allocations: list[ResourceAllocationInputData] | None = None,
    ) -> CalendarEvent:
        """
        Create a recurring event with the specified recurrence rule.

        This method is just a shortcut, the `create_event` method also supports the
        creation of recurring events.
        """
        return self._get_event_service().create_recurring_event(
            calendar_id=calendar_id,
            title=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            recurrence_rule=recurrence_rule,
            attendances=attendances,
            external_attendances=external_attendances,
            resource_allocations=resource_allocations,
        )

    def create_recurring_event_exception(
        self,
        parent_event: CalendarEvent,
        exception_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        modified_timezone: str | None = None,
        is_cancelled: bool = False,
    ) -> CalendarEvent | None:
        """
        Create an exception for a recurring event (either cancelled or modified).

        See ``CalendarEventService.create_recurring_event_exception`` for full semantics.
        """
        return self._get_event_service().create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_date,
            modified_title=modified_title,
            modified_description=modified_description,
            modified_start_time=modified_start_time,
            modified_end_time=modified_end_time,
            modified_timezone=modified_timezone,
            is_cancelled=is_cancelled,
        )

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
        return self._get_event_service().get_recurring_event_instances(
            recurring_event, start_date, end_date, include_exceptions
        )

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        optimize_queryset: Callable[[CalendarEventQuerySet], CalendarEventQuerySet] | None = None,
    ) -> list[CalendarEvent]:
        """
        Get all calendar events in a date range with recurring events expanded to instances.

        See ``CalendarEventService.get_calendar_events_expanded`` for full semantics.
        """
        return self._get_event_service().get_calendar_events_expanded(
            calendar, start_date, end_date, optimize_queryset
        )

    def _delete_bundle_event(self, bundle_event: CalendarEvent) -> None:
        """Delete a bundle event — delegates to ``CalendarBundleService``.

        This method is the ``EventServiceHost`` seam: ``CalendarEventService`` calls
        ``self._host._delete_bundle_event(...)`` when deleting a bundle primary event.
        The facade is the host, so control reaches here and is forwarded to the bundle
        sub-service. See ``CalendarBundleService.delete_bundle_event`` for full semantics.
        """
        self._get_bundle_service().delete_bundle_event(bundle_event)

    def delete_event(self, calendar_id: int, event_id: int, delete_series: bool = False) -> None:
        """
        Delete an event from the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_id: Unique identifier of the event to delete.
        :param delete_series: If True and the event is recurring, delete the entire series
        :return: None
        """
        return self._get_event_service().delete_event(calendar_id, event_id, delete_series)

    def transfer_event(self, event: CalendarEvent, new_calendar: Calendar) -> CalendarEvent:
        """
        Transfer an event to a different calendar.
        :param event_id: Unique identifier of the event to transfer.
        :param new_calendar_external_id: External ID of the new calendar.
        :return: Transferred CalendarEvent instance.
        """
        return self._get_event_service().transfer_event(event, new_calendar)

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
        trigger_source: CalendarSyncTriggerSource = CalendarSyncTriggerSource.MANUAL,
    ) -> CalendarSync | None:
        """
        Request a calendar synchronization for a specific date range.
        :param calendar: The calendar to synchronize.
        :param start_datetime: Start date for the event search.
        :param end_datetime: End date for the event search.
        :param should_update_events: Whether to update existing events.
        :param trigger_source: What kicked off this sync (import/manual/webhook/admin).
        :return: Created CalendarSync instance, or None if the calendar has sync disabled.
        """
        from calendar_integration.tasks import sync_calendar_task

        if not is_authenticated_calendar_service(self):
            raise

        if not self.calendar_adapter:
            raise NotImplementedError(
                "Calendar adapter is not implemented for the current account provider."
            )

        # Honor the per-calendar opt-out (holidays, birthdays, org-wide calendars, etc.).
        if not calendar.sync_enabled:
            logging.getLogger(__name__).info(
                "Skipping sync for calendar %s: sync_enabled is False.", calendar.id
            )
            return None

        calendar_sync = CalendarSync.objects.create(
            calendar=calendar,
            organization_id=calendar.organization_id,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=should_update_events,
            trigger_source=trigger_source,
        )
        account_type: Literal["social_account", "google_service_account"] = (
            "social_account"
            if isinstance(self.account, SocialAccount)
            else "google_service_account"
        )

        if not self.account or not self.account.id:
            raise NotImplementedError("Account is not set for the current service instance.")

        # Capture ids by value so the closure is independent of mutable self state.
        _account_type = account_type
        _account_id = self.account.id
        _calendar_sync_id = calendar_sync.id
        _organization_id = calendar.organization_id

        transaction.on_commit(
            lambda: sync_calendar_task.delay(  # type: ignore
                _account_type, _account_id, _calendar_sync_id, _organization_id
            )
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
        # Materialize so we can collect the incoming external ids up front; the
        # batch is already held fully in memory while building `changes` below.
        events = list(events_dict["events"])
        next_sync_token = events_dict["next_sync_token"]

        # Match existing rows by the external ids actually being synced, regardless
        # of the sync window. An event whose stored instant falls outside this
        # window (boundary/multi-day events, timezone shifts) must still update its
        # existing row instead of re-inserting it and colliding with the
        # (calendar_fk_id, external_id) unique constraint.
        incoming_external_ids = {e.external_id for e in events if e.external_id}

        # Prepare existing data mappings
        (
            calendar_events_by_external_id,
            blocked_times_by_external_id,
        ) = self._get_existing_calendar_data(
            calendar.id, start_date, end_date, incoming_external_ids
        )

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
        self,
        calendar_id: int,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        incoming_external_ids: set[str] | None = None,
    ):
        """Get existing calendar events and blocked times to reconcile against.

        Loads rows that are either (a) inside the sync window — needed so the
        full-sync deletion pass can spot rows that vanished from the provider — or
        (b) carry one of the ``incoming_external_ids`` being synced now, even if
        their stored instant sits outside the window. Without (b), an out-of-window
        event is treated as new and re-inserted, colliding with the
        ``(calendar_fk_id, external_id)`` unique constraint.
        """
        if not self.organization:
            return ({}, {})

        window = Q(start_time__gte=start_date, end_time__lte=end_date)
        if incoming_external_ids:
            window |= Q(external_id__in=incoming_external_ids)

        calendar_events_by_external_id = {
            e.external_id: e
            for e in CalendarEvent.objects.filter(
                window,
                calendar_fk_id=calendar_id,
                organization_id=self.organization.id,
            )
        }
        blocked_times_by_external_id = {
            e.external_id: e
            for e in BlockedTime.objects.filter(
                window,
                calendar_fk_id=calendar_id,
                organization_id=self.organization.id,
            )
        }
        return calendar_events_by_external_id, blocked_times_by_external_id

    def _process_events_for_sync(
        self,
        events: Iterable[CalendarEventAdapterOutputData],
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
        event: CalendarEventAdapterOutputData,
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
        event: CalendarEventAdapterOutputData,
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
        self, event: CalendarEventAdapterOutputData, calendar: Calendar, changes: EventsSyncChanges
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
                    start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.start_time, event.timezone
                    ),
                    end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.end_time, event.timezone
                    ),
                    timezone=event.timezone,
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
                        start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                            event.start_time, event.timezone
                        ),
                        end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                            event.end_time, event.timezone
                        ),
                        timezone=event.timezone,
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
                start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                    event.start_time, event.timezone
                ),
                end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                    event.end_time, event.timezone
                ),
                timezone=event.timezone,
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
                    start_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.start_time, event.timezone
                    ),
                    end_time_tz_unaware=self.convert_naive_utc_datetime_to_timezone(
                        event.end_time, event.timezone
                    ),
                    timezone=event.timezone,
                    reason=event.title,
                    external_id=event.external_id,
                    meta={"latest_original_payload": event.original_payload or {}},
                    organization_id=calendar.organization_id,
                )
            )

        changes.matched_event_ids.add(event.external_id)

    def _process_event_attendees(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
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
                changes.blocked_times_to_update,
                ["start_time_tz_unaware", "end_time_tz_unaware", "reason", "external_id"],
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
    ) -> None:
        """Delegation: removes AvailableTime windows that overlap with BlockedTime or CalendarEvent.

        Kept on the facade for backward compatibility with existing tests and internal
        callers (sync flow at _execute_calendar_sync). Delegates to AvailabilityService.
        """
        return self._get_availability_service()._remove_available_time_windows_that_overlap_with_blocked_times_and_events(
            calendar_id, blocked_times, events, start_time, end_time
        )

    @staticmethod
    def _subtract_busy_intervals(
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        busy_intervals: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        """Delegation: return the parts of [window_start, window_end] not covered by any busy interval.

        Kept on the facade as a static method for backward compatibility with existing tests
        that call it as ``CalendarService._subtract_busy_intervals(...)``. Delegates to
        AvailabilityService.
        """
        return AvailabilityService._subtract_busy_intervals(
            window_start, window_end, busy_intervals
        )

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
        return self._get_availability_service().get_unavailable_time_windows_in_range(
            calendar, start_datetime, end_datetime
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
        return self._get_availability_service().get_availability_windows_in_range(
            calendar, start_datetime, end_datetime
        )

    @transaction.atomic()
    def get_default_calendar_for_user(self, user: "User") -> Calendar | None:
        """Resolve a user's default calendar in the service's organization.

        The default is the active CalendarOwnership flagged ``is_default``, restricted
        to active calendars. Returns None when the user has no default calendar.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        ownership = (
            CalendarOwnership.objects.filter_by_organization(self.organization.id)
            .filter(user=user, is_default=True, calendar__visibility=CalendarVisibility.ACTIVE)
            .select_related("calendar")
            .order_by("id")
            .first()
        )
        return ownership.calendar if ownership else None

    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[
            tuple[datetime.datetime, datetime.datetime, str, str | None]
        ],
    ) -> Iterable[AvailableTime]:
        """
        Create availability windows for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the availability windows for.
        :param availability_windows: Iterable of tuples containing (start_time, end_time, rrule_string).
        :return: List of created AvailableTime instances.
        """
        return self._get_availability_service().bulk_create_availability_windows(
            calendar, availability_windows
        )

    def batch_modify_available_times(
        self,
        calendar: Calendar,
        operations: Iterable[dict],
    ) -> list[AvailableTime]:
        """Apply a batch of create/update/delete operations to a calendar's available times.

        Row-atomic: each operation acts on a whole AvailableTime row. Runs in a single
        transaction — any failure rolls the whole batch back. Update/delete operations
        are scoped to this calendar (and organization); a missing id raises ValueError.

        :param calendar: The calendar whose available times are being modified.
        :param operations: Iterable of dicts, each with an ``action`` of
            ``create`` / ``update`` / ``delete`` plus the relevant fields
            (``id``, ``start_time``, ``end_time``, ``timezone``, ``rrule_string``).
        :return: The calendar's available times after the batch is applied.
        """
        return self._get_availability_service().batch_modify_available_times(calendar, operations)

    @transaction.atomic()
    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str, str, str | None]],
    ) -> Iterable[BlockedTime]:
        """
        Create new blocked times for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the blocked times for.
        :param blocked_times: Iterable of tuples containing (start_time, end_time, timezone, reason, rrule_string).
        :return: List of created BlockedTime instances.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        blocked_times_to_create = []

        for i, (start_time, end_time, timezone, reason, rrule_string) in enumerate(blocked_times):
            # Create recurrence rule if provided
            recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)

            # Generate unique external_id to avoid constraint violations
            external_id = f"manual-{start_time.isoformat()}-{i}"

            blocked_time = BlockedTime(
                calendar=calendar,
                start_time_tz_unaware=start_time,
                end_time_tz_unaware=end_time,
                timezone=timezone,
                reason=reason,
                external_id=external_id,
                organization_id=calendar.organization_id,
                recurrence_rule=recurrence_rule,
            )
            blocked_times_to_create.append(blocked_time)

        return BlockedTime.objects.bulk_create(blocked_times_to_create)

    # Convenience methods for single object creation
    def create_blocked_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        reason: str = "",
        rrule_string: str | None = None,
    ) -> BlockedTime:
        """Create a single blocked time (optionally recurring)."""
        return self._get_availability_service().create_blocked_time(
            calendar=calendar,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            reason=reason,
            rrule_string=rrule_string,
        )

    def create_available_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        rrule_string: str | None = None,
    ) -> AvailableTime:
        """Create a single available time (optionally recurring)."""
        return self._get_availability_service().create_available_time(
            calendar=calendar,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            rrule_string=rrule_string,
        )

    def get_blocked_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[BlockedTime]:
        """Get all blocked times in a date range with recurring blocked times expanded to instances."""
        return self._get_availability_service().get_blocked_times_expanded(
            calendar, start_date, end_date
        )

    def get_available_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[AvailableTime]:
        """Get all available times in a date range with recurring available times expanded to instances."""
        return self._get_availability_service().get_available_times_expanded(
            calendar, start_date, end_date
        )

    def create_recurring_blocked_time_exception(
        self,
        parent_blocked_time: BlockedTime,
        exception_date: datetime.date,
        modified_reason: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        modified_timezone: str | None = None,
        is_cancelled: bool = False,
    ) -> BlockedTime | None:
        """
        Create an exception for a recurring blocked time (either cancelled or modified).

        :param parent_blocked_time: The recurring blocked time to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_reason: New reason for the modified occurrence (if not cancelled)
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param modified_timezone: New timezone for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified blocked time or None if cancelled
        """
        return self._get_availability_service().create_recurring_blocked_time_exception(
            parent_blocked_time=parent_blocked_time,
            exception_date=exception_date,
            modified_reason=modified_reason,
            modified_start_time=modified_start_time,
            modified_end_time=modified_end_time,
            modified_timezone=modified_timezone,
            is_cancelled=is_cancelled,
        )

    def create_recurring_available_time_exception(
        self,
        parent_available_time: AvailableTime,
        exception_date: datetime.date,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        modified_timezone: str | None = None,
        is_cancelled: bool = False,
    ) -> AvailableTime | None:
        """
        Create an exception for a recurring available time (either cancelled or modified).

        :param parent_available_time: The recurring available time to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param modified_timezone: New timezone for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified available time or None if cancelled
        """
        return self._get_availability_service().create_recurring_available_time_exception(
            parent_available_time=parent_available_time,
            exception_date=exception_date,
            modified_start_time=modified_start_time,
            modified_end_time=modified_end_time,
            modified_timezone=modified_timezone,
            is_cancelled=is_cancelled,
        )

    def create_recurring_event_bulk_modification(
        self,
        parent_event: CalendarEvent,
        modification_start_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        is_bulk_cancelled: bool = False,
        modification_rrule_string: str | None = None,
    ) -> CalendarEvent | None:
        """Create a bulk modification for a recurring event from the specified date onwards."""
        return self._get_event_service().create_recurring_event_bulk_modification(
            parent_event=parent_event,
            modification_start_date=modification_start_date,
            modified_title=modified_title,
            modified_description=modified_description,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            is_bulk_cancelled=is_bulk_cancelled,
            modification_rrule_string=modification_rrule_string,
        )

    def create_recurring_blocked_time_bulk_modification(
        self,
        parent_blocked_time: BlockedTime,
        modification_start_date: datetime.datetime,
        modified_reason: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        is_bulk_cancelled: bool = False,
        modification_rrule_string: str | None = None,
    ) -> BlockedTime | None:
        """Create a bulk modification for a recurring blocked time from the specified date onwards."""
        return self._get_availability_service().create_recurring_blocked_time_bulk_modification(
            parent_blocked_time=parent_blocked_time,
            modification_start_date=modification_start_date,
            modified_reason=modified_reason,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            is_bulk_cancelled=is_bulk_cancelled,
            modification_rrule_string=modification_rrule_string,
        )

    def create_recurring_available_time_bulk_modification(
        self,
        parent_available_time: AvailableTime,
        modification_start_date: datetime.datetime,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        is_bulk_cancelled: bool = False,
        modification_rrule_string: str | None = None,
    ) -> AvailableTime | None:
        """Create a bulk modification for a recurring available time from the specified date onwards."""
        return self._get_availability_service().create_recurring_available_time_bulk_modification(
            parent_available_time=parent_available_time,
            modification_start_date=modification_start_date,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            is_bulk_cancelled=is_bulk_cancelled,
            modification_rrule_string=modification_rrule_string,
        )

    def modify_recurring_event_from_date(
        self,
        parent_event: CalendarEvent,
        modification_start_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        modification_rrule_string: str | None = None,
    ) -> CalendarEvent | None:
        """Modify recurring event series from the given date onwards."""
        return self._get_event_service().modify_recurring_event_from_date(
            parent_event=parent_event,
            modification_start_date=modification_start_date,
            modified_title=modified_title,
            modified_description=modified_description,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            modification_rrule_string=modification_rrule_string,
        )

    def cancel_recurring_event_from_date(
        self,
        parent_event: CalendarEvent,
        modification_start_date: datetime.datetime,
        modification_rrule_string: str | None = None,
    ) -> None:
        """Cancel all occurrences from modification_start_date onwards."""
        return self._get_event_service().cancel_recurring_event_from_date(
            parent_event=parent_event,
            modification_start_date=modification_start_date,
            modification_rrule_string=modification_rrule_string,
        )

    def modify_recurring_blocked_time_from_date(
        self,
        parent_blocked_time: BlockedTime,
        modification_start_date: datetime.datetime,
        modified_reason: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        modification_rrule_string: str | None = None,
    ) -> BlockedTime | None:
        return self._get_availability_service().modify_recurring_blocked_time_from_date(
            parent_blocked_time=parent_blocked_time,
            modification_start_date=modification_start_date,
            modified_reason=modified_reason,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            modification_rrule_string=modification_rrule_string,
        )

    def cancel_recurring_blocked_time_from_date(
        self,
        parent_blocked_time: BlockedTime,
        modification_start_date: datetime.datetime,
        modification_rrule_string: str | None = None,
    ) -> None:
        self._get_availability_service().cancel_recurring_blocked_time_from_date(
            parent_blocked_time=parent_blocked_time,
            modification_start_date=modification_start_date,
            modification_rrule_string=modification_rrule_string,
        )

    def modify_recurring_available_time_from_date(
        self,
        parent_available_time: AvailableTime,
        modification_start_date: datetime.datetime,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        modification_rrule_string: str | None = None,
    ) -> AvailableTime | None:
        return self._get_availability_service().modify_recurring_available_time_from_date(
            parent_available_time=parent_available_time,
            modification_start_date=modification_start_date,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            modification_rrule_string=modification_rrule_string,
        )

    def cancel_recurring_available_time_from_date(
        self,
        parent_available_time: AvailableTime,
        modification_start_date: datetime.datetime,
        modification_rrule_string: str | None = None,
    ) -> None:
        self._get_availability_service().cancel_recurring_available_time_from_date(
            parent_available_time=parent_available_time,
            modification_start_date=modification_start_date,
            modification_rrule_string=modification_rrule_string,
        )

    # Webhook-related methods

    def request_webhook_triggered_sync(
        self,
        external_calendar_id: str,
        webhook_event: CalendarWebhookEvent,
        sync_window_hours: int = 24,
    ) -> CalendarSync | None:
        """
        Request calendar sync triggered by webhook notification.
        Reuses existing request_calendar_sync with webhook-specific optimizations.

        Args:
            external_calendar_id: External calendar ID from webhook
            webhook_event: The webhook event that triggered this sync
            sync_window_hours: Hours around current time to sync

        Returns:
            CalendarSync instance if sync was triggered, None if skipped
        """
        logger = logging.getLogger(__name__)
        now = datetime.datetime.now(tz=datetime.UTC)

        if not is_initialized_or_authenticated_calendar_service(self):
            raise ValueError("Calendar service not properly initialized")

        # Find calendar by external ID
        try:
            calendar = Calendar.objects.get(
                organization_id=self.organization.id,
                external_id=external_calendar_id,
                provider=webhook_event.provider,
            )
        except Calendar.DoesNotExist:
            logger.warning("Calendar not found for external_id: %s", external_calendar_id)
            return None

        # Check for recent syncs to prevent excessive syncing (deduplication)
        recent_sync = CalendarSync.objects.filter(
            calendar=calendar,
            created__gte=now - datetime.timedelta(minutes=5),
            status__in=[CalendarSyncStatus.IN_PROGRESS, CalendarSyncStatus.SUCCESS],
        ).first()

        if recent_sync:
            logger.info(
                "Skipping sync for calendar %s, recent sync exists: %s", calendar.id, recent_sync.id
            )
            webhook_event.calendar_sync = recent_sync
            webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
            webhook_event.save()
            return recent_sync

        # Define sync window around current time
        now = now
        start_datetime = now - datetime.timedelta(hours=sync_window_hours // 2)
        end_datetime = now + datetime.timedelta(hours=sync_window_hours // 2)

        # Use existing request_calendar_sync method
        calendar_sync = self.request_calendar_sync(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=True,  # Webhook implies changes, so update existing events
            trigger_source=CalendarSyncTriggerSource.WEBHOOK,
        )

        # Link webhook event to triggered sync
        webhook_event.calendar_sync = calendar_sync
        webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
        webhook_event.save()

        return calendar_sync

    def create_calendar_webhook_subscription(
        self,
        calendar: Calendar,
        callback_url: str | None = None,
        expiration_hours: int = 24,
    ) -> CalendarWebhookSubscription:
        """
        Create webhook subscription using existing adapter methods.
        Works for both Google and Microsoft calendars.

        Args:
            calendar: Calendar to create subscription for
            callback_url: URL to receive webhook notifications (optional, will generate if not provided)
            expiration_hours: Hours until subscription expires

        Returns:
            CalendarWebhookSubscription instance

        Raises:
            ValueError: If calendar service not authenticated or provider not supported
        """
        if not is_authenticated_calendar_service(self):
            raise ValueError("Calendar service not authenticated")

        if not self.calendar_adapter:
            raise ValueError("Calendar adapter not available")

        # Generate callback URL if not provided
        if not callback_url:
            if calendar.provider == CalendarProvider.GOOGLE:
                callback_url = reverse(
                    "calendar_integration:google_webhook",
                    kwargs={"organization_id": calendar.organization_id},
                )
            elif calendar.provider == CalendarProvider.MICROSOFT:
                callback_url = reverse(
                    "calendar_integration:microsoft_webhook",
                    kwargs={"organization_id": calendar.organization_id},
                )
            else:
                raise ValueError(
                    f"Webhook subscriptions not supported for provider: {calendar.provider}"
                )

            # Convert to absolute URL if needed
            # In production, you might want to configure the domain via settings
            if callback_url.startswith("/"):
                domain = getattr(settings, "WEBHOOK_DOMAIN", "https://your-domain.com")
                callback_url = f"{domain.rstrip('/')}{callback_url}"

        # Use adapter-specific subscription creation
        if calendar.provider == CalendarProvider.GOOGLE:
            subscription_data = self.calendar_adapter.create_webhook_subscription_with_tracking(
                resource_id=calendar.external_id,
                callback_url=callback_url,
                tracking_params={"ttl_seconds": expiration_hours * 3600},
            )
        elif calendar.provider == CalendarProvider.MICROSOFT:
            subscription_data = self.calendar_adapter.create_webhook_subscription_with_tracking(
                resource_id=calendar.external_id,
                callback_url=callback_url,
                tracking_params={"expiration_hours": expiration_hours},
            )
        else:
            raise ValueError(
                f"Webhook subscriptions not supported for provider: {calendar.provider}"
            )

        # Calculate expiration datetime
        expiration_timestamp = subscription_data.get("expiration")
        expires_at = None
        if expiration_timestamp is not None:
            try:
                if calendar.provider == CalendarProvider.GOOGLE:
                    # Google returns expiration as milliseconds since epoch
                    expires_at = datetime.datetime.fromtimestamp(
                        int(expiration_timestamp) / 1000, tz=datetime.UTC
                    )
                elif calendar.provider == CalendarProvider.MICROSOFT:
                    # Microsoft returns expiration as ISO 8601 string
                    expires_at = datetime.datetime.fromisoformat(
                        expiration_timestamp.replace("Z", "+00:00")
                    )
            except (ValueError, TypeError):
                # Log or handle malformed expiration value gracefully
                expires_at = None
                # Log or handle malformed expiration value gracefully
                expires_at = None

        # Create tracking record
        webhook_subscription = CalendarWebhookSubscription.objects.create(
            calendar=calendar,
            organization_id=calendar.organization_id,
            provider=calendar.provider,
            external_subscription_id=subscription_data.get("subscription_id")
            or subscription_data.get("channel_id"),
            external_resource_id=subscription_data.get("resource_id", ""),
            callback_url=callback_url,
            channel_id=subscription_data.get("channel_id", ""),
            resource_uri=subscription_data.get("resource_uri", ""),
            verification_token=subscription_data.get("client_state")
            or subscription_data.get("channel_token")
            or "",
            expires_at=expires_at,
        )

        return webhook_subscription

    def process_webhook_notification(
        self,
        provider: str,
        calendar_external_id: str,
        headers: dict[str, str],
        payload: dict | str | None = None,
        validation_token: str | None = None,
    ) -> CalendarWebhookEvent | None:
        """
        Process incoming webhook notification using adapter validation.
        Returns CalendarWebhookEvent for notification

        Args:
            provider: Calendar provider (google, microsoft)
            headers: HTTP headers from webhook request
            payload: Webhook payload data
            validation_token: Validation token for subscription setup

        Returns:
            CalendarWebhookEvent for notifications

        Raises:
            ValueError: If validation fails or provider not supported
        """
        logger = logging.getLogger(__name__)

        if not is_initialized_or_authenticated_calendar_service(self):
            # For webhook processing, we can proceed with limited functionality
            logger.warning(
                "Webhook received but calendar service not authenticated, webhook event recorded for later processing"
            )

        # Try to get calendar and adapter, but don't fail if not authenticated
        calendar = None
        calendar_adapter = None

        try:
            calendar = self._get_calendar_by_external_id(calendar_external_id)
            calendar_adapter = self._get_write_adapter_for_calendar(calendar)
        except (ServiceNotAuthenticatedError, Calendar.DoesNotExist):
            # Calendar not found or not authenticated - we'll still record the webhook event
            pass

        # Handle provider-specific validation/parsing
        # Use static validation if we don't have an authenticated adapter
        if calendar_adapter:
            parsed_data = calendar_adapter.validate_webhook_notification(
                headers, json.dumps(payload) if payload else ""
            )
        else:
            # Use static validation method
            calendar_adapter_cls = self._get_calendar_adapter_cls_for_provider(
                CalendarProvider(provider)
            )
            parsed_data = calendar_adapter_cls.validate_webhook_notification_static(
                headers, json.dumps(payload) if payload else ""
            )

        if not self.organization:
            raise ValueError("Organization context not set on calendar service")

        # Create webhook event record
        webhook_event = CalendarWebhookEvent.objects.create(
            organization_id=self.organization.id,
            provider=provider,
            event_type=parsed_data.get("event_type", "unknown"),
            external_calendar_id=parsed_data.get("calendar_id", ""),
            external_event_id=parsed_data.get("event_id", ""),
            raw_payload=payload if isinstance(payload, dict) else {"raw": str(payload or "")},
            headers=headers,
        )

        # Trigger calendar sync only if service is authenticated
        # For webhook processing, we record the event even if sync can't be triggered immediately
        try:
            if is_authenticated_calendar_service(self, raise_error=False):
                calendar_sync = self.request_webhook_triggered_sync(
                    external_calendar_id=parsed_data["calendar_id"], webhook_event=webhook_event
                )

                if calendar_sync:
                    logger.info(
                        "Webhook triggered sync %s for calendar %s",
                        calendar_sync.id,
                        parsed_data["calendar_id"],
                    )
                    return webhook_event
                else:
                    webhook_event.processing_status = IncomingWebhookProcessingStatus.IGNORED
                    webhook_event.save()
            else:
                # Service not authenticated - just record the webhook event for later processing
                logger.warning(
                    "Webhook received but calendar service not authenticated, webhook event recorded for later processing"
                )
                webhook_event.processing_status = IncomingWebhookProcessingStatus.PENDING
                webhook_event.save()

        except Exception as e:
            webhook_event.processing_status = IncomingWebhookProcessingStatus.FAILED
            webhook_event.save()
            logger.exception("Failed to process webhook: %s", e)
            # Don't re-raise the exception - webhook event is recorded

        return webhook_event

    def handle_webhook(
        self, provider: CalendarProvider, request: HttpRequest
    ) -> CalendarWebhookEvent | None:
        """
        Handle Google Calendar webhook processing with organization context.

        Args:
            request: HttpRequest object containing webhook data

        Returns:
            CalendarWebhookEvent if processed successfully, None for sync notifications

        Raises:
            ValueError: If webhook validation fails or organization not found
            Exception: If processing fails
        """

        if not request.resolver_match:
            raise ValueError("Invalid request object")

        # Extract organization ID from URL path
        organization_id = request.resolver_match.kwargs.get("organization_id")
        if not organization_id:
            raise ValueError("Organization ID not found in request")

        # Get organization
        try:
            organization = Organization.objects.get(id=organization_id)
        except Organization.DoesNotExist as exc:
            raise ValueError(f"Organization not found: {organization_id}") from exc

        calendar_adapter_cls = self._get_calendar_adapter_cls_for_provider(provider)

        headers = calendar_adapter_cls.parse_webhook_headers(request.headers)
        calendar_external_id = (
            calendar_adapter_cls.extract_calendar_external_id_from_webhook_request(request)
        )

        # Set organization context on the service
        self.organization = organization

        # Process the webhook notification
        try:
            return self.process_webhook_notification(
                provider=provider,
                calendar_external_id=calendar_external_id,
                headers=headers,
            )
        except WebhookIgnoredError:
            return None

    def list_webhook_subscriptions(self) -> QuerySet[CalendarWebhookSubscription]:
        """List all active webhook subscriptions for the organization.

        Returns:
            QuerySet of CalendarWebhookSubscription objects for the organization

        Raises:
            ValueError: If organization is not set
        """
        if not self.organization:
            raise ValueError("Organization must be set")

        return CalendarWebhookSubscription.objects.filter(
            organization=self.organization, is_active=True
        ).select_related("calendar")

    def delete_webhook_subscription(self, subscription_id: int) -> bool:
        """Delete a webhook subscription from provider and database.

        Args:
            subscription_id: ID of the subscription to delete

        Returns:
            True if successfully deleted, False if subscription not found

        Raises:
            ValueError: If organization is not set or subscription not found
        """
        if not self.organization:
            raise ValueError("Organization must be set")

        try:
            subscription = CalendarWebhookSubscription.objects.get(
                id=subscription_id, organization=self.organization
            )
        except CalendarWebhookSubscription.DoesNotExist:
            return False

        # TODO: Add provider-specific subscription deletion when implementing
        # Google Calendar and Microsoft Graph subscription deletion APIs
        # For now, just mark as inactive
        subscription.is_active = False
        subscription.save(update_fields=["is_active", "modified"])

        return True

    def refresh_webhook_subscription(
        self, subscription_id: int
    ) -> CalendarWebhookSubscription | None:
        """Refresh/renew a webhook subscription with the provider.

        Args:
            subscription_id: ID of the subscription to refresh

        Returns:
            Updated CalendarWebhookSubscription if successful, None if not found

        Raises:
            ValueError: If organization is not set
        """
        if not self.organization:
            raise ValueError("Organization must be set")

        try:
            subscription = CalendarWebhookSubscription.objects.get(
                id=subscription_id, organization=self.organization, is_active=True
            )
        except CalendarWebhookSubscription.DoesNotExist:
            return None

        # TODO: Implement provider-specific subscription renewal
        # For now, extend expiration by default duration based on provider
        now = datetime.datetime.now(tz=datetime.UTC)
        if subscription.provider == CalendarProvider.GOOGLE:
            # Google allows max 7 days (604800 seconds)
            new_expiration = now + datetime.timedelta(days=7)
        elif subscription.provider == CalendarProvider.MICROSOFT:
            # Microsoft allows max ~70 hours (4230 minutes)
            new_expiration = now + datetime.timedelta(minutes=4230)
        else:
            # Default to 1 day for other providers
            new_expiration = now + datetime.timedelta(days=1)

        subscription.expires_at = new_expiration
        subscription.save(update_fields=["expires_at", "modified"])

        return subscription

    def get_webhook_health_status(self) -> WebhookHealthStatus:
        """Get webhook system health status for the organization.

        Returns:
            WebhookHealthStatus with webhook health metrics

        Raises:
            ValueError: If organization is not set
        """
        if not self.organization:
            raise ValueError("Organization must be set")

        # Time boundaries
        now = datetime.datetime.now(tz=datetime.UTC)
        twenty_four_hours_ago = now - datetime.timedelta(hours=24)
        expiring_soon_threshold = now + datetime.timedelta(hours=24)

        # Subscription counts
        subscriptions_qs = CalendarWebhookSubscription.objects.filter(
            organization=self.organization
        )
        total_subscriptions = subscriptions_qs.count()
        active_subscriptions = subscriptions_qs.filter(is_active=True).count()
        expired_subscriptions = subscriptions_qs.filter(is_active=True, expires_at__lt=now).count()
        expiring_soon_subscriptions = subscriptions_qs.filter(
            is_active=True,
            expires_at__gte=now,
            expires_at__lte=expiring_soon_threshold,
        ).count()

        # Event counts in last 24 hours
        events_qs = CalendarWebhookEvent.objects.filter(
            organization=self.organization, created__gte=twenty_four_hours_ago
        )
        recent_events_count = events_qs.count()
        failed_events_count = events_qs.filter(
            processing_status=IncomingWebhookProcessingStatus.FAILED
        ).count()

        # Calculate success rate
        if recent_events_count > 0:
            success_rate = ((recent_events_count - failed_events_count) / recent_events_count) * 100
        else:
            success_rate = 100.0

        return WebhookHealthStatus(
            {
                "total_subscriptions": total_subscriptions,
                "active_subscriptions": active_subscriptions,
                "expired_subscriptions": expired_subscriptions,
                "expiring_soon_subscriptions": expiring_soon_subscriptions,
                "recent_events_count": recent_events_count,
                "failed_events_count": failed_events_count,
                "success_rate": success_rate,
            }
        )
