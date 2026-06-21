import base64
import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated

from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from audit.constants import AuditAction
from calendar_integration.exceptions import (
    InvalidParameterCombinationError,
    InvalidTokenError,
    MissingRequiredParameterError,
    NoPermissionsSpecifiedError,
    PermissionServiceInitializationError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.models import (
    CalendarGroup,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    CalendarOwnership,
    EventManagementPermissions,
)
from calendar_integration.services.dataclasses import (
    CalendarEventData,
    CalendarEventInputData,
    CalendarSettingsData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
)
from common.utils.authentication_utils import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from organizations.models import OrganizationMembership
from users.models import User


if TYPE_CHECKING:
    from audit.services import AuditService
    from public_api.models import SystemUser


def _resolve_token_membership_user_id(user: User, organization_id: int) -> int | None:
    """Resolve the membership-scoped actor id for a CalendarManagementToken mint.

    The raw-SQL composite PROTECT FK on ``CalendarManagementToken.membership``
    requires a non-NULL ``membership_user_id`` to reference a real
    ``OrganizationMembership(user_id, organization_id)``. Returns ``user.id`` only
    when such a membership exists, else ``None``.

    A token minted for a non-member is not a meaningful internal actor — the actor
    can only act inside an organization through a membership — so the token is left
    with a NULL membership (no internal actor), exactly as it would be for a
    null-user / external-attendee token. This mirrors
    ``calendar_service._resolve_owner_membership_user_id`` and keeps a non-member
    mint from tripping the FK and aborting the request.
    """
    if OrganizationMembership.objects.filter(
        user_id=user.id,
        organization_id=organization_id,
    ).exists():
        return user.id
    return None


DEFAULT_CALENDAR_OWNER_PERMISSIONS = [
    EventManagementPermissions.CREATE,
    EventManagementPermissions.UPDATE_ATTENDEES,
    EventManagementPermissions.UPDATE_DETAILS,
    EventManagementPermissions.RESCHEDULE,
    EventManagementPermissions.CANCEL,
]


DEFAULT_ATTENDEE_PERMISSIONS = [
    EventManagementPermissions.UPDATE_ATTENDEES,
    EventManagementPermissions.UPDATE_DETAILS,
    EventManagementPermissions.RESCHEDULE,
    EventManagementPermissions.CANCEL,
]
DEFAULT_EXTERNAL_ATTENDEE_PERMISSIONS = [
    EventManagementPermissions.UPDATE_SELF_RSVP,
    EventManagementPermissions.RESCHEDULE,
    EventManagementPermissions.CANCEL,
]


class CalendarPermissionService:
    token: CalendarManagementToken | None

    @inject
    def __init__(
        self,
        audit_service: Annotated["AuditService | None", Provide["audit_service"]] = None,
    ) -> None:
        self.token = None
        self.audit_service = audit_service

    def _audit_token_write(
        self,
        action: str,
        token: CalendarManagementToken,
        organization_id: int,
        actor: object,
        diff: dict | None = None,
    ) -> None:
        """Emit an audit record for a CalendarManagementToken lifecycle write.

        No-op when no ``audit_service`` is bound, so token minting/revocation never
        breaks if the service was built without DI (e.g. directly in a test).
        """
        if self.audit_service is None:
            return
        self.audit_service.record(
            organization_id=organization_id,
            action=action,
            actor=actor,  # type: ignore[arg-type]
            subject=self.audit_service.subject_from_instance(token),
            diff=diff,
        )

    def initialize_with_token(self, token_str_base64: str, organization_id: int):
        try:
            token_full_str = base64.b64decode(token_str_base64).decode("utf-8")
            token_parts = token_full_str.split(":")
            if len(token_parts) != 2:
                raise ValueError("Invalid token format")
            token_id = token_parts[0]
            token_str = token_parts[1]
        except (ValueError, UnicodeDecodeError) as e:
            raise InvalidTokenError("Invalid token format") from e

        try:
            token = (
                CalendarManagementToken.objects.prefetch_related("permissions")
                .select_related("calendar", "event")
                .filter(organization_id=organization_id)
                .get(id=token_id, revoked_at__isnull=True)
            )
        except CalendarManagementToken.DoesNotExist as e:
            # Handle any exceptions that occur during initialization
            raise InvalidTokenError("Invalid token string provided") from e

        if not verify_long_lived_token(token_str, token.token_hash):
            raise InvalidTokenError("Invalid token string provided") from None

        self.token = token

    def initialize_with_user(
        self,
        user: User,
        organization_id: int,
        event_id: int | None = None,
        calendar_id: int | None = None,
    ):
        if calendar_id is not None and event_id is not None:
            raise InvalidParameterCombinationError(
                "Specify either calendar_id or event_id, not both."
            )

        if calendar_id is None and event_id is None:
            raise MissingRequiredParameterError("Either calendar_id or event_id must be specified.")

        try:
            if event_id is not None:
                # Looking for event-specific token
                self.token = (
                    CalendarManagementToken.objects.prefetch_related("permissions")
                    .select_related("calendar", "event")
                    .filter(organization_id=organization_id)
                    .get(
                        event_fk_id=event_id,
                        membership_user_id=user.id,
                        revoked_at__isnull=True,
                    )
                )
            else:
                # Looking for calendar-level token
                self.token = (
                    CalendarManagementToken.objects.prefetch_related("permissions")
                    .select_related("calendar", "event")
                    .filter(organization_id=organization_id)
                    .get(
                        calendar_fk_id=calendar_id,
                        event_fk_id__isnull=True,
                        membership_user_id=user.id,
                        revoked_at__isnull=True,
                    )
                )
        except CalendarManagementToken.DoesNotExist as e:
            raise PermissionServiceInitializationError(
                "Error initializing CalendarPermissionCheckService"
            ) from e

    def has_permission(self, permission: EventManagementPermissions) -> bool:
        if not hasattr(self, "token") or self.token is None:
            raise PermissionServiceInitializationError(
                "Service not initialized. Call initialize_with_token or initialize_with_user first."
            )

        permissions_to_check = [permission]

        if permission == EventManagementPermissions.UPDATE_SELF_RSVP:
            # tokens with EventUpdatePermissions.ATTENDEES can also update self RSVP
            permissions_to_check.append(EventManagementPermissions.UPDATE_ATTENDEES)

        return any(p for p in self.token.permissions.all() if p.permission in permissions_to_check)

    def _check_attendances_update_necessary_permissions(
        self,
        old_attendances: Iterable[EventInternalAttendeeData],
        old_external_attendances: Iterable[EventExternalAttendeeData],
        new_attendances: Iterable[EventInternalAttendeeData],
        new_external_attendances: Iterable[EventExternalAttendeeData],
    ) -> EventManagementPermissions | None:
        """
        Check what are the required permissions given the nature of the changes in the attendances
        """
        if not hasattr(self, "token") or self.token is None:
            raise ValueError(
                "Service not initialized. Call initialize_with_token or initialize_with_user first."
            )

        old_attendance_dict = {a.user_id: a for a in old_attendances if a.user_id is not None}
        new_attendance_dict = {a.user_id: a for a in new_attendances if a.user_id is not None}

        old_external_attendance_dict = {
            a.email.lower(): a for a in old_external_attendances if a.email is not None
        }
        new_external_attendance_dict = {
            a.email.lower(): a for a in new_external_attendances if a.email is not None
        }

        # Check for changes in internal attendances
        for user_id in new_attendance_dict.keys():
            old_attendance = old_attendance_dict.get(user_id)
            if not old_attendance:
                # New attendance added
                return EventManagementPermissions.UPDATE_ATTENDEES

        for user_id in old_attendance_dict.keys():
            new_attendance = new_attendance_dict.get(user_id)
            if not new_attendance:
                # Attendance removed
                if user_id == self.token.membership_user_id:
                    return EventManagementPermissions.UPDATE_SELF_RSVP
                return EventManagementPermissions.UPDATE_ATTENDEES

        for user_email in new_external_attendance_dict.keys():
            old_external_attendance = old_external_attendance_dict.get(user_email)
            if not old_external_attendance:
                # New external attendance added
                return EventManagementPermissions.UPDATE_ATTENDEES

        for user_email in old_external_attendance_dict.keys():
            new_external_attendance = new_external_attendance_dict.get(user_email)
            if not new_external_attendance:
                # External attendance removed
                if (
                    self.token.external_attendee
                    and user_email == self.token.external_attendee.email
                ):
                    return EventManagementPermissions.UPDATE_SELF_RSVP
                return EventManagementPermissions.UPDATE_ATTENDEES

        return None

    def _check_event_details_update_necessary_permissions(
        self,
        old_title: str,
        old_description: str,
        new_title: str,
        new_description: str,
    ) -> EventManagementPermissions | None:
        """
        Check what are the required permissions given the nature of the changes in the event details
        """
        if not hasattr(self, "token") or self.token is None:
            raise ValueError(
                "Service not initialized. Call initialize_with_token or initialize_with_user first."
            )

        if old_title != new_title or old_description != new_description:
            return EventManagementPermissions.UPDATE_DETAILS

        return None

    def _check_event_reschedule_necessary_permissions(
        self,
        old_start_time,
        old_end_time,
        new_start_time,
        new_end_time,
    ) -> EventManagementPermissions | None:
        """
        Check what are the required permissions given the nature of the changes in the event schedule
        """
        if not hasattr(self, "token") or self.token is None:
            raise ValueError(
                "Service not initialized. Call initialize_with_token or initialize_with_user first."
            )

        if old_start_time != new_start_time or old_end_time != new_end_time:
            return EventManagementPermissions.RESCHEDULE

        return None

    def _check_event_cancellation_necessary_permissions(self) -> EventManagementPermissions:
        """
        Check what are the required permissions to cancel the event
        """
        if not hasattr(self, "token") or self.token is None:
            raise ValueError(
                "Service not initialized. Call initialize_with_token or initialize_with_user first."
            )

        return EventManagementPermissions.CANCEL

    def _determine_required_update_permissions(
        self,
        old_event: CalendarEventData,
        new_event: CalendarEventData | None,
    ) -> set[EventManagementPermissions]:
        """
        Determine the set of required permissions based on the changes between the old and new event data.
        """
        if not hasattr(self, "token") or self.token is None:
            raise ValueError(
                "Service not initialized. Call initialize_with_token or initialize_with_user first."
            )

        required_permissions = set()

        if new_event is None:
            return {self._check_event_cancellation_necessary_permissions()}

        # Check for attendance changes
        attendance_permission = self._check_attendances_update_necessary_permissions(
            old_event.attendees,
            old_event.external_attendees,
            new_event.attendees,
            new_event.external_attendees,
        )
        if attendance_permission:
            required_permissions.add(attendance_permission)

        # Check for event detail changes
        details_permission = self._check_event_details_update_necessary_permissions(
            old_event.title,
            old_event.description,
            new_event.title,
            new_event.description,
        )
        if details_permission:
            required_permissions.add(details_permission)

        # Check for schedule changes
        reschedule_permission = self._check_event_reschedule_necessary_permissions(
            old_event.start_time,
            old_event.end_time,
            new_event.start_time,
            new_event.end_time,
        )
        if reschedule_permission:
            required_permissions.add(reschedule_permission)

        return required_permissions

    def can_perform_update(
        self,
        old_event: CalendarEventData,
        new_event: CalendarEventData | None,
    ) -> bool:
        """
        Check if the token has all the required permissions to perform the update.
        """
        if not hasattr(self, "token") or self.token is None:
            return False

        if old_event.id != self.token.event_fk_id:  # type: ignore
            return False

        required_permissions = self._determine_required_update_permissions(old_event, new_event)
        for permission in required_permissions:
            if not self.has_permission(permission):
                return False
        return True

    def can_perform_scheduling(
        self,
        calendar_id: int,
        calendar_settings: CalendarSettingsData,
        event: CalendarEventInputData,
    ) -> bool:
        """Check if the token has all the required permissions to perform the scheduling.

        Authorization is granted when any of the following holds:

        1. The calendar accepts public scheduling (``accepts_public_scheduling=True``).
        2. The token is calendar-scoped (``calendar_fk_id == calendar_id``) and has
           the CREATE permission.
        3. The token is group-scoped (``calendar_group_fk_id`` is set), has the CREATE
           permission, **and** ``calendar_id`` is a member of one of that group's slots.
           This case covers group-booking codes: the code is minted with a group scope,
           and the create call targets the primary calendar of the group.
        """
        if calendar_settings.accepts_public_scheduling:
            return True

        if not hasattr(self, "token") or self.token is None:
            return False

        if self.token.calendar_fk_id == calendar_id:  # type: ignore
            return self.has_permission(EventManagementPermissions.CREATE)

        # Group-scoped token: authorize if calendar_id belongs to any slot of the bound group.
        if self.token.calendar_group_fk_id is not None and self.has_permission(
            EventManagementPermissions.CREATE
        ):
            return (
                CalendarGroupSlotMembership.objects.filter_by_organization(
                    self.token.organization_id
                )
                .filter(
                    slot__group_fk_id=self.token.calendar_group_fk_id,
                    calendar_fk_id=calendar_id,
                )
                .exists()
            )

        return False

    def can_perform_group_scheduling(
        self,
        group: CalendarGroup,
    ) -> bool:
        """Check if the current context is authorized to book through ``group``.

        Authorization is granted when any of the following holds:

        1. The group accepts public scheduling (``group.accepts_public_scheduling=True``).
           This is the codeless public path — no token is required.
        2. The token is group-scoped (``calendar_group_fk_id == group.id``) and has
           the CREATE permission. This covers group-scoped management tokens and
           single-use booking codes whose scope is the group.

        Args:
            group: The ``CalendarGroup`` being booked.

        Returns:
            ``True`` if the booking is authorized; ``False`` otherwise.
        """
        if group.accepts_public_scheduling:
            return True

        if not hasattr(self, "token") or self.token is None:
            return False

        if self.token.calendar_group_fk_id == group.id and self.has_permission(  # type: ignore[attr-defined]
            EventManagementPermissions.CREATE
        ):
            return True

        return False

    def can_manage_calendar_group(self, user: User, group: CalendarGroup) -> bool:
        """Return True if `user` may create/update/delete `group` and create
        events against it.

        Rules, in order:
          1. Org admins in the group's organization can always manage it —
             matches "admin-of-org can administer org-scoped resources" so
             schedulers/ops who don't personally own any pool calendar still
             work.
          2. Otherwise, the user must own at least one calendar inside the
             group's slot pools (scoped to the group's organization).
        """
        if user.is_organization_admin(group.organization_id):
            return True
        return (
            CalendarOwnership.objects.filter_by_organization(group.organization_id)
            .filter(
                membership_user_id=user.id,
                calendar_fk__group_slots__group_fk=group,
            )
            .exists()
        )

    def create_calendar_owner_token(
        self,
        organization_id: int,
        user: User,
        calendar_id: int,
        permissions: list[EventManagementPermissions] | None = None,
    ):
        """
        Create a new CalendarEventUpdateToken with specified permissions.
        """
        if permissions is None:
            permissions = DEFAULT_CALENDAR_OWNER_PERMISSIONS

        if len(permissions) == 0:
            raise NoPermissionsSpecifiedError(
                "At least one permission must be specified to create a token."
            )

        token, _ = CalendarManagementToken.objects.get_or_create(
            organization_id=organization_id,
            calendar_fk_id=calendar_id,
            membership_user_id=_resolve_token_membership_user_id(user, organization_id),
        )

        token.permissions.all().delete()

        for perm in permissions:
            token.permissions.create(permission=perm, organization_id=organization_id)

        if self.audit_service is not None:
            self._audit_token_write(
                AuditAction.CREATE,
                token,
                organization_id,
                self.audit_service.actor_from_user_or_token(user, organization_id),
            )

        return token

    def create_attendee_token(
        self,
        organization_id: int,
        user: User,
        event_id: int,
        permissions: list[EventManagementPermissions] | None = None,
    ) -> CalendarManagementToken:
        """
        Create a new CalendarEventUpdateToken with specified permissions.
        """
        if permissions is None:
            permissions = DEFAULT_ATTENDEE_PERMISSIONS

        if len(permissions) == 0:
            raise ValueError("At least one permission must be specified to create a token.")

        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)
        token, _ = CalendarManagementToken.objects.get_or_create(
            organization_id=organization_id,
            event_fk_id=event_id,
            membership_user_id=_resolve_token_membership_user_id(user, organization_id),
            defaults={
                "token_hash": hashed_token,
            },
        )

        token.permissions.all().delete()

        for perm in permissions:
            token.permissions.create(permission=perm, organization_id=organization_id)

        if self.audit_service is not None:
            self._audit_token_write(
                AuditAction.CREATE,
                token,
                organization_id,
                self.audit_service.actor_from_user_or_token(user, organization_id),
            )

        return token

    def create_external_attendee_update_token(
        self,
        organization_id: int,
        event_id: int,
        external_attendee_id: int,
        permissions: list[EventManagementPermissions] | None = None,
    ) -> CalendarManagementToken:
        """
        Create a new CalendarEventUpdateToken for an external attendee with specified permissions.
        """
        if permissions is None:
            permissions = DEFAULT_EXTERNAL_ATTENDEE_PERMISSIONS
        if len(permissions) == 0:
            raise ValueError("At least one permission must be specified to create a token.")

        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)
        token, _ = CalendarManagementToken.objects.get_or_create(
            organization_id=organization_id,
            event_fk_id=event_id,
            external_attendee_fk_id=external_attendee_id,
            defaults={
                "token_hash": hashed_token,
            },
        )

        token.permissions.all().delete()

        for perm in permissions:
            token.permissions.create(permission=perm, organization_id=organization_id)

        if self.audit_service is not None:
            self._audit_token_write(
                AuditAction.CREATE,
                token,
                organization_id,
                self.audit_service.system_actor(),
            )

        return token

    def create_external_attendee_schedule_token(
        self,
        organization_id: int,
        calendar_id: int,
        external_attendee_id: int,
    ) -> CalendarManagementToken:
        """
        Create a new CalendarEventUpdateToken for an external attendee with specified permissions.
        """
        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)
        token, _ = CalendarManagementToken.objects.get_or_create(
            organization_id=organization_id,
            calendar_fk_id=calendar_id,
            external_attendee_fk_id=external_attendee_id,
            defaults={
                "token_hash": hashed_token,
            },
        )
        token.permissions.all().delete()
        token.permissions.create(
            permission=EventManagementPermissions.CREATE, organization_id=organization_id
        )

        if self.audit_service is not None:
            self._audit_token_write(
                AuditAction.CREATE,
                token,
                organization_id,
                self.audit_service.system_actor(),
            )

        return token

    # ------------------------------------------------------------------
    # Single-use booking-code API (Phase 0+)
    # ------------------------------------------------------------------

    def create_booking_token(
        self,
        organization_id: int,
        permissions: list[EventManagementPermissions],
        expires_at: datetime.datetime | None = None,
        minted_by: "SystemUser | None" = None,
        calendar_id: int | None = None,
        calendar_group_id: int | None = None,
        event_id: int | None = None,
    ) -> tuple[CalendarManagementToken, str]:
        """Mint a new single-use booking code token.

        Creates a fresh ``CalendarManagementToken`` with a one-time-use plaintext
        code.  The plaintext code is returned exactly once here and never stored —
        only the hash is persisted.  Callers must pass the plaintext to the client.

        Scope rules (at most one of ``calendar_id``, ``calendar_group_id``,
        ``event_id`` should be supplied, though ``calendar_id``/``calendar_group_id``
        and ``event_id`` may be combined for reschedule/cancel codes):

        - Booking codes: ``calendar_id`` OR ``calendar_group_id`` (no ``event_id``).
        - Reschedule / cancel codes: ``event_id`` PLUS either ``calendar_id`` or
          ``calendar_group_id`` to record which calendar/group the event belongs to.

        Args:
            organization_id: Tenant scope.
            permissions: One or more ``EventManagementPermissions`` to grant.
            expires_at: Optional expiry datetime (UTC).  Pass ``None`` for no expiry.
            minted_by: The ``SystemUser`` that created this code, for audit.
            calendar_id: Scope to a single calendar (booking or reschedule/cancel).
            calendar_group_id: Scope to a calendar group (group booking or reschedule/cancel).
            event_id: Scope to a specific event (reschedule/cancel codes only).

        Returns:
            A ``(token, plaintext_code)`` tuple.  The ``plaintext_code`` must be
            delivered to the end-user; it is not recoverable after this call.

        Raises:
            NoPermissionsSpecifiedError: If ``permissions`` is empty.
        """
        if len(permissions) == 0:
            raise NoPermissionsSpecifiedError(
                "At least one permission must be specified to create a booking token."
            )

        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)

        # Build the plaintext token in the same base64 format as initialize_with_token expects:
        # "<id>:<raw_token>" base64-encoded.  We'll set the id after creation.
        token = CalendarManagementToken(
            organization_id=organization_id,
            token_hash=hashed_token,
            expires_at=expires_at,
            minted_by_system_user=minted_by,
        )
        if calendar_id is not None:
            token.calendar_fk_id = calendar_id
        if calendar_group_id is not None:
            token.calendar_group_fk_id = calendar_group_id
        if event_id is not None:
            token.event_fk_id = event_id

        token.save()

        for perm in permissions:
            token.permissions.create(permission=perm, organization_id=organization_id)

        if self.audit_service is not None:
            self._audit_token_write(
                AuditAction.CREATE,
                token,
                organization_id,
                self.audit_service.actor_from_user_or_token(minted_by, organization_id),
            )

        # Encode as "<id>:<raw>" in base64, matching initialize_with_token's decode logic.
        plaintext_code = base64.b64encode(f"{token.pk}:{token_str}".encode()).decode("utf-8")

        return token, plaintext_code

    def resolve_code(self, code: str) -> CalendarManagementToken:
        """Decode and validate a booking code WITHOUT a known organization.

        This method is for unauthenticated reads where the org is derived FROM
        the code itself.  It performs the same decode/hash/verify logic as
        ``validate_code`` but looks up the token by id alone (no org filter).
        The org is safe to derive from the returned token's ``organization_id``
        because the secret-hash verification gates access to the token data.

        Args:
            code: The base64-encoded plaintext code as returned by
                ``create_booking_token``.

        Returns:
            The active ``CalendarManagementToken`` instance, with
            ``permissions``, ``calendar``, ``calendar_group``, and ``event``
            pre-fetched.

        Raises:
            InvalidTokenError: If the code is malformed or does not match any token.
            TokenRevokedError: If the token was revoked.
            TokenAlreadyUsedError: If the token was already consumed.
            TokenExpiredError: If the token's ``expires_at`` has passed.
        """
        try:
            token_full_str = base64.b64decode(code).decode("utf-8")
            token_parts = token_full_str.split(":")
            if len(token_parts) != 2:
                raise ValueError("Invalid token format")
            token_id = token_parts[0]
            token_str = token_parts[1]
        except (ValueError, UnicodeDecodeError) as e:
            raise InvalidTokenError("Invalid booking code format") from e

        try:
            # Look up by id alone — no org filter.  This is safe because:
            #   1. The integer id alone is useless without the secret token string.
            #   2. The constant-time hash verify below is the actual gate.
            # We use ``original_manager`` (the plain Django Manager defined on
            # OrganizationModel) to bypass the tenant-required guard that
            # CalendarManagementToken.objects enforces — the org is derived FROM
            # the token, not passed in.
            token = (
                CalendarManagementToken.original_manager.select_related(
                    "calendar",
                    "event",
                    "calendar_group",
                    "event__calendar",
                    "event__calendar_group",
                )
                .prefetch_related("permissions")
                .get(id=token_id)
            )
        except CalendarManagementToken.DoesNotExist as e:
            raise InvalidTokenError("Invalid booking code") from e

        if not verify_long_lived_token(token_str, token.token_hash):
            raise InvalidTokenError("Invalid booking code") from None

        # Check terminal lifecycle states in priority order (same as validate_code).
        if token.revoked_at is not None:
            raise TokenRevokedError()

        if token.used_at is not None:
            raise TokenAlreadyUsedError()

        if token.expires_at is not None and token.expires_at <= timezone.now():
            raise TokenExpiredError()

        return token

    def validate_code(self, code: str, organization_id: int) -> CalendarManagementToken:
        """Decode and validate a booking code, returning the active token.

        Delegates decode/verify/lifecycle to ``resolve_code`` then additionally
        asserts that the token belongs to the given org.  Use ``resolve_code``
        directly when the org is unknown and must be derived from the token.

        Args:
            code: The base64-encoded plaintext code as returned by
                ``create_booking_token``.
            organization_id: Tenant scope.  The token must belong to this org.

        Returns:
            The active ``CalendarManagementToken`` instance, with
            ``permissions`` and ``calendar``/``event`` pre-fetched.

        Raises:
            InvalidTokenError: If the code is malformed, does not match any
                token, or belongs to a different organization.
            TokenExpiredError: If the token's ``expires_at`` has passed.
            TokenAlreadyUsedError: If the token was already consumed.
            TokenRevokedError: If the token was revoked.
        """
        token = self.resolve_code(code)

        if token.organization_id != organization_id:
            raise InvalidTokenError("Invalid booking code") from None

        return token

    def consume_code(self, token: CalendarManagementToken, source_ip: str) -> None:
        """Atomically consume a booking-code token.

        Delegates to ``CalendarManagementToken.objects.consume()`` which
        acquires a ``SELECT FOR UPDATE`` row lock before committing the
        ``used_at`` + ``consumed_source_ip`` update.  Must be called inside
        the same transaction as the booking action.

        Args:
            token: A ``CalendarManagementToken`` previously returned by
                ``validate_code``.
            source_ip: The IP address of the consuming client.

        Raises:
            TokenExpiredError: If the token expired between validation and consume.
            TokenAlreadyUsedError: If a concurrent request consumed the token first.
            TokenRevokedError: If the token was revoked between validation and consume.
        """
        CalendarManagementToken.objects.consume(token, source_ip)

    def revoke_token(self, organization_id: int, token_id: int) -> bool:
        """Revoke a booking code by its opaque id (idempotent).

        Fetch the token scoped to the organization, set ``revoked_at`` to
        the current time if not already set, and save. If the token is already
        revoked, return True without changing the timestamp (idempotent).

        Args:
            organization_id: Tenant scope.
            token_id: The id of the token to revoke.

        Returns:
            True on success (revoked or already-revoked).

        Raises:
            InvalidTokenError: If no token with the given id exists in the
                organization.
        """
        try:
            token = CalendarManagementToken.objects.filter_by_organization(organization_id).get(
                id=token_id
            )
        except CalendarManagementToken.DoesNotExist as e:
            raise InvalidTokenError("Token not found") from e

        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])

            if self.audit_service is not None:
                self._audit_token_write(
                    AuditAction.UPDATE,
                    token,
                    organization_id,
                    self.audit_service.system_actor(),
                    diff={"revoked_at": {"old": None, "new": token.revoked_at.isoformat()}},
                )

        return True
