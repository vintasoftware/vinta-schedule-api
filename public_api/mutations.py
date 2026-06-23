import datetime
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from calendar_integration.constants import CalendarType
from calendar_integration.exceptions import (
    CalendarIntegrationError,
    NoAvailableTimeWindowsError,
)
from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    BlockedTimeGraphQLType,
    CalendarBundleGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
)
from calendar_integration.models import Calendar, CalendarEvent
from calendar_integration.mutations import (
    CalendarGroupMutations,
    ExternalEventChangeRequestMutations,
)
from calendar_integration.services.dataclasses import (
    CalendarEventInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
)
from organizations.exceptions import NoServiceAccountConfiguredError, UserAlreadyHasMembershipError
from organizations.models import Organization, OrganizationBranding, OrganizationMembership
from organizations.services import OrganizationService
from public_api.capabilities import assert_org_can_invite, assert_target_in_subtree
from public_api.constants import PROVIDER_SCOPED_RESOURCES, PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess
from public_api.scoping import assert_calendar_in_owner_scope
from public_api.services import PublicAPIAuthService
from public_api.types import (
    BrandingResult,
    CreateInvitationInput,
    CreateInvitationResult,
    CreateOrganizationInput,
    CreateOrganizationResult,
    CreateScopedSystemUserInput,
    CreateScopedSystemUserResult,
    CreateSystemUserTokenInput,
    CreateSystemUserTokenResult,
    InvitationResult,
    OrganizationResult,
    PublicApiHttpRequest,
    UpdateBrandingInput,
    UpdateBrandingResult,
)
from webhooks.graphql import WebhookConfigurationGraphQLType
from webhooks.models import WebhookConfiguration


if TYPE_CHECKING:
    from webhooks.services.webhook_service import WebhookService


if TYPE_CHECKING:
    from calendar_integration.services.calendar_group_service import CalendarGroupService
    from calendar_integration.services.calendar_service import CalendarService


# Module-scope constants for validation
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
_url_validator = URLValidator(schemes=["http", "https"])
# Mirrors CalendarEvent.title's max_length so an over-long title is rejected with a clean
# GraphQL error rather than surfacing as a DB-level error after work has begun.
EVENT_TITLE_MAX_LENGTH = 255


@dataclass
class MutationDependencies:
    public_api_auth_service: PublicAPIAuthService
    organization_service: OrganizationService
    webhook_service: "WebhookService"


@inject
def get_mutation_dependencies(
    public_api_auth_service: Annotated[
        PublicAPIAuthService | None, Provide["public_api_auth_service"]
    ] = None,
    organization_service: Annotated[
        OrganizationService | None, Provide["organization_service"]
    ] = None,
    webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
) -> MutationDependencies:
    required_dependencies = [public_api_auth_service, organization_service, webhook_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return MutationDependencies(
        public_api_auth_service=cast(PublicAPIAuthService, public_api_auth_service),
        organization_service=cast(OrganizationService, organization_service),
        webhook_service=cast("WebhookService", webhook_service),
    )


@dataclass
class CalendarMutationDependencies:
    """Dependencies for calendar mutations."""

    calendar_service: "CalendarService"
    calendar_group_service: "CalendarGroupService"


@inject
def get_calendar_mutation_dependencies(
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
    calendar_group_service: Annotated[
        "CalendarGroupService | None", Provide["calendar_group_service"]
    ] = None,
) -> CalendarMutationDependencies:
    """Get calendar mutation dependencies from DI container."""
    required_dependencies = [calendar_service, calendar_group_service]
    if any(dep is None for dep in required_dependencies):
        missing = [d for d in required_dependencies if d is None]
        raise GraphQLError(f"Missing required dependencies: {missing}")

    return CalendarMutationDependencies(
        calendar_service=cast("CalendarService", calendar_service),
        calendar_group_service=cast("CalendarGroupService", calendar_group_service),
    )


def _get_org_and_init_calendar_service(
    info: strawberry.Info,
) -> tuple["CalendarService", Organization]:
    """Resolve org from request context and initialize calendar service.

    Returns:
        Tuple of (initialized calendar_service, organization)

    Raises:
        GraphQLError: If organization is not found in request context
    """
    org = info.context.request.public_api_organization
    if not org:
        raise GraphQLError("Organization not found in request context")

    deps = get_calendar_mutation_dependencies()
    request: PublicApiHttpRequest = info.context.request
    deps.calendar_service.initialize_without_provider(
        user_or_token=request.public_api_system_user, organization=org
    )

    return deps.calendar_service, org


@strawberry.type
class AuthPayload:
    token_valid: bool


# ---------------------------------------------------------------------------
# WebhookConfiguration CRUD input/result types
# ---------------------------------------------------------------------------


@strawberry.input
class CreateWebhookConfigurationInput:
    """Input for creating a new outgoing webhook configuration."""

    event_type: str
    url: str
    headers: strawberry.scalars.JSON = strawberry.field(default=None)  # type: ignore[assignment]


@strawberry.type
class CreateWebhookConfigurationResult:
    """Result of creating a webhook configuration."""

    configuration: WebhookConfigurationGraphQLType | None = None
    error_message: str | None = None


@strawberry.input
class UpdateWebhookConfigurationInput:
    """Input for partially updating an outgoing webhook configuration."""

    id: int  # noqa: A003
    event_type: str | None = None
    url: str | None = None
    headers: strawberry.scalars.JSON | None = None  # type: ignore[assignment]


@strawberry.type
class UpdateWebhookConfigurationResult:
    """Result of updating a webhook configuration."""

    configuration: WebhookConfigurationGraphQLType | None = None
    error_message: str | None = None


@strawberry.input
class DeleteWebhookConfigurationInput:
    """Input for soft-deleting an outgoing webhook configuration."""

    id: int  # noqa: A003


@strawberry.type
class DeleteWebhookConfigurationResult:
    """Result of deleting a webhook configuration."""

    success: bool
    error_message: str | None = None


@strawberry.input
class DeleteSystemUserInput:
    system_user_id: int


@strawberry.type
class DeleteSystemUserResult:
    success: bool
    error_message: str | None = None


@strawberry.input
class CreateResourceCalendarInput:
    """Input for creating a manual resource (room/equipment) calendar."""

    organization_id: int
    name: str
    description: str | None = None
    capacity: int | None = None
    manage_available_windows: bool = False
    is_private: bool = True


@strawberry.type
class CreateResourceCalendarResult:
    """Result of the createResourceCalendar mutation."""

    success: bool
    error_message: str | None = None
    calendar: CalendarGraphQLType | None = None


@strawberry.input
class DisableResourceCalendarInput:
    """Input for disabling a resource calendar."""

    organization_id: int
    calendar_id: int


@strawberry.type
class DisableResourceCalendarResult:
    """Result of the disableResourceCalendar mutation."""

    success: bool
    error_message: str | None = None


@strawberry.input
class ImportResourceCalendarsInput:
    """Input for triggering a Google Workspace resource calendar import."""

    organization_id: int
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None


@strawberry.type
class ImportResourceCalendarsResult:
    """Result of the importResourceCalendars mutation (async enqueue — no payload)."""

    success: bool
    error_message: str | None = None


@strawberry.input
class CreateAvailableTimeInput:
    """Input for creating a single (optionally recurring) available time on a calendar."""

    organization_id: int
    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    rrule_string: str | None = None


@strawberry.type
class CreateAvailabilityWindowResult:
    """Result of the createAvailabilityWindow mutation."""

    success: bool
    error_message: str | None = None
    available_time: AvailableTimeGraphQLType | None = None


@strawberry.input
class UpdateAvailableTimeInput:
    """Input for updating a single available time via the batch path."""

    organization_id: int
    calendar_id: int
    available_time_id: int
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    timezone: str | None = None
    rrule_string: str | None = None


@strawberry.type
class UpdateAvailabilityWindowResult:
    """Result of the updateAvailabilityWindow mutation."""

    success: bool
    error_message: str | None = None
    available_time: AvailableTimeGraphQLType | None = None


@strawberry.input
class DeleteAvailableTimeInput:
    """Input for deleting a single available time via the batch path.

    Note: the v2 doc proposed a deleteSeries argument, but batch_modify_available_times
    supports only single-row delete. Series deletion is not implemented here.
    """

    organization_id: int
    calendar_id: int
    available_time_id: int


@strawberry.type
class DeleteAvailabilityWindowResult:
    """Result of the deleteAvailabilityWindow mutation."""

    success: bool
    error_message: str | None = None


@strawberry.input
class BatchAvailabilityOperationInput:
    """A single create/update/delete operation in a batch availability update.

    For action='create': start_time, end_time, and timezone are required;
    rrule_string is optional.
    For action='update': available_time_id is required; other fields are optional
    (only provided fields are updated).
    For action='delete': available_time_id is required; no other fields are needed.
    """

    action: str
    available_time_id: int | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    timezone: str | None = None
    rrule_string: str | None = None


@strawberry.input
class BatchAvailabilityInput:
    """Input for applying an atomic batch of availability operations to a calendar."""

    organization_id: int
    calendar_id: int
    operations: list[BatchAvailabilityOperationInput]


@strawberry.type
class BatchUpdateAvailabilityWindowsResult:
    """Result of the batchUpdateAvailabilityWindows mutation.

    On success, available_times contains the full list of the calendar's available times
    after the batch is applied. On failure, available_times is an empty list.
    """

    success: bool
    error_message: str | None = None
    available_times: list[AvailableTimeGraphQLType]


@strawberry.input
class CreateBlockedTimeInput:
    """Input for creating a single (optionally recurring) blocked time on a calendar."""

    organization_id: int
    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    reason: str = ""
    rrule_string: str | None = None


@strawberry.type
class CreateBlockedTimeResult:
    """Result of the createBlockedTime mutation."""

    success: bool
    error_message: str | None = None
    blocked_time: BlockedTimeGraphQLType | None = None


@strawberry.input
class UpdateBlockedTimeInput:
    """Input for updating an existing blocked time (partial update — only provided fields change)."""

    organization_id: int
    calendar_id: int
    blocked_time_id: int
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    timezone: str | None = None
    reason: str | None = None
    rrule_string: str | None = None


@strawberry.type
class UpdateBlockedTimeResult:
    """Result of the updateBlockedTime mutation."""

    success: bool
    error_message: str | None = None
    blocked_time: BlockedTimeGraphQLType | None = None


@strawberry.input
class DeleteBlockedTimeInput:
    """Input for deleting a blocked time (single-row delete).

    Note: a recurring blocked time is stored as one row (rrule on RecurrenceRule).
    Deleting it removes the whole recurrence series. Materialized exception rows are not
    separately handled. The v2 doc proposed a deleteSeries arg, but since a recurring
    blocked time is one row (not a series of rows), there is no robust series-delete
    backing distinct from single-row delete — ``deleteSeries`` is intentionally omitted.
    """

    organization_id: int
    calendar_id: int
    blocked_time_id: int


@strawberry.type
class DeleteBlockedTimeResult:
    """Result of the deleteBlockedTime mutation."""

    success: bool
    error_message: str | None = None


@strawberry.input
class CreateCalendarInput:
    """Input for creating a plain (personal) calendar.

    Creates an internal PERSONAL calendar scoped to the token's organization.
    is_private controls whether the calendar can be booked via codeless public scheduling
    links. Defaults to True (private) — public scheduling is opt-in.
    """

    organization_id: int
    name: str
    description: str | None = None
    is_private: bool = True


@strawberry.type
class CreateCalendarResult:
    """Result of the createCalendar mutation."""

    success: bool
    error_message: str | None = None
    calendar: CalendarGraphQLType | None = None


@strawberry.input
class UpdateCalendarInput:
    """Input for partially updating a plain (personal) calendar.

    Only provided (non-None) fields are updated; omitted fields leave the calendar unchanged.
    The target calendar must belong to the token's organization and must be a PERSONAL type.
    is_private: If provided (non-None), updates the calendar's privacy.
        True -> accepts_public_scheduling=False (private, codeless booking disallowed).
        False -> accepts_public_scheduling=True (public, codeless booking allowed).
        Omit (None) to leave accepts_public_scheduling unchanged.
    """

    organization_id: int
    calendar_id: int
    name: str | None = None
    description: str | None = None
    is_private: bool | None = None


@strawberry.type
class UpdateCalendarResult:
    """Result of the updateCalendar mutation."""

    success: bool
    error_message: str | None = None
    calendar: CalendarGraphQLType | None = None


@strawberry.input
class CreateCalendarBundleInput:
    """Input for creating a bundle calendar from child calendars.

    children_ids: IDs of existing org-scoped calendars to include in the bundle.
    primary_calendar_id: Optional. The ID of one of the children_ids calendars that will
        be designated as the primary (hosts the real external event). Must be present in
        children_ids when provided.
    is_private: Boolean indicating whether this bundle is private (default: True).
        When True, codeless public scheduling is disabled for this bundle.
    """

    organization_id: int
    name: str
    description: str | None = None
    children_ids: list[int]
    primary_calendar_id: int | None = None
    is_private: bool = True


@strawberry.type
class CreateCalendarBundleResult:
    """Result of the createCalendarBundle mutation."""

    success: bool
    error_message: str | None = None
    bundle: CalendarBundleGraphQLType | None = None


@strawberry.input
class UpdateCalendarBundleInput:
    """Input for updating a bundle calendar's name, description, children set, and primary.

    name: If provided (non-None), updates the bundle's name.
    description: If provided (non-None), updates the bundle's description.
    children_ids: Full desired set of child calendar IDs (reconciles adds/removals).
    primary_calendar_id: Optional. Must be present in children_ids when provided.
    is_private: Optional. If provided (non-None), updates the bundle's privacy setting.
        Omit to leave accepts_public_scheduling unchanged.
    """

    organization_id: int
    bundle_id: int
    name: str | None = None
    description: str | None = None
    children_ids: list[int]
    primary_calendar_id: int | None = None
    is_private: bool | None = None


@strawberry.type
class UpdateCalendarBundleResult:
    """Result of the updateCalendarBundle mutation."""

    success: bool
    error_message: str | None = None
    bundle: CalendarBundleGraphQLType | None = None


@strawberry.input
class DisableCalendarBundleInput:
    """Input for disabling a bundle calendar."""

    organization_id: int
    bundle_id: int


@strawberry.type
class DisableCalendarBundleResult:
    """Result of the disableCalendarBundle mutation."""

    success: bool
    error_message: str | None = None


@strawberry.input
class ScheduleEventExternalAttendeeInput:
    """An external (non-user) attendee on a scheduled event: an email and optional name."""

    email: str
    name: str = ""


@strawberry.input
class ScheduleEventInput:
    """Input for scheduling a calendar event on an owned calendar.

    A scoped public-API token may schedule events only on calendars its owner owns. The
    target calendar is identified by ``calendar_id``; ``attendee_user_ids`` are internal
    org users (validated as active members of the caller's organization) and
    ``external_attendees`` are email/name pairs. ``rrule_string`` (RFC-5545) makes the
    event recurring.
    """

    organization_id: int
    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    title: str
    description: str = ""
    attendee_user_ids: list[int] = strawberry.field(default_factory=list)
    external_attendees: list[ScheduleEventExternalAttendeeInput] = strawberry.field(
        default_factory=list
    )
    rrule_string: str | None = None


@strawberry.input
class RescheduleCalendarEventInput:
    """Input for rescheduling a single-calendar event via a public-API token.

    Supports three modes:
    - **Whole event / series** (``recurrence_id`` omitted): updates the event's time fields
      and, optionally, its recurrence rule. The existing rule is preserved when
      ``rrule_string`` is omitted — callers must not strip the series accidentally.
    - **Series with new rule** (``recurrence_id`` omitted, ``rrule_string`` provided):
      moves the event AND updates the recurrence pattern.
    - **Single occurrence** (``recurrence_id`` provided): reschedules exactly the
      occurrence whose original start equals ``recurrence_id`` without touching the
      master or any other occurrence.

    An owner-scoped token may only reschedule events on calendars its owner owns;
    an org-wide token acts org-wide.
    """

    organization_id: int
    calendar_id: int
    event_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    # Optional: change the series' recurrence pattern. Omit to PRESERVE the existing rule.
    rrule_string: str | None = None
    # Optional: when set, reschedule ONLY this occurrence of a recurring series
    # (the occurrence's original start == CalendarEvent.recurrence_id). Omit for whole event/series.
    recurrence_id: datetime.datetime | None = None


@strawberry.type
class Mutation(ExternalEventChangeRequestMutations, CalendarGroupMutations):
    @strawberry.mutation
    def check_token(
        self,
        system_user_id: int,
        token: str,
    ) -> AuthPayload:
        deps = get_mutation_dependencies()

        try:
            system_user, authenticated = deps.public_api_auth_service.check_system_user_token(
                system_user_id, token
            )
        except SystemUser.DoesNotExist as e:
            raise GraphQLError("System user does not exist") from e
        if not system_user or not authenticated:
            raise GraphQLError("Invalid credentials")

        return AuthPayload(token_valid=True)  # type: ignore

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_system_user(
        self,
        info: strawberry.Info,
        input: DeleteSystemUserInput,  # noqa: A002
    ) -> DeleteSystemUserResult:
        org = info.context.request.public_api_organization
        if not org:
            return DeleteSystemUserResult(success=False, error_message="Organization not found")

        try:
            system_user = SystemUser.objects.get(
                id=input.system_user_id,
                organization=org,
                deleted_at__isnull=True,
            )
        except SystemUser.DoesNotExist:
            return DeleteSystemUserResult(success=False, error_message="System user not found")

        if system_user.is_active:
            return DeleteSystemUserResult(
                success=False,
                error_message="System user must be inactive before deletion",
            )

        system_user.deleted_at = timezone.now()
        system_user.save(update_fields=["deleted_at"])

        return DeleteSystemUserResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_organization(
        self,
        info: strawberry.Info,
        input: CreateOrganizationInput,  # noqa: A002
    ) -> CreateOrganizationResult:
        """
        Create a child organization under the acting (reseller) organization.

        The mutation:
        1. Checks that the acting org has the can_invite_organizations flag (via assert_org_can_invite).
        2. Ensures no sibling with the same name already exists under the parent.
        3. Creates the child with parent=acting_org and can_invite_organizations=False.
        4. Returns the created organization's id and name.

        The token's OrganizationResourceAccess must include the ORGANIZATION resource.
        """
        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Validate no duplicate name under the same parent
        if Organization.objects.filter(parent=acting_org, name=input.name).exists():
            raise GraphQLError(
                f"An organization with name '{input.name}' already exists under this parent."
            )

        # Create the child org with parent=acting_org and can_invite_organizations=False
        try:
            child_org = Organization.objects.create(
                name=input.name,
                parent=acting_org,
                can_invite_organizations=False,
            )
        except IntegrityError as e:
            raise GraphQLError(
                f"An organization with name '{input.name}' already exists under this parent."
            ) from e

        return CreateOrganizationResult(
            organization=OrganizationResult(id=child_org.id, name=child_org.name)
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_invitation(
        self,
        info: strawberry.Info,
        input: CreateInvitationInput,  # noqa: A002
    ) -> CreateInvitationResult:
        """
        Create a pending organization invitation for an end-user (reseller bundle).

        The mutation:
        1. Checks the acting org has can_invite_organizations (via assert_org_can_invite).
        2. Validates organizationId is the acting org or a descendant (subtree guard).
        3. Checks the user is not already an active member of the target org.
        4. Creates (or resets) a pending OrganizationInvitation via OrganizationService.
        5. Sends the invitation email (Phase 3: sendEmail=true path only).
           Phase 4 will add the sendEmail=false branch returning the raw token.
        6. Returns the invitation with token=None and invite_url=None (email path).

        The token's OrganizationResourceAccess must include the INVITATION resource.
        """
        deps = get_mutation_dependencies()

        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Resolve the target organization
        try:
            target_org = Organization.objects.get(id=int(input.organization_id))
        except (Organization.DoesNotExist, ValueError) as e:
            raise GraphQLError(f"Organization with id '{input.organization_id}' not found.") from e

        # Tenant-isolation guard: target must be the acting org or a descendant
        assert_target_in_subtree(acting_org, target_org)

        # Already-active-member guard: reject if the email belongs to an existing member.
        # We check by email because the invitation itself creates the user (the user may not
        # exist yet).
        user_model = get_user_model()
        try:
            existing_user = user_model.objects.get(email=input.user_email)
        except user_model.DoesNotExist:
            existing_user = None

        if existing_user is not None:
            if OrganizationMembership.objects.filter(
                user=existing_user,
                organization=target_org,
                is_active=True,
            ).exists():
                raise GraphQLError(
                    UserAlreadyHasMembershipError.default_detail,
                )

        # Create (or reset) the pending invitation via OrganizationService.
        # invited_by=None because the public-API caller is a SystemUser, not a Django User.
        # first_name/last_name are empty strings — invite_user_to_organization creates (or
        # reuses) the user and stores names for email rendering only.
        #
        # Phase 4: when send_email=False the service suppresses the email and attaches the raw
        # token as invitation._raw_token (transient, never persisted in plaintext).
        invitation = deps.organization_service.invite_user_to_organization(
            email=input.user_email,
            first_name="",
            last_name="",
            organization=target_org,
            invited_by=None,
            role=input.role.to_model_role(),
            send_email=input.send_email,
        )

        raw_token: str | None = None
        invite_url: str | None = None

        if not input.send_email:
            # The service always attaches _raw_token; retrieve it once so it is not
            # inadvertently retained beyond this scope.
            raw_token = invitation._raw_token  # type: ignore[attr-defined]
            # Build the invite URL using the same template the branded email uses.
            # Phase 6+ may refine this URL from the reseller's return_url_allowlist once
            # OrganizationBranding is available; for now we use the same base as the email.
            url_template: str = getattr(settings, "HEADLESS_FRONTEND_URLS", {}).get(
                "account_accept_invitation", ""
            )
            invite_url = url_template.format(token=raw_token) if url_template else None

        return CreateInvitationResult(
            invitation=InvitationResult(
                id=invitation.id,
                email=invitation.email,
                expires_at=invitation.expires_at,
            ),
            token=raw_token,
            invite_url=invite_url,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_system_user_token(
        self,
        info: strawberry.Info,
        input: CreateSystemUserTokenInput,  # noqa: A002
    ) -> CreateSystemUserTokenResult:
        """
        Mint a delegated Public API token for the reseller's subtree (reseller bundle).

        The mutation:
        1. Checks that the acting org has the can_invite_organizations flag (via assert_org_can_invite).
        2. Resolves the target org from organizationId and validates it is the acting org or
           a descendant (subtree guard — reuses assert_target_in_subtree).
        3. Validates that resources is non-empty and every item is a valid PublicAPIResources value.
           ORGANIZATION may be included to delegate the invite-orgs capability; it still cannot
           set the DB flag (that is DB/admin-only).
        4. Mints a SystemUser via PublicAPIAuthService.create_system_user and bulk-creates
           ResourceAccess rows for the requested resources (mirrors REST SystemUserTokenCreate).
        5. Returns { systemUserId, token } — plaintext token exposed once, never persisted.

        The token's OrganizationResourceAccess must include the SYSTEM_USER resource.
        """
        deps = get_mutation_dependencies()

        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Resolve the target organization
        try:
            target_org = Organization.objects.get(id=int(input.organization_id))
        except (Organization.DoesNotExist, ValueError) as e:
            raise GraphQLError(f"Organization with id '{input.organization_id}' not found.") from e

        # Tenant-isolation guard: target must be the acting org or a descendant
        assert_target_in_subtree(acting_org, target_org)

        # Validate resources: must be non-empty and all values must be valid PublicAPIResources
        if not input.resources:
            raise GraphQLError("resources must not be empty.")

        valid_values = {r.value for r in PublicAPIResources}
        invalid = [r for r in input.resources if r not in valid_values]
        if invalid:
            raise GraphQLError(
                f"Invalid resource(s): {', '.join(invalid)}. "
                f"Valid values are: {', '.join(sorted(valid_values))}."
            )

        # Mint the system user and persist ResourceAccess rows (mirrors REST create)
        try:
            with transaction.atomic():
                system_user, plaintext_token = deps.public_api_auth_service.create_system_user(
                    integration_name=input.integration_name,
                    organization=target_org,
                )
                # dict.fromkeys dedupes while preserving order; prevents constraint violations
                ResourceAccess.objects.bulk_create(
                    [
                        ResourceAccess(system_user=system_user, resource_name=resource_name)
                        for resource_name in dict.fromkeys(input.resources)
                    ]
                )
        except IntegrityError as e:
            raise GraphQLError(
                f"A token with integration_name '{input.integration_name}' already exists."
            ) from e

        return CreateSystemUserTokenResult(
            system_user_id=strawberry.ID(str(system_user.id)),
            token=plaintext_token,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_scoped_system_user(
        self,
        info: strawberry.Info,
        input: CreateScopedSystemUserInput,  # noqa: A002
    ) -> CreateScopedSystemUserResult:
        """
        Mint a provider-scoped Public API token.

        The mutation:
        1. Resolves the caller's organization from the request context.
        2. Validates that scoped_to_user_id refers to an active member of that organization.
        3. Validates that every value in available_resources is a valid PublicAPIResources
           value AND is in the PROVIDER_SCOPED_RESOURCES allow-list (no over-grant).
        4. Validates that available_resources is non-empty.
        5. Creates the SystemUser with scoped_to_user set and bulk-creates ResourceAccess rows.
           Duplicate integration_name is rejected (IntegrityError → GraphQLError).
        6. Returns the plaintext token exactly once — it is never persisted.

        The token's OrganizationResourceAccess must include the SYSTEM_USER resource.
        """
        deps = get_mutation_dependencies()

        org = info.context.request.public_api_organization
        if not org:
            raise GraphQLError("Organization not found")

        # Validate owner: resolve the active membership of the given user in the caller's org.
        # This single query both validates active membership AND yields the value to store.
        try:
            membership = OrganizationMembership.objects.get(
                user_id=input.scoped_to_user_id,
                organization=org,
                is_active=True,
            )
        except OrganizationMembership.DoesNotExist as e:
            raise GraphQLError(
                f"User with id '{input.scoped_to_user_id}' is not an active member of "
                "the caller's organization."
            ) from e

        # Validate available_resources: non-empty
        if not input.available_resources:
            raise GraphQLError("available_resources must not be empty.")

        # Validate each resource is a known PublicAPIResources value
        valid_values = {r.value for r in PublicAPIResources}
        invalid_resources = [r for r in input.available_resources if r not in valid_values]
        if invalid_resources:
            raise GraphQLError(
                f"Invalid resource(s): {', '.join(invalid_resources)}. "
                f"Valid values are: {', '.join(sorted(valid_values))}."
            )

        # Validate each resource is within the provider allow-list (no over-grant)
        over_grant = [r for r in input.available_resources if r not in PROVIDER_SCOPED_RESOURCES]
        if over_grant:
            raise GraphQLError(
                f"Resource(s) not permitted for provider-scoped tokens: {', '.join(over_grant)}. "
                f"Allowed resources are: {', '.join(sorted(PROVIDER_SCOPED_RESOURCES))}."
            )

        # Create the system user and resource-access rows atomically
        try:
            with transaction.atomic():
                system_user, plaintext_token = deps.public_api_auth_service.create_system_user(
                    integration_name=input.integration_name,
                    organization=org,
                    scoped_to_membership=membership,
                )
                # dict.fromkeys dedupes while preserving order; prevents constraint violations
                ResourceAccess.objects.bulk_create(
                    [
                        ResourceAccess(system_user=system_user, resource_name=resource_name)
                        for resource_name in dict.fromkeys(input.available_resources)
                    ]
                )
        except IntegrityError as e:
            # Only convert to "already exists" when the integration_name uniqueness constraint
            # fired; a ResourceAccess constraint failure would have a different message and
            # must not be silently mislabeled.
            if "integration_name" in str(e).lower():
                raise GraphQLError(
                    f"A token with integration_name '{input.integration_name}' already exists."
                ) from e
            raise

        granted_resources = list(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )

        # scoped_to_membership_user_id is always set here — we passed membership to
        # create_system_user above.
        assert system_user.scoped_to_membership_user_id is not None  # noqa: S101

        return CreateScopedSystemUserResult(
            id=system_user.id,
            integration_name=system_user.integration_name,
            is_active=system_user.is_active,
            available_resources=granted_resources,
            scoped_to_user_id=membership.user_id,
            token=plaintext_token,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_branding(
        self,
        info: strawberry.Info,
        input: UpdateBrandingInput,  # noqa: A002
    ) -> UpdateBrandingResult:
        """
        Update or create branding for the acting (reseller) organization.

        The mutation:
        1. Checks that the acting org has can_invite_organizations (via assert_org_can_invite).
        2. Validates app_name: non-empty and max 120 characters.
        3. Validates primary_color and secondary_color format (#RRGGBB or #RRGGBBAA).
        4. Validates each entry in return_url_allowlist is a valid URL using Django's URLValidator.
        5. Upserts OrganizationBranding on the acting org only (always keyed to acting_org).
        6. Returns the upserted branding row (without internal fields like support_email/allowlist).

        The token's OrganizationResourceAccess must include the BRANDING resource.
        """
        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Validate app_name: must be non-empty and at most 120 characters
        if input.app_name:
            if not input.app_name.strip():
                raise GraphQLError("app_name must not be empty or whitespace-only.")
            if len(input.app_name) > 120:
                raise GraphQLError("app_name must be 120 characters or fewer.")

        # Validate color format: #RRGGBB or #RRGGBBAA (6 or 8 hex chars after #)
        if input.primary_color and not HEX_COLOR_PATTERN.match(input.primary_color):
            raise GraphQLError(
                f"Invalid primary_color format: '{input.primary_color}'. "
                "Expected #RRGGBB or #RRGGBBAA."
            )

        if input.secondary_color and not HEX_COLOR_PATTERN.match(input.secondary_color):
            raise GraphQLError(
                f"Invalid secondary_color format: '{input.secondary_color}'. "
                "Expected #RRGGBB or #RRGGBBAA."
            )

        # Validate return_url_allowlist entries are valid URLs using Django's URLValidator
        allowlist = input.return_url_allowlist or []
        for url in allowlist:
            try:
                _url_validator(url)
            except DjangoValidationError as e:
                raise GraphQLError(
                    f"Invalid URL in return_url_allowlist: '{url}'. Must be a valid http(s) URL."
                ) from e

        # Upsert branding on the acting org (always acts on acting org, never another org)
        branding, _ = OrganizationBranding.objects.update_or_create(
            organization=acting_org,
            defaults={
                "app_name": input.app_name,
                "logo_url": input.logo_url,
                "primary_color": input.primary_color,
                "secondary_color": input.secondary_color,
                "support_email": input.support_email,
                "return_url_allowlist": allowlist,
            },
        )

        # Return the branding without internal fields (no support_email, no allowlist)
        branding_result = BrandingResult(
            id=branding.id,
            app_name=branding.app_name,
            logo_url=branding.logo_url,
            primary_color=branding.primary_color,
            secondary_color=branding.secondary_color,
        )

        return UpdateBrandingResult(branding=branding_result)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_webhook_configuration(
        self,
        info: strawberry.Info,
        input: CreateWebhookConfigurationInput,  # noqa: A002
    ) -> CreateWebhookConfigurationResult:
        """Create an outgoing webhook configuration for the caller's organization.

        The mutation:
        1. Delegates event_type and url validation to the service layer.
        2. Creates the configuration scoped to the acting organization.
        3. Returns the created configuration.

        The token's OrganizationResourceAccess must include the WEBHOOK_CONFIGURATION resource.
        """
        deps = get_mutation_dependencies()

        org = info.context.request.public_api_organization
        if not org:
            return CreateWebhookConfigurationResult(
                error_message="Organization not found",
            )

        headers: dict = cast(dict, input.headers) if input.headers is not None else {}

        try:
            configuration = deps.webhook_service.create_configuration(
                organization=org,
                event_type=input.event_type,
                url=input.url,
                headers=headers,
            )
        except ValueError as e:
            raise GraphQLError(str(e)) from e

        return CreateWebhookConfigurationResult(configuration=configuration)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_webhook_configuration(
        self,
        info: strawberry.Info,
        input: UpdateWebhookConfigurationInput,  # noqa: A002
    ) -> UpdateWebhookConfigurationResult:
        """Partially update an outgoing webhook configuration (org-scoped).

        The mutation:
        1. Looks up the configuration by id, acting org, and non-deleted status.
        2. Returns a not-found error if missing or belonging to another org.
        3. Applies partial updates to event_type, url, and/or headers.
        4. Delegates validation of event_type and url to the service layer.
        5. Returns the updated configuration.

        The token's OrganizationResourceAccess must include the WEBHOOK_CONFIGURATION resource.
        """
        deps = get_mutation_dependencies()

        org = info.context.request.public_api_organization
        if not org:
            return UpdateWebhookConfigurationResult(
                error_message="Organization not found",
            )

        # Tenant-scoped lookup: id + org + not-deleted
        try:
            configuration = (
                WebhookConfiguration.objects.filter_by_organization(org.id)
                .live()
                .get(
                    id=input.id,
                )
            )
        except WebhookConfiguration.DoesNotExist:
            return UpdateWebhookConfigurationResult(
                error_message="Webhook configuration not found.",
            )

        # Resolve final values (partial update — fall back to current values)
        new_event_type_str = (
            input.event_type if input.event_type is not None else configuration.event_type
        )
        new_url = input.url if input.url is not None else configuration.url
        new_headers: dict = (
            cast(dict, input.headers) if input.headers is not None else configuration.headers
        )

        try:
            updated = deps.webhook_service.update_configuration(
                configuration=configuration,
                event_type=new_event_type_str,
                url=new_url,
                headers=new_headers,
            )
        except ValueError as e:
            raise GraphQLError(str(e)) from e

        return UpdateWebhookConfigurationResult(configuration=updated)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_webhook_configuration(
        self,
        info: strawberry.Info,
        input: DeleteWebhookConfigurationInput,  # noqa: A002
    ) -> DeleteWebhookConfigurationResult:
        """Soft-delete an outgoing webhook configuration (org-scoped).

        The mutation:
        1. Looks up the configuration by id, acting org, and non-deleted status.
        2. Returns a not-found error if missing or belonging to another org.
        3. Sets deleted_at on the configuration (soft delete).
        4. Returns success=True.

        The token's OrganizationResourceAccess must include the WEBHOOK_CONFIGURATION resource.
        """
        deps = get_mutation_dependencies()

        org = info.context.request.public_api_organization
        if not org:
            return DeleteWebhookConfigurationResult(
                success=False, error_message="Organization not found"
            )

        # Tenant-scoped lookup: id + org + not-deleted
        try:
            configuration = (
                WebhookConfiguration.objects.filter_by_organization(org.id)
                .live()
                .get(
                    id=input.id,
                )
            )
        except WebhookConfiguration.DoesNotExist:
            return DeleteWebhookConfigurationResult(
                success=False, error_message="Webhook configuration not found."
            )

        deps.webhook_service.delete_configuration(configuration)
        return DeleteWebhookConfigurationResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_resource_calendar(
        self,
        info: strawberry.Info,
        input: CreateResourceCalendarInput,  # noqa: A002
    ) -> CreateResourceCalendarResult:
        """Create a manual resource (room/equipment) calendar for the acting organization.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Delegates to CalendarService.create_resource_calendar with the supplied parameters.
        3. Returns the created Calendar on success, or success=False + errorMessage on failure.

        The token's OrganizationResourceAccess must include the CREATE_RESOURCE_CALENDAR resource.
        """
        calendar_service, _org = _get_org_and_init_calendar_service(info)

        try:
            calendar = calendar_service.create_resource_calendar(
                name=input.name,
                # Calendar.description is NOT NULL (no null=True on the field); normalize None -> "" to avoid IntegrityError.
                description=input.description if input.description is not None else "",
                capacity=input.capacity,
                manage_available_windows=input.manage_available_windows,
                accepts_public_scheduling=not input.is_private,
            )
        except (ValueError, DjangoValidationError, IntegrityError) as e:
            return CreateResourceCalendarResult(success=False, error_message=str(e))

        return CreateResourceCalendarResult(success=True, calendar=calendar)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_calendar(
        self,
        info: strawberry.Info,
        input: CreateCalendarInput,  # noqa: A002
    ) -> CreateCalendarResult:
        """Create a plain (personal) internal calendar for the acting organization.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Delegates to CalendarService.create_calendar with the supplied parameters.
           is_private is translated to accepts_public_scheduling = not is_private.
        3. Returns the created CalendarGraphQLType on success, or success=False + errorMessage
           on failure.

        The token's OrganizationResourceAccess must include the CALENDAR resource.
        """
        calendar_service, _org = _get_org_and_init_calendar_service(info)

        try:
            calendar = calendar_service.create_calendar(
                name=input.name,
                # Calendar.description is NOT NULL; normalize None -> "" to avoid IntegrityError.
                description=input.description if input.description is not None else "",
                accepts_public_scheduling=not input.is_private,
            )
        except (ValueError, DjangoValidationError, IntegrityError) as e:
            return CreateCalendarResult(success=False, error_message=str(e))

        return CreateCalendarResult(success=True, calendar=calendar)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_calendar(
        self,
        info: strawberry.Info,
        input: UpdateCalendarInput,  # noqa: A002
    ) -> UpdateCalendarResult:
        """Partially update a plain (personal) calendar (org-scoped).

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Delegates to CalendarService.update_calendar with the supplied parameters.
           Only provided (non-None) fields are written; omitted fields leave the calendar unchanged.
           is_private (when provided) is translated to accepts_public_scheduling = not is_private.
        3. Returns the updated CalendarGraphQLType on success, or success=False + errorMessage
           on failure.

        The token's OrganizationResourceAccess must include the CALENDAR resource.
        """
        calendar_service, _org = _get_org_and_init_calendar_service(info)

        accepts_public_scheduling: bool | None = None
        if input.is_private is not None:
            accepts_public_scheduling = not input.is_private

        try:
            calendar = calendar_service.update_calendar(
                calendar_id=input.calendar_id,
                name=input.name,
                description=input.description,
                accepts_public_scheduling=accepts_public_scheduling,
            )
        except Calendar.DoesNotExist:
            return UpdateCalendarResult(success=False, error_message="Calendar not found.")
        except (ValueError, DjangoValidationError, IntegrityError) as e:
            return UpdateCalendarResult(success=False, error_message=str(e))

        return UpdateCalendarResult(success=True, calendar=calendar)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def disable_resource_calendar(
        self,
        info: strawberry.Info,
        input: DisableResourceCalendarInput,  # noqa: A002
    ) -> DisableResourceCalendarResult:
        """Disable a resource calendar by setting its visibility to INACTIVE.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Delegates to CalendarService.disable_resource_calendar with the supplied calendar_id.
        3. Returns success=True on success, or success=False + errorMessage on failure.

        The token's OrganizationResourceAccess must include the DISABLE_RESOURCE_CALENDAR resource.
        """
        calendar_service, _org = _get_org_and_init_calendar_service(info)

        try:
            calendar_service.disable_resource_calendar(calendar_id=input.calendar_id)
        except Calendar.DoesNotExist:
            return DisableResourceCalendarResult(success=False, error_message="Calendar not found.")
        except (ValueError, DjangoValidationError) as e:
            return DisableResourceCalendarResult(success=False, error_message=str(e))

        return DisableResourceCalendarResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def import_resource_calendars(
        self,
        info: strawberry.Info,
        input: ImportResourceCalendarsInput,  # noqa: A002
    ) -> ImportResourceCalendarsResult:
        """Trigger a Google Workspace resource calendar import for the acting organization.

        The mutation:
        1. Resolves the organization from the request context.
        2. Delegates to OrganizationService.request_rooms_sync, which resolves the org-level
           GoogleCalendarServiceAccount, authenticates the calendar service, and enqueues
           the import for the given [start_time, end_time] window (defaults: now / now+365d).
        3. Returns success=True on success (async enqueue — no payload), or success=False
           + errorMessage when no service account is configured or input is invalid.

        The token's OrganizationResourceAccess must include the IMPORT_RESOURCE_CALENDARS resource.
        """
        org = info.context.request.public_api_organization
        if not org:
            return ImportResourceCalendarsResult(
                success=False, error_message="Organization not found in request context."
            )

        deps = get_mutation_dependencies()

        try:
            # requested_by is typed as User but not used inside request_rooms_sync;
            # the Public API caller is a SystemUser with no Django User equivalent.
            deps.organization_service.request_rooms_sync(
                organization=org,
                requested_by=None,
                start_time=input.start_time,
                end_time=input.end_time,
            )
        except NoServiceAccountConfiguredError:
            return ImportResourceCalendarsResult(
                success=False,
                error_message="No Google service account configured for this organization.",
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError) as e:
            return ImportResourceCalendarsResult(success=False, error_message=str(e))

        return ImportResourceCalendarsResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_availability_window(
        self,
        info: strawberry.Info,
        input: CreateAvailableTimeInput,  # noqa: A002
    ) -> CreateAvailabilityWindowResult:
        """Create a single (optionally recurring) available time on a calendar.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Delegates to CalendarService.create_available_time with the supplied parameters.
        5. Returns the created AvailableTime on success, or success=False + errorMessage on failure.
           Note: the service raises ValueError if calendar.manage_available_windows is False.

        The token's OrganizationResourceAccess must include the CREATE_AVAILABILITY_WINDOW resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # Raises Calendar.DoesNotExist (same as a genuinely missing calendar) so a
            # cross-owner attempt reveals nothing about the target's existence.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return CreateAvailabilityWindowResult(
                success=False, error_message="Calendar not found."
            )

        try:
            available_time = calendar_service.create_available_time(
                calendar=calendar,
                start_time=input.start_time,
                end_time=input.end_time,
                timezone=input.timezone,
                rrule_string=input.rrule_string,
            )
        except (ValueError, DjangoValidationError, CalendarIntegrationError) as e:
            return CreateAvailabilityWindowResult(success=False, error_message=str(e))

        return CreateAvailabilityWindowResult(
            success=True,
            available_time=available_time,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_availability_window(
        self,
        info: strawberry.Info,
        input: UpdateAvailableTimeInput,  # noqa: A002
    ) -> UpdateAvailabilityWindowResult:
        """Update a single available time via the batch path (action=update).

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Builds a single-op batch dict including only the fields provided in the input.
        5. Delegates to CalendarService.batch_modify_available_times with the single op.
        6. Finds the updated AvailableTime by id in the returned list and returns it.
           Note: a missing or cross-calendar available_time_id raises ValueError (success=False).
           Note: the service raises ValueError if calendar.manage_available_windows is False.

        The token's OrganizationResourceAccess must include the UPDATE_AVAILABILITY_WINDOW resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # Raises Calendar.DoesNotExist (same as a genuinely missing calendar) so a
            # cross-owner attempt reveals nothing about the target's existence.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return UpdateAvailabilityWindowResult(
                success=False, error_message="Calendar not found."
            )

        # Build the op dict — always include action + id; include optional fields only when provided.
        op: dict[str, object] = {"action": "update", "id": input.available_time_id}
        if input.start_time is not None:
            op["start_time"] = input.start_time
        if input.end_time is not None:
            op["end_time"] = input.end_time
        if input.timezone is not None:
            op["timezone"] = input.timezone
        if input.rrule_string is not None:
            op["rrule_string"] = input.rrule_string

        try:
            updated_times = calendar_service.batch_modify_available_times(
                calendar=calendar, operations=[op]
            )
        except (
            CalendarIntegrationError,
            ValueError,
            DjangoValidationError,
            Calendar.DoesNotExist,
        ) as e:
            return UpdateAvailabilityWindowResult(success=False, error_message=str(e))

        # Find the updated row in the returned list.
        updated_time = next((at for at in updated_times if at.id == input.available_time_id), None)
        if updated_time is None:
            return UpdateAvailabilityWindowResult(
                success=False,
                error_message="Updated available time not found in result set.",
            )
        return UpdateAvailabilityWindowResult(
            success=True,
            available_time=updated_time,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_availability_window(
        self,
        info: strawberry.Info,
        input: DeleteAvailableTimeInput,  # noqa: A002
    ) -> DeleteAvailabilityWindowResult:
        """Delete a single available time via the batch path (action=delete).

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Delegates to CalendarService.batch_modify_available_times with a single delete op.
        5. Returns success=True on success, or success=False + errorMessage on failure.
           Note: a missing or cross-calendar available_time_id raises ValueError (success=False).
           Note: the service raises ValueError if calendar.manage_available_windows is False.
           Note: the v2 doc proposed a deleteSeries argument, but batch_modify_available_times
           supports only single-row delete. Series deletion is not supported at this time.

        The token's OrganizationResourceAccess must include the DELETE_AVAILABILITY_WINDOW resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # Raises Calendar.DoesNotExist (same as a genuinely missing calendar) so a
            # cross-owner attempt reveals nothing about the target's existence.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return DeleteAvailabilityWindowResult(
                success=False, error_message="Calendar not found."
            )

        op: dict[str, object] = {"action": "delete", "id": input.available_time_id}

        try:
            calendar_service.batch_modify_available_times(calendar=calendar, operations=[op])
        except (CalendarIntegrationError, ValueError, DjangoValidationError) as e:
            return DeleteAvailabilityWindowResult(success=False, error_message=str(e))

        return DeleteAvailabilityWindowResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def batch_update_availability_windows(
        self,
        info: strawberry.Info,
        input: BatchAvailabilityInput,  # noqa: A002
    ) -> BatchUpdateAvailabilityWindowsResult:
        """Apply an atomic create/update/delete batch of available times on a calendar.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
           The single input.calendar_id governs the whole atomic batch; one guard call up front
           rejects a cross-owner batch wholesale with no partial write (individual operations
           share the same calendar_id — they carry no per-op calendar_id of their own).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Validates every operation's action is one of {create, update, delete}.
        5. Translates each BatchAvailabilityOperationInput into the service dict shape
           (mapping available_time_id -> id; including only fields that are not None).
        6. Delegates to CalendarService.batch_modify_available_times with the full ops list.
        7. Returns the calendar's full AvailableTime list after the batch is applied.
           The entire batch is rolled back if any operation fails (ATOMIC_REQUESTS = True
           means the request transaction wraps the whole mutation).

        The token's OrganizationResourceAccess must include the BATCH_UPDATE_AVAILABILITY_WINDOWS
        resource.
        """
        _valid_actions = {"create", "update", "delete"}

        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # One guard call covers the entire batch because all operations share this
            # single calendar_id — individual BatchAvailabilityOperationInput entries carry
            # no per-operation calendar_id. Raises Calendar.DoesNotExist (same as a genuinely
            # missing calendar) so a cross-owner attempt reveals nothing about the target.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return BatchUpdateAvailabilityWindowsResult(
                success=False, error_message="Calendar not found.", available_times=[]
            )

        # Validate all actions before calling the service — fail fast on invalid action.
        for op_input in input.operations:
            if op_input.action not in _valid_actions:
                return BatchUpdateAvailabilityWindowsResult(
                    success=False,
                    error_message=f"Invalid operation action: {op_input.action}",
                    available_times=[],
                )
            if op_input.action == "create" and (
                op_input.start_time is None
                or op_input.end_time is None
                or op_input.timezone is None
            ):
                return BatchUpdateAvailabilityWindowsResult(
                    success=False,
                    error_message="create operation requires startTime, endTime, and timezone",
                    available_times=[],
                )

        # Translate each BatchAvailabilityOperationInput to the service dict shape.
        ops: list[dict[str, object]] = []
        for op_input in input.operations:
            op: dict[str, object] = {"action": op_input.action}
            if op_input.available_time_id is not None:
                op["id"] = op_input.available_time_id
            if op_input.start_time is not None:
                op["start_time"] = op_input.start_time
            if op_input.end_time is not None:
                op["end_time"] = op_input.end_time
            if op_input.timezone is not None:
                op["timezone"] = op_input.timezone
            if op_input.rrule_string is not None:
                op["rrule_string"] = op_input.rrule_string
            ops.append(op)

        try:
            available_times = calendar_service.batch_modify_available_times(
                calendar=calendar, operations=ops
            )
        except (
            CalendarIntegrationError,
            ValueError,
            DjangoValidationError,
            Calendar.DoesNotExist,
        ) as e:
            return BatchUpdateAvailabilityWindowsResult(
                success=False, error_message=str(e), available_times=[]
            )

        return BatchUpdateAvailabilityWindowsResult(
            success=True,
            available_times=available_times,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_blocked_time(
        self,
        info: strawberry.Info,
        input: CreateBlockedTimeInput,  # noqa: A002
    ) -> CreateBlockedTimeResult:
        """Create a single (optionally recurring) blocked time on a calendar.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Delegates to CalendarService.create_blocked_time with the supplied parameters.
        5. Returns the created BlockedTime on success, or success=False + errorMessage on failure.

        The token's OrganizationResourceAccess must include the CREATE_BLOCKED_TIME resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # Raises Calendar.DoesNotExist (same as a genuinely missing calendar) so a
            # cross-owner attempt reveals nothing about the target's existence.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return CreateBlockedTimeResult(success=False, error_message="Calendar not found.")

        try:
            blocked_time = calendar_service.create_blocked_time(
                calendar=calendar,
                start_time=input.start_time,
                end_time=input.end_time,
                timezone=input.timezone,
                reason=input.reason,
                rrule_string=input.rrule_string,
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError, IntegrityError) as e:
            return CreateBlockedTimeResult(success=False, error_message=str(e))

        return CreateBlockedTimeResult(
            success=True,
            blocked_time=blocked_time,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_blocked_time(
        self,
        info: strawberry.Info,
        input: UpdateBlockedTimeInput,  # noqa: A002
    ) -> UpdateBlockedTimeResult:
        """Update an existing blocked time (partial update — only provided fields change).

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Delegates to CalendarService.update_blocked_time with the supplied parameters.
           Only fields present (non-None) in the input are applied; others are left unchanged.
        5. Returns the updated BlockedTime on success, or success=False + errorMessage on failure.
           Note: a missing or cross-calendar blocked_time_id raises ValueError (success=False).

        The token's OrganizationResourceAccess must include the UPDATE_BLOCKED_TIME resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # Raises Calendar.DoesNotExist (same as a genuinely missing calendar) so a
            # cross-owner attempt reveals nothing about the target's existence.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return UpdateBlockedTimeResult(success=False, error_message="Calendar not found.")

        try:
            blocked_time = calendar_service.update_blocked_time(
                calendar=calendar,
                blocked_time_id=input.blocked_time_id,
                start_time=input.start_time,
                end_time=input.end_time,
                timezone=input.timezone,
                reason=input.reason,
                rrule_string=input.rrule_string,
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError, IntegrityError) as e:
            return UpdateBlockedTimeResult(success=False, error_message=str(e))

        return UpdateBlockedTimeResult(
            success=True,
            blocked_time=blocked_time,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_blocked_time(
        self,
        info: strawberry.Info,
        input: DeleteBlockedTimeInput,  # noqa: A002
    ) -> DeleteBlockedTimeResult:
        """Delete a blocked time (single-row delete).

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Asserts the calendar is within the token owner's scope (no-op for org-wide tokens).
        3. Fetches the calendar org-scoped to prevent cross-org access.
        4. Delegates to CalendarService.delete_blocked_time with the supplied blocked_time_id.
        5. Returns success=True on success, or success=False + errorMessage on failure.
           Note: a missing or cross-calendar blocked_time_id raises ValueError (success=False).

        Note on recurrence: a recurring blocked time is stored as one row (rrule on
        RecurrenceRule). Deleting it removes the whole recurrence series; materialized
        exception rows are not separately handled. The v2 doc proposed a deleteSeries arg,
        but since a recurring blocked time is one row, single-row delete already covers the
        series — the arg is intentionally omitted.

        The token's OrganizationResourceAccess must include the DELETE_BLOCKED_TIME resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard: a scoped token may only write to its owner's calendars.
            # Raises Calendar.DoesNotExist (same as a genuinely missing calendar) so a
            # cross-owner attempt reveals nothing about the target's existence.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return DeleteBlockedTimeResult(success=False, error_message="Calendar not found.")

        try:
            calendar_service.delete_blocked_time(
                calendar=calendar,
                blocked_time_id=input.blocked_time_id,
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError) as e:
            return DeleteBlockedTimeResult(success=False, error_message=str(e))

        return DeleteBlockedTimeResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_calendar_bundle(
        self,
        info: strawberry.Info,
        input: CreateCalendarBundleInput,  # noqa: A002
    ) -> CreateCalendarBundleResult:
        """Create a bundle calendar from a set of child calendars.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Fetches the child calendars org-scoped: all children_ids must belong to the org.
           If any id is missing or cross-org, returns success=False.
        3. If primary_calendar_id is provided, verifies it is among children_ids;
           returns success=False if not.
        4. Delegates to CalendarService.create_bundle_calendar with name, description
           (None normalized to ""), child_calendars, and primary_calendar.
        5. Returns the created bundle CalendarBundleGraphQLType on success, or
           success=False + errorMessage on failure.

        The token's OrganizationResourceAccess must include the CREATE_CALENDAR_BUNDLE resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)

        # Fetch child calendars org-scoped (rejects cross-org / missing ids)
        unique_children_ids = list(dict.fromkeys(input.children_ids))
        children = list(
            Calendar.objects.filter_by_organization(org.id).filter(id__in=unique_children_ids)
        )
        if len(children) != len(unique_children_ids):
            return CreateCalendarBundleResult(
                success=False,
                error_message="One or more child calendars not found.",
            )

        # Resolve primary calendar if requested
        primary: Calendar | None = None
        if input.primary_calendar_id is not None:
            if input.primary_calendar_id not in unique_children_ids:
                return CreateCalendarBundleResult(
                    success=False,
                    error_message="primary_calendar_id must be one of the children_ids.",
                )
            primary = next(c for c in children if c.id == input.primary_calendar_id)

        try:
            bundle = calendar_service.create_bundle_calendar(
                name=input.name,
                # Calendar.description is NOT NULL; normalize None -> "" to avoid IntegrityError.
                description=input.description if input.description is not None else "",
                child_calendars=children,
                primary_calendar=primary,
                accepts_public_scheduling=not input.is_private,
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError, IntegrityError) as e:
            return CreateCalendarBundleResult(success=False, error_message=str(e))

        return CreateCalendarBundleResult(success=True, bundle=bundle)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_calendar_bundle(
        self,
        info: strawberry.Info,
        input: UpdateCalendarBundleInput,  # noqa: A002
    ) -> UpdateCalendarBundleResult:
        """Update a bundle calendar's name, description, children set, and/or primary.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Fetches the bundle Calendar org-scoped, restricted to BUNDLE type.
           Returns success=False ("Bundle not found") if missing or wrong type.
        3. If name is non-None, sets bundle.name = input.name.
           If description is non-None, sets bundle.description = input.description.
           If either field changed, saves only those fields to the DB.
        4. Fetches child calendars org-scoped + deduplicates children_ids.
           Returns success=False if any id is missing or cross-org.
        5. Resolves primary_calendar from children when provided;
           returns success=False if primary_calendar_id is not in children_ids.
        6. Delegates to CalendarService.update_bundle_calendar to reconcile children/primary.
        7. Returns the updated bundle on success, or success=False + errorMessage on failure.

        The token's OrganizationResourceAccess must include the UPDATE_CALENDAR_BUNDLE resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)

        # Fetch child calendars org-scoped (rejects cross-org / missing ids) — validation
        # that returns success=False for friendly errors runs BEFORE the atomic block.
        unique_children_ids = list(dict.fromkeys(input.children_ids))
        children = list(
            Calendar.objects.filter_by_organization(org.id).filter(id__in=unique_children_ids)
        )
        if len(children) != len(unique_children_ids):
            return UpdateCalendarBundleResult(
                success=False,
                error_message="One or more child calendars not found.",
            )

        # Resolve primary calendar if requested
        primary: Calendar | None = None
        if input.primary_calendar_id is not None:
            if input.primary_calendar_id not in unique_children_ids:
                return UpdateCalendarBundleResult(
                    success=False,
                    error_message="primary_calendar_id must be one of the children_ids.",
                )
            primary = next(c for c in children if c.id == input.primary_calendar_id)

        try:
            with transaction.atomic():
                # Fetch the bundle org-scoped and restricted to BUNDLE type inside the atomic
                # block so that the DoesNotExist error rolls back any prior savepoint.
                bundle = (
                    Calendar.objects.filter_by_organization(org.id)
                    .filter(calendar_type=CalendarType.BUNDLE)
                    .get(id=input.bundle_id)
                )

                # Update name/description/privacy in the resolver (the service does NOT update these).
                # Runs inside the atomic block so a subsequent service failure rolls back the save.
                update_fields: list[str] = []
                if input.name is not None:
                    bundle.name = input.name
                    update_fields.append("name")
                if input.description is not None:
                    bundle.description = input.description
                    update_fields.append("description")
                if input.is_private is not None:
                    bundle.accepts_public_scheduling = not input.is_private
                    update_fields.append("accepts_public_scheduling")
                if update_fields:
                    bundle.save(update_fields=update_fields)

                updated_bundle = calendar_service.update_bundle_calendar(
                    bundle_calendar=bundle,
                    child_calendars=children,
                    primary_calendar=primary,
                )
        except Calendar.DoesNotExist:
            return UpdateCalendarBundleResult(success=False, error_message="Bundle not found.")
        except (CalendarIntegrationError, ValueError, DjangoValidationError, IntegrityError) as e:
            return UpdateCalendarBundleResult(success=False, error_message=str(e))

        return UpdateCalendarBundleResult(
            success=True,
            bundle=updated_bundle,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def disable_calendar_bundle(
        self,
        info: strawberry.Info,
        input: DisableCalendarBundleInput,  # noqa: A002
    ) -> DisableCalendarBundleResult:
        """Disable a bundle calendar by setting its visibility to INACTIVE.

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Delegates to CalendarService.disable_bundle_calendar with the supplied bundle_id.
        3. Returns success=True on success, or success=False + errorMessage on failure.

        The token's OrganizationResourceAccess must include the DISABLE_CALENDAR_BUNDLE resource.
        """
        calendar_service, _org = _get_org_and_init_calendar_service(info)

        try:
            calendar_service.disable_bundle_calendar(bundle_id=input.bundle_id)
        except Calendar.DoesNotExist:
            return DisableCalendarBundleResult(success=False, error_message="Bundle not found.")
        except (ValueError, DjangoValidationError) as e:
            return DisableCalendarBundleResult(success=False, error_message=str(e))

        return DisableCalendarBundleResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def schedule_event(
        self,
        info: strawberry.Info,
        input: ScheduleEventInput,  # noqa: A002
    ) -> CalendarEventGraphQLType:
        """Schedule a calendar event on a calendar owned by the token's owner.

        Event creation is blocked for org-wide public-API tokens; only an owner-scoped
        token may schedule, and only on its owner's calendars. The mutation:
        1. Resolves the organization and initializes the calendar service via the token.
        2. Asserts the calendar is within the token owner's scope (defense in depth — the
           service independently re-verifies ownership). A cross-owner / missing calendar
           raises the same "Calendar not found." error, revealing nothing about the target.
        3. Validates the title length and that every attendee_user_id is an ACTIVE member of
           the caller's organization (a stray / out-of-org id is rejected before any write,
           so it can never reach the DB as an opaque IntegrityError or attach an arbitrary
           user).
        4. Builds the event input (internal + external attendees, optional rrule) and
           delegates to CalendarService.create_event, which enforces the sanctioned
           owner-scoped allowance and rejects bundle calendars / org-wide tokens.
        5. Maps service-layer errors (PermissionDenied, no-availability, malformed input) to
           clean GraphQL errors — never a 500.

        The token's OrganizationResourceAccess must include the CALENDAR_EVENT resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        try:
            # Owner-scope guard (defense in depth): a scoped token may only target its
            # owner's calendars. Raises Calendar.DoesNotExist — same as a genuinely missing
            # calendar — so a cross-owner attempt reveals nothing about the target.
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Calendar not found.") from exc

        if len(input.title) > EVENT_TITLE_MAX_LENGTH:
            raise GraphQLError(f"Title must be at most {EVENT_TITLE_MAX_LENGTH} characters.")

        # Pre-validate internal attendees: every id must be an ACTIVE member of this org.
        # De-duplicate first so a repeated id doesn't skew the membership count check.
        attendee_user_ids = list(dict.fromkeys(input.attendee_user_ids))
        if attendee_user_ids:
            active_member_ids = set(
                OrganizationMembership.objects.filter(
                    organization_id=org.id,
                    is_active=True,
                    user_id__in=attendee_user_ids,
                ).values_list("user_id", flat=True)
            )
            missing = [uid for uid in attendee_user_ids if uid not in active_member_ids]
            if missing:
                raise GraphQLError(
                    "One or more attendees are not active members of this organization."
                )

        event_input = CalendarEventInputData(
            title=input.title,
            description=input.description or "",
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            attendances=[
                EventAttendanceInputData(user_id=user_id) for user_id in attendee_user_ids
            ],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=external.email,
                        name=external.name,
                    )
                )
                for external in input.external_attendees
            ],
            resource_allocations=[],
            recurrence_rule=input.rrule_string,
        )

        try:
            event = calendar_service.create_event(input.calendar_id, event_input)
        except Calendar.DoesNotExist as exc:
            # A race / direct service-level not-found must stay indistinguishable.
            raise GraphQLError("Calendar not found.") from exc
        except NoAvailableTimeWindowsError as exc:
            raise GraphQLError("No available time window covers the requested event time.") from exc
        except PermissionDenied as exc:
            raise GraphQLError(
                str(exc) or "You do not have permission to schedule this event."
            ) from exc
        except (ValueError, DjangoValidationError, CalendarIntegrationError) as exc:
            raise GraphQLError(str(exc)) from exc

        return event  # type: ignore[return-value]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def reschedule_calendar_event(
        self,
        info: strawberry.Info,
        input: RescheduleCalendarEventInput,  # noqa: A002
    ) -> CalendarEventGraphQLType:
        """Reschedule a single-calendar event (whole, series-preserving, or single-occurrence).

        Three distinct paths share a single resolver:

        1. **Single-occurrence** (``input.recurrence_id`` is set): delegates to
           ``CalendarService.reschedule_event_occurrence``, which creates or updates a
           modified-occurrence ``EventRecurrenceException`` without touching the master or the
           series rule.

        2. **Whole event / series** (``input.recurrence_id`` is None): builds a
           ``CalendarEventInputData`` that preserves the existing event's non-time fields
           (title, description, attendances, external attendances, resource allocations) while
           overriding start/end/timezone and the recurrence rule.  **Rule preservation:** if
           ``input.rrule_string`` is omitted, the master's existing rule string is re-passed so
           ``update_event`` does not silently strip the series.

        Authorization:
        - Owner-scoped token: ``assert_calendar_in_owner_scope`` restricts to calendars owned
          by the token's owner; cross-owner → ``"Calendar not found."`` (same as missing).
        - Org-wide token: ``assert_calendar_in_owner_scope`` is a no-op → acts org-wide.
        - The service independently re-verifies ownership as defense-in-depth.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        # Owner-scope guard: a scoped token may only target its owner's calendars.
        # Cross-owner and missing calendars return the identical error — no existence leak.
        try:
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Calendar not found.") from exc

        # Load the event — needed for the whole-event/series path (to preserve non-time
        # fields) and to validate ownership independently of the calendar guard.
        try:
            existing_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar", "recurrence_rule")
                .prefetch_related(
                    "attendances",
                    "external_attendances__external_attendee",
                    "resource_allocations",
                )
                .get(id=input.event_id, calendar_fk_id=input.calendar_id)
            )
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc

        try:
            if input.recurrence_id is not None:
                # Single-occurrence path: create / update a modified exception for exactly
                # this one occurrence; master and series rule are untouched.
                event = calendar_service.reschedule_event_occurrence(
                    calendar_id=input.calendar_id,
                    master_event_id=input.event_id,
                    recurrence_id=input.recurrence_id,
                    start_time=input.start_time,
                    end_time=input.end_time,
                    timezone=input.timezone,
                )
            else:
                # Whole-event / series path: preserve all non-time fields from the existing
                # event and override only start/end/timezone (+ optionally the rrule).

                # Preserve internal attendances.
                preserved_attendances = [
                    EventAttendanceInputData(user_id=attendance.membership_user_id)
                    for attendance in existing_event.attendances.all()
                    if attendance.membership_user_id is not None
                ]

                # Preserve external attendances — carry the ExternalAttendee id so that
                # update_event can correlate status and detect "no change" correctly.
                preserved_external_attendances = [
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            email=ea.external_attendee.email,
                            name=ea.external_attendee.name or "",
                            id=ea.external_attendee_fk_id,
                        )
                    )
                    for ea in existing_event.external_attendances.all()
                    if ea.external_attendee_fk_id is not None
                ]

                # Preserve resource allocations.
                preserved_resource_allocations = [
                    ResourceAllocationInputData(resource_id=ra.calendar_fk_id)  # type: ignore[arg-type]
                    for ra in existing_event.resource_allocations.all()
                    if ra.calendar_fk_id
                ]

                # Recurrence rule preservation: if the caller omits rrule_string, re-pass
                # the existing rule string so update_event does NOT strip the series.
                # (update_event deletes the rule when recurrence_rule=None.)
                if input.rrule_string is not None:
                    recurrence_rule = input.rrule_string
                elif existing_event.is_recurring:
                    recurrence_rule = existing_event.recurrence_rule.to_rrule_string()
                else:
                    recurrence_rule = None

                event_data = CalendarEventInputData(
                    title=existing_event.title,
                    description=existing_event.description or "",
                    start_time=input.start_time,
                    end_time=input.end_time,
                    timezone=input.timezone,
                    attendances=preserved_attendances,
                    external_attendances=preserved_external_attendances,
                    resource_allocations=preserved_resource_allocations,
                    recurrence_rule=recurrence_rule,
                )
                event = calendar_service.update_event(input.calendar_id, input.event_id, event_data)
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Calendar not found.") from exc
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc
        except PermissionDenied as exc:
            raise GraphQLError(
                str(exc) or "You do not have permission to reschedule this event."
            ) from exc
        except (ValueError, DjangoValidationError, CalendarIntegrationError) as exc:
            raise GraphQLError(str(exc)) from exc

        return event  # type: ignore[return-value]
