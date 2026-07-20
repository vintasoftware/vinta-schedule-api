"""CalendarService — thin facade over the calendar sub-services.

``CalendarService`` is the injected entry point for all calendar operations. It is
registered in ``di_core/containers.py`` as a ``providers.Factory`` and is the only
calendar service visible to views, GraphQL resolvers, Celery tasks, and sibling
services such as ``CalendarGroupService``.

**Responsibility of this module (facade only):**

* Own the authentication / initialization state (``organization``, ``user_or_token``,
  ``account``, ``calendar_adapter``) and build a ``CalendarServiceContext`` snapshot
  after each ``authenticate()`` / ``initialize_without_provider()`` call.
* Lazily construct sub-service instances (``_get_event_service()``, etc.) that share
  the auth context, the per-instance calendar cache, and the recurrence manager.
* Forward every public method to the appropriate sub-service.
* Retain a small set of methods that genuinely belong on the facade because they
  read/write facade-owned state directly: ``create_application_calendar``,
  ``create_virtual_calendar``, ``bulk_create_manual_blocked_times``,
  ``get_default_calendar_for_user``, ``handle_webhook``, and the cache/permission
  helpers.

**Sub-services (plain classes, not DI providers):**

* ``CalendarEventService`` — single + recurring event CRUD, transfer, expansion.
* ``CalendarBundleService`` — bundle calendar CRUD, bundle-event fan-out.
* ``AvailabilityService`` — available times, blocked times, window arithmetic.
* ``CalendarSyncService`` — calendar/account import, event sync state machine.
* ``CalendarWebhookService`` — webhook subscription lifecycle, triggered sync.
* ``RecurrenceManager`` — stateless template-method engine for all recurrence families.
"""

import datetime
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Annotated

from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone as _tz

from allauth.socialaccount.models import SocialAccount, SocialToken
from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from audit.diff import compute_diff
from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    CalendarType,
    CalendarVisibility,
)
from calendar_integration.exceptions import (
    BookingPolicyViolationError,
    InvalidCalendarTokenError,
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
    ChildrenCalendarRelationship,
    EventAttendance,
    EventExternalAttendance,
    GoogleCalendarServiceAccount,
    RecurrenceRule,
)
from calendar_integration.querysets import CalendarEventQuerySet
from calendar_integration.services import slot_engine
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.booking_policy_service import BookingPolicyService
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
    resolve_acting_single_use_token as _resolve_acting_single_use_token,
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
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.calendar_webhook_service import (
    CalendarWebhookService,
    WebhookHealthStatus,
)
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    AvailableTimeWindow,
    BookableSlotProposal,
    CalendarEventAdapterOutputData,
    CalendarEventData,
    CalendarEventInputData,
    CalendarResourceData,
    EffectivePolicy,
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
from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import Entitlement, LimitedResource
from payments.exceptions import OverLimitError
from public_api.models import SystemUser
from users.models import User


if TYPE_CHECKING:
    from audit.services import AuditService
    from calendar_integration.services.external_event_change_request_service import (
        ExternalEventChangeRequestService,
    )
    from payments.services.entitlement_service import EntitlementService


logger = logging.getLogger(__name__)

# Sentinel for partial updates: distinguishes "omit capacity" from "explicit null"
_UNCHANGED = object()

# The boolean entitlement gating each external provider, checked in `authenticate()` --
# the chokepoint both the Google and Microsoft connection paths flow through. Providers
# with no entry (INTERNAL, APPLE, ICS) are ungated: the spec's `Entitlement` closed set
# only names Google and Microsoft.
_PROVIDER_ENTITLEMENTS: dict[str, str] = {
    CalendarProvider.GOOGLE: Entitlement.EXTERNAL_CALENDAR_GOOGLE,
    CalendarProvider.MICROSOFT: Entitlement.EXTERNAL_CALENDAR_MICROSOFT,
}


def _provider_for_account(account: object) -> str | None:
    """The ``CalendarProvider`` an account authenticates against, or ``None`` when it
    cannot be determined without resolving an adapter.

    ``None`` is returned for a bare ``User``: which of that user's social accounts is
    used is ``get_calendar_adapter_for_account``'s decision, so the caller has to gate
    after resolution instead. Every other case is readable off the object.
    """
    if isinstance(account, GoogleCalendarServiceAccount):
        return CalendarProvider.GOOGLE
    if isinstance(account, User):
        return None
    return getattr(account, "provider", None)


def _resolve_owner_membership_user_id(user: User, organization: Organization) -> int | None:
    """Resolve the membership-scoped owner id for a CalendarOwnership write.

    Mirrors the sync path's guard: the raw-SQL composite PROTECT FK on
    ``CalendarOwnership.membership`` requires a non-NULL ``membership_user_id`` to
    reference a real ``OrganizationMembership(user_id, organization_id)``. Returns
    ``user.id`` only when such a membership exists, else ``None`` (an orphan
    ownership) so a non-member ``user_or_token`` never triggers an FK
    IntegrityError that aborts the request.
    """
    if OrganizationMembership.objects.filter(
        user_id=user.id,
        organization_id=organization.id,
    ).exists():
        return user.id
    return None


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
        audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
        external_event_change_request_service: Annotated[
            "ExternalEventChangeRequestService | None",
            Provide["external_event_change_request_service"],
        ] = None,
        booking_policy_service: Annotated[
            "BookingPolicyService | None", Provide["booking_policy_service"]
        ] = None,
        entitlement_service: Annotated[
            "EntitlementService | None", Provide["entitlement_service"]
        ] = None,
    ) -> None:
        """Initialize a CalendarService instance. Call authenticate() before using calendar operations."""
        self.organization = None
        self.user_or_token = None
        self.account = None
        self.calendar_adapter = None
        self.calendar_side_effects_service = calendar_side_effects_service
        self.calendar_permission_service = calendar_permission_service
        self.audit_service = audit_service
        self.external_event_change_request_service = external_event_change_request_service
        self.booking_policy_service = booking_policy_service
        self.entitlement_service = entitlement_service
        # Set by authenticate(bypass_limits=True); disables every provider entitlement
        # guard on this instance, not just the authenticate-time one.
        self._bypass_entitlement_limits = False
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

    def _audit_calendar_write(
        self,
        action: str,
        calendar: Calendar,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a calendar-level business write.

        Resolves the actor from the facade's ``user_or_token`` auth context. A no-op
        when no ``audit_service`` or ``organization`` is bound (e.g. a service built
        directly in a test without DI), so instrumentation never breaks a write path.
        """
        if self.audit_service is None or self.organization is None:
            return
        self.audit_service.record(
            organization_id=self.organization.id,
            action=action,
            actor=self.audit_service.actor_from_user_or_token(
                self.user_or_token,
                self.organization.id,
                single_use_token=_resolve_acting_single_use_token(
                    self.user_or_token, self.calendar_permission_service
                ),
            ),
            subject=self.audit_service.subject_from_instance(calendar, label=calendar.name),
            diff=diff,
        )

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

            return GoogleCalendarAdapter.from_service_account(
                {
                    "account_id": str(account.id),
                    "email": account.email,
                    "private_key_id": account.private_key_id,
                    "private_key": account.private_key,
                    "admin_email": account.admin_email,
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

    def _assert_provider_entitlement(self, provider: str | None) -> None:
        """Raise ``OverLimitError`` if ``provider`` is gated and the organization is not
        entitled to it.

        Providers with no ``_PROVIDER_ENTITLEMENTS`` entry (INTERNAL, APPLE, ICS) are
        ungated: the spec's closed ``Entitlement`` set only names Google and Microsoft.
        A no-op when the service has no ``entitlement_service`` injected, or when
        ``authenticate(bypass_limits=True)`` put this instance in bypass mode.
        """
        if self._bypass_entitlement_limits or self.entitlement_service is None:
            return
        entitlement_key = _PROVIDER_ENTITLEMENTS.get(provider) if provider else None
        if entitlement_key is None or self.organization is None:
            return
        if not self.entitlement_service.has_entitlement(self.organization, entitlement_key):
            raise OverLimitError.from_missing_entitlement(entitlement_key)

    def authenticate(
        self,
        account: "User | SocialAccount | GoogleCalendarServiceAccount",
        organization: Organization,
        bypass_limits: bool = False,
    ) -> None:
        """
        Authenticate the service with the provided account.
        :param account: A ``User``, a ``SocialAccount``, or a
            ``GoogleCalendarServiceAccount``. When a ``SocialAccount`` is given,
            the owning ``User`` is used for record attribution (e.g.
            ``CalendarOwnership``).
        :param organization: Calendar organization instance.
        :param bypass_limits: When True, skips the ``external_calendar_google`` /
            ``external_calendar_microsoft`` entitlement guard below, and every guard on
            this service instance for the rest of its life (writes resolved through
            ``_get_write_adapter_for_calendar`` included). Only management commands and
            one-off repair scripts should pass this -- never a request-handling path.
        :raises OverLimitError: When the resolved account's provider is Google or
            Microsoft and the organization lacks the matching entitlement. Every caller
            that authenticates a Google or Microsoft account, including calendar sync,
            routes through here -- but this is **not** the only enforcement point:
            ``_get_write_adapter_for_calendar`` gates on the *calendar's* provider,
            which can differ from the authenticated account's. See its docstring.
        """
        if isinstance(account, User):
            self.user_or_token = account
        elif isinstance(account, SocialAccount):
            self.user_or_token = account.user
        else:
            self.user_or_token = None
        self.organization = organization
        self._bypass_entitlement_limits = bypass_limits

        # Gate *before* `get_calendar_adapter_for_account` where the provider can be read
        # off `account` without resolving an adapter. That call refreshes an expired
        # access token while constructing the adapter -- an outbound provider call and a
        # token mutation. Doing that work for a request we are about to reject with a 402
        # is both wasteful and a side effect on data the caller has no entitlement to
        # touch. The `User` branch is the exception: which social account it resolves to
        # is `get_calendar_adapter_for_account`'s decision, so it can only be gated after.
        early_provider = _provider_for_account(account)
        if early_provider is not None:
            self._assert_provider_entitlement(early_provider)

        self.calendar_adapter, self.account = self.get_calendar_adapter_for_account(account)

        if early_provider is None:
            self._assert_provider_entitlement(_provider_for_account(self.account))

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
            audit_service=self.audit_service,
            entitlement_service=self.entitlement_service,
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
            audit_service=self.audit_service,
            entitlement_service=self.entitlement_service,
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
            audit_service=self.audit_service,
            entitlement_service=self.entitlement_service,
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

    def _get_sync_service(self) -> CalendarSyncService:
        """Return the sync sub-service bound to the facade's current auth context.

        The sync service shares the facade-owned calendar cache and routes
        available-time pruning (Phase 4) and the shared owner-permission helper back
        through the facade (``host=self``). The context is rebuilt from the live facade
        attributes each call (a cheap dataclass construction — no network / adapter
        rebuild), so it never goes stale across re-authentication or direct attribute
        mutation.
        """
        return CalendarSyncService(
            context=self._build_context_snapshot(),
            calendar_cache=self._calendar_cache,
            host=self,
            external_event_change_request_service=self.external_event_change_request_service,
        )

    def _get_webhook_service(self) -> CalendarWebhookService:
        """Return the webhook sub-service bound to the facade's current auth context.

        The webhook service shares the facade-owned calendar cache and routes
        sync triggering (Phase 5 seam), adapter-class lookup, write-adapter resolution,
        and external-id calendar lookup back through the facade (``host=self``). The
        context is rebuilt from the live facade attributes each call (a cheap dataclass
        construction — no network / adapter rebuild), so it never goes stale across
        re-authentication or direct attribute mutation.
        """
        return CalendarWebhookService(
            context=self._build_context_snapshot(),
            calendar_cache=self._calendar_cache,
            host=self,
        )

    def request_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        return self._get_sync_service().request_organization_calendar_resources_import(
            start_time, end_time
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
        return self._get_sync_service().import_organization_calendar_resources(
            import_workflow_state
        )

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        import_workflow_state: CalendarOrganizationResourcesImport | None = None,
        bypass_limits: bool = False,
    ) -> Iterable[CalendarResourceData]:
        """Delegation: import organization calendar resources within a time range.

        Kept on the facade for backward compatibility with existing tests that call
        it as ``service._execute_organization_calendar_resources_import(...)``.
        Delegates to ``CalendarSyncService``.

        :param import_workflow_state: When given, a partial-import warning (headroom
            exhausted on ``resource_calendars``) is recorded on its ``error_message``.
        :param bypass_limits: When True, skips the ``resource_calendars`` headroom guard.
            Only management commands and one-off repair scripts should pass this.
        """
        return self._get_sync_service()._execute_organization_calendar_resources_import(
            start_time,
            end_time,
            import_workflow_state=import_workflow_state,
            bypass_limits=bypass_limits,
        )

    def create_application_calendar(
        self, name: str, organization: Organization
    ) -> ApplicationCalendarData:
        """
        Create a new application calendar using the calendar adapter.

        Phase 6b: **not** guarded by a limit check. ``LimitedResource`` (the closed
        set defined in Phase 3) caps only ``resource_calendars`` (type RESOURCE) and
        ``bundle_calendars`` (type BUNDLE) among calendar types; this method creates
        an application-owned calendar with no ``calendar_type`` counted by either. See
        ``create_resource_calendar`` for the guarded sibling.
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

        # Create calendar ownership for the user who created it. Guard the
        # membership-scoped FK: only set membership_user_id when the creator is a
        # member of this org, else create an orphan ownership (membership_user_id
        # NULL) to avoid an FK IntegrityError aborting the request.
        if isinstance(self.user_or_token, User):
            CalendarOwnership.objects.create(
                organization=organization,
                calendar=calendar,
                membership_user_id=_resolve_owner_membership_user_id(
                    self.user_or_token, organization
                ),
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

        self._audit_calendar_write(AuditAction.CREATE, calendar)

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
        return self._get_sync_service().request_calendars_import(sync_after_import)

    def import_account_calendars(self, sync_after_import: bool = True):
        """
        Import calendars associated with the authenticated account and create them as Calendar
        records.

        :param sync_after_import: When True (default), enqueue an event sync for each
            imported calendar that has sync enabled. The per-calendar ``sync_enabled``
            flag still gates whether a sync actually runs.
        """
        return self._get_sync_service().import_account_calendars(sync_after_import)

    @transaction.atomic()
    def create_virtual_calendar(
        self,
        name: str,
        description: str | None = None,
    ) -> Calendar:
        """
        Create a new calendar in the application without linking to an external provider.

        Phase 6b: **not** guarded by a limit check -- ``calendar_type=VIRTUAL`` is not a
        member of ``LimitedResource`` (see ``create_resource_calendar`` for the guarded
        sibling and the closed-set rationale).
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
        )

        # Create calendar ownership for the user who created it. Guard the
        # membership-scoped FK: only set membership_user_id when the creator is a
        # member of this org, else create an orphan ownership (membership_user_id
        # NULL) to avoid an FK IntegrityError aborting the request.
        if isinstance(self.user_or_token, User):
            CalendarOwnership.objects.create(
                organization=self.organization,
                calendar=calendar,
                membership_user_id=_resolve_owner_membership_user_id(
                    self.user_or_token, self.organization
                ),
                is_default=False,
            )

        # Grant permissions to calendar owners
        self._grant_calendar_owner_permissions(calendar)

        self._audit_calendar_write(AuditAction.CREATE, calendar)

        return calendar

    @transaction.atomic()
    def create_resource_calendar(
        self,
        name: str,
        description: str | None = None,
        capacity: int | None = None,
        manage_available_windows: bool = False,
        accepts_public_scheduling: bool = False,
        bypass_limits: bool = False,
    ) -> Calendar:
        """
        Create a new internal (manual) resource calendar without linking to an external provider.

        Resource calendars represent shared bookable resources (rooms, equipment, etc.). Unlike
        synced resource calendars imported from a provider, these are created and owned by the
        organization directly (``provider=INTERNAL``).

        :param name: Name of the resource calendar.
        :param description: Description of the resource calendar.
        :param capacity: Maximum number of attendees the resource can accommodate.
        :param manage_available_windows: Whether the calendar manages its own available windows.
        :param accepts_public_scheduling: If True, the calendar can be booked via codeless public
            scheduling links. Defaults to False (private).
        :param bypass_limits: When True, skips the ``resource_calendars`` limit guard below.
            Only management commands and one-off repair scripts should pass this -- never a
            request-handling path.
        :raises OverLimitError: When the organization is at its effective ``resource_calendars``
            ceiling. Nothing is created. Checked and locked (``SELECT ... FOR UPDATE`` on the
            billing root's subscription) inside this method's own transaction, so two concurrent
            creates for the last unit of capacity serialize on that row.
        :return: Created Calendar instance.
        """
        # Read before the type-guard narrows `self` below: the narrowed Protocol
        # type doesn't declare `entitlement_service` (it isn't part of the
        # authentication-state contract those protocols exist to describe).
        entitlement_service = self.entitlement_service

        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        if not bypass_limits and entitlement_service is not None:
            result = entitlement_service.check_limit(
                self.organization, LimitedResource.RESOURCE_CALENDARS, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        calendar = Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.RESOURCE,
            capacity=capacity,
            manage_available_windows=manage_available_windows,
            accepts_public_scheduling=accepts_public_scheduling,
        )

        # Create calendar ownership for the user who created it. Guard the
        # membership-scoped FK: only set membership_user_id when the creator is a
        # member of this org, else create an orphan ownership (membership_user_id
        # NULL) to avoid an FK IntegrityError aborting the request.
        if isinstance(self.user_or_token, User):
            CalendarOwnership.objects.create(
                organization=self.organization,
                calendar=calendar,
                membership_user_id=_resolve_owner_membership_user_id(
                    self.user_or_token, self.organization
                ),
                is_default=False,
            )

        # Grant permissions to calendar owners
        self._grant_calendar_owner_permissions(calendar)

        self._audit_calendar_write(AuditAction.CREATE, calendar)

        return calendar

    @transaction.atomic()
    def create_calendar(
        self,
        name: str,
        description: str | None = None,
        accepts_public_scheduling: bool = False,
    ) -> Calendar:
        """Create a new plain (personal) internal calendar without linking to an external provider.

        Plain calendars are owned directly by the organization (``provider=INTERNAL``,
        ``calendar_type=PERSONAL``). Unlike resource or bundle calendars, they carry no
        capacity or availability-window management semantics.

        Phase 6b: **not** guarded by a limit check -- ``calendar_type=PERSONAL`` is not a
        member of ``LimitedResource`` (see ``create_resource_calendar`` for the guarded
        sibling and the closed-set rationale).

        :param name: Name of the calendar.
        :param description: Description of the calendar.
        :param accepts_public_scheduling: If True, the calendar can be booked via codeless public
            scheduling links. Defaults to False (private).
        :return: Created Calendar instance.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar = Calendar.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            accepts_public_scheduling=accepts_public_scheduling,
        )

        # Create calendar ownership for the user who created it. Guard the
        # membership-scoped FK: only set membership_user_id when the creator is a
        # member of this org, else create an orphan ownership (membership_user_id
        # NULL) to avoid an FK IntegrityError aborting the request.
        if isinstance(self.user_or_token, User):
            CalendarOwnership.objects.create(
                organization=self.organization,
                calendar=calendar,
                membership_user_id=_resolve_owner_membership_user_id(
                    self.user_or_token, self.organization
                ),
                is_default=False,
            )

        # Grant permissions to calendar owners
        self._grant_calendar_owner_permissions(calendar)

        self._audit_calendar_write(AuditAction.CREATE, calendar)

        return calendar

    @transaction.atomic()
    def update_calendar(
        self,
        calendar_id: int,
        name: str | None = None,
        description: str | None = None,
        accepts_public_scheduling: bool | None = None,
    ) -> Calendar:
        """Partially update a plain (personal) calendar.

        Only the provided (non-None) fields are written; omitted fields remain unchanged.
        The target calendar must be of type PERSONAL and must belong to the service's
        organization.

        :param calendar_id: Primary key of the Calendar to update.
        :param name: New name, or None to leave unchanged.
        :param description: New description, or None to leave unchanged.
        :param accepts_public_scheduling: New scheduling flag, or None to leave unchanged.
        :return: The updated Calendar instance.
        :raises Calendar.DoesNotExist: If no calendar with this id exists within the org.
        :raises ValueError: If the calendar is not of type PERSONAL.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar = Calendar.objects.filter_by_organization(self.organization.id).get(id=calendar_id)

        if calendar.calendar_type != CalendarType.PERSONAL:
            raise ValueError(
                f"Calendar {calendar_id} is not a personal calendar "
                f"(type={calendar.calendar_type})."
            )

        before = {
            "name": calendar.name,
            "description": calendar.description,
            "accepts_public_scheduling": calendar.accepts_public_scheduling,
        }
        update_fields: list[str] = []
        if name is not None:
            calendar.name = name
            update_fields.append("name")
        if description is not None:
            calendar.description = description
            update_fields.append("description")
        if accepts_public_scheduling is not None:
            calendar.accepts_public_scheduling = accepts_public_scheduling
            update_fields.append("accepts_public_scheduling")
        if update_fields:
            calendar.save(update_fields=update_fields)
            after = {
                "name": calendar.name,
                "description": calendar.description,
                "accepts_public_scheduling": calendar.accepts_public_scheduling,
            }
            self._audit_calendar_write(
                AuditAction.UPDATE, calendar, diff=compute_diff(before, after)
            )

        return calendar

    def disable_resource_calendar(self, calendar_id: int) -> Calendar:
        """Disable a resource calendar by setting its visibility to INACTIVE.

        Fetches the calendar with an org-scoped query, validates it is a resource calendar,
        then sets ``visibility = INACTIVE`` and saves.

        :param calendar_id: Primary key of the Calendar to disable.
        :return: The updated Calendar instance.
        :raises Calendar.DoesNotExist: If no calendar with this id exists within the org.
        :raises ValueError: If the calendar is not of type RESOURCE.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar = Calendar.objects.filter_by_organization(self.organization.id).get(id=calendar_id)

        if calendar.calendar_type != CalendarType.RESOURCE:
            raise ValueError(
                f"Calendar {calendar_id} is not a resource calendar "
                f"(type={calendar.calendar_type})."
            )

        old_visibility = calendar.visibility
        calendar.visibility = CalendarVisibility.INACTIVE
        calendar.save(update_fields=["visibility"])

        self._audit_calendar_write(
            AuditAction.UPDATE,
            calendar,
            diff={"visibility": {"old": old_visibility, "new": calendar.visibility}},
        )

        return calendar

    @transaction.atomic()
    def update_resource_calendar(
        self,
        calendar_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        capacity: int | None = _UNCHANGED,  # type: ignore[assignment]
        manage_available_windows: bool | None = None,
        accepts_public_scheduling: bool | None = None,
        visibility: str | None = None,
    ) -> Calendar:
        """Partially update a resource calendar.

        Only the provided (non-None, non-_UNCHANGED) fields are written; omitted fields
        remain unchanged. The target calendar must be of type RESOURCE and must have
        provider INTERNAL (not synced from an external provider).

        Special handling for capacity: if ``capacity`` is explicitly ``None``, it clears
        the capacity to unlimited (null). If ``capacity`` is the sentinel ``_UNCHANGED``
        (the default), the existing value is left untouched. Any integer value sets it.

        :param calendar_id: Primary key of the Calendar to update.
        :param name: New name, or None to leave unchanged.
        :param description: New description, or None to leave unchanged.
        :param capacity: Maximum attendees, None to clear (unlimited), _UNCHANGED to
            leave unchanged, or an int to set.
        :param manage_available_windows: New availability-window flag, or None to
            leave unchanged.
        :param accepts_public_scheduling: New scheduling flag, or None to leave unchanged.
        :param visibility: New visibility string (e.g. ACTIVE/INACTIVE), or None to
            leave unchanged.
        :return: The updated Calendar instance.
        :raises Calendar.DoesNotExist: If no calendar with this id exists within the org.
        :raises ValueError: If the calendar is not of type RESOURCE or is synced from
            an external provider (not INTERNAL).
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar = Calendar.objects.filter_by_organization(self.organization.id).get(id=calendar_id)

        if calendar.provider != CalendarProvider.INTERNAL:
            raise ValueError(
                f"Calendar {calendar_id} is synced from an external provider "
                f"(provider={calendar.provider}) and cannot be edited."
            )

        if calendar.calendar_type != CalendarType.RESOURCE:
            raise ValueError(
                f"Calendar {calendar_id} is not a resource calendar "
                f"(type={calendar.calendar_type})."
            )

        before = {
            "name": calendar.name,
            "description": calendar.description,
            "capacity": calendar.capacity,
            "manage_available_windows": calendar.manage_available_windows,
            "accepts_public_scheduling": calendar.accepts_public_scheduling,
            "visibility": calendar.visibility,
        }
        update_fields: list[str] = []

        if name is not None:
            calendar.name = name
            update_fields.append("name")
        if description is not None:
            calendar.description = description
            update_fields.append("description")
        if capacity is not _UNCHANGED:
            calendar.capacity = capacity
            update_fields.append("capacity")
        if manage_available_windows is not None:
            calendar.manage_available_windows = manage_available_windows
            update_fields.append("manage_available_windows")
        if accepts_public_scheduling is not None:
            calendar.accepts_public_scheduling = accepts_public_scheduling
            update_fields.append("accepts_public_scheduling")
        if visibility is not None:
            if visibility not in CalendarVisibility.values:
                raise ValueError(
                    f"Invalid visibility {visibility!r}; must be one of {CalendarVisibility.values}."
                )
            calendar.visibility = visibility
            update_fields.append("visibility")

        if update_fields:
            calendar.save(update_fields=update_fields)
            after = {
                "name": calendar.name,
                "description": calendar.description,
                "capacity": calendar.capacity,
                "manage_available_windows": calendar.manage_available_windows,
                "accepts_public_scheduling": calendar.accepts_public_scheduling,
                "visibility": calendar.visibility,
            }
            self._audit_calendar_write(
                AuditAction.UPDATE, calendar, diff=compute_diff(before, after)
            )

        return calendar

    def disable_bundle_calendar(self, bundle_id: int) -> Calendar:
        """Disable a bundle calendar by setting its visibility to INACTIVE.

        Fetches the calendar with an org-scoped query, validates it is a bundle calendar,
        then sets ``visibility = INACTIVE`` and saves.

        :param bundle_id: Primary key of the Calendar to disable.
        :return: The updated Calendar instance.
        :raises Calendar.DoesNotExist: If no calendar with this id exists within the org.
        :raises ValueError: If the calendar is not of type BUNDLE.
        """
        if not is_initialized_or_authenticated_calendar_service(self):
            raise

        calendar = Calendar.objects.filter_by_organization(self.organization.id).get(id=bundle_id)

        if calendar.calendar_type != CalendarType.BUNDLE:
            raise ValueError(
                f"Calendar {bundle_id} is not a bundle calendar (type={calendar.calendar_type})."
            )

        old_visibility = calendar.visibility
        calendar.visibility = CalendarVisibility.INACTIVE
        calendar.save(update_fields=["visibility"])

        self._audit_calendar_write(
            AuditAction.UPDATE,
            calendar,
            diff={"visibility": {"old": old_visibility, "new": calendar.visibility}},
        )

        return calendar

    def create_bundle_calendar(
        self,
        name: str,
        description: str | None = None,
        child_calendars: Iterable[Calendar] | None = None,
        primary_calendar: Calendar | None = None,
        accepts_public_scheduling: bool = False,
        bypass_limits: bool = False,
    ) -> Calendar:
        """
        Create a new bundle calendar in the application without linking to an external provider.
        :param name: Name of the calendar.
        :param description: Description of the calendar.
        :param child_calendars: Iterable of child Calendar instances to include in the bundle.
        :param primary_calendar: The child calendar to be designated as primary. Must be in child_calendars.
        :param accepts_public_scheduling: If True, the bundle can be booked via codeless public
            scheduling links. Defaults to False (private).
        :param bypass_limits: When True, skips the ``bundle_calendars`` limit guard. Only
            management commands and one-off repair scripts should pass this.
        :raises OverLimitError: When the organization is at its effective ``bundle_calendars``
            ceiling. Nothing is created.
        :return: Created Calendar instance.
        """
        return self._get_bundle_service().create_bundle_calendar(
            name=name,
            description=description,
            child_calendars=child_calendars,
            primary_calendar=primary_calendar,
            accepts_public_scheduling=accepts_public_scheduling,
            bypass_limits=bypass_limits,
        )

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
        """Resolve the adapter to write ``calendar`` through, gated on the calendar's own
        provider entitlement.

        :raises OverLimitError: When ``calendar.provider`` is Google or Microsoft and the
            organization lacks the matching entitlement.

        **This is a second enforcement point, not a redundant one.** ``authenticate()``
        gates the *authenticated account's* provider; this method resolves an adapter
        from the *calendar's* provider, which need not be the same one. Concretely: an
        organization entitled to ``external_calendar_google`` but not
        ``external_calendar_microsoft`` has a Microsoft calendar ``C`` owned by a user who
        holds a Microsoft ``SocialAccount``. An actor authenticates with their Google
        account — ``authenticate()`` passes, correctly. Any write to ``C`` then reaches
        the branch below, which resolves that owner and builds a **Microsoft** adapter via
        the *static* ``get_calendar_adapter_for_account``, bypassing the instance the
        authenticate-time gate ran against. Without this check that is unmetered,
        ungated Microsoft traffic.

        Raises rather than returning ``None``: several callers treat ``None`` as "no
        external sync configured" and complete the local write silently
        (``if write_adapter := ...``). Silently diverging local and provider state on a
        billing decision is worse than a loud, recoverable 402.
        """
        self._assert_provider_entitlement(calendar.provider)

        # if the authenticated account doesn't own the calendar:
        if not self.account or not (
            (
                isinstance(self.account, SocialAccount)
                and calendar.ownerships.filter(membership_user_id=self.account.user_id).exists()
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
                .filter(
                    membership_user_id__in=User.objects.filter(
                        socialaccount__provider=calendar.provider,
                    ).values("id")
                )
                .first()
            )

            # Resolve the owning user from the denormalized scalar instead of
            # dereferencing ``ownership.membership`` (the ForeignObject), which
            # resolves to ``None`` when ``membership_user_id`` is stale (the
            # membership was deleted) and would raise ``AttributeError``.
            if ownership and ownership.membership_user_id:
                owner = User.objects.filter(id=ownership.membership_user_id).first()
                if owner:
                    return CalendarService.get_calendar_adapter_for_account(owner)[0]

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

    def _check_booking_policy(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        now: datetime.datetime,
    ) -> None:
        """Enforce the resolved EffectivePolicy for a booking request.

        This is the single enforcement gate for ALL ``create_event`` paths: single
        calendar, bundle calendar (both fan-out to the same facade entry point), and
        the code-gated single-calendar path (which also calls ``create_event``).

        Steps:
        1. Skip entirely when ``booking_policy_service`` is not injected or no
           policy resolves for the target (``EffectivePolicy.unconstrained()``) —
           preserving byte-for-byte pre-feature behavior (the data-presence gate).
        2. Resolve the EffectivePolicy: ``resolve_for_bundle`` for bundle calendars,
           ``resolve_for_calendar`` for all others.
        3. Build a single ``BookableSlotProposal(start_time, end_time)`` and fetch
           the buffer blocking spans the same way Phase 5 does (all target calendars,
           window widened by the buffer magnitudes).
        4. Call ``slot_engine.apply_policy_filter`` — if the result is empty, the
           booking violates the policy and ``BookingPolicyViolationError`` is raised.

        This re-reads current calendar state inside the existing ``create_event``
        transaction, so a slot valid at discovery but invalidated by a concurrent
        booking is correctly rejected (the concurrency guard).
        """
        if self.booking_policy_service is None or self.organization is None:
            return

        self.booking_policy_service.initialize(self.organization)

        if calendar.calendar_type == CalendarType.BUNDLE:
            policy = self.booking_policy_service.resolve_for_bundle(calendar)
            target_calendar_ids: set[int] = set(
                ChildrenCalendarRelationship.objects.filter_by_organization(self.organization.id)
                .filter(bundle_calendar_fk_id=calendar.pk)
                .values_list("child_calendar_fk_id", flat=True)
            )
            if not target_calendar_ids:
                target_calendar_ids = {calendar.id}
        else:
            policy = self.booking_policy_service.resolve_for_calendar(calendar)
            target_calendar_ids = {calendar.id}

        if policy == EffectivePolicy.unconstrained():
            # No policy → skip all enforcement (data-presence gate).
            return

        # Fetch buffer blocking spans across all target calendars (managed + unmanaged),
        # widening the window by the buffer magnitudes — mirrors Phase 5's
        # ``BookableSlotsService._buffer_blocking_spans``.
        no_buffer = policy.buffer_before <= datetime.timedelta(
            0
        ) and policy.buffer_after <= datetime.timedelta(0)
        if no_buffer:
            buffer_blocking_spans: slot_engine.SpansByCalendarId = {}
        else:
            buffer_blocking_spans = slot_engine.fetch_blocking_spans(
                self.organization.id,
                target_calendar_ids,
                start_time - policy.buffer_after,
                end_time + policy.buffer_before,
                with_bulk_modifications=False,
            )

        proposal = BookableSlotProposal(start_time=start_time, end_time=end_time)
        allowed = slot_engine.apply_policy_filter([proposal], policy, now, buffer_blocking_spans)
        if not allowed:
            raise BookingPolicyViolationError()

    def create_event(
        self,
        calendar_id: int,
        event_data: CalendarEventInputData,
        *,
        _enforce_policy: bool = True,
        _check_postpaid_allowance: bool = True,
    ) -> CalendarEvent:
        """
        Create a new event in the calendar.
        :param calendar_id: Internal ID of the calendar
        :param event_data: Dictionary containing event details.
        :param _enforce_policy: Internal flag; callers must NOT pass this. Set to
            ``False`` by the bundle fan-out so policy is enforced exactly once at the
            top-level entry point (using ``resolve_for_bundle``), not again for each
            child create (which would use ``resolve_for_calendar`` on the child and
            could falsely reject bookings the bundle policy permits).
        :param _check_postpaid_allowance: Internal flag; callers must NOT pass this.
            Forwarded verbatim to ``CalendarEventService.create_event`` -- see its
            docstring. Set to ``False`` by the bundle fan-out for the same reason as
            ``_enforce_policy``: it already checked headroom once for the whole
            fan-out count.
        :return: Response from the calendar client.
        """
        if _enforce_policy and self.organization is not None:
            # Enforcement runs inside the existing transaction (ATOMIC_REQUESTS) so any
            # violation rolls back the entire write — no event or blocked time is created.
            try:
                calendar = Calendar.objects.filter_by_organization(self.organization.id).get(
                    id=calendar_id
                )
            except Calendar.DoesNotExist:
                # Let the event service raise the not-found; do not hide the error.
                return self._get_event_service().create_event(
                    calendar_id, event_data, _check_postpaid_allowance=_check_postpaid_allowance
                )
            self._check_booking_policy(
                calendar,
                start_time=event_data.start_time,
                end_time=event_data.end_time,
                now=_tz.now(),
            )
            # Populate the calendar cache so the event service does not re-query the
            # same row. The cache is keyed on (organization_id, calendar_id) — the same
            # shape used by _get_calendar_by_id_util.
            self._calendar_cache[(self.organization.id, calendar_id)] = calendar
        return self._get_event_service().create_event(
            calendar_id, event_data, _check_postpaid_allowance=_check_postpaid_allowance
        )

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

    def reschedule_event_occurrence(
        self,
        calendar_id: int,
        master_event_id: int,
        recurrence_id: datetime.datetime,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
    ) -> CalendarEvent:
        """Reschedule a single occurrence of a recurring series to a new time.

        See ``CalendarEventService.reschedule_event_occurrence`` for full semantics.
        """
        return self._get_event_service().reschedule_event_occurrence(
            calendar_id=calendar_id,
            master_event_id=master_event_id,
            recurrence_id=recurrence_id,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
        )

    def cancel_event_occurrence(
        self,
        calendar_id: int,
        master_event_id: int,
        recurrence_id: datetime.datetime,
    ) -> None:
        """Cancel a single occurrence of a recurring series.

        See ``CalendarEventService.cancel_event_occurrence`` for full semantics.
        """
        return self._get_event_service().cancel_event_occurrence(
            calendar_id=calendar_id,
            master_event_id=master_event_id,
            recurrence_id=recurrence_id,
        )

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
        exception_date: datetime.date,
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

    def get_calendar_events_expanded_for_calendars(
        self,
        calendar_ids: Iterable[int],
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        optimize_queryset: Callable[[CalendarEventQuerySet], CalendarEventQuerySet] | None = None,
    ) -> list[CalendarEvent]:
        """
        Get all calendar events in a date range across multiple calendars, with recurring
        events expanded and deduped.

        See ``CalendarEventService.get_calendar_events_expanded_for_calendars`` for full
        semantics.
        """
        return self._get_event_service().get_calendar_events_expanded_for_calendars(
            calendar_ids, start_date, end_date, optimize_queryset
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
        return self._get_sync_service().request_calendar_sync(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=should_update_events,
            trigger_source=trigger_source,
        )

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
        return self._get_sync_service().sync_events(calendar_sync)

    def _execute_calendar_sync(
        self,
        calendar_sync: CalendarSync,
        sync_token: str | None = None,
    ) -> None:
        """Delegation: execute a calendar sync run.

        Kept on the facade for backward compatibility with existing tests that call
        it as ``service._execute_calendar_sync(...)``. Delegates to ``CalendarSyncService``.
        """
        return self._get_sync_service()._execute_calendar_sync(calendar_sync, sync_token)

    def _get_existing_calendar_data(
        self,
        calendar_id: int,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        incoming_external_ids: set[str] | None = None,
    ):
        """Delegation: get existing calendar events and blocked times to reconcile against.

        Kept on the facade for backward compatibility with existing tests that call
        it as ``service._get_existing_calendar_data(...)``. Delegates to
        ``CalendarSyncService``.
        """
        return self._get_sync_service()._get_existing_calendar_data(
            calendar_id, start_date, end_date, incoming_external_ids
        )

    def _process_existing_event(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
        update_events: bool,
    ):
        """Delegation: process an existing calendar event during sync.

        Kept on the facade for backward compatibility with existing tests. Delegates
        to ``CalendarSyncService``.
        """
        return self._get_sync_service()._process_existing_event(
            event, existing_event, changes, update_events
        )

    def _process_existing_blocked_time(
        self,
        event: CalendarEventAdapterOutputData,
        existing_blocked_time: BlockedTime,
        changes: EventsSyncChanges,
    ):
        """Delegation: process an existing blocked time during sync.

        Kept on the facade for backward compatibility with existing tests. Delegates
        to ``CalendarSyncService``.
        """
        return self._get_sync_service()._process_existing_blocked_time(
            event, existing_blocked_time, changes
        )

    def _process_new_event(
        self, event: CalendarEventAdapterOutputData, calendar: Calendar, changes: EventsSyncChanges
    ):
        """Delegation: process a new event during sync.

        Kept on the facade for backward compatibility with existing tests. Delegates
        to ``CalendarSyncService``.
        """
        return self._get_sync_service()._process_new_event(event, calendar, changes)

    def _process_event_attendees(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
    ):
        """Delegation: process attendees for an existing event during sync.

        Kept on the facade for backward compatibility with existing tests. Delegates
        to ``CalendarSyncService``.
        """
        return self._get_sync_service()._process_event_attendees(event, existing_event, changes)

    def _handle_deletions_for_full_sync(
        self,
        calendar_id: int,
        calendar_events_by_external_id: dict,
        matched_event_ids: set[str],
        start_date: datetime.datetime,
    ):
        """Delegation: handle deletions when doing a full sync (no sync_token).

        Kept on the facade for backward compatibility with existing tests. Delegates
        to ``CalendarSyncService``.
        """
        return self._get_sync_service()._handle_deletions_for_full_sync(
            calendar_id, calendar_events_by_external_id, matched_event_ids, start_date
        )

    def _apply_sync_changes(self, calendar_id: int, changes: EventsSyncChanges):
        """Delegation: apply all the collected changes to the database.

        Kept on the facade for backward compatibility with existing tests that call
        it as ``service._apply_sync_changes(...)``. Delegates to ``CalendarSyncService``.
        """
        return self._get_sync_service()._apply_sync_changes(calendar_id, changes)

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
            .filter(
                membership_user_id=user.id,
                is_default=True,
                calendar__visibility=CalendarVisibility.ACTIVE,
            )
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
        bypass_limits: bool = False,
    ) -> Iterable[AvailableTime]:
        """
        Create availability windows for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the availability windows for.
        :param availability_windows: Iterable of tuples containing (start_time, end_time, rrule_string).
        :param bypass_limits: When True, skips the ``availability_windows`` limit guard. Only
            management commands and one-off repair scripts should pass this.
        :raises OverLimitError: When creating ``len(availability_windows)`` more windows would
            take the organization past its effective ``availability_windows`` ceiling. Nothing
            is created.
        :return: List of created AvailableTime instances.
        """
        return self._get_availability_service().bulk_create_availability_windows(
            calendar, availability_windows, bypass_limits=bypass_limits
        )

    def batch_modify_available_times(
        self,
        calendar: Calendar,
        operations: Iterable[dict],
        bypass_limits: bool = False,
    ) -> list[AvailableTime]:
        """Apply a batch of create/update/delete operations to a calendar's available times.

        Row-atomic: each operation acts on a whole AvailableTime row. Runs in a single
        transaction — any failure rolls the whole batch back. Update/delete operations
        are scoped to this calendar (and organization); a missing id raises ValueError.

        :param calendar: The calendar whose available times are being modified.
        :param operations: Iterable of dicts, each with an ``action`` of
            ``create`` / ``update`` / ``delete`` plus the relevant fields
            (``id``, ``start_time``, ``end_time``, ``timezone``, ``rrule_string``).
        :param bypass_limits: When True, skips the ``availability_windows`` limit guard on
            the batch's ``create`` operations. Only management commands and one-off repair
            scripts should pass this.
        :raises OverLimitError: When the batch's ``create`` operations would take the
            organization past its effective ``availability_windows`` ceiling. Nothing in
            the batch is applied.
        :return: The calendar's available times after the batch is applied.
        """
        return self._get_availability_service().batch_modify_available_times(
            calendar, operations, bypass_limits=bypass_limits
        )

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

    def update_blocked_time(
        self,
        calendar: Calendar,
        blocked_time_id: int,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        timezone: str | None = None,
        reason: str | None = None,
        rrule_string: str | None = None,
    ) -> BlockedTime:
        """Update an existing blocked time's fields (partial update).

        Delegates to AvailabilityService.update_blocked_time.

        :param calendar: The calendar the blocked time belongs to.
        :param blocked_time_id: The id of the blocked time to update.
        :param start_time: New start time, or None to leave unchanged.
        :param end_time: New end time, or None to leave unchanged.
        :param timezone: New timezone string, or None to leave unchanged.
        :param reason: New reason string, or None to leave unchanged.
        :param rrule_string: New recurrence rule string, or None to leave unchanged.
        :return: The updated BlockedTime instance.
        :raises ValueError: If blocked_time_id is not found in this calendar.
        """
        return self._get_availability_service().update_blocked_time(
            calendar=calendar,
            blocked_time_id=blocked_time_id,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            reason=reason,
            rrule_string=rrule_string,
        )

    def delete_blocked_time(
        self,
        calendar: Calendar,
        blocked_time_id: int,
    ) -> None:
        """Delete an existing blocked time (single-row delete).

        Delegates to AvailabilityService.delete_blocked_time.

        A recurring blocked time is stored as one row (with an rrule on its RecurrenceRule).
        Deleting it removes the whole recurrence series.

        :param calendar: The calendar the blocked time belongs to.
        :param blocked_time_id: The id of the blocked time to delete.
        :raises ValueError: If blocked_time_id is not found in this calendar.
        """
        self._get_availability_service().delete_blocked_time(
            calendar=calendar,
            blocked_time_id=blocked_time_id,
        )

    def create_available_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        rrule_string: str | None = None,
        bypass_limits: bool = False,
    ) -> AvailableTime:
        """Create a single available time (optionally recurring).

        :param bypass_limits: When True, skips the ``availability_windows`` limit guard.
            Only management commands and one-off repair scripts should pass this.
        :raises OverLimitError: When the organization is at its effective
            ``availability_windows`` ceiling. Nothing is created.
        """
        return self._get_availability_service().create_available_time(
            calendar=calendar,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            rrule_string=rrule_string,
            bypass_limits=bypass_limits,
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

    # Webhook-related methods — delegated to CalendarWebhookService

    def request_webhook_triggered_sync(
        self,
        external_calendar_id: str,
        webhook_event: CalendarWebhookEvent,
        sync_window_hours: int = 24,
    ) -> CalendarSync | None:
        """Request calendar sync triggered by webhook notification.

        Delegates to :class:`CalendarWebhookService`. Also a ``WebhookServiceHost``
        seam: ``CalendarWebhookService.process_webhook_notification`` calls this
        through the host so that ``@patch.object(CalendarService,
        "request_webhook_triggered_sync")`` in the existing test suite intercepts
        the call before it reaches the sub-service.
        """
        return self._get_webhook_service().request_webhook_triggered_sync(
            external_calendar_id=external_calendar_id,
            webhook_event=webhook_event,
            sync_window_hours=sync_window_hours,
        )

    def create_calendar_webhook_subscription(
        self,
        calendar: Calendar,
        callback_url: str | None = None,
        expiration_hours: int = 24,
    ) -> CalendarWebhookSubscription:
        """Create webhook subscription. Delegates to :class:`CalendarWebhookService`."""
        return self._get_webhook_service().create_calendar_webhook_subscription(
            calendar=calendar,
            callback_url=callback_url,
            expiration_hours=expiration_hours,
        )

    def process_webhook_notification(
        self,
        provider: str,
        calendar_external_id: str,
        headers: dict[str, str],
        payload: dict | str | None = None,
        validation_token: str | None = None,
    ) -> CalendarWebhookEvent | None:
        """Process incoming webhook notification. Delegates to :class:`CalendarWebhookService`."""
        return self._get_webhook_service().process_webhook_notification(
            provider=provider,
            calendar_external_id=calendar_external_id,
            headers=headers,
            payload=payload,
            validation_token=validation_token,
        )

    def handle_webhook(
        self, provider: CalendarProvider, request: HttpRequest
    ) -> CalendarWebhookEvent | None:
        """Handle calendar webhook processing with organization context.

        Extracts the organization from the HTTP request and writes it to
        ``self.organization`` so that :meth:`_build_context_snapshot` picks it
        up when the webhook sub-service calls back through the host
        (``request_webhook_triggered_sync`` → ``request_calendar_sync``).
        The header parsing and notification dispatching are delegated to
        :class:`CalendarWebhookService`.

        Args:
            provider: Calendar provider enum
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

        # Set organization context on the facade so that _build_context_snapshot()
        # includes it in any subsequent sub-service constructions (e.g. the
        # request_webhook_triggered_sync callback through the host seam).
        self.organization = organization

        return self._get_webhook_service().handle_webhook(provider, request)

    def list_webhook_subscriptions(self) -> QuerySet[CalendarWebhookSubscription]:
        """List active webhook subscriptions. Delegates to :class:`CalendarWebhookService`."""
        return self._get_webhook_service().list_webhook_subscriptions()

    def delete_webhook_subscription(self, subscription_id: int) -> bool:
        """Delete a webhook subscription. Delegates to :class:`CalendarWebhookService`."""
        return self._get_webhook_service().delete_webhook_subscription(subscription_id)

    def refresh_webhook_subscription(
        self, subscription_id: int
    ) -> CalendarWebhookSubscription | None:
        """Refresh a webhook subscription. Delegates to :class:`CalendarWebhookService`."""
        return self._get_webhook_service().refresh_webhook_subscription(subscription_id)

    def get_webhook_health_status(self) -> WebhookHealthStatus:
        """Get webhook health status. Delegates to :class:`CalendarWebhookService`."""
        return self._get_webhook_service().get_webhook_health_status()
