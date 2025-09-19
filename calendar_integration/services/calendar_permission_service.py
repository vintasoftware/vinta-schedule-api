import base64
from collections.abc import Iterable

from calendar_integration.exceptions import (
    InvalidParameterCombinationError,
    InvalidTokenError,
    MissingRequiredParameterError,
    NoPermissionsSpecifiedError,
    PermissionServiceInitializationError,
)
from calendar_integration.models import (
    CalendarManagementToken,
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
from users.models import User


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

    def initialize_with_token(self, token_str_base64: str, organization_id: int):
        token_full_str = base64.b64decode(token_str_base64).decode("utf-8")
        token_parts = token_full_str.split(":")
        token_id = token_parts[0]
        token_str = token_parts[1]

        try:
            token = (
                CalendarManagementToken.objects.prefetch_related("permissions")
                .select_related("calendar", "event")
                .get(organization_id=organization_id, id=token_id, revoked_at__isnull=True)
            )
        except CalendarManagementToken.DoesNotExist as e:
            # Handle any exceptions that occur during initialization
            raise InvalidTokenError("Invalid token string provided.") from e

        if not verify_long_lived_token(token_str, token.token_hash):
            raise InvalidTokenError("Invalid token string provided.")

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
                    .get(
                        organization_id=organization_id,
                        event_fk_id=event_id,
                        user=user,
                        revoked_at__isnull=True,
                    )
                )
            else:
                # Looking for calendar-level token
                self.token = (
                    CalendarManagementToken.objects.prefetch_related("permissions")
                    .select_related("calendar", "event")
                    .get(
                        organization_id=organization_id,
                        calendar_fk_id=calendar_id,
                        event_fk_id__isnull=True,
                        user=user,
                        revoked_at__isnull=True,
                    )
                )
        except CalendarManagementToken.DoesNotExist as e:
            # Handle any exceptions that occur during initialization
            raise PermissionServiceInitializationError(str(e)) from e

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
                if user_id == self.token.user_id:
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
        """
        Check if the token has all the required permissions to perform the scheduling.
        """
        if calendar_settings.accepts_public_scheduling:
            return True

        if not hasattr(self, "token") or self.token is None:
            return False

        if self.token.calendar_fk_id == calendar_id:  # type: ignore
            return self.has_permission(EventManagementPermissions.CREATE)

        return False

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
            user=user,
        )

        token.permissions.all().delete()

        for perm in permissions:
            token.permissions.create(permission=perm, organization_id=organization_id)

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
            user=user,
            defaults={
                "token_hash": hashed_token,
            },
        )

        token.permissions.all().delete()

        for perm in permissions:
            token.permissions.create(permission=perm, organization_id=organization_id)

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

        return token
