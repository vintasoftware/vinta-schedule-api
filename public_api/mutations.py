import datetime
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError
from vinta_billing.exceptions import OverLimitError
from vinta_billing.services.subscription_service import SubscriptionService

from audit.constants import AuditAction
from audit.diff import compute_diff
from audit.services import AuditService
from calendar_integration.constants import CalendarType
from calendar_integration.exceptions import (
    BookingPolicyViolationError,
    CalendarGroupSlotConfigNotFoundError,
    CalendarGroupValidationError,
    CalendarIntegrationError,
    DuplicateBookingPolicyError,
    NoAvailableTimeWindowsError,
)
from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    BlockedTimeGraphQLType,
    BookingPolicyResult,
    CalendarBundleGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
    CreateBookingPolicyInput,
    DeleteBookingPolicyInput,
    DeleteBookingPolicyResult,
    GroupScopedAvailabilityWindowGraphQLType,
    GroupScopedBlockedTimeGraphQLType,
    GroupScopedQuotaRuleGraphQLType,
    UpdateBookingPolicyInput,
    group_scoped_availability_window_from_model,
    group_scoped_blocked_time_from_model,
    group_scoped_quota_rule_from_model,
)
from calendar_integration.models import BookingPolicy, Calendar, CalendarEvent, CalendarGroup
from calendar_integration.mutations import (
    CalendarGroupMutations,
    ExternalEventChangeRequestMutations,
)
from calendar_integration.services.calendar_service import _UNCHANGED
from calendar_integration.services.dataclasses import (
    CalendarEventInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ExternalClientIdentifierData,
    ResourceAllocationInputData,
)
from organizations.branding_logo import (
    branding_diff_state,
    build_logo_display_url,
    normalize_uploaded_logo_key,
    sign_branding_logo_upload,
)
from organizations.exceptions import (
    BrandingLogoUploadRejectedError,
    NoServiceAccountConfiguredError,
    OrganizationGroupNotAssignableError,
    UserAlreadyHasMembershipError,
)
from organizations.invitation_urls import build_invitation_accept_url
from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationMembership,
    resolve_branding_for_display,
)
from organizations.permission_catalog import group_for_invitation_groups
from organizations.permissions import (
    BrandingWriteGateReason,
    evaluate_branding_write_gate,
    is_branding_eligible_organization,
)
from organizations.redirect_url_validation import validate_redirect_url
from organizations.services import OrganizationService
from organizations.slug_validation import validate_organization_slug
from public_api.capabilities import assert_org_can_invite, assert_target_in_subtree
from public_api.constants import PROVIDER_SCOPED_RESOURCES, PublicAPIResources
from public_api.extensions import raise_over_limit_graphql_error
from public_api.models import ResourceAccess, SystemUser
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess
from public_api.scoping import assert_calendar_in_owner_scope
from public_api.services import PublicAPIAuthService
from public_api.types import (
    BrandingLogoUploadResult,
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
    from calendar_integration.services.booking_policy_permission_service import (
        BookingPolicyPermissionService,
    )
    from calendar_integration.services.booking_policy_service import BookingPolicyService
    from calendar_integration.services.calendar_group_service import CalendarGroupService
    from calendar_integration.services.calendar_service import CalendarService


# Module-scope constants for validation
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
# Mirrors CalendarEvent.title's max_length so an over-long title is rejected with a clean
# GraphQL error rather than surfacing as a DB-level error after work has begun.
EVENT_TITLE_MAX_LENGTH = 255

# One message per ``organizations.permissions.BrandingWriteGateReason`` failure --
# this surface's translation of the shared write gate into its own error idiom
# (GraphQLError). Kept distinct in wording from the REST 403 bodies
# (organizations.exceptions) and the admin form error (organizations.admin) so
# each surface reads naturally, while preserving the same distinguishability.
# Two entries, not three: the gate's ``NO_SLUG`` reason was retired and later
# removed, so no message exists for it here.
_BRANDING_GATE_MESSAGES: dict[BrandingWriteGateReason, str] = {
    BrandingWriteGateReason.HAS_PARENT: (
        "This organization has a parent organization and cannot manage its own "
        "branding. Branding for organizations inside a hierarchy is controlled by "
        "the reseller organization above them."
    ),
    BrandingWriteGateReason.NOT_ENTITLED: (
        "This organization's plan does not include white-label branding."
    ),
}


def _apply_input_slug(organization: Organization, slug: str) -> None:
    """Validate ``slug`` with the shared organization-slug rules, check
    uniqueness excluding ``organization`` itself, and persist it immediately.

    Called from ``update_branding`` when ``UpdateBrandingInput.slug`` is
    supplied -- see that mutation's docstring. The caller wraps this call in
    ``transaction.atomic()``:
    an invalid or colliding slug raises ``GraphQLError`` here, and because that
    propagates out of the atomic block, the slug write (and anything else the
    block did) is rolled back rather than partially applied.

    The uniqueness pre-check above is a TOCTOU race: a concurrent caller could
    claim the same slug between the ``exists()`` check and this function's own
    ``save()``. The ``save()`` call is wrapped in its own nested
    ``transaction.atomic()`` (a savepoint) so a resulting ``IntegrityError`` can
    be caught and converted to the same friendly ``GraphQLError`` without
    poisoning the caller's outer atomic block -- an uncaught ``IntegrityError``
    marks the enclosing transaction/savepoint for rollback, so catching it
    anywhere outside its own savepoint would leave the outer block unusable.
    """
    try:
        validate_organization_slug(slug)
    except DjangoValidationError as e:
        raise GraphQLError("; ".join(e.messages)) from e

    if Organization.objects.filter(slug=slug).exclude(pk=organization.pk).exists():
        raise GraphQLError(f"An organization with the slug '{slug}' already exists.")

    organization.slug = slug
    try:
        with transaction.atomic():
            organization.save(update_fields=["slug"])
    except IntegrityError as e:
        raise GraphQLError(f"An organization with the slug '{slug}' already exists.") from e


def _map_external_client_identifiers(
    identifiers: "list[ExternalClientIdentifierInput] | None",
) -> list[ExternalClientIdentifierData] | None:
    """Map the tri-state GraphQL input to the tri-state Phase 2 dataclass list.

    ``strawberry.UNSET`` (the field was omitted from the request) maps to ``None`` --
    "leave untouched", per ``ExternalClientIdentifierService.replace_for_target``. Any
    supplied value -- including an explicit ``null`` or ``[]`` -- maps to a (possibly
    empty) list, which replaces the stored set and clears it when empty. Explicit
    ``null`` is treated the same as ``[]`` (clear): the GraphQL field is nullable
    (``[ExternalClientIdentifierInput!]``, no outer ``!``) but the plan only assigns
    meaning to two states -- omitted vs. supplied -- so a caller-supplied ``null`` is
    "supplied, nothing there", not "omitted".
    """
    if identifiers is strawberry.UNSET:
        return None
    return [
        ExternalClientIdentifierData(system=item.system, identifier=item.identifier)
        for item in (identifiers or [])
    ]


@dataclass
class MutationDependencies:
    public_api_auth_service: PublicAPIAuthService
    organization_service: OrganizationService
    webhook_service: "WebhookService"
    subscription_service: SubscriptionService


@inject
def get_mutation_dependencies(
    public_api_auth_service: Annotated[
        PublicAPIAuthService | None, Provide["public_api_auth_service"]
    ] = None,
    organization_service: Annotated[
        OrganizationService | None, Provide["organization_service"]
    ] = None,
    webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
    subscription_service: Annotated[
        SubscriptionService | None, Provide["subscription_service"]
    ] = None,
) -> MutationDependencies:
    required_dependencies = [
        public_api_auth_service,
        organization_service,
        webhook_service,
        subscription_service,
    ]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return MutationDependencies(
        public_api_auth_service=cast(PublicAPIAuthService, public_api_auth_service),
        organization_service=cast(OrganizationService, organization_service),
        webhook_service=cast("WebhookService", webhook_service),
        subscription_service=cast(SubscriptionService, subscription_service),
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


@inject
def get_booking_policy_mutation_dependencies(
    booking_policy_service: Annotated[
        "BookingPolicyService | None", Provide["booking_policy_service"]
    ] = None,
) -> "BookingPolicyService":
    """Resolve the BookingPolicyService from the DI container."""
    if booking_policy_service is None:
        raise GraphQLError("Missing required dependency: booking_policy_service")
    return booking_policy_service


@inject
def get_booking_policy_permission_service(
    booking_policy_permission_service: Annotated[
        "BookingPolicyPermissionService | None",
        Provide["booking_policy_permission_service"],
    ] = None,
) -> "BookingPolicyPermissionService":
    """Resolve the BookingPolicyPermissionService from the DI container."""
    if booking_policy_permission_service is None:
        raise GraphQLError("Missing required dependency: booking_policy_permission_service")
    return booking_policy_permission_service


@inject
def get_audit_service(
    audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
) -> "AuditService":
    """Resolve the AuditService from the DI container.

    Used by mutations that write directly (no dedicated service layer holding
    its own injected ``audit_service``) -- e.g. ``update_branding`` -- mirroring
    ``get_booking_policy_mutation_dependencies``'s resolve-or-raise shape.
    """
    if audit_service is None:
        raise GraphQLError("Missing required dependency: audit_service")
    return audit_service


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
class UpdateResourceCalendarInput:
    """Input for partially updating a resource calendar.

    Only provided (non-None) fields are updated; omitted fields leave the calendar unchanged.
    The target calendar must be of type RESOURCE and must have provider INTERNAL (not synced
    from an external provider).

    is_private: If provided (non-None), updates the calendar's privacy.
        True  -> accepts_public_scheduling=False (private, codeless booking disallowed).
        False -> accepts_public_scheduling=True  (public, codeless booking allowed).
        Omit (None) to leave accepts_public_scheduling unchanged.

    capacity: If provided as an integer, sets the capacity to that value. If provided as
        null (None), clears the capacity to unlimited. If omitted entirely (UNSET), the
        existing capacity is left unchanged.
    """

    organization_id: int
    calendar_id: int
    name: str | None = None
    description: str | None = None
    capacity: int | None = strawberry.UNSET  # type: ignore[assignment]
    manage_available_windows: bool | None = None
    is_private: bool | None = None
    visibility: str | None = None


@strawberry.type
class UpdateResourceCalendarResult:
    """Result of the updateResourceCalendar mutation."""

    success: bool
    error_message: str | None = None
    calendar: CalendarGraphQLType | None = None


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
class GroupScopedAvailabilityWindowOperationInput:
    """A single create/update/delete operation in a batch group-scoped
    availability window upsert.

    ``calendarId`` is required on every operation (not only ``create``) so
    the owner-scope guard can be applied per-operation before any service
    call, mirroring how ``updateAvailabilityWindow`` / ``deleteAvailabilityWindow``
    carry ``calendarId`` alongside their id even though the id alone would
    resolve it.

    For action='create': calendarId, startTime, endTime, and timezone are
    required; rruleString is optional. An identical create (same calendar,
    group slot, start/end time, timezone, and rrule as an existing window)
    is a no-op — replaying the same batch never duplicates a window (spec
    UC-5).
    For action='update': windowId is required; other fields are optional
    (only provided fields change).
    For action='delete': windowId is required; no other fields are needed.
    """

    action: str
    calendar_id: int
    window_id: int | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    timezone: str | None = None
    rrule_string: str | None = None


@strawberry.input
class BatchGroupScopedAvailabilityWindowsInput:
    """Input for applying an atomic batch of group-scoped availability window
    operations within one group slot's roster."""

    organization_id: int
    group_slot_id: int
    operations: list[GroupScopedAvailabilityWindowOperationInput]


@strawberry.type
class BatchUpsertGroupScopedAvailabilityWindowsResult:
    """Result of the batchUpsertGroupScopedAvailabilityWindows mutation.

    On success, ``windows`` contains every group-scoped window in the group
    slot's roster (all calendars) after the batch is applied. On failure,
    ``windows`` is an empty list and nothing was written.
    """

    success: bool
    error_message: str | None = None
    windows: list[GroupScopedAvailabilityWindowGraphQLType]


@strawberry.input
class GroupScopedBlockedTimeOperationInput:
    """A single create/update/delete operation in a batch group-scoped
    blocked-time upsert.

    Mirrors ``GroupScopedAvailabilityWindowOperationInput`` exactly, plus
    ``reason``. ``calendarId`` is required on every operation (not only
    ``create``) so the owner-scope guard can be applied per-operation before
    any service call.

    For action='create': calendarId, startTime, endTime, and timezone are
    required; reason and rruleString are optional. An identical create
    (same calendar, group slot, start/end time, timezone, reason, and rrule
    as an existing block) is a no-op — replaying the same batch never
    duplicates a block (spec UC-5).
    For action='update': blockId is required; other fields are optional
    (only provided fields change).
    For action='delete': blockId is required; no other fields are needed.
    """

    action: str
    calendar_id: int
    block_id: int | None = None
    start_time: datetime.datetime | None = None
    end_time: datetime.datetime | None = None
    timezone: str | None = None
    reason: str | None = None
    rrule_string: str | None = None


@strawberry.input
class BatchGroupScopedBlockedTimesInput:
    """Input for applying an atomic batch of group-scoped blocked-time
    operations within one group slot's roster."""

    organization_id: int
    group_slot_id: int
    operations: list[GroupScopedBlockedTimeOperationInput]


@strawberry.type
class BatchUpsertGroupScopedBlockedTimesResult:
    """Result of the batchUpsertGroupScopedBlockedTimes mutation.

    On success, ``blockedTimes`` contains every group-scoped block in the
    group slot's roster (all calendars) after the batch is applied. On
    failure, ``blockedTimes`` is an empty list and nothing was written.
    """

    success: bool
    error_message: str | None = None
    blocked_times: list[GroupScopedBlockedTimeGraphQLType]


@strawberry.input
class GroupScopedQuotaRuleOperationInput:
    """A single create/update/delete operation in a batch group-scoped
    quota-rule upsert.

    Simpler than ``GroupScopedBlockedTimeOperationInput``: quota rules are
    non-recurring and have no time range, so there is no ``startTime``/
    ``endTime``/``timezone``/``rruleString`` -- just ``period`` and ``cap``.
    ``calendarId`` is required on every operation (not only ``create``) so
    the owner-scope guard can be applied per-operation before any service
    call.

    For action='create': calendarId, period, and cap are required. An
    identical create (same calendar, group slot, period, and cap as an
    existing rule) is a no-op — replaying the same batch never duplicates a
    rule (spec UC-5). A create naming an ALREADY-USED period with a
    DIFFERENT cap is rejected as a validation error (the model's unique
    constraint on (calendar, slot, period)), never an unhandled server error.
    For action='update': ruleId is required; period and/or cap are optional
    (only provided fields change).
    For action='delete': ruleId is required; no other fields are needed.
    """

    action: str
    calendar_id: int
    rule_id: int | None = None
    period: str | None = None
    cap: int | None = None


@strawberry.input
class BatchGroupScopedQuotaRulesInput:
    """Input for applying an atomic batch of group-scoped quota-rule
    operations within one group slot's roster."""

    organization_id: int
    group_slot_id: int
    operations: list[GroupScopedQuotaRuleOperationInput]


@strawberry.type
class BatchUpsertGroupScopedQuotaRulesResult:
    """Result of the batchUpsertGroupScopedQuotaRules mutation.

    On success, ``quotaRules`` contains every group-scoped quota rule in the
    group slot's roster (all calendars) after the batch is applied. On
    failure, ``quotaRules`` is an empty list and nothing was written.
    """

    success: bool
    error_message: str | None = None
    quota_rules: list[GroupScopedQuotaRuleGraphQLType]


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
class ExternalClientIdentifierInput:
    """One ``(system, identifier)`` client-owned reference pair.

    ``system`` is normalized (case + trailing slash) before storage and matching --
    see ``calendar_integration.external_client_identifiers.normalize_system``.

    Rejected with a GraphQL error (no partial write) when:
    - ``system`` is not a valid URL.
    - ``identifier`` is blank or whitespace-only.
    - ``identifier`` is over 255 characters.
    - the ``(system, identifier)`` pair is already claimed by another record of the
      same type (event or external attendee) in the organization.
    - two pairs in the same payload normalize to the same ``system`` (e.g. differing
      only by case or a trailing slash).
    """

    system: str
    identifier: str


@strawberry.input
class ScheduleEventExternalAttendeeInput:
    """An external (non-user) attendee on a scheduled event: an email and optional name."""

    email: str
    name: str = ""
    # UNSET (omitted) = leave untouched (a no-op on create, since there is nothing to
    # leave untouched yet). An explicit list -- including [] or null -- replaces the
    # full set. Must default to UNSET, never a list, so existing callers that have
    # never heard of this field are unaffected. See
    # ``ExternalClientIdentifierService.replace_for_target``.
    external_client_identifiers: list[ExternalClientIdentifierInput] | None = strawberry.field(  # type: ignore[assignment]
        default=strawberry.UNSET,
        description=(
            "Client-owned (system, identifier) pairs for this external attendee. "
            "Omitted = leave untouched; an explicit list (including [] or null) replaces "
            "the full set. Rejected with a GraphQL error, and no partial write, when: an "
            "invalid system URL is given; an identifier is blank/whitespace or over 255 "
            "characters; a (system, identifier) pair is already claimed by another record "
            "of the same type in the organization; or two pairs in this payload normalize "
            "to the same system. See ExternalClientIdentifierInput."
        ),
    )


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
    # UNSET (omitted) = leave untouched. An explicit list -- including [] or null --
    # replaces the full set. Must default to UNSET, never a list: with a list default
    # every existing scheduleEvent caller that has never heard of this field would
    # start wiping identifiers on every call. See
    # ``ExternalClientIdentifierService.replace_for_target``.
    external_client_identifiers: list[ExternalClientIdentifierInput] | None = strawberry.field(  # type: ignore[assignment]
        default=strawberry.UNSET,
        description=(
            "Client-owned (system, identifier) pairs for this event. Omitted = leave "
            "untouched; an explicit list (including [] or null) replaces the full set. "
            "Rejected with a GraphQL error, and no event created, when: an invalid system "
            "URL is given; an identifier is blank/whitespace or over 255 characters; a "
            "(system, identifier) pair is already claimed by another record of the same "
            "type in the organization; or two pairs in this payload normalize to the same "
            "system. See ExternalClientIdentifierInput."
        ),
    )


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


@strawberry.input
class RescheduleCalendarGroupEventInput:
    """Input for rescheduling a grouped calendar event via a public-API token.

    Grouped events consist of a primary ``CalendarEvent`` on the primary calendar plus
    linked non-primary ``BlockedTime`` rows on each additional calendar in the group.
    All of them move together when this mutation succeeds.

    Whole-event only — group events are not recurring in v1 (no ``recurrenceId``).

    An owner-scoped token may only reschedule grouped events whose primary calendar is
    owned by the token's owner; an org-wide token acts org-wide.
    """

    organization_id: int
    event_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str


@strawberry.input
class UpdateCalendarEventInput:
    """Input for updating a single-calendar event's metadata, attendees and identifiers.

    Every field below ``event_id`` is ``strawberry.UNSET``-defaulted: **omitted means
    untouched**. This mutation never widens what a caller can affect just by naming a
    field it did not intend to change -- an update that supplies only ``title`` leaves
    description, attendees and identifiers exactly as they were.

    Deliberately absent: ``startTime``, ``endTime``, ``timezone`` and ``rruleString``.
    Those stay owned by ``rescheduleCalendarEvent`` -- this mutation does not overlap
    it, and a caller who needs to move an event or change its recurrence must use that
    mutation instead.

    An owner-scoped token may only update events on calendars its owner owns; an
    org-wide token acts org-wide. A cross-owner ``event_id`` returns the identical
    ``"Event not found."`` error a genuinely missing event would -- existence is never
    leaked.

    Field semantics:
    - ``title``: omit to keep the current title. An explicit ``null`` is rejected (an
      event must always have a title).
    - ``description``: omit to keep the current description. An explicit ``null`` is
      treated as clearing it to ``""``, matching ``scheduleEvent``'s own convention.
    - ``attendee_user_ids``: omit to keep the current internal attendees. A supplied
      list (including ``[]`` or ``null``) replaces the full set; every id must be an
      ACTIVE member of the caller's organization.
    - ``external_attendees``: omit to keep the current external attendees untouched,
      including each one's own stored identifiers. Omitted is a true skip -- the
      stored attendees are not read, not rewritten, and emit no
      ``CALENDAR_EVENT_ATTENDEE_UPDATED`` webhooks. A supplied list (including ``[]``
      or ``null``) replaces the full set: an attendee whose email matches an existing
      one is updated in place (its identifiers stay untouched unless that entry's own
      ``external_client_identifiers`` is supplied); an attendee with a new email is
      created; an existing attendee whose email is absent from the new list is removed,
      firing the attendee-removed webhook. Matching is by EMAIL, normalized (stripped
      + lowercased), because ``ScheduleEventExternalAttendeeInput`` carries no id --
      dropping the match and replacing wholesale instead would cascade-delete the
      identifiers of every re-sent attendee.
    - ``external_client_identifiers``: omit to keep the event's own identifiers
      untouched. A supplied list (including ``[]`` or ``null``) replaces the full set;
      ``[]``/``null`` clears it.

    Rejected with a GraphQL error, and no partial write, when: an invalid ``system``
    URL is given; an ``identifier`` is blank/whitespace or over 255 characters; a
    ``(system, identifier)`` pair is already claimed by another record of the same type
    in the organization; or two pairs in one identifier list normalize to the same
    system. (Two of the six domain errors from Phase 2 -- an out-of-allowlist target and
    a cross-organization target -- can never be reached from this caller-supplied body:
    the target is always this resolved event or one of its own attendees, and the
    organization is always the resolved org, never a caller-supplied value.)
    """

    organization_id: int
    event_id: int
    title: str | None = strawberry.UNSET  # type: ignore[assignment]
    description: str | None = strawberry.UNSET  # type: ignore[assignment]
    attendee_user_ids: list[int] | None = strawberry.UNSET  # type: ignore[assignment]
    external_attendees: list[ScheduleEventExternalAttendeeInput] | None = strawberry.UNSET  # type: ignore[assignment]
    external_client_identifiers: list[ExternalClientIdentifierInput] | None = strawberry.field(  # type: ignore[assignment]
        default=strawberry.UNSET,
        description=(
            "Client-owned (system, identifier) pairs for this event. Omitted = leave "
            "untouched; an explicit list (including [] or null) replaces the full set. "
            "Rejected with a GraphQL error, and no partial write, when: an invalid "
            "system URL is given; an identifier is blank/whitespace or over 255 "
            "characters; a (system, identifier) pair is already claimed by another "
            "record of the same type in the organization; or two pairs in this payload "
            "normalize to the same system. See ExternalClientIdentifierInput."
        ),
    )


@strawberry.type
class CancelEventResult:
    """Result of a ``cancelEvent`` mutation.

    ``success`` is ``True`` when the event (or the targeted occurrence / series)
    was cancelled successfully.
    """

    success: bool


@strawberry.input
class CancelEventInput:
    """Input for cancelling a single-calendar or grouped calendar event via a public-API token.

    Supports three cancellation modes:

    - **Single occurrence** (``recurrence_id`` set): cancels exactly the occurrence
      whose original start equals ``recurrence_id`` by creating a cancellation
      ``EventRecurrenceException`` (``is_cancelled=True``).  The master event and
      series rule remain intact.  Use this to omit one occurrence without touching
      the rest of the series.

    - **Whole event / series** (``recurrence_id`` omitted, ``delete_series=False``):
      deletes the event row (the master for recurring events).

      .. warning::
          For a recurring master, passing ``delete_series=False`` without providing
          a ``recurrence_id`` deletes the **master event row** outright — this is
          intentional but may not be what you want.  To cancel a single occurrence,
          supply ``recurrence_id`` instead.  To delete the entire series including
          all instances and the rule, set ``delete_series=True``.

    - **Whole series delete** (``delete_series=True``): deletes the master event
      together with all generated instances, exceptions, and the recurrence rule.

    An owner-scoped token may only cancel events on calendars its owner owns;
    an org-wide token acts org-wide.
    """

    organization_id: int
    calendar_id: int
    event_id: int
    # When True, deletes the entire series (master + instances + exceptions + rule).
    delete_series: bool = False
    # When set, cancels ONLY this occurrence of a recurring series addressed by its
    # original start time (== CalendarEvent.recurrence_id).
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

        # Every organization always has exactly one active plan — there is no
        # plan-less state. This raw insert bypasses OrganizationService entirely, so
        # unlike the REST/signup paths this hook has to be called explicitly here.
        # It is a no-op today: assert_org_can_invite above guarantees acting_org is
        # a billing root, and child_org is always created with parent=acting_org and
        # can_invite_organizations=False, so it never satisfies is_billing_root
        # (see payments.services.subscription_service) and always pools against
        # acting_org's subscription instead of getting its own. Kept as
        # defence-in-depth: if this mutation body is ever changed to create a
        # parent-less or reseller child, this call is what stops it from ending up
        # plan-less rather than a second, disconnected invariant to keep in sync.
        deps = get_mutation_dependencies()
        deps.subscription_service.create_subscription_for_organization(child_org)

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
        4. Translates the requested groups into the invitation's stored state and
           creates (or resets) a pending OrganizationInvitation via OrganizationService.
        5. Sends the invitation email when sendEmail=true.
           When sendEmail=false, suppresses the email and returns the raw token instead.
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

        # A list in, one group stored: the invitation row holds a single
        # group name, which ``OrganizationService`` puts the membership created
        # on acceptance into. Refuses an unknown group, and refuses
        # ``organization_billing_owner``, which an invitation has nowhere to
        # store.
        try:
            invited_group = group_for_invitation_groups(input.groups)
        except OrganizationGroupNotAssignableError as exc:
            raise GraphQLError(str(exc)) from exc

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
        # When send_email=False the service suppresses the email and attaches the raw
        # token as invitation._raw_token (transient, never persisted in plaintext).
        #
        # invite_user_to_organization raises OverLimitError at the organization's seat
        # limit. Rendered identically to the REST 402 body via
        # raise_over_limit_graphql_error — the partner API is not a bypass.
        try:
            invitation = deps.organization_service.invite_user_to_organization(
                email=input.user_email,
                first_name="",
                last_name="",
                organization=target_org,
                invited_by=None,
                group=invited_group,
                send_email=input.send_email,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)

        raw_token: str | None = None
        invite_url: str | None = None

        if not input.send_email:
            # The service always attaches _raw_token; retrieve it once so it is not
            # inadvertently retained beyond this scope.
            raw_token = invitation._raw_token  # type: ignore[attr-defined]
            # Build the invite URL exactly as the branded email does -- keyed on the
            # branding root's slug (not target_org's directly), so a child
            # organization's invite carries its reseller's slug. See
            # organizations.invitation_urls.build_invitation_accept_url.
            branding_root = resolve_branding_for_display(target_org)
            invite_url = build_invitation_accept_url(
                branding_root.organization if branding_root else None, raw_token
            )

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

        # Create the system user and persist ResourceAccess rows (mirrors REST create).
        # create_system_user raises OverLimitError at the organization's
        # public_api_system_users ceiling. Caught outside the IntegrityError branch and
        # rendered via raise_over_limit_graphql_error (also rolls back the request
        # transaction -- see that function's docstring for why that matters under
        # ATOMIC_REQUESTS).
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
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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

        # Create the system user and resource-access rows atomically.
        # create_system_user raises OverLimitError at the organization's
        # public_api_system_users ceiling. Caught outside the IntegrityError branch and
        # rendered via raise_over_limit_graphql_error (also rolls back the request
        # transaction -- see that function's docstring for why that matters under
        # ATOMIC_REQUESTS).
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
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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
        Update or create branding for the acting organization.

        The mutation:
        1. When ``input.slug`` is supplied, validates it with the shared
           organization-slug rules and a uniqueness check (excluding the acting
           org itself) and applies it to the acting org BEFORE step 2 -- see
           ``UpdateBrandingInput.slug``'s docstring and ``_apply_input_slug``.
        2. Evaluates the shared branding write gate -- parentless and entitled
           (``organizations.permissions.evaluate_branding_write_gate``; its
           third, slug-set condition is retired -- see that function).
           Replaces the old ``can_invite_organizations``-only check
           (``assert_org_can_invite``): a reseller is not exempt from either
           condition. Raises a distinguishable ``GraphQLError`` per failed
           condition.
        3. Validates app_name: non-empty and max 120 characters.
        4. Validates primary_color and secondary_color format (#RRGGBB or #RRGGBBAA).
        5. Validates redirect_url: HTTPS scheme, no wildcard, no path-prefix pattern
           (organizations.redirect_url_validation, shared with the REST serializer).
        6. Upserts OrganizationBranding on the acting org only (always keyed to acting_org).
        7. Returns the upserted branding row (without internal fields like support_email/
           redirect_url).

        Steps 1 through 6 run inside one ``transaction.atomic()`` block: a
        rejected slug, a failed gate, or any field-validation failure rolls
        back everything this call did -- including the slug write -- rather
        than partially applying.

        This upsert is audited: every raise above (slug rejection, gate
        failure, field validation) happens before the upsert and rolls back
        the whole atomic block, so a refused write never reaches the audit
        call below -- nothing is recorded for it. A
        first-time upsert records a CREATE with no diff; an upsert that
        replaces an existing row records an UPDATE with a diff naming only the
        fields that changed, using the before-state captured BEFORE the
        transaction starts. Actor is the token's system user
        (``AuditService.actor_from_system_user``), matching the actor
        derivation already used by the BookingPolicy partner-API mutations.

        The token's OrganizationResourceAccess must include the BRANDING resource.
        """
        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        existing_branding = OrganizationBranding.objects.filter(organization=acting_org).first()
        before_state = (
            branding_diff_state(existing_branding) if existing_branding is not None else None
        )

        with transaction.atomic():
            # Omitted (``strawberry.UNSET``) means "leave the slug alone". An
            # explicit ``null`` or ``""`` is refused rather than treated as
            # omitted: the column is NOT NULL and the
            # ``organization_slug_not_blank`` constraint rejects a blank
            # value, so silently ignoring it would tell the caller it had
            # cleared an identifier that is in fact unchanged. This mirrors
            # ``OrganizationSerializer.validate_slug`` on the REST surface --
            # both refuse an explicit null/blank on update.
            if input.slug is not strawberry.UNSET:
                if not input.slug or not input.slug.strip():
                    raise GraphQLError(
                        "Slug cannot be cleared once set. Send a new slug, or omit the "
                        "field to leave it unchanged."
                    )
                _apply_input_slug(acting_org, input.slug)

            gate_reason = evaluate_branding_write_gate(acting_org)
            if gate_reason is not BrandingWriteGateReason.OK:
                raise GraphQLError(_BRANDING_GATE_MESSAGES[gate_reason])

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

            # Validate redirect_url: HTTPS scheme, no wildcard, no path-prefix pattern.
            # Shared with the REST serializer's validate_redirect_url.
            try:
                validate_redirect_url(input.redirect_url)
            except DjangoValidationError as e:
                raise GraphQLError("; ".join(e.messages)) from e

            # `logo_url` is write-only here despite the name: normalize a bare key or a
            # full signed/public URL down to the bare S3 key -- the persisted value,
            # never a URL. See organizations.branding_logo.normalize_uploaded_logo_key.
            # Raises BrandingLogoUploadRejectedError if the normalized key falls
            # outside the branding_logos upload prefix (e.g. a key from another
            # destination in the shared media bucket) -- see BRANDING_LOGO_KEY_PREFIX.
            try:
                logo_key = normalize_uploaded_logo_key(input.logo_url)
            except BrandingLogoUploadRejectedError as e:
                raise GraphQLError(str(e)) from e

            # Upsert branding on the acting org (always acts on acting org, never another org)
            branding, created = OrganizationBranding.objects.update_or_create(
                organization=acting_org,
                defaults={
                    "app_name": input.app_name,
                    "logo": logo_key,
                    "primary_color": input.primary_color,
                    "secondary_color": input.secondary_color,
                    "support_email": input.support_email,
                    "redirect_url": input.redirect_url,
                },
            )

        audit_service = get_audit_service()
        actor = AuditService.actor_from_system_user(info.context.request.public_api_system_user)
        subject = audit_service.subject_from_instance(branding, label=branding.app_name)
        if created:
            audit_service.record(
                organization_id=acting_org.id,
                action=AuditAction.CREATE,
                actor=actor,
                subject=subject,
            )
        else:
            after_state = branding_diff_state(branding)
            diff = compute_diff(before_state or {}, after_state)
            audit_service.record(
                organization_id=acting_org.id,
                action=AuditAction.UPDATE,
                actor=actor,
                subject=subject,
                diff=diff,
            )

        # Return the branding without internal fields (no support_email, no redirect_url).
        # logo_url is a signed URL for the just-written logo (so the caller renders the
        # new image immediately, with no cache to invalidate), or the default-logo
        # delivery URL when the row has no logo -- see build_logo_display_url.
        branding_result = BrandingResult(
            id=branding.id,
            app_name=branding.app_name,
            logo_url=build_logo_display_url(branding, request=info.context.request),
            primary_color=branding.primary_color,
            secondary_color=branding.secondary_color,
        )

        return UpdateBrandingResult(branding=branding_result)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_branding_logo_upload(
        self,
        info: strawberry.Info,
        file_name: str,
        file_type: str,
        file_size: int,
    ) -> BrandingLogoUploadResult:
        """
        Mint a presigned S3 upload URL for the acting organization's branding logo.

        A REST-reachable equivalent of s3direct's own signing view, for partner-API
        callers that cannot POST to that Django-session-scoped `/s3direct/` endpoint
        directly -- but returns a presigned PUT URL instead of bare AWS credentials,
        so no credential ever reaches the caller. Gated on the branding eligibility
        helper -- the acting organization must have no parent and hold the `white_label_branding`
        entitlement -- evaluated against the acting organization directly
        (`is_branding_eligible_organization`), NOT the destination's own `auth`
        callable: that callable only ever receives a bare user (see
        `organizations.permissions.user_administers_branding_eligible_organization`),
        so this mutation gets the tighter, org-specific check its caller's token
        already carries via `OrganizationResourceAccess`.

        Content-type and size are re-validated against the `branding_logos`
        destination's own allowlist/size cap (PNG/JPEG/WebP only, size-capped) --
        rejected before any presigned URL is minted, naming the specific rule
        broken.

        The token's OrganizationResourceAccess must include the BRANDING resource.
        """
        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        if not is_branding_eligible_organization(acting_org):
            raise GraphQLError(
                "This organization is not eligible to manage branding: it must have "
                "no parent organization and hold the white-label branding entitlement."
            )

        try:
            payload = sign_branding_logo_upload(file_name, file_type, file_size)
        except BrandingLogoUploadRejectedError as e:
            raise GraphQLError(str(e)) from e

        return BrandingLogoUploadResult(**payload)

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

        # create_configuration raises OverLimitError at the organization's
        # webhook_subscriptions ceiling. Caught before the ValueError branch and rendered
        # via raise_over_limit_graphql_error (also rolls back the request transaction --
        # see that function's docstring for why that matters under ATOMIC_REQUESTS).
        try:
            configuration = deps.webhook_service.create_configuration(
                organization=org,
                event_type=input.event_type,
                url=input.url,
                headers=headers,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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

        # create_resource_calendar raises OverLimitError at the organization's
        # resource_calendars limit. Rendered identically to the REST 402 body via
        # raise_over_limit_graphql_error, which also rolls back the request transaction
        # (graphql-core swallows resolver exceptions and always returns 200).
        try:
            calendar = calendar_service.create_resource_calendar(
                name=input.name,
                # Calendar.description is NOT NULL (no null=True on the field); normalize None -> "" to avoid IntegrityError.
                description=input.description if input.description is not None else "",
                capacity=input.capacity,
                manage_available_windows=input.manage_available_windows,
                accepts_public_scheduling=not input.is_private,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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

        # CalendarService.create_calendar creates a PERSONAL calendar, which is not a
        # member of LimitedResource (only resource_calendars and bundle_calendars are
        # limited among calendar types) -- no OverLimitError check applies here. See
        # CalendarService.create_calendar's docstring.
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
    def update_resource_calendar(
        self,
        info: strawberry.Info,
        input: UpdateResourceCalendarInput,  # noqa: A002
    ) -> UpdateResourceCalendarResult:
        """Partially update a resource calendar (org-scoped).

        The mutation:
        1. Resolves the organization and initializes the calendar service via the system-user token.
        2. Delegates to CalendarService.update_resource_calendar with the supplied parameters.
           Only provided (non-None, non-UNSET) fields are written; omitted fields leave the
           calendar unchanged.
           is_private (when provided) is translated to accepts_public_scheduling = not is_private.
           capacity uses three states: omit (UNSET → _UNCHANGED sentinel, left unchanged),
           explicit null (None → clears capacity to unlimited), or integer (sets capacity).
        3. Returns the updated CalendarGraphQLType on success, or success=False + errorMessage
           on failure.

        The target calendar must be of type RESOURCE and have provider INTERNAL; synced
        calendars and non-RESOURCE types raise ValueError → success=False with the reason.
        Cross-org / missing calendar surfaces as "Calendar not found." (no existence leak).

        The token's OrganizationResourceAccess must include the UPDATE_RESOURCE_CALENDAR resource.
        """
        calendar_service, _org = _get_org_and_init_calendar_service(info)

        accepts_public_scheduling: bool | None = None
        if input.is_private is not None:
            accepts_public_scheduling = not input.is_private

        # Translate UNSET → the _UNCHANGED sentinel so the service knows to leave capacity alone.
        # An explicit None clears the capacity; an integer sets it.
        # mypy: _UNCHANGED is object() but CalendarService.update_resource_calendar accepts it as
        # the sentinel "int | None"; suppress the mismatch, matching the service's own annotation.
        capacity: int | None = (  # type: ignore[assignment]
            _UNCHANGED if input.capacity is strawberry.UNSET else input.capacity  # type: ignore[assignment]
        )

        try:
            calendar = calendar_service.update_resource_calendar(
                calendar_id=input.calendar_id,
                name=input.name,
                description=input.description,
                capacity=capacity,
                manage_available_windows=input.manage_available_windows,
                accepts_public_scheduling=accepts_public_scheduling,
                visibility=input.visibility,
            )
        except Calendar.DoesNotExist:
            return UpdateResourceCalendarResult(success=False, error_message="Calendar not found.")
        except (ValueError, DjangoValidationError, IntegrityError) as e:
            return UpdateResourceCalendarResult(success=False, error_message=str(e))

        return UpdateResourceCalendarResult(success=True, calendar=calendar)  # type: ignore[arg-type]

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

        # create_available_time raises OverLimitError at the organization's
        # availability_windows limit. Rendered identically to the REST 402 body via
        # raise_over_limit_graphql_error (also rolls back the request transaction).
        try:
            available_time = calendar_service.create_available_time(
                calendar=calendar,
                start_time=input.start_time,
                end_time=input.end_time,
                timezone=input.timezone,
                rrule_string=input.rrule_string,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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

        # A batch containing `create` operations raises OverLimitError when it would take
        # the organization past its availability_windows limit -- a batch of N creates is
        # exactly the kind of bulk path a single-window check would miss.
        try:
            available_times = calendar_service.batch_modify_available_times(
                calendar=calendar, operations=ops
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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
    def batch_upsert_group_scoped_availability_windows(
        self,
        info: strawberry.Info,
        input: BatchGroupScopedAvailabilityWindowsInput,  # noqa: A002
    ) -> BatchUpsertGroupScopedAvailabilityWindowsResult:
        """Apply an atomic create/update/delete batch of group-scoped
        availability windows within one group slot's roster.

        Mirrors ``batchUpdateAvailabilityWindows``'s all-or-nothing and
        over-limit behavior exactly (same transaction/entitlement structure,
        same ``OverLimitError`` -> ``raise_over_limit_graphql_error`` render),
        with one addition: a ``create`` operation is an upsert — an identical
        replay of the same batch (spec UC-5: "the system replays the same
        batch after a network timeout") is a no-op, landing on the same final
        state rather than duplicating windows.

        The mutation:
        1. Resolves the organization and validates every operation's shape
           up front (no partial write on a malformed batch).
        2. Asserts every operation's ``calendarId`` is within the token
           owner's scope (no-op for org-wide tokens) — one guard per
           operation, since (unlike the base availability batch) this batch
           may span several calendars in the slot's roster. This only proves
           the token owns each op's ``calendarId``; it does not prove that an
           update/delete op's ``windowId`` belongs to that calendar.
        3. Delegates to ``CalendarGroupService.batch_upsert_group_scoped_availability_windows``,
           which resolves every touched row, cross-checks that an
           update/delete op's resolved window actually belongs to that op's
           own ``calendarId`` (closing the gap step 2 leaves open), checks the
           ``availability_windows`` plan limit against the batch's net
           genuine-create growth, and applies the whole batch inside its own
           transaction.
        4. Returns every group-scoped window in the slot's roster after the
           batch is applied.

        The token's OrganizationResourceAccess must include the
        BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS resource.
        """
        _valid_actions = {"create", "update", "delete"}

        org = info.context.request.public_api_organization
        if not org:
            return BatchUpsertGroupScopedAvailabilityWindowsResult(
                success=False,
                error_message="Organization not found in request context.",
                windows=[],
            )
        if input.organization_id != org.id:
            return BatchUpsertGroupScopedAvailabilityWindowsResult(
                success=False,
                error_message="Organization not found in request context.",
                windows=[],
            )

        # Validate all operations before touching anything -- fail fast, no writes.
        for op_input in input.operations:
            if op_input.action not in _valid_actions:
                return BatchUpsertGroupScopedAvailabilityWindowsResult(
                    success=False,
                    error_message=f"Invalid operation action: {op_input.action}",
                    windows=[],
                )
            if op_input.action == "create" and (
                op_input.start_time is None
                or op_input.end_time is None
                or op_input.timezone is None
            ):
                return BatchUpsertGroupScopedAvailabilityWindowsResult(
                    success=False,
                    error_message="create operation requires startTime, endTime, and timezone",
                    windows=[],
                )
            if op_input.action in ("update", "delete") and op_input.window_id is None:
                return BatchUpsertGroupScopedAvailabilityWindowsResult(
                    success=False,
                    error_message=f"{op_input.action} operation requires windowId",
                    windows=[],
                )

        # Owner-scope guard per operation's calendarId -- reveals nothing about
        # existence for a calendar outside a scoped token's owner set. Checked
        # for EVERY operation up front, so a cross-owner op anywhere in the
        # batch rejects the whole thing before any service call. This only
        # proves the token owns op.calendarId; it does NOT prove that an
        # update/delete op's windowId actually resolves to that calendar --
        # CalendarGroupService.batch_upsert_group_scoped_availability_windows
        # cross-checks that itself (window.calendar_fk_id == op.calendar_id)
        # before applying anything, so a token can't pair a calendarId it owns
        # with a windowId belonging to a different calendar.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        try:
            for op_input in input.operations:
                assert_calendar_in_owner_scope(system_user, org, op_input.calendar_id)
        except Calendar.DoesNotExist:
            return BatchUpsertGroupScopedAvailabilityWindowsResult(
                success=False, error_message="Calendar not found.", windows=[]
            )

        ops: list[dict[str, object]] = []
        for op_input in input.operations:
            op: dict[str, object] = {"action": op_input.action, "calendar_id": op_input.calendar_id}
            if op_input.window_id is not None:
                op["window_id"] = op_input.window_id
            if op_input.start_time is not None:
                op["start_time"] = op_input.start_time
            if op_input.end_time is not None:
                op["end_time"] = op_input.end_time
            if op_input.timezone is not None:
                op["timezone"] = op_input.timezone
            if op_input.rrule_string is not None:
                op["rrule_string"] = op_input.rrule_string
            ops.append(op)

        deps = get_calendar_mutation_dependencies()
        deps.calendar_group_service.initialize(organization=org)

        if system_user is None:
            return BatchUpsertGroupScopedAvailabilityWindowsResult(
                success=False,
                error_message="Organization not found in request context.",
                windows=[],
            )

        try:
            windows = deps.calendar_group_service.batch_upsert_group_scoped_availability_windows(
                group_slot_id=input.group_slot_id,
                operations=ops,
                acting_principal=system_user,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
        except CalendarGroupSlotConfigNotFoundError:
            return BatchUpsertGroupScopedAvailabilityWindowsResult(
                success=False, error_message="Group slot not found.", windows=[]
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError) as e:
            return BatchUpsertGroupScopedAvailabilityWindowsResult(
                success=False, error_message=str(e), windows=[]
            )

        return BatchUpsertGroupScopedAvailabilityWindowsResult(
            success=True,
            windows=[group_scoped_availability_window_from_model(w) for w in windows],
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def batch_upsert_group_scoped_blocked_times(
        self,
        info: strawberry.Info,
        input: BatchGroupScopedBlockedTimesInput,  # noqa: A002
    ) -> BatchUpsertGroupScopedBlockedTimesResult:
        """Apply an atomic create/update/delete batch of group-scoped blocked
        times within one group slot's roster.

        Direct mirror of ``batchUpsertGroupScopedAvailabilityWindows`` -- same
        validation, owner-scope, and IDOR cross-check structure -- with ONE
        deliberate difference: blocked time is not metered yet, so this
        mutation never surfaces an ``OverLimitError`` for a plan-limit
        ceiling; ``CalendarGroupService.batch_upsert_group_scoped_blocked_times``
        still enforces ``check_not_restricted`` (a ``RESTRICTED`` billing
        root still blocks the write), but there is no delta/limit check.

        The token's OrganizationResourceAccess must include the
        BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES resource.
        """
        _valid_actions = {"create", "update", "delete"}

        org = info.context.request.public_api_organization
        if not org:
            return BatchUpsertGroupScopedBlockedTimesResult(
                success=False,
                error_message="Organization not found in request context.",
                blocked_times=[],
            )
        if input.organization_id != org.id:
            return BatchUpsertGroupScopedBlockedTimesResult(
                success=False,
                error_message="Organization not found in request context.",
                blocked_times=[],
            )

        # Validate all operations before touching anything -- fail fast, no writes.
        for op_input in input.operations:
            if op_input.action not in _valid_actions:
                return BatchUpsertGroupScopedBlockedTimesResult(
                    success=False,
                    error_message=f"Invalid operation action: {op_input.action}",
                    blocked_times=[],
                )
            if op_input.action == "create" and (
                op_input.start_time is None
                or op_input.end_time is None
                or op_input.timezone is None
            ):
                return BatchUpsertGroupScopedBlockedTimesResult(
                    success=False,
                    error_message="create operation requires startTime, endTime, and timezone",
                    blocked_times=[],
                )
            if op_input.action in ("update", "delete") and op_input.block_id is None:
                return BatchUpsertGroupScopedBlockedTimesResult(
                    success=False,
                    error_message=f"{op_input.action} operation requires blockId",
                    blocked_times=[],
                )

        # Owner-scope guard per operation's calendarId -- reveals nothing about
        # existence for a calendar outside a scoped token's owner set. Checked
        # for EVERY operation up front, so a cross-owner op anywhere in the
        # batch rejects the whole thing before any service call. This only
        # proves the token owns op.calendarId; it does NOT prove that an
        # update/delete op's blockId actually resolves to that calendar --
        # CalendarGroupService.batch_upsert_group_scoped_blocked_times
        # cross-checks that itself (block.calendar_fk_id == op.calendar_id)
        # before applying anything, so a token can't pair a calendarId it owns
        # with a blockId belonging to a different calendar.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        try:
            for op_input in input.operations:
                assert_calendar_in_owner_scope(system_user, org, op_input.calendar_id)
        except Calendar.DoesNotExist:
            return BatchUpsertGroupScopedBlockedTimesResult(
                success=False, error_message="Calendar not found.", blocked_times=[]
            )

        ops: list[dict[str, object]] = []
        for op_input in input.operations:
            op: dict[str, object] = {"action": op_input.action, "calendar_id": op_input.calendar_id}
            if op_input.block_id is not None:
                op["block_id"] = op_input.block_id
            if op_input.start_time is not None:
                op["start_time"] = op_input.start_time
            if op_input.end_time is not None:
                op["end_time"] = op_input.end_time
            if op_input.timezone is not None:
                op["timezone"] = op_input.timezone
            if op_input.reason is not None:
                op["reason"] = op_input.reason
            if op_input.rrule_string is not None:
                op["rrule_string"] = op_input.rrule_string
            ops.append(op)

        deps = get_calendar_mutation_dependencies()
        deps.calendar_group_service.initialize(organization=org)

        if system_user is None:
            return BatchUpsertGroupScopedBlockedTimesResult(
                success=False,
                error_message="Organization not found in request context.",
                blocked_times=[],
            )

        try:
            blocks = deps.calendar_group_service.batch_upsert_group_scoped_blocked_times(
                group_slot_id=input.group_slot_id,
                operations=ops,
                acting_principal=system_user,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
        except CalendarGroupSlotConfigNotFoundError:
            return BatchUpsertGroupScopedBlockedTimesResult(
                success=False, error_message="Group slot not found.", blocked_times=[]
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError) as e:
            return BatchUpsertGroupScopedBlockedTimesResult(
                success=False, error_message=str(e), blocked_times=[]
            )

        return BatchUpsertGroupScopedBlockedTimesResult(
            success=True,
            blocked_times=[group_scoped_blocked_time_from_model(b) for b in blocks],
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def batch_upsert_group_scoped_quota_rules(
        self,
        info: strawberry.Info,
        input: BatchGroupScopedQuotaRulesInput,  # noqa: A002
    ) -> BatchUpsertGroupScopedQuotaRulesResult:
        """Apply an atomic create/update/delete batch of group-scoped quota
        rules within one group slot's roster.

        Direct mirror of ``batchUpsertGroupScopedBlockedTimes`` -- same
        validation, owner-scope, and IDOR cross-check structure -- with two
        deliberate differences: quota rules are non-recurring (no
        startTime/endTime/timezone/rruleString, just period and cap) and are
        NOT metered (spec: "Windows and blocks both consume the limit; quota
        rules do not") -- this mutation never surfaces an ``OverLimitError``,
        and ``CalendarGroupService.batch_upsert_group_scoped_quota_rules``
        still enforces ``check_not_restricted`` (a ``RESTRICTED`` billing
        root still blocks the write) but there is no delta/limit check.

        The (calendar, slot, period) uniqueness constraint is surfaced as a
        clean ``success=False`` result (``CalendarGroupValidationError`` ->
        the ``CalendarIntegrationError`` branch below), never an unhandled
        server error.

        The token's OrganizationResourceAccess must include the
        BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES resource.
        """
        _valid_actions = {"create", "update", "delete"}

        org = info.context.request.public_api_organization
        if not org:
            return BatchUpsertGroupScopedQuotaRulesResult(
                success=False,
                error_message="Organization not found in request context.",
                quota_rules=[],
            )
        if input.organization_id != org.id:
            return BatchUpsertGroupScopedQuotaRulesResult(
                success=False,
                error_message="Organization not found in request context.",
                quota_rules=[],
            )

        # Validate all operations before touching anything -- fail fast, no writes.
        for op_input in input.operations:
            if op_input.action not in _valid_actions:
                return BatchUpsertGroupScopedQuotaRulesResult(
                    success=False,
                    error_message=f"Invalid operation action: {op_input.action}",
                    quota_rules=[],
                )
            if op_input.action == "create" and (op_input.period is None or op_input.cap is None):
                return BatchUpsertGroupScopedQuotaRulesResult(
                    success=False,
                    error_message="create operation requires period and cap",
                    quota_rules=[],
                )
            if op_input.action in ("update", "delete") and op_input.rule_id is None:
                return BatchUpsertGroupScopedQuotaRulesResult(
                    success=False,
                    error_message=f"{op_input.action} operation requires ruleId",
                    quota_rules=[],
                )

        # Owner-scope guard per operation's calendarId -- reveals nothing about
        # existence for a calendar outside a scoped token's owner set. Checked
        # for EVERY operation up front, so a cross-owner op anywhere in the
        # batch rejects the whole thing before any service call. This only
        # proves the token owns op.calendarId; it does NOT prove that an
        # update/delete op's ruleId actually resolves to that calendar --
        # CalendarGroupService.batch_upsert_group_scoped_quota_rules
        # cross-checks that itself (rule.calendar_fk_id == op.calendar_id)
        # before applying anything, so a token can't pair a calendarId it owns
        # with a ruleId belonging to a different calendar.
        request: PublicApiHttpRequest = info.context.request
        system_user = request.public_api_system_user
        try:
            for op_input in input.operations:
                assert_calendar_in_owner_scope(system_user, org, op_input.calendar_id)
        except Calendar.DoesNotExist:
            return BatchUpsertGroupScopedQuotaRulesResult(
                success=False, error_message="Calendar not found.", quota_rules=[]
            )

        ops: list[dict[str, object]] = []
        for op_input in input.operations:
            op: dict[str, object] = {"action": op_input.action, "calendar_id": op_input.calendar_id}
            if op_input.rule_id is not None:
                op["rule_id"] = op_input.rule_id
            if op_input.period is not None:
                op["period"] = op_input.period
            if op_input.cap is not None:
                op["cap"] = op_input.cap
            ops.append(op)

        deps = get_calendar_mutation_dependencies()
        deps.calendar_group_service.initialize(organization=org)

        if system_user is None:
            return BatchUpsertGroupScopedQuotaRulesResult(
                success=False,
                error_message="Organization not found in request context.",
                quota_rules=[],
            )

        try:
            rules = deps.calendar_group_service.batch_upsert_group_scoped_quota_rules(
                group_slot_id=input.group_slot_id,
                operations=ops,
                acting_principal=system_user,
            )
        except OverLimitError as exc:
            # Not a plan-limit ceiling (quota rules are unmetered) -- this is
            # `check_not_restricted`'s RESTRICTED-billing-root guard, which
            # raises the same `OverLimitError` subclass and renders through
            # the same GraphQL error shape every other guarded write uses.
            raise_over_limit_graphql_error(exc)
        except CalendarGroupSlotConfigNotFoundError:
            return BatchUpsertGroupScopedQuotaRulesResult(
                success=False, error_message="Group slot not found.", quota_rules=[]
            )
        except (CalendarIntegrationError, ValueError, DjangoValidationError) as e:
            return BatchUpsertGroupScopedQuotaRulesResult(
                success=False, error_message=str(e), quota_rules=[]
            )

        return BatchUpsertGroupScopedQuotaRulesResult(
            success=True,
            quota_rules=[group_scoped_quota_rule_from_model(r) for r in rules],
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

        # create_bundle_calendar raises OverLimitError at the organization's
        # bundle_calendars limit. Rendered identically to the REST 402 body via
        # raise_over_limit_graphql_error (also rolls back the request transaction).
        try:
            bundle = calendar_service.create_bundle_calendar(
                name=input.name,
                # Calendar.description is NOT NULL; normalize None -> "" to avoid IntegrityError.
                description=input.description if input.description is not None else "",
                child_calendars=children,
                primary_calendar=primary,
                accepts_public_scheduling=not input.is_private,
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
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
                        external_client_identifiers=_map_external_client_identifiers(
                            external.external_client_identifiers
                        ),
                    )
                )
                for external in input.external_attendees
            ],
            resource_allocations=[],
            recurrence_rule=input.rrule_string,
            external_client_identifiers=_map_external_client_identifiers(
                input.external_client_identifiers
            ),
        )

        try:
            event = calendar_service.create_event(input.calendar_id, event_input)
        except Calendar.DoesNotExist as exc:
            # A race / direct service-level not-found must stay indistinguishable.
            raise GraphQLError("Calendar not found.") from exc
        except NoAvailableTimeWindowsError as exc:
            raise GraphQLError("No available time window covers the requested event time.") from exc
        except BookingPolicyViolationError as exc:
            raise GraphQLError(
                str(exc)
                or "The requested time slot is not available under the current booking policy."
            ) from exc
        except PermissionDenied as exc:
            raise GraphQLError(
                str(exc) or "You do not have permission to schedule this event."
            ) from exc
        # create_event raises OverLimitError at the organization's postpaid
        # event_occurrences allowance (no payment method on file); rendered via
        # raise_over_limit_graphql_error (also rolls back the request transaction --
        # see its docstring).
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
        # IntegrityError: a (system, identifier) pair already claimed by another record
        # of the same type in this organization -- ExternalClientIdentifierService
        # writes via bulk_create, so the DB's extclientid_uniq_system_ident constraint
        # is the enforcement point, not a pre-check. create_event runs inside its own
        # @transaction.atomic() savepoint, so catching this here does not poison the
        # request-level transaction (ATOMIC_REQUESTS) -- see _apply_input_slug's
        # docstring for the same pattern.
        except (ValueError, DjangoValidationError, CalendarIntegrationError, IntegrityError) as exc:
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
           overriding start/end/timezone and the recurrence rule.

           - **Series-preserving sub-case** (``input.rrule_string`` is None): the master's
             existing rule string is re-passed so ``update_event`` does not silently strip the
             series.
           - **Explicit-new-rule sub-case** (``input.rrule_string`` provided): the new rule
             replaces the existing one.

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

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_calendar_event(
        self,
        info: strawberry.Info,
        input: UpdateCalendarEventInput,  # noqa: A002
    ) -> CalendarEventGraphQLType:
        """Update a single-calendar event's title, description, attendees and identifiers.

        Every field on ``UpdateCalendarEventInput`` besides ``event_id`` is
        ``strawberry.UNSET``-defaulted: an omitted field is left exactly as stored, so a
        caller that supplies only ``title`` cannot accidentally wipe attendees or
        identifiers by having them collapse to an empty list. See that input's
        docstring for the full per-field contract.

        Times, timezone and recurrence are NOT part of this mutation -- they stay owned
        by ``rescheduleCalendarEvent``. This resolver always re-passes the event's
        current start/end/timezone/recurrence rule to ``CalendarEventService.update_event``
        unchanged.

        Authorization:
        - Owner-scoped token: ``assert_calendar_in_owner_scope`` restricts to events on
          calendars owned by the token's owner; a cross-owner ``event_id`` raises the
          identical ``"Event not found."`` error a genuinely missing event would, so
          existence is never leaked. The service independently re-verifies ownership as
          defense-in-depth.
        - Org-wide token: the scope guard is a no-op -> acts org-wide.

        The token's ``OrganizationResourceAccess`` must include the ``CALENDAR_EVENT``
        resource.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        # Load the event first (organization-scoped only -- this mutation has no
        # calendar_id in its input) so the owner-scope guard below can be checked
        # against the event's OWN calendar. A missing event and a cross-owner event
        # both end up producing the exact same "Event not found." message.
        try:
            existing_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar", "recurrence_rule")
                # ``attendances`` is deliberately NOT prefetched: since this
                # resolver stopped reconstructing the internal attendee list (an
                # omitted field now goes to update_event as ``None``), nothing here
                # reads it, and prefetching it would cost a query per request for
                # a relation no branch touches.
                .prefetch_related(
                    "external_attendances__external_attendee",
                    "resource_allocations",
                )
                .get(id=input.event_id)
            )
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc

        # Sentinel emitted by CalendarEventService.update_event's
        # _public_token_may_write when a scoped-admin token's membership doesn't
        # independently own the target calendar. assert_calendar_in_owner_scope below
        # is a no-op for scoped_admin (scoped_calendar_ids returns None -- unrestricted
        # -- for that scope), so this is the only place that check can still surface;
        # remapped to the uniform not-found message to prevent a discriminating oracle.
        calendar_not_found_sentinel = "Calendar matching query does not exist."

        try:
            # Owner-scope guard (defense in depth): a scoped token may only target
            # events on its owner's calendars. Raises Calendar.DoesNotExist when the
            # event's calendar is outside that scope -- caught and remapped to the
            # SAME "Event not found." message used above, so a cross-owner attempt is
            # indistinguishable from a genuinely missing event.
            assert_calendar_in_owner_scope(
                request.public_api_system_user, org, existing_event.calendar_fk_id
            )
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc

        # Bundle primary events: CalendarEventService.update_event returns EARLY (via
        # _update_bundle_event) before the identifier replace step -- so
        # externalClientIdentifiers would be silently ignored for these events,
        # breaking this mutation's "clearing identifiers with [] removes exactly
        # those rows" contract. Reject explicitly rather than silently ignoring the
        # field. This also prevents this resolver from ever reaching
        # CalendarBundleService.update_bundle_event's callback into update_event on
        # an is_bundle_primary event.
        if existing_event.is_bundle_primary:
            raise GraphQLError("updateCalendarEvent does not support bundle primary events.")

        # --- title -----------------------------------------------------------
        # Omitted -> ``None``, passed straight through: CalendarEventInputData is
        # tri-state, so update_event skips the assignment entirely. This resolver
        # deliberately does NOT read the stored title and re-send it -- doing so
        # (as it did before Phase 7) would overwrite a value a concurrent update
        # may have changed between this request's read and its commit.
        if input.title is strawberry.UNSET:
            title = None
        elif input.title is None:
            raise GraphQLError("Title cannot be null.")
        else:
            if len(input.title) > EVENT_TITLE_MAX_LENGTH:
                raise GraphQLError(f"Title must be at most {EVENT_TITLE_MAX_LENGTH} characters.")
            title = input.title

        # --- description -------------------------------------------------------
        if input.description is strawberry.UNSET:
            description = None
        else:
            # Explicit null is "supplied, nothing there" -> clears to "", matching
            # scheduleEvent's own `input.description or ""` convention.
            description = input.description or ""

        # --- internal attendees --------------------------------------------------
        attendee_user_ids: list[int] | None
        if input.attendee_user_ids is strawberry.UNSET:
            # Untouched: ``None`` reaches update_event, which skips the internal
            # attendance reconciliation outright -- no read, no create, no delete,
            # and nothing for a concurrent attendee write to lose.
            attendee_user_ids = None
        else:
            # Supplied (including [] / null): replaces the full set. De-duplicate
            # first so a repeated id doesn't skew the membership count check.
            attendee_user_ids = list(dict.fromkeys(input.attendee_user_ids or []))
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

        # --- external attendees ---------------------------------------------------
        external_attendances: list[EventExternalAttendanceInputData] | None
        if input.external_attendees is strawberry.UNSET:
            # Untouched: ``None`` reaches update_event, which skips the external
            # attendance reconciliation outright. Every stored attendee, and every
            # identifier hanging off it, is left exactly as it is -- and no
            # CALENDAR_EVENT_ATTENDEE_UPDATED webhook fires for any of them, which
            # the pre-Phase-7 re-send of each existing attendee (id included, so
            # every one landed on update_event's update-in-place branch) did emit.
            external_attendances = None
        else:
            # Supplied (including [] / null): replaces the full set. ScheduleEvent-
            # ExternalAttendeeInput carries no id (schedule_event only ever creates),
            # so an attendee surviving this replace is identified by EMAIL against the
            # event's current external attendees. A matched email carries its existing
            # external_attendee id through -- keeping it on update_event's "update in
            # place" path so its stored identifiers survive untouched unless this
            # entry's own external_client_identifiers is explicitly supplied. An
            # unmatched email is a genuinely new attendee; an existing email absent
            # from the new list is removed (fires the attendee-removed webhook).
            # Keys are normalized (stripped + lowercased) so a case or whitespace
            # difference in the caller's payload doesn't miss an existing row --
            # mirroring calendar_permission_service.py's identical normalization. A
            # miss here routes the entry down update_event's create branch while the
            # existing row is hard-deleted, cascading away its ExternalClientIdentifier
            # rows.
            existing_external_attendee_by_email = {
                ea.external_attendee.email.strip().lower(): ea.external_attendee
                for ea in existing_event.external_attendances.all()
                if ea.external_attendee_fk_id is not None
            }

            # Reject duplicate normalized emails up front: two entries resolving to
            # the same external_attendee_fk_id would both take the "update in place"
            # branch on the same row, so the second bulk_update/replace_for_target
            # silently overwrites the first and the caller gets one attendee back
            # after asking for two.
            supplied_normalized_emails = [
                external.email.strip().lower() for external in (input.external_attendees or [])
            ]
            duplicate_emails = {
                email
                for email in supplied_normalized_emails
                if supplied_normalized_emails.count(email) > 1
            }
            if duplicate_emails:
                raise GraphQLError(
                    "externalAttendees contains duplicate email addresses (case/"
                    "whitespace-insensitive): " + ", ".join(sorted(duplicate_emails))
                )

            external_attendances = []
            for external in input.external_attendees or []:
                matched_attendee = existing_external_attendee_by_email.get(
                    external.email.strip().lower()
                )
                external_attendances.append(
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            id=matched_attendee.id if matched_attendee else None,
                            email=external.email,
                            # An empty supplied name on a matched attendee falls back to
                            # the stored name rather than blanking it -- `name` defaults
                            # to "" (not strawberry.UNSET), so a caller supplying only
                            # `{email}` to mean "keep this attendee, touch nothing else"
                            # would otherwise silently wipe the stored name.
                            name=external.name
                            or (matched_attendee.name if matched_attendee else ""),
                            external_client_identifiers=_map_external_client_identifiers(
                                external.external_client_identifiers
                            ),
                        )
                    )
                )

        # --- recurrence rule: always preserved, never part of this mutation --------
        recurrence_rule = (
            existing_event.recurrence_rule.to_rrule_string()
            if existing_event.is_recurring
            else None
        )

        # --- resource allocations: always preserved, not part of this mutation -----
        resource_allocations = [
            ResourceAllocationInputData(resource_id=ra.calendar_fk_id)  # type: ignore[arg-type]
            for ra in existing_event.resource_allocations.all()
            if ra.calendar_fk_id
        ]

        event_data = CalendarEventInputData(
            title=title,
            description=description,
            # Times/timezone are NOT part of this mutation -- always re-pass the
            # event's own current values unchanged.
            start_time=existing_event.start_time,
            end_time=existing_event.end_time,
            timezone=existing_event.timezone,
            attendances=(
                None
                if attendee_user_ids is None
                else [EventAttendanceInputData(user_id=user_id) for user_id in attendee_user_ids]
            ),
            external_attendances=external_attendances,
            resource_allocations=resource_allocations,
            recurrence_rule=recurrence_rule,
            external_client_identifiers=_map_external_client_identifiers(
                input.external_client_identifiers
            ),
        )

        try:
            event = calendar_service.update_event(
                existing_event.calendar_fk_id, input.event_id, event_data
            )
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc
        except PermissionDenied as exc:
            # Defense-in-depth: update_event raises PermissionDenied with the sentinel
            # message when a scoped-admin (or racing cross-owner) SystemUser's
            # calendar-ownership check fails. Surface it as the uniform not-found
            # rather than the distinct sentinel -- see the comment on
            # calendar_not_found_sentinel above.
            if str(exc) == calendar_not_found_sentinel:
                raise GraphQLError("Event not found.") from exc
            raise GraphQLError(
                str(exc) or "You do not have permission to update this event."
            ) from exc
        except (ValueError, DjangoValidationError, CalendarIntegrationError) as exc:
            raise GraphQLError(str(exc)) from exc
        except IntegrityError as exc:
            # A (system, identifier) pair already claimed by another record of the
            # same type in this organization. Do NOT surface str(exc) here -- it
            # leaks the DB constraint name, column tuple, and internal
            # organization_id/content_type_id values to an external API token.
            raise GraphQLError(
                "That (system, identifier) pair is already in use by another record."
            ) from exc

        return event  # type: ignore[return-value]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def reschedule_calendar_group_event(
        self,
        info: strawberry.Info,
        input: RescheduleCalendarGroupEventInput,  # noqa: A002
    ) -> CalendarEventGraphQLType:
        """Reschedule a grouped event's times while preserving all other details.

        Moves the primary ``CalendarEvent`` on the primary calendar AND the linked
        non-primary ``BlockedTime`` rows (identified by the
        ``group-event-{event_id}-cal-{cid}`` external_id convention) to the new
        start/end/timezone simultaneously.

        Whole-event only — group events are not recurring in v1 (no ``recurrenceId``).

        Authorization:
        - Owner-scoped token: restricted to grouped events whose primary calendar is
          owned by the token's owner; cross-owner → ``"Event not found."`` (same
          as missing event — no existence leak).
        - Org-wide token: acts org-wide.
        - The service independently re-verifies ownership as defense-in-depth.

        Returns the updated primary ``CalendarEvent``.
        """
        # Sentinel emitted by CalendarEventService.update_event when a SystemUser's
        # scoped calendar can't be found (racing cross-owner path). Map it to the
        # uniform not-found message to prevent a discriminating oracle.
        calendar_not_found_sentinel = "Calendar matching query does not exist."

        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        # input.organization_id is intentionally ignored: org is derived from the
        # authenticated request context, not from caller-supplied input.

        # Load the grouped event scoped to the organization to derive the primary
        # calendar and validate that it is actually a grouped event.
        try:
            grouped_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar")
                .get(id=input.event_id)
            )
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc

        if grouped_event.calendar_group_fk_id is None:
            # Non-grouped event — uniform not-found, no existence leak.
            raise GraphQLError("Event not found.")

        primary_calendar_id: int = grouped_event.calendar_fk_id  # type: ignore[assignment]

        # Owner-scope guard on the PRIMARY calendar: a scoped token may only target
        # grouped events whose primary calendar its owner owns. The grouped event was
        # already loaded org-scoped with select_related("calendar"), so the derived
        # primary calendar provably exists — only the ownership check can fail here.
        # Map Calendar.DoesNotExist (cross-owner) to "Event not found." — uniform
        # not-found, no existence leak.
        try:
            assert_calendar_in_owner_scope(request.public_api_system_user, org, primary_calendar_id)
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc

        # Wire the group service: inject the already-initialized calendar_service so
        # that the public-token auth context flows into reschedule_grouped_event →
        # update_event. This mirrors exactly how rescheduleCalendarGroupEventWithCode
        # wires its deps (deps.calendar_group_service.calendar_service = deps.calendar_service).
        group_deps = get_calendar_mutation_dependencies()
        group_deps.calendar_group_service.calendar_service = calendar_service
        group_deps.calendar_group_service.initialize(organization=org)

        try:
            event = group_deps.calendar_group_service.reschedule_grouped_event(
                event_id=input.event_id,
                start_time=input.start_time,
                end_time=input.end_time,
                tz=input.timezone,
            )
        except Calendar.DoesNotExist as exc:
            # Derived-calendar miss must not leak existence either (caller addresses by event).
            raise GraphQLError("Event not found.") from exc
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc
        except PermissionDenied as exc:
            # Defense-in-depth: update_event raises PermissionDenied with the sentinel
            # message when a racing cross-owner SystemUser calendar lookup fails.
            # Surface it as the uniform not-found rather than the distinct sentinel.
            if str(exc) == calendar_not_found_sentinel:
                raise GraphQLError("Event not found.") from exc
            raise GraphQLError(
                str(exc) or "You do not have permission to reschedule this event."
            ) from exc
        except CalendarGroupValidationError as exc:
            raise GraphQLError(str(exc)) from exc
        except (ValueError, DjangoValidationError, CalendarIntegrationError) as exc:
            raise GraphQLError(str(exc)) from exc

        return event  # type: ignore[return-value]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def cancel_event(
        self,
        info: strawberry.Info,
        input: CancelEventInput,  # noqa: A002
    ) -> CancelEventResult:
        """Cancel a single-calendar or grouped event, an entire series, or one occurrence.

        Three distinct execution paths share a single resolver:

        1. **Single-occurrence** (``input.recurrence_id`` is set): delegates to
           ``CalendarService.cancel_event_occurrence``, which creates a cancellation
           ``EventRecurrenceException`` (``is_cancelled=True``) for that occurrence without
           touching the master event or the series rule.

        2. **Grouped event** (``event.calendar_group_fk_id is not None``, no ``recurrence_id``):
           delegates to ``CalendarGroupService.cancel_grouped_event``, which deletes the
           primary ``CalendarEvent`` AND the linked non-primary ``BlockedTime`` rows
           identified by the ``group-event-{event_id}-cal-{cid}`` external_id convention.
           ``input.delete_series`` is forwarded to the group service.

        3. **Single-calendar event** (no ``recurrence_id``, not grouped): delegates to
           ``CalendarService.delete_event`` with ``delete_series=input.delete_series``.

           .. warning::
               For a recurring master, passing ``delete_series=False`` (default) deletes
               the master event row outright — not a silent no-op, but also not an entire
               series wipe.  To cancel one occurrence use ``recurrence_id``; to delete the
               whole series set ``delete_series=True``.

        Authorization:
        - Owner-scoped token: ``assert_calendar_in_owner_scope`` restricts to calendars
          owned by the token's owner; cross-owner → ``"Calendar not found."`` (same as
          missing — no existence leak).  This is correct here because the input is
          calendar-addressed (``calendar_id`` is explicit), so "Calendar not found." for
          a cross-owner calendar_id is the intended uniform response.
        - Org-wide token: ``assert_calendar_in_owner_scope`` is a no-op → acts org-wide.
        - The service independently re-verifies ownership as defense-in-depth.
        """
        calendar_service, org = _get_org_and_init_calendar_service(info)
        request: PublicApiHttpRequest = info.context.request

        # Owner-scope guard (calendar-addressed input): a scoped token may only target
        # its owner's calendars.  Cross-owner and genuinely missing calendars return the
        # identical error — no existence leak.
        try:
            assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)
            Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Calendar not found.") from exc

        # Load the event — needed to detect grouped-ness before branching.
        try:
            event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar")
                .get(id=input.event_id, calendar_fk_id=input.calendar_id)
            )
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc

        try:
            if input.recurrence_id is not None:
                # Single-occurrence path: create / update a cancellation exception for
                # exactly this occurrence; master and series rule are left intact.
                calendar_service.cancel_event_occurrence(
                    calendar_id=input.calendar_id,
                    master_event_id=input.event_id,
                    recurrence_id=input.recurrence_id,
                )
            elif event.calendar_group_fk_id is not None:
                # Grouped-event path: wire the group service with the already-initialized
                # calendar_service so that the public-token auth context flows through
                # cancel_grouped_event → delete_event.  Mirrors how cancel_event_with_code
                # and reschedule_calendar_group_event wire their deps.
                group_deps = get_calendar_mutation_dependencies()
                group_deps.calendar_group_service.calendar_service = calendar_service
                group_deps.calendar_group_service.initialize(organization=org)
                group_deps.calendar_group_service.cancel_grouped_event(
                    event_id=input.event_id,
                    delete_series=input.delete_series,
                )
            else:
                # Single-calendar path.
                calendar_service.delete_event(
                    calendar_id=input.calendar_id,
                    event_id=input.event_id,
                    delete_series=input.delete_series,
                )
        except Calendar.DoesNotExist as exc:
            raise GraphQLError("Calendar not found.") from exc
        except CalendarEvent.DoesNotExist as exc:
            raise GraphQLError("Event not found.") from exc
        except PermissionDenied as exc:
            raise GraphQLError(
                str(exc) or "You do not have permission to cancel this event."
            ) from exc
        except CalendarGroupValidationError as exc:
            raise GraphQLError(str(exc)) from exc
        except (ValueError, DjangoValidationError, CalendarIntegrationError) as exc:
            raise GraphQLError(str(exc)) from exc

        return CancelEventResult(success=True)

    # ------------------------------------------------------------------
    # BookingPolicy mutations
    # ------------------------------------------------------------------

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_booking_policy(
        self,
        info: strawberry.Info,
        input: CreateBookingPolicyInput,  # noqa: A002
    ) -> BookingPolicyResult:
        """Create a new BookingPolicy for the caller's organization.

        Exactly one of ``calendar_id``, ``membership_user_id``,
        ``calendar_group_id``, or ``is_organization_default=True`` must be set.
        Returns an error when a policy already exists for the given target.
        Write is audited via ``BookingPolicyService``.

        The token's ``OrganizationResourceAccess`` must include the
        ``BOOKING_POLICY`` resource.
        """
        org = info.context.request.public_api_organization
        if not org:
            raise GraphQLError("Organization not found in request context")

        request: PublicApiHttpRequest = info.context.request
        service = get_booking_policy_mutation_dependencies()
        service.initialize(org)
        actor = AuditService.actor_from_system_user(request.public_api_system_user)
        service.set_actor(actor)

        # Owner-scope enforcement: a membership-scoped token may only manage its
        # own calendar / membership policies; org-wide tokens are unrestricted.
        permission_service = get_booking_policy_permission_service()
        if not permission_service.can_system_user_manage_target(
            system_user=request.public_api_system_user,
            organization_id=org.id,
            calendar_id=input.calendar_id,
            membership_user_id=input.membership_user_id,
            calendar_group_id=input.calendar_group_id,
            is_organization_default=input.is_organization_default,
        ):
            raise GraphQLError(
                "You do not have permission to manage a booking policy for this target."
            )

        # Resolve optional FK targets.
        calendar: Calendar | None = None
        if input.calendar_id is not None:
            try:
                calendar = Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
            except Calendar.DoesNotExist as exc:
                raise GraphQLError("Calendar not found.") from exc

        calendar_group: CalendarGroup | None = None
        if input.calendar_group_id is not None:
            try:
                calendar_group = CalendarGroup.objects.filter_by_organization(org.id).get(
                    id=input.calendar_group_id
                )
            except CalendarGroup.DoesNotExist as exc:
                raise GraphQLError("Calendar group not found.") from exc

        try:
            policy = service.create_booking_policy(
                calendar=calendar,
                membership_user_id=input.membership_user_id,
                calendar_group=calendar_group,
                is_organization_default=input.is_organization_default,
                lead_time_seconds=input.lead_time_seconds,
                max_horizon_seconds=input.max_horizon_seconds,
                buffer_before_seconds=input.buffer_before_seconds,
                buffer_after_seconds=input.buffer_after_seconds,
            )
        except DuplicateBookingPolicyError as exc:
            raise GraphQLError(str(exc)) from exc
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

        return BookingPolicyResult(success=True, policy=policy)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_booking_policy(
        self,
        info: strawberry.Info,
        input: UpdateBookingPolicyInput,  # noqa: A002
    ) -> BookingPolicyResult:
        """Update the rule fields of an existing BookingPolicy (org-scoped).

        Target fields are immutable. Only the four rule second-count fields may
        be changed; any field supplied as ``None`` is left unchanged.
        Write is audited via ``BookingPolicyService``.

        The token's ``OrganizationResourceAccess`` must include the
        ``BOOKING_POLICY`` resource.
        """
        org = info.context.request.public_api_organization
        if not org:
            raise GraphQLError("Organization not found in request context")

        request: PublicApiHttpRequest = info.context.request
        service = get_booking_policy_mutation_dependencies()
        service.initialize(org)
        actor = AuditService.actor_from_system_user(request.public_api_system_user)
        service.set_actor(actor)

        try:
            policy = BookingPolicy.objects.filter_by_organization(org.id).get(id=input.policy_id)
        except BookingPolicy.DoesNotExist as exc:
            raise GraphQLError("Booking policy not found.") from exc

        # Owner-scope enforcement — a scoped token may not touch a policy outside
        # its scope. Reuse the not-found message so it cannot probe for existence.
        permission_service = get_booking_policy_permission_service()
        if not permission_service.can_system_user_manage_policy(
            system_user=request.public_api_system_user, policy=policy
        ):
            raise GraphQLError("Booking policy not found.")

        policy = service.update_booking_policy(
            policy,
            lead_time_seconds=input.lead_time_seconds,
            max_horizon_seconds=input.max_horizon_seconds,
            buffer_before_seconds=input.buffer_before_seconds,
            buffer_after_seconds=input.buffer_after_seconds,
        )

        return BookingPolicyResult(success=True, policy=policy)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_booking_policy(
        self,
        info: strawberry.Info,
        input: DeleteBookingPolicyInput,  # noqa: A002
    ) -> DeleteBookingPolicyResult:
        """Delete a BookingPolicy (idempotent no-op when absent).

        Returns ``success=True`` regardless of whether the policy existed.
        Write is audited when an actual row is deleted.

        The token's ``OrganizationResourceAccess`` must include the
        ``BOOKING_POLICY`` resource.
        """
        org = info.context.request.public_api_organization
        if not org:
            raise GraphQLError("Organization not found in request context")

        request: PublicApiHttpRequest = info.context.request
        service = get_booking_policy_mutation_dependencies()
        service.initialize(org)
        actor = AuditService.actor_from_system_user(request.public_api_system_user)
        service.set_actor(actor)

        # Idempotent: a missing policy is a no-op success.
        try:
            policy: BookingPolicy | None = BookingPolicy.objects.filter_by_organization(org.id).get(
                id=input.policy_id
            )
        except BookingPolicy.DoesNotExist:
            policy = None

        # Owner-scope enforcement — a policy the token may not manage is treated
        # exactly like an absent one: no delete, success returned. This keeps the
        # idempotent contract and prevents a scoped token from probing existence.
        if policy is not None:
            permission_service = get_booking_policy_permission_service()
            if not permission_service.can_system_user_manage_policy(
                system_user=request.public_api_system_user, policy=policy
            ):
                policy = None

        service.delete_booking_policy(policy)
        return DeleteBookingPolicyResult(success=True)
